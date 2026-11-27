"""Review storage: ids, review.json, round files, design profiles, locking.

Layout (under config.data_root()):
    <review_id>/review.json
    <review_id>/rounds/<n>/a/decisions.json        decision round: the board renders it natively (no iframe)
    <review_id>/rounds/<n>/<variant>/index.html    prototype round (and legacy variant rounds) (+ assets/)
    <review_id>/rounds/<n>/a/design/                the project's design profile, copied in (design.json, mock.css, ...)
    <review_id>/rounds/<n>/feedback.md|feedback.json  (after a submit)
    designs/<name>/design.json                      saved design profiles, one per project
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from uuid import uuid4

from . import config, locks, settings

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
PIN_ID_RE = re.compile(r"^[a-z0-9]{1,24}$")
VARIANT_IDS = "abcd"
VARIANT_RE = re.compile(r"^[a-d]$")
MODES = ("tailscale", "local", "public")
MAX_VARIANTS = 4
MAX_TITLE = 160
MAX_BRIEF = 4000
MAX_LABEL = 60
MAX_STANCE = 240
MAX_ASSETS_BYTES = 50 * 1024 * 1024


def size_label(n: int) -> str:
    """A byte cap as the error messages say it: whole megabytes, or exact bytes under 1 MB (never "0 MB")."""
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{n} bytes"

# Injected into every variant page (see board/probe.js): reports page size and
# comment-mode taps to the board via postMessage and draws pin markers.
PROBE_MARKER = "mockreview-probe"
_LIB_EXPORTS = "export {\n  finder,\n  position,\n  quote\n};"
_ANCHORS_IMPORT = "import {finder, quote, position} from './lib/anchors.js';"
_PROBE_IMPORT = "import {LIVE, CONTROL, controlOf, presses, anchorFor, cleanTarget, surface, installAPI} from './anchors.js';"


@functools.lru_cache(maxsize=1)
def _probe() -> str:
    """The probe as one classic inline script: a data: module import is refused by ARTIFACT_CSP, and a classic script
    runs synchronously where it sits, so `window.mockreview` exists before an artifact page's own scripts do."""
    lib = (config.BOARD_DIR / 'lib' / 'anchors.js').read_text(encoding='utf-8')
    if _LIB_EXPORTS not in lib:
        raise RuntimeError("board/lib/anchors.js: the expected export block is missing; rebuild the vendored bundle")
    lib = lib.replace(_LIB_EXPORTS, 'return {finder: finder, quote: quote, position: position};')
    lib = 'var __mrLib = (function(){\n' + lib + '\n})();'    # its top-level names never reach the other two files
    anchors = (config.BOARD_DIR / 'anchors.js').read_text(encoding='utf-8')
    if _ANCHORS_IMPORT not in anchors:
        raise RuntimeError("board/anchors.js: the expected lib import is missing")
    anchors = anchors.replace(_ANCHORS_IMPORT, '')
    anchors = re.sub(r'(?m)^export (?=(?:const|function|let|var|class)\b)', '', anchors)
    anchors = 'var finder = __mrLib.finder, quote = __mrLib.quote, position = __mrLib.position;\n' + anchors
    probe = (config.BOARD_DIR / 'probe.js').read_text(encoding='utf-8')
    if _PROBE_IMPORT not in probe:
        raise RuntimeError("board/probe.js: the expected anchors import is missing")
    probe = probe.replace(_PROBE_IMPORT, '')
    js = '(function(){\n' + lib + '\n' + anchors + '\n' + probe + '\n})();'
    return '<script>/*' + PROBE_MARKER + '*/\n' + js.replace('</script', '<\\/script') + '</script>'


class ReviewError(Exception):
    """An error with an HTTP-ish status code and a message safe to show users."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_pin_id() -> str:
    """A pin id the board and the server both accept: `p` plus 8 hex characters."""
    return "p" + uuid4().hex[:8]


def validate_id(review_id: str) -> str:
    if not isinstance(review_id, str) or not ID_RE.match(review_id):
        raise ReviewError("invalid review id", 404)
    return review_id


def review_dir(review_id: str) -> Path:
    validate_id(review_id)
    root = config.data_root().resolve()
    d = (root / review_id).resolve()
    if d.parent != root:
        raise ReviewError("invalid review id", 404)
    return d


def exists(review_id: str) -> bool:
    try:
        return (review_dir(review_id) / "review.json").is_file()
    except ReviewError:
        return False


@contextlib.contextmanager
def locked(review_id: str) -> Iterator[None]:
    d = review_dir(review_id)
    d.mkdir(parents=True, exist_ok=True)
    with locks.exclusive(d / ".lock"):
        yield


@contextlib.contextmanager
def io_lock(d: Path) -> Iterator[None]:
    """Held while review.json is read and while it is replaced, so a replace never meets an open reader (Windows
    refuses to replace a file another handle has open). Separate from locked(): a writer loads under locked()."""
    with locks.exclusive(d / ".io.lock"):
        yield


def load(review_id: str) -> Dict[str, Any]:
    p = review_dir(review_id) / "review.json"
    if not p.is_file():
        raise ReviewError("review not found", 404)
    with io_lock(p.parent):
        text = p.read_text(encoding="utf-8")
    return json.loads(text)


# On Windows, replacing a file another thread or process has open for reading fails with ERROR_ACCESS_DENIED
# (PermissionError) until that reader closes it, which is a few milliseconds here: the board reads review.json on every
# request. Retry the rename briefly there instead of failing the write (a Send answered 500 on Windows CI).
_WINDOWS = os.name == "nt"
_REPLACE_TRIES = 40
_REPLACE_DELAY = 0.025


def _replace(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not _WINDOWS or attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(_REPLACE_DELAY)


def atomic_write(path: Path, text: str, lock: bool = False) -> None:
    """Write via a temp file and a replace; lock=True takes io_lock around the replace (review.json)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        with io_lock(path.parent) if lock else contextlib.nullcontext():
            _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def save(review_id: str, data: Dict[str, Any], *, pin_round: Optional[int] = None,
         block_patched: Optional[Dict[str, Any]] = None) -> None:
    data["updated_at"] = now_iso()
    atomic_write(review_dir(review_id) / "review.json", json.dumps(data, indent=2, ensure_ascii=False), lock=True)

    if block_patched is not None or pin_round is not None:
        from . import live
        # the review lock orders invalidations after their corresponding saves
        if block_patched is not None:
            live.publish(review_id, live.BLOCK_PATCHED, "", "", block_patched)
        if pin_round is not None:
            live.publish(review_id, live.PIN_REQUESTS, "", "", {"round": pin_round})


def list_ids() -> List[str]:
    root = config.data_root()
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if ID_RE.match(p.name) and (p / "review.json").is_file()
    )


def _slug(text: str, limit: int = 28) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "review"


def new_review_id(title: str) -> str:
    rid = f"{_slug(title)}-{secrets.token_hex(8)}"
    return validate_id(rid)


def _clean_line(text: str, limit: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).strip()
    return text[:limit]


def _clean_block(text: str, limit: int) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text)).replace("\r\n", "\n").strip()
    return text[:limit]


def parse_variant_spec(spec: str) -> Dict[str, Any]:
    """Parse "label|stance|/abs/path/index.html". The path is the last field."""
    if spec.count("|") < 2:
        raise ReviewError(f"--variant must be 'label|stance|/abs/path.html', got {spec!r}")
    head, path = spec.rsplit("|", 1)
    label, stance = head.split("|", 1)
    label, stance, path = label.strip(), stance.strip(), path.strip()
    if not label:
        raise ReviewError("variant label is empty")
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ReviewError(f"variant path must be absolute: {path}")
    if not p.is_file():
        raise ReviewError(f"variant file not found: {path}")
    if p.suffix.lower() not in (".html", ".htm"):
        raise ReviewError(f"variant file must be .html: {path}")
    return {"label": _clean_line(label, MAX_LABEL), "stance": _clean_line(stance, MAX_STANCE), "src": p}


ROUND_KINDS = ("decisions", "prototype", "variants")
MAX_DECISIONS_BYTES = 3 * 1024 * 1024
MAX_BLOCKS = 60
MAX_OPTIONS = 6
DESIGN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?$")
TOKEN_KEYS = ("bg", "surface", "surface2", "line", "line2", "text", "muted", "faint", "accent", "accentInk", "ok", "warn", "err")
COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([0-9.,\s%/]+\))$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
LAYOUTS = ("side", "switch", "steps", "before-after")
BLOCK_TYPES = ("text", "recap", "decision", "cards", "callout", "mock", "table", "settled", "defaults", "question", "piece",
               "artifact")
ID_BLOCKS = ("decision", "mock", "question", "piece", "defaults", "artifact")   # one id namespace (recaps join it via the clash check)
MAX_ARTIFACT_HTML = 2 * 1024 * 1024      # an artifact's own page, inline; media is bundled in later (bundle.py) under its caps
ARTIFACT_HEIGHT = (80, 4000)
ARTIFACT_DISPLAYS = ("frame", "page", "long")   # frame: a card; page: one viewport tall, scrolls inside; long: full length, inline
# The meta CSP of every artifact page. The page runs with an opaque origin (sandbox="allow-scripts", no
# allow-same-origin) and can load nothing but its own inline code and data:/blob: media: no network, no frames.
ARTIFACT_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
                "media-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; "
                "frame-src 'none'")
TONES = ("neutral", "ok", "warn", "err", "accent")
BLOCK_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
MAX_CARDS = 12
MAX_COLUMNS = 6
MAX_ROWS = 30
PRESETS = ("design", "plan")
NOTE_STATUSES = ("done", "second-look", "needs-pick")
GATE_LABEL = "Build"
MAX_ITEMS = 30
MAX_MILESTONES = 6
MAX_NOTES_ANSWERED = 40
MAX_GATE_AFTER = 12
MAX_GATE_LATER = 20
MAX_PROJECTS = 50


def designs_root() -> Path:
    return config.data_root() / "designs"


def _valid_tokens(t: Any, where: str) -> Dict[str, str]:
    if not isinstance(t, dict):
        raise ReviewError(f"{where}: tokens must be an object")
    out = {}
    for k in TOKEN_KEYS:
        v = t.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or not COLOR_RE.match(v.strip()):
            raise ReviewError(f"{where}: token {k!r} must be a #hex or rgb() colour, got {v!r}")
        out[k] = v.strip()
    for k in ("bg", "surface", "text", "accent"):
        if k not in out:
            raise ReviewError(f"{where}: token {k!r} is required")
    return out


def validate_design(d: Path) -> Dict[str, Any]:
    """A design profile dir: design.json (+ mock.css, fonts.css, fonts/, icon). Returns the cleaned design.json."""
    f = d / "design.json"
    if not f.is_file():
        raise ReviewError(f"design profile has no design.json: {d}")
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ReviewError(f"design.json is not valid JSON: {e}")
    if not isinstance(raw, dict):
        raise ReviewError("design.json must be an object")
    themes = raw.get("themes")
    if not isinstance(themes, dict) or not themes or not set(themes) <= {"dark", "light"}:
        raise ReviewError("design.json: themes must have 'dark' and/or 'light'")
    out: Dict[str, Any] = {
        "name": _clean_line(raw.get("name") or d.name, 60),
        "label": _clean_line(raw.get("label") or raw.get("name") or d.name, 60),
        "themes": {k: _valid_tokens(v, f"themes.{k}") for k, v in themes.items()},
        "default_theme": raw.get("default_theme") if raw.get("default_theme") in themes else sorted(themes)[0],
        "font": _clean_line(raw.get("font") or "", 200),
        "source": _clean_line(raw.get("source") or "", 300),
        "captured_at": _clean_line(raw.get("captured_at") or "", 40),
    }
    for key in ("icon", "mock_css", "fonts_css"):
        v = raw.get(key)
        if v:
            if not isinstance(v, str) or not FILE_RE.match(v):
                raise ReviewError(f"design.json: {key} must be a file name inside the profile")
            if not (d / v).is_file():
                raise ReviewError(f"design.json: {key} file not found: {v}")
            out[key] = v
    out["projects"] = _clean_projects(raw.get("projects"))
    return out


def _clean_projects(v: Any) -> List[str]:
    """design.json `projects`: absolute directory paths the profile is bound to (deduplicated, at most MAX_PROJECTS)."""
    if v is None:
        return []
    if not isinstance(v, list):
        raise ReviewError("design.json: projects must be a list of absolute paths")
    out: List[str] = []
    seen = set()
    for p in v:
        if not isinstance(p, str) or not p.strip() or len(p) > 500 or not Path(p.strip()).is_absolute():
            raise ReviewError(f"design.json: projects must hold absolute paths, got {p!r}")
        p = p.strip()
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    if len(out) > MAX_PROJECTS:
        raise ReviewError(f"design.json: at most {MAX_PROJECTS} projects per profile")
    return out


@contextlib.contextmanager
def designs_locked() -> Iterator[None]:
    """Serialises every edit of a saved profile (save, bind, unbind) so none loses another's `projects`."""
    designs_root().mkdir(parents=True, exist_ok=True)
    with locks.exclusive(designs_root() / ".lock"):
        yield


def _read_projects(d: Path) -> List[str]:
    """The `projects` of a saved profile dir, [] when it has none or cannot be read."""
    try:
        return _clean_projects(json.loads((d / "design.json").read_text(encoding="utf-8")).get("projects"))
    except (OSError, ValueError, ReviewError, AttributeError):
        return []


def project_bindings() -> Dict[str, List[str]]:
    """{profile name: bound project paths} over the saved profiles that have any."""
    out: Dict[str, List[str]] = {}
    root = designs_root()
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and DESIGN_RE.match(d.name):
                projects = _read_projects(d)
                if projects:
                    out[d.name] = projects
    return out


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _write_projects(d: Path, projects: List[str]) -> None:
    raw = json.loads((d / "design.json").read_text(encoding="utf-8"))
    raw["projects"] = projects
    atomic_write(d / "design.json", json.dumps(raw, indent=2, ensure_ascii=False))


def _saved_design(name: str) -> Path:
    if not DESIGN_RE.match(name or ""):
        raise ReviewError("design name must be lowercase letters, digits and dashes")
    d = designs_root() / name
    if not d.is_dir():
        raise ReviewError(f"unknown design {name!r} (save it first with `mockreview design save`)")
    validate_design(d)
    return d


def _conflict(path: str, name: str, bindings: Dict[str, List[str]]) -> None:
    for other, paths in bindings.items():
        if other != name and any(_same_path(path, p) for p in paths):
            raise ReviewError(f"{path} is already bound to design {other!r}; unbind it there first "
                              f"(`hermes mockreview design unbind {other} {path}`)")


def bind_design(name: str, path: str) -> List[str]:
    """Bind a project directory to a saved profile. Returns the profile's project list."""
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ReviewError(f"project path must be absolute: {path}")
    if not Path(path).is_dir():
        raise ReviewError(f"project directory not found: {path}")
    real = os.path.realpath(path)
    with designs_locked():
        d = _saved_design(name)
        _conflict(real, name, project_bindings())
        projects = _read_projects(d)
        if any(_same_path(real, p) for p in projects):
            return projects
        if len(projects) >= MAX_PROJECTS:
            raise ReviewError(f"design {name!r} already has {MAX_PROJECTS} projects")
        projects.append(real)
        _write_projects(d, projects)
        return projects


def unbind_design(name: str, path: str) -> List[str]:
    """Remove a project directory from a saved profile. Returns the profile's project list."""
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ReviewError(f"project path must be absolute: {path}")
    with designs_locked():
        d = _saved_design(name)
        projects = _read_projects(d)
        kept = [p for p in projects if not _same_path(path, p)]
        if len(kept) == len(projects):
            raise ReviewError(f"{path} is not bound to design {name!r}")
        _write_projects(d, kept)
        return kept


def save_design(name: str, src: Path) -> Dict[str, Any]:
    """Copy a design profile dir into designs/<name>/ (replacing the old one). The project bindings of the profile being
    replaced carry over unless the source design.json brings its own `projects`."""
    if not DESIGN_RE.match(name):
        raise ReviewError("design name must be lowercase letters, digits and dashes")
    src = Path(src).expanduser()
    if not src.is_dir():
        raise ReviewError(f"design dir not found: {src}")
    info = validate_design(src)
    dest = designs_root() / name
    tmp = designs_root() / f".{name}.tmp"
    with designs_locked():
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        _copy_assets(src, tmp)
        own = "projects" in json.loads((src / "design.json").read_text(encoding="utf-8"))
        projects = info["projects"] if own else (_read_projects(dest) if dest.is_dir() else [])
        others = project_bindings()
        for p in projects:
            _conflict(p, name, others)
        if projects != info["projects"] or (own and not projects):
            _write_projects(tmp, projects)
        info["projects"] = projects
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
    return info


def design_dir(name_or_path: str) -> Path:
    """A saved profile name, or an absolute path to a profile dir."""
    if name_or_path.startswith(("/", "~")) or Path(name_or_path).is_absolute():   # is_absolute: C:\... on Windows
        d = Path(name_or_path).expanduser()
    else:
        if not DESIGN_RE.match(name_or_path):
            raise ReviewError(f"unknown design {name_or_path!r}")
        d = designs_root() / name_or_path
    if not d.is_dir():
        raise ReviewError(f"design profile not found: {name_or_path} (save one with `mockreview design save`)")
    validate_design(d)
    return d


def saved_design_names() -> List[str]:
    """The names a profile is saved under (its directory names, the ones `design=` takes), sorted."""
    root = designs_root()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and DESIGN_RE.match(d.name))


def list_designs() -> List[Dict[str, Any]]:
    root = designs_root()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and DESIGN_RE.match(d.name):
                try:
                    info = validate_design(d)
                except ReviewError as e:
                    info = {"error": e.message}
                out.append({"name": d.name, **info})
    return out


def _txt(v: Any, limit: int) -> str:
    return _clean_block(v, limit) if isinstance(v, str) else ""


def _tone(v: Any, where: str, default: Optional[str] = None) -> str:
    if v is None and default:
        return default
    if v not in TONES:
        raise ReviewError(f"{where}: tone must be one of {TONES}")
    return v


def _width(b: Dict[str, Any], where: str) -> int:
    dw = b.get("desktop_width") or settings.get("desktop_width")
    if isinstance(dw, bool) or not isinstance(dw, int) or not 320 <= dw <= 1920:
        raise ReviewError(f"{where}: desktop_width must be 320-1920 px")
    return dw


def _cell(v: Any, limit: int, where: str) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise ReviewError(f"{where}: must be text (or a number)")
    return _clean_line(v, limit)


def _cards(b: Dict[str, Any], where: str) -> Dict[str, Any]:
    items = b.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CARDS:
        raise ReviewError(f"{where}: cards needs items, a list of 1-{MAX_CARDS}")
    out = []
    for j, it in enumerate(items):
        w = f"{where}.items[{j}]"
        if not isinstance(it, dict):
            raise ReviewError(f"{w}: must be an object {{title, text?, pill?, tone?}}")
        title = _clean_line(it.get("title") or "", 120) if isinstance(it.get("title"), str) else ""
        if not title:
            raise ReviewError(f"{w}: title is required")
        out.append({"title": title, "text": _txt(it.get("text"), 600),
                    "pill": _clean_line(it["pill"], 40) if isinstance(it.get("pill"), str) else "",
                    "tone": _tone(it.get("tone"), w, "neutral")})
    return {"items": out}


def _callout(b: Dict[str, Any], where: str) -> Dict[str, Any]:
    text = _txt(b.get("text"), 1200)
    if not text:
        raise ReviewError(f"{where}: callout needs text")
    return {"tone": _tone(b.get("tone"), where), "text": text}


def _table(b: Dict[str, Any], where: str) -> Dict[str, Any]:
    cols = b.get("columns")
    if not isinstance(cols, list) or not 2 <= len(cols) <= MAX_COLUMNS:
        raise ReviewError(f"{where}: table needs columns, a list of 2-{MAX_COLUMNS} headings")
    columns = [_cell(c, 60, f"{where}.columns[{j}]") for j, c in enumerate(cols)]
    rows = b.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ReviewError(f"{where}: table needs rows, a list of 1-{MAX_ROWS} rows")
    out_rows = []
    for j, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ReviewError(f"{where}.rows[{j}]: must be a list of {len(columns)} cells (one per column)")
        out_rows.append([_cell(c, 240, f"{where}.rows[{j}][{k}]") for k, c in enumerate(row)])
    hl = b.get("highlight_col")
    if hl is not None and (isinstance(hl, bool) or not isinstance(hl, int) or not 0 <= hl < len(columns)):
        raise ReviewError(f"{where}: highlight_col must be a column index 0-{len(columns) - 1}")
    return {"columns": columns, "rows": out_rows, "highlight_col": hl}


def _line(v: Any, limit: int) -> str:
    return _clean_line(v, limit) if isinstance(v, str) else ""


def _obj(v: Any, where: str, shape: str) -> Dict[str, Any]:
    if not isinstance(v, dict):
        raise ReviewError(f"{where}: must be an object {shape}")
    return v


def _list(v: Any, where: str, lo: int, hi: int, what: str) -> List[Any]:
    if v is None and lo == 0:
        return []
    if not isinstance(v, list) or not lo <= len(v) <= hi:
        raise ReviewError(f"{where}: {what} must be a list of {lo}-{hi}")
    return v


def _strings(v: Any, where: str, lo: int, hi: int, limit: int, what: str = "items") -> List[str]:
    out = []
    for j, x in enumerate(_list(v, where, lo, hi, what)):
        s = _line(x, limit)
        if not s:
            raise ReviewError(f"{where}.{what}[{j}]: must be non-empty text")
        out.append(s)
    return out


def _markable_extras(b: Dict[str, Any]) -> Dict[str, str]:
    """revised / asked / answer / group: the round 2+ badges and strip, and the plan preset's section label."""
    return {"revised": _line(b.get("revised"), 40), "asked": _txt(b.get("asked"), 600),
            "answer": _txt(b.get("answer"), 600), "group": _line(b.get("group"), 40)}


def _settled(b: Dict[str, Any], where: str) -> Dict[str, Any]:
    out = []
    for j, it in enumerate(_list(b.get("items"), where, 1, MAX_ITEMS, "settled items")):
        w = f"{where}.items[{j}]"
        it = _obj(it, w, "{title, quote?, rule?}")
        title = _line(it.get("title"), 120)
        if not title:
            raise ReviewError(f"{w}: title is required")
        out.append({"title": title, "quote": _txt(it.get("quote"), 600), "rule": _txt(it.get("rule"), 1200)})
    return {"items": out}


def _question(b: Dict[str, Any], where: str, title: str) -> Dict[str, Any]:
    if not title:
        raise ReviewError(f"{where}: question needs a title")
    rec = _obj(b.get("recommendation"), f"{where}.recommendation", "{answer, why?}")
    answer = _line(rec.get("answer"), 300)
    if not answer:
        raise ReviewError(f"{where}.recommendation: answer is required")
    return {"text": _txt(b.get("text"), 2000), "recommendation": {"answer": answer, "why": _txt(rec.get("why"), 600)}}


def _piece(b: Dict[str, Any], where: str, title: str) -> Dict[str, Any]:
    if not title:
        raise ReviewError(f"{where}: piece needs a title")
    out: Dict[str, Any] = {"tag": _line(b.get("tag"), 80), "text": _txt(b.get("text"), 2000),
                           "items": _strings(b.get("items"), where, 0, MAX_ITEMS, 600)}
    if b.get("html") is not None:
        if not isinstance(b["html"], str) or not b["html"].strip():
            raise ReviewError(f"{where}: piece html must be a non-empty html string")
        out.update(html=b["html"], desktop_width=_width(b, where))
    return out


def _artifact(b: Dict[str, Any], where: str) -> Dict[str, Any]:
    """An artifact block: a page of the agent's own HTML, CSS and script, shown in a sandboxed frame. Exactly one of
    `html` (the page, up to 2 MB) or `src` (an absolute .html path; its local media is bundled in as data: URIs, see
    bundle.bundle_artifacts). The page has no network; it may keep a bounded per-block page state (see
    feedback.sanitize_decisions) and, when markable, set its own mark."""
    has_html, has_src = b.get("html") is not None, b.get("src") is not None
    if has_html == has_src:
        raise ReviewError(f"{where}: artifact needs exactly one of html (the page) or src (an absolute .html path)")
    out: Dict[str, Any] = {"text": _txt(b.get("text"), 2000), "caption": _clean_line(b.get("caption") or "", 300)}
    if has_html:
        if not isinstance(b["html"], str) or not b["html"].strip():
            raise ReviewError(f"{where}: artifact html must be a non-empty html string")
        if len(b["html"].encode("utf-8")) > MAX_ARTIFACT_HTML:
            raise ReviewError(f"{where}: artifact html is larger than {MAX_ARTIFACT_HTML // (1024 * 1024)} MB; "
                              "give the page as src and keep media in files next to it")
        out["html"] = b["html"]
    else:
        out["src"] = _artifact_src(b["src"], where)
    height = b.get("height", "auto")
    lo, hi = ARTIFACT_HEIGHT
    if height != "auto" and (isinstance(height, bool) or not isinstance(height, int) or not lo <= height <= hi):
        raise ReviewError(f"{where}: artifact height must be {lo}-{hi} px or \"auto\"")
    out["height"] = height
    out["desktop_width"] = _width(b, where) if b.get("desktop_width") is not None else None
    markable = b.get("markable", False)
    if not isinstance(markable, bool):
        raise ReviewError(f"{where}: artifact markable must be true or false")
    out["markable"] = markable
    display = b.get("display", "frame")
    if display not in ARTIFACT_DISPLAYS:
        raise ReviewError(f"{where}: artifact display must be \"frame\", \"page\" or \"long\"")
    out["display"] = display
    return out


def _artifact_src(v: Any, where: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ReviewError(f"{where}: artifact src must be an absolute .html path")
    p = Path(v.strip()).expanduser()
    if not p.is_absolute():
        raise ReviewError(f"{where}: artifact src must be absolute: {v}")
    if p.suffix.lower() not in (".html", ".htm"):
        raise ReviewError(f"{where}: artifact src must be an .html file: {v}")
    if not p.is_file():
        raise ReviewError(f"{where}: artifact src not found: {v}")
    if p.stat().st_size > MAX_ARTIFACT_HTML:
        raise ReviewError(f"{where}: artifact src is larger than {MAX_ARTIFACT_HTML // (1024 * 1024)} MB; "
                          "keep media in files next to it (they are bundled in separately)")
    return str(p)


def _status(v: Any) -> Optional[Dict[str, Any]]:
    if v is None:
        return None
    v = _obj(v, "decisions.json: status", "{label?, milestones?}")
    ms = []
    for j, m in enumerate(_list(v.get("milestones"), "decisions.json: status", 0, MAX_MILESTONES, "milestones")):
        w = f"decisions.json: status.milestones[{j}]"
        m = _obj(m, w, "{label, date?}")
        label = _line(m.get("label"), 40)
        if not label:
            raise ReviewError(f"{w}: label is required")
        ms.append({"label": label, "date": _line(m.get("date"), 40)})
    return {"label": _line(v.get("label"), 120), "milestones": ms}


def _notes_answered(v: Any, ids: set) -> List[Dict[str, str]]:
    out = []
    for j, n in enumerate(_list(v, "decisions.json: notes_answered", 0, MAX_NOTES_ANSWERED, "notes_answered")):
        w = f"decisions.json: notes_answered[{j}]"
        n = _obj(n, w, "{ref, note, answer, status?}")
        ref = n.get("ref")
        if not isinstance(ref, str) or ref not in ids:
            raise ReviewError(f"{w}: ref must be the id of a block in this file, got {ref!r}")
        note, answer = _txt(n.get("note"), 600), _txt(n.get("answer"), 600)
        if not note or not answer:
            raise ReviewError(f"{w}: note and answer are required")
        status = n.get("status") or "done"
        if status not in NOTE_STATUSES:
            raise ReviewError(f"{w}: status must be one of {NOTE_STATUSES}")
        out.append({"ref": ref, "note": note, "answer": answer, "status": status})
    return out


def _gate(v: Any, preset: str) -> Optional[Dict[str, Any]]:
    """The locked final approval. Opt-in on a design board, on by default on a plan board."""
    if v is None:
        if preset != "plan":
            return None
        v = {}
    v = _obj(v, "decisions.json: gate", "{label?, text?, after?, later?}")
    w = "decisions.json: gate"
    return {"label": _line(v.get("label"), 40) or GATE_LABEL, "text": _txt(v.get("text"), 600),
            "after": _strings(v.get("after"), w, 0, MAX_GATE_AFTER, 300, "after"),
            "later": _strings(v.get("later"), w, 0, MAX_GATE_LATER, 300, "later")}


def validate_decisions(raw: Any) -> Dict[str, Any]:
    """decisions.json: the content of a decision round. The board renders it; mocks are static HTML strings.
    Markable blocks (decision, question, piece, defaults, a markable `recap` and a markable `artifact`) are voted on;
    text / cards / callout / mock / table / settled and a plain `artifact` are context."""
    if not isinstance(raw, dict):
        raise ReviewError("decisions.json must be an object")
    blocks = raw.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ReviewError("decisions.json: blocks must be a non-empty list")
    if len(blocks) > MAX_BLOCKS:
        raise ReviewError(f"decisions.json: at most {MAX_BLOCKS} blocks")
    preset = raw.get("preset") or "design"
    if preset not in PRESETS:
        raise ReviewError(f"decisions.json: preset must be one of {PRESETS}")
    ids = set()   # decision, question, piece, defaults and context-mock ids: one namespace (pins, DOM ids)
    recap_ids = set()
    all_recap_ids = set()
    out_blocks = []
    for i, b in enumerate(blocks):
        where = f"blocks[{i}]"
        if not isinstance(b, dict) or b.get("type") not in BLOCK_TYPES:
            raise ReviewError(f"{where}: type must be one of {BLOCK_TYPES}")
        t = b["type"]
        ob: Dict[str, Any] = {"type": t, "title": _clean_line(b.get("title") or "", 200)}
        if t in ID_BLOCKS:
            did = b.get("id") or ("defaults" if t == "defaults" else None)
            if not isinstance(did, str) or not BLOCK_ID_RE.fullmatch(did):
                raise ReviewError(f"{where}: {t} id must be short lowercase (d1, d2, ...)")
            if did in RESERVED_REFS:
                raise ReviewError(f"decisions.json: block id '{did}' is reserved for the {RESERVED_REF_WHERE[did]}")
            if did in ids:
                raise ReviewError(f"{where}: duplicate block id {did}")
            ids.add(did)
        if t == "text":
            ob["text"] = _txt(b.get("text"), 6000)
        elif t == "recap":
            items = b.get("items") if isinstance(b.get("items"), list) else []
            ob["items"] = [_clean_line(x, 600) for x in items if isinstance(x, str)][:40]
            ob["id"] = _clean_line(b.get("id") or "recap", 24)
            if ob["id"] in RESERVED_REFS:
                raise ReviewError(f"decisions.json: block id '{ob['id']}' is reserved for the {RESERVED_REF_WHERE[ob['id']]}")
            ob["markable"] = bool(b.get("markable", True))
            all_recap_ids.add(ob["id"])
            if ob["markable"]:
                recap_ids.add(ob["id"])
                ob.update(_markable_extras(b))
        elif t == "cards":
            ob.update(_cards(b, where))
        elif t == "callout":
            ob.update(_callout(b, where))
        elif t == "table":
            ob.update(_table(b, where))
        elif t == "settled":
            ob.update(_settled(b, where))
        elif t == "defaults":
            ob.update({"id": did, "text": _txt(b.get("text"), 1200),
                       "items": _strings(b.get("items"), where, 1, MAX_ITEMS, 600)}, **_markable_extras(b))
        elif t == "question":
            ob.update({"id": did}, **_question(b, where, ob["title"]), **_markable_extras(b))
        elif t == "piece":
            ob.update({"id": did}, **_piece(b, where, ob["title"]), **_markable_extras(b))
        elif t == "artifact":
            art = _artifact(b, where)
            ob.update({"id": did}, **art)
            if art["markable"]:
                ob.update(_markable_extras(b))
        elif t == "mock":
            if not isinstance(b.get("html"), str) or not b["html"].strip():
                raise ReviewError(f"{where}: mock needs an html string")
            ob.update({"id": did, "text": _txt(b.get("text"), 2000), "caption": _clean_line(b.get("caption") or "", 300),
                       "html": b["html"], "desktop_width": _width(b, where)})
        else:
            layout = b.get("layout") or "side"
            if layout not in LAYOUTS:
                raise ReviewError(f"{where}: layout must be one of {LAYOUTS}")
            opts = b.get("options")
            if not isinstance(opts, list) or not 1 <= len(opts) <= MAX_OPTIONS:
                raise ReviewError(f"{where}: needs 1-{MAX_OPTIONS} options")
            oo, oids = [], set()
            for j, o in enumerate(opts):
                if not isinstance(o, dict) or not isinstance(o.get("html"), str):
                    raise ReviewError(f"{where}.options[{j}]: needs an html string")
                oid = o.get("id") or "abcdef"[j]
                if not re.fullmatch(r"[a-z0-9]{1,8}", str(oid)) or oid in oids:
                    raise ReviewError(f"{where}.options[{j}]: bad or duplicate option id")
                oids.add(oid)
                oo.append({"id": oid, "label": _clean_line(o.get("label") or oid.upper(), 80),
                           "text": _clean_line(o.get("text") or "", 400), "recommended": bool(o.get("recommended")),
                           "html": o["html"]})
            ob.update({"id": did, "tag": _clean_line(b.get("tag") or "", 80), "nav": _clean_line(b.get("nav") or "", 24),
                       "today": _txt(b.get("today"), 1200), "text": _txt(b.get("text"), 2000), "layout": layout,
                       "options": oo, "desktop_width": _width(b, where)}, **_markable_extras(b))
        out_blocks.append(ob)
    clash = ids & recap_ids
    if clash:
        raise ReviewError(f"decisions.json: block id {sorted(clash)[0]} is used twice (a recap and a decision/mock)")
    return {"intro": _txt(raw.get("intro"), 3000), "next": _txt(raw.get("next"), 600),
            "extra_css": raw.get("extra_css") if isinstance(raw.get("extra_css"), str) else "",
            "preset": preset, "status": _status(raw.get("status")),
            "notes_answered": _notes_answered(raw.get("notes_answered"), ids | all_recap_ids),
            "gate": _gate(raw.get("gate"), preset),
            "blocks": out_blocks}


def _entry(b: Dict[str, Any], kind: str, **extra: Any) -> Dict[str, Any]:
    e = {"id": b["id"], "title": b["title"], "tag": "", "nav": "", "layout": kind, "options": [], "kind": kind,
         "group": b.get("group", ""), "revised": b.get("revised", ""), "pinnable": False}
    e.update(extra)
    return e


def decision_index(dec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What feedback validation + markdown need: every markable block, its title and its options. `kind` says which
    control the block takes (decision, recap, piece, defaults: approve/change/skip; question: a choice); `pinnable`
    says whether pins may land on it. Rounds stored before 1.2 lack both (see entry_kind / feedback)."""
    out: List[Dict[str, Any]] = []
    for b in dec["blocks"]:
        if b["type"] == "decision":
            out.append(_entry(b, "decision", tag=b["tag"], nav=b["nav"], layout=b["layout"], pinnable=True,
                              options=[{"id": o["id"], "label": o["label"], "recommended": o["recommended"]}
                                       for o in b["options"]]))
        elif b["type"] == "recap" and b.get("markable"):
            out.append(_entry(b, "recap", title=b["title"] or "Recap", nav="Recap", pinnable=True))
        elif b["type"] == "defaults":
            out.append(_entry(b, "defaults", title=b["title"] or "How I read the rest"))
        elif b["type"] == "question":
            out.append(_entry(b, "question", recommendation=dict(b["recommendation"])))
        elif b["type"] == "piece":
            out.append(_entry(b, "piece", tag=b["tag"], pinnable="html" in b))
        elif b["type"] == "artifact" and b.get("markable"):
            out.append(_entry(b, "artifact", pinnable=True))
    return out


def entry_kind(e: Dict[str, Any]) -> str:
    """An index entry's kind, also for rounds stored before format 1.2 (no `kind` key)."""
    return e.get("kind") or {"recap": "recap", "prototype": "prototype"}.get(e.get("layout"), "decision")


def mock_index(dec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Context mocks and artifacts that are not markable: pins may land on them (block = their id), but they are not
    decisions (no marks, no tally). `kind` is set on an artifact only."""
    return [dict({"id": b["id"], "title": b["title"]}, **({"kind": "artifact"} if b["type"] == "artifact" else {}))
            for b in dec["blocks"] if b["type"] == "mock" or (b["type"] == "artifact" and not b.get("markable"))]


def round_mocks(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (r.get("mocks") or []) if r.get("kind") == "decisions" else []


def round_artifacts(r: Dict[str, Any]) -> List[str]:
    """The ids of the round's artifact blocks, markable and not. The board's page-state store is per block, and only
    these ids may carry state."""
    if r.get("kind") != "decisions":
        return []
    ids = [e["id"] for e in (r.get("decisions") or []) if entry_kind(e) == "artifact"]
    ids += [m["id"] for m in round_mocks(r) if m.get("kind") == "artifact"]
    return ids


SECTION_TITLES = {"text": "Text", "cards": "Cards", "callout": "Callout", "table": "Table", "settled": "Already decided",
                  "recap": "Recap", "defaults": "How I read the rest", "question": "Question", "decision": "Decision",
                  "piece": "Piece", "mock": "Mock", "artifact": "Artifact"}

# The three board-level surfaces of a decisions round. Each names exactly one rendered element and is refused as a
# decisions.json block id.
RESERVED_REFS = ("page", "else", "doc")
RESERVED_REF_WHERE = {"page": "board header", "else": "board's \"Anything else\" section", "doc": "board canvas"}


def section_index(dec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One pinnable entry per block of decisions.json, in file order: `ref` is the block id when it has one, else a
    generated `s<N>` that never collides with a block id (ids and generated refs are one namespace). `page`, `else`
    and `doc` are reserved for the board header, "Anything else" and the board canvas; they are refused as block ids
    by validate_decisions and are not part of this index."""
    ids = {b["id"] for b in dec["blocks"] if b.get("id")}
    out: List[Dict[str, Any]] = []
    n = 0
    for b in dec["blocks"]:
        ref = b.get("id")
        if not ref:
            n += 1
            ref = f"s{n}"
            while ref in ids:
                n += 1
                ref = f"s{n}"
        title = b.get("title") or SECTION_TITLES.get(b["type"], b["type"].title())
        out.append({"ref": ref, "type": b["type"], "title": title})
    return out


def round_sections(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The round's rendered sections: one entry per block, in file order. The reserved board refs (`page`, `else`,
    `doc`) are not listed."""
    return (r.get("sections") or []) if r.get("kind") == "decisions" else []


PROTOTYPE_INDEX = [{"id": "proto", "title": "Prototype", "tag": "", "nav": "", "layout": "prototype", "options": [],
                    "kind": "prototype", "group": "", "revised": "", "pinnable": True}]


def round_index(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The round's markable index, each entry carrying the block's current `rev` (0 on old rounds and unpatched blocks)."""
    revs = r.get("blocks_rev") or {}
    if r.get("kind") == "decisions":
        return [dict(e, rev=int(revs.get(e["id"], 0) or 0)) for e in (r.get("decisions") or [])]
    if r.get("kind") == "prototype":
        return [dict(PROTOTYPE_INDEX[0], rev=int(revs.get(PROTOTYPE_INDEX[0]["id"], 0) or 0))]
    return []


def parse_round_spec(decisions: Optional[str] = None, prototype: Optional[str] = None,
                     design: Optional[str] = None, variants: Optional[List[str]] = None) -> Dict[str, Any]:
    """CLI input -> a round spec. Exactly one of decisions / prototype / (legacy, tests) variants."""
    given = [x for x in (decisions, prototype, variants) if x]
    if len(given) != 1:
        raise ReviewError("pass exactly one of --decisions /abs/decisions.json or --prototype /abs/index.html")
    spec: Dict[str, Any] = {"design": design_dir(design) if design else None}
    if decisions:
        p = Path(decisions).expanduser()
        if not p.is_absolute() or not p.is_file():
            raise ReviewError(f"decisions file not found (absolute path): {decisions}")
        if p.stat().st_size > MAX_DECISIONS_BYTES:
            raise ReviewError("decisions.json is larger than 3 MB")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ReviewError(f"decisions.json is not valid JSON: {e}")
        spec.update(kind="decisions", decisions=validate_decisions(raw), src=p)
    elif prototype:
        v = parse_variant_spec(f"Prototype||{prototype}")
        spec.update(kind="prototype", pages=[v])
    else:
        spec.update(kind="variants", pages=[parse_variant_spec(v) for v in variants])
    return spec


def inject_probe(html: str) -> str:
    if PROBE_MARKER in html:
        return html
    probe = _probe()
    idx = html.lower().rfind("</body>")
    if idx == -1:
        return html + probe
    return html[:idx] + probe + html[idx:]


def _copy_assets(src_dir: Path, dest_dir: Path, used: int = 0) -> None:
    """Copy a variant's sibling assets/ dir (or a design profile). Dot entries are never copied (dotfiles and whole
    dot-directories), symlinks are never followed, and a source that is itself a symlink or resolves outside the
    directory the caller named is refused. Size is capped; `used`: bytes the round already holds (its artifacts'
    inlined media), counted against the same cap."""
    src_dir = Path(src_dir)
    if src_dir.is_symlink():
        raise ReviewError(f"refusing to copy {src_dir}: it is a symlink")
    if src_dir.resolve() != src_dir.parent.resolve() / src_dir.name:
        raise ReviewError(f"refusing to copy {src_dir}: it resolves outside {src_dir.parent}")
    total = used
    for root, dirs, files in os.walk(src_dir, followlinks=False):
        rootp = Path(root)
        dirs[:] = [d for d in dirs if not d.startswith(".") and not (rootp / d).is_symlink()]
        for name in files:
            if name.startswith("."):
                continue
            f = rootp / name
            if f.is_symlink() or not f.is_file():
                continue
            total += f.stat().st_size
            if total > MAX_ASSETS_BYTES:
                raise ReviewError(f"assets dir {src_dir} is larger than {size_label(MAX_ASSETS_BYTES)}"
                                  + (" together with the round's artifact media" if used else ""))
            target = dest_dir / f.relative_to(src_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)


def _artifact_header() -> str:
    csp = ARTIFACT_CSP.replace("&", "&amp;").replace('"', "&quot;")
    return '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + csp + '">'


def artifact_page(html: str) -> str:
    """The served file of an artifact: the CSP meta first (nothing runs before it), then the probe, then the page.
    The page's own doctype is dropped (ours leads, so the page is never in quirks mode); its <html>/<head> tags merge
    into the document the parser has already opened. The probe sits at the top so `window.mockreview` exists before the
    page's own scripts run."""
    html = re.sub(r"\A\ufeff?\s*<!doctype[^>]*>", "", html, count=1, flags=re.I)
    return _artifact_header() + _probe() + html


def artifact_board_page(body: bytes) -> bytes:
    """Use the current board probe for a previously installed artifact without rewriting its file.

    The probe is matched by its marker, not by the newline after it: a page written on Windows starts the probe's
    script with `\\r\\n` (`Path.write_text` translates every `\\n`), and it must upgrade exactly like an LF page.
    """
    header = _artifact_header().encode()
    probe = _probe().encode()
    if body.startswith(header + probe) or not body.startswith(header + f"<script>/*{PROBE_MARKER}*/".encode()):
        return body
    end = body.find(b"</script>", len(header))
    return body if end < 0 else header + probe + body[end + len(b"</script>"):]


def _stored_decisions(dec: Dict[str, Any], vdir: Path, used: int = 0) -> Tuple[Dict[str, Any], int]:
    """decisions.json as the board reads it, and the bytes of media the round's artifacts hold inline (`used` on entry:
    what the caller already counted). An artifact's page goes to blocks/<id>.html (served sandboxed, with the CSP meta
    and the probe) and the block keeps only `file`: neither its html nor the owner's `src` path is stored. Every `src`
    page is bundled against the one round budget, so a spec that did not pass through `api._spec` cannot exceed it."""
    blocks = []
    for b in dec["blocks"]:
        if b["type"] != "artifact":
            blocks.append(b)
            continue
        html = b.get("html")
        if html is None:
            from . import bundle   # lazy: bundle imports this module
            html, used = bundle.bundle_artifact(b["src"], b["id"], used)
        (vdir / "blocks").mkdir(exist_ok=True)
        (vdir / "blocks" / f"{b['id']}.html").write_text(artifact_page(html), encoding="utf-8", newline="\n")
        blocks.append({k: v for k, v in b.items() if k not in ("html", "src")} | {"file": f"blocks/{b['id']}.html"})
    return dict(dec, blocks=blocks), used


def _install_round(review_id: str, n: int, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    from .targets import stamp_html, stamp_decisions
    base = review_dir(review_id) / "rounds" / str(n)
    if base.exists():
        shutil.rmtree(base)
    variants: List[Dict[str, Any]] = []
    if spec["kind"] == "decisions":
        vdir = base / "a"
        vdir.mkdir(parents=True)
        # `inlined`: media bundle.bundle_artifacts already inlined into the artifacts' pages (not rebundled here)
        stored, used = _stored_decisions(spec["decisions"], vdir, spec.get("inlined", 0))
        (vdir / "decisions.json").write_text(json.dumps(stamp_decisions(stored), ensure_ascii=False), encoding="utf-8")
        assets = spec["src"].parent / "assets"
        if assets.is_dir() and not assets.is_symlink():
            _copy_assets(assets, vdir / "assets", used)      # one round budget: artifact media inline plus this copy
        variants.append({"id": "a", "label": "Decisions", "stance": "", "file": f"rounds/{n}/a/decisions.json",
                         "source": str(spec["src"])})
    else:
        pages = spec["pages"]
        if not 1 <= len(pages) <= MAX_VARIANTS:
            raise ReviewError(f"need 1-{MAX_VARIANTS} pages, got {len(pages)}")
        for vid, p in zip(VARIANT_IDS, pages):
            vdir = base / vid
            vdir.mkdir(parents=True)
            html = p["src"].read_text(encoding="utf-8", errors="replace")
            (vdir / "index.html").write_text(inject_probe(stamp_html(html) if spec['kind'] == 'prototype' else html), encoding="utf-8")
            assets = p["src"].parent / "assets"
            if assets.is_dir() and not assets.is_symlink():
                _copy_assets(assets, vdir / "assets")
            variants.append({"id": vid, "label": p["label"], "stance": p["stance"],
                             "file": f"rounds/{n}/{vid}/index.html", "source": str(p["src"])})
    if spec.get("design"):
        _copy_assets(spec["design"], base / "a" / "design")
        _strip_projects(base / "a" / "design")
    return variants


def _strip_projects(d: Path) -> None:
    """A round's copy of a profile never carries its `projects`: the board serves that folder, and the paths are the
    owner's, not the reviewer's."""
    f = d / "design.json"
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(raw, dict) and "projects" in raw:
        del raw["projects"]
        atomic_write(f, json.dumps(raw, indent=2, ensure_ascii=False))


def _round_record(n: int, spec: Dict[str, Any], variants: List[Dict[str, Any]]) -> Dict[str, Any]:
    r: Dict[str, Any] = {"n": n, "kind": spec["kind"], "created_at": now_iso(), "variants": variants,
                         "feedback": None, "submission": None,
                         "pin_requests": {}, "pin_fix": {}, "blocks_rev": {}, "history": []}
    if spec["kind"] == "decisions":
        r["decisions"] = decision_index(spec["decisions"])
        r["sections"] = section_index(spec["decisions"])
        mocks = mock_index(spec["decisions"])
        if mocks:
            r["mocks"] = mocks
        r["preset"] = spec["decisions"].get("preset") or "design"
        if spec["decisions"].get("gate"):
            r["gate"] = spec["decisions"]["gate"]   # feedback.gate_state / the build action read it from here
    if spec.get("design"):
        r["design"] = validate_design(spec["design"])
        r["design"].pop("projects", None)
    if spec.get("off_brand"):
        r["off_brand"] = spec["off_brand"]
    return r


def normalize_origin(origin: Any, wake: bool = True) -> Dict[str, str]:
    """Validate an explicit delivery origin; never infer a platform from an identifier.

    A Desktop (`local`) origin also carries `session_key`, the route key its own session is woken by (taken from that
    session's context at create, see platforms.session_origin); no other platform has one. Origins stored before it
    existed load unchanged."""
    if not origin and not wake:
        return {}
    if not isinstance(origin, dict):
        raise ReviewError("origin needs explicit platform and chat_id; legacy thread-only origins need migration")
    platform = origin.get("platform")
    chat = origin.get("chat_id")
    thread = origin.get("thread_id")
    if not isinstance(platform, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", platform):
        raise ReviewError("origin platform is required (for example discord or telegram)")
    identifier = r"[A-Za-z0-9_.:@!#+/~=-]{1,256}"
    if not isinstance(chat, str) or not re.fullmatch(identifier, chat):
        raise ReviewError("origin chat_id is required (1-256 safe identifier characters); "
                          "migrate legacy thread-only origins explicitly before waking")
    out = {"platform": platform, "chat_id": chat}
    if "thread_id" in origin:
        if not isinstance(thread, str) or not re.fullmatch(identifier, thread):
            raise ReviewError("origin thread_id must contain 1-256 safe identifier characters")
        out["thread_id"] = thread
    if "session_key" in origin:
        key = origin.get("session_key")
        if platform != "local":
            raise ReviewError("origin session_key is only for a Desktop (local) origin")
        if not isinstance(key, str) or not re.fullmatch(identifier, key):
            raise ReviewError("origin session_key must contain 1-256 safe identifier characters")
        out["session_key"] = key
    return out


def create_review(title: str, brief: str, origin: Any, mode: str, spec: Any,
                  look: Optional[Dict[str, Any]] = None, wake: bool = True) -> Dict[str, Any]:
    """`look`: the per-review settings (settings.review_settings()); stored so the review never changes under the
    reviewer when config changes. None = old behaviour (the board's built-in defaults)."""
    spec = _as_spec(spec)
    if mode not in MODES:
        raise ReviewError(f"mode must be one of {MODES}")
    title = _clean_line(title, MAX_TITLE)
    if not title:
        raise ReviewError("title is empty")
    origin = normalize_origin(origin, wake=wake)
    rid = new_review_id(title)
    while exists(rid):
        rid = new_review_id(title)
    with locked(rid):
        variants = _install_round(rid, 1, spec)
        data: Dict[str, Any] = {
            "schema": 1,
            "id": rid,
            "title": title,
            "brief": _clean_block(brief, MAX_BRIEF),
            "mode": mode,
            "origin": origin,
            "status": "open",
            # Public state exposes this token. It prevents stale submissions; it is not authentication.
            "token": secrets.token_urlsafe(24),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "rounds": [_round_record(1, spec, variants)],
        }
        if look:
            data["settings"] = dict(look)
        if spec.get("no_design"):
            data["no_design"] = True       # later rounds need not repeat it (see api.add_round)
        if spec.get("off_brand"):
            data["off_brand"] = spec["off_brand"]
        if not wake:
            data["wake"] = False       # a board nobody is woken by: Send stores feedback, sends nothing
        save(rid, data)
    return data


def inherited_design(review_id: str, data: Dict[str, Any]) -> Optional[Path]:
    """The design dir the next round of this review inherits (the newest round's copy), or None."""
    prev = review_dir(review_id) / "rounds" / str(len(data["rounds"])) / "a" / "design"
    return prev if prev.is_dir() else None


def add_round(review_id: str, spec: Any, look: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`look`: replaces the review's stored settings (only when the caller passed per-review overrides)."""
    spec = _as_spec(spec)
    with locked(review_id):
        data = load(review_id)
        if data["status"] != "open":
            raise ReviewError("review is closed", 409)
        n = len(data["rounds"]) + 1
        if not spec.get("design"):
            prev = inherited_design(review_id, data)
            if prev:
                spec = dict(spec, design=prev)   # a new round keeps the review's design unless told otherwise
        variants = _install_round(review_id, n, spec)
        data["rounds"].append(_round_record(n, spec, variants))
        if spec.get("off_brand"):
            data["off_brand"] = spec["off_brand"]
        if look:
            data["settings"] = dict(look)
        save(review_id, data)
    return data


def _as_spec(spec: Any) -> Dict[str, Any]:
    """A round spec, or (legacy callers / tests) a list of parse_variant_spec() dicts."""
    if isinstance(spec, list):
        return {"kind": "prototype" if len(spec) == 1 else "variants", "pages": spec, "design": None}
    return spec


def get_round(data: Dict[str, Any], n: Optional[int]) -> Dict[str, Any]:
    if n is None:
        return data["rounds"][-1]
    for r in data["rounds"]:
        if r["n"] == n:
            return r
    raise ReviewError(f"round {n} not found", 404)


def guest_submission(round_: Dict[str, Any]) -> bool:
    """True when the round's canonical submission was made by a share-link guest: owner-only data, never agent data."""
    return bool((round_.get("submission") or {}).get("guest"))


def review_url(data: Dict[str, Any], public_base: Optional[str] = None) -> str:
    """The review's board address. `public_base` lets a caller listing many reviews resolve config.public_link_base()
    (which may ask the tailscale CLI) once instead of once per review."""
    if data["mode"] == "local":
        return f"{config.local_link_base()}/r/{data['id']}"
    base = config.public_link_base() if public_base is None else public_base
    return f"{base}/r/{data['id']}" if base else ""


def public_state(data: Dict[str, Any]) -> Dict[str, Any]:
    """What the board sees. No filesystem paths or raw pin requests."""
    from . import service   # service imports this module at load time
    rounds = []
    minutes = service.pin_timeout_minutes()
    for r in data["rounds"]:
        sub = r.get("submission") or None
        pin_requests = {}
        for pid, q in (r.get("pin_requests") or {}).items():
            status = service.pin_fix_status(data, q)
            active = (r is data["rounds"][-1] and data["status"] == "open"
                      and data.get("wake") is not False and not q.get("quiet"))
            deadline = (service.pin_request_deadline(data, q, minutes)
                        if active and status["state"] in ("queued", "working") else None)
            pin_requests[q.get("id") or pid] = {
                "id": q.get("id") or pid, "block": q.get("block"), "option": q.get("option"),
                "status": status["state"], "requested_at": q.get("requested_at"), "deadline": deadline,
                "awaiting_delivery": (active and q.get("source") == "mention"
                                      and status["state"] == "queued" and deadline is None),
                "done_at": q.get("done_at"), "rev": q.get("rev"),
                "thread_id": q.get("thread_id"), "pin_id": q.get("pin_id"),
                "position": q.get("position"),
                "detail": "" if status["state"] == "failed" else status["detail"],
                "timed_out": q.get("status") == "timeout",
            }
        rounds.append({
            "n": r["n"],
            "kind": r.get("kind", "variants"),
            "design": r.get("design"),
            "decisions": round_index(r),
            "blocks_rev": {str(k): int(v or 0) for k, v in (r.get("blocks_rev") or {}).items()},
            "sections": round_sections(r),
            "preset": r.get("preset", "design"),
            "gate": r.get("gate"),
            "created_at": r["created_at"],
            "variants": [{k: v[k] for k in ("id", "label", "stance", "file")} for v in r["variants"]],
            "feedback": r.get("feedback"),
            "pin_requests": pin_requests,
            "submission": None if not sub else {
                "status": sub.get("status"),
                "detail": sub.get("stored_detail") or "",
                "submitted_at": sub.get("submitted_at"),
                "notified_at": sub.get("notified_at"),
                "amended": bool(sub.get("seq", 1) > 1),
                "notice_notified_at": (sub.get("notice") or {}).get("notified_at"),
                "stalled_notice_at": (sub.get("stalled_notice") or {}).get("notified_at"),
            },
        })
    state = {
        "id": data["id"],
        "title": data["title"],
        "brief": data.get("brief", ""),
        "mode": data["mode"],
        "status": data["status"],
        "wake": data.get("wake") is not False,
        "token": data["token"],
        "current_round": data["rounds"][-1]["n"],
        "rounds": rounds,
        "submit": {"kind": "local"},
        "settings": data.get("settings"),
        "generated_at": now_iso(),
    }
    return state


def variant_has_artifact(review_id: str, round_n: str, variant: str) -> bool:
    """True when this round's folder holds an artifact page (blocks/<id>.html, written by `_stored_decisions` and nowhere
    else). The server then gives every file of the folder the artifact-grade header, so a page that navigates its own
    frame to another file of the round still has no network. Looks at the folder only: no decisions.json parse, and a
    symlinked `blocks` or page is not counted."""
    if not re.fullmatch(r"[0-9]{1,3}", round_n) or not VARIANT_RE.match(variant):
        return False
    d = review_dir(review_id) / "rounds" / str(int(round_n)) / variant / "blocks"
    try:
        if d.is_symlink() or not d.is_dir():
            return False
        return any(e.name.endswith(".html") and e.is_file(follow_symlinks=False) for e in os.scandir(d))
    except OSError:
        return False


def review_has_artifact(review_id: str) -> bool:
    """True when any round of this review holds an artifact page (blocks/<id>.html). The server then gives every round
    file of the review the artifact-grade header, not only the artifact's own round: a document navigated into the
    artifact frame from an older round must have no network either. Folder scan only, like `variant_has_artifact`;
    a symlinked round, variant or `blocks` folder and a symlinked page are not counted. False on a bad id or a read
    error."""
    try:
        rounds = review_dir(review_id) / "rounds"
        if rounds.is_symlink() or not rounds.is_dir():
            return False
        for rd in os.scandir(rounds):
            if not rd.is_dir(follow_symlinks=False) or not re.fullmatch(r"[0-9]{1,3}", rd.name):
                continue
            for vd in os.scandir(rd.path):
                if not vd.is_dir(follow_symlinks=False) or not VARIANT_RE.match(vd.name):
                    continue
                blocks = Path(vd.path) / "blocks"
                if blocks.is_symlink() or not blocks.is_dir():
                    continue
                if any(e.name.endswith(".html") and e.is_file(follow_symlinks=False) for e in os.scandir(blocks)):
                    return True
        return False
    except (OSError, ReviewError):
        return False


def safe_variant_file(review_id: str, round_n: str, variant: str, rel: str) -> Path:
    """Resolve a file inside rounds/<n>/<variant>/, refusing anything that escapes it."""
    if not re.fullmatch(r"[0-9]{1,3}", round_n) or not VARIANT_RE.match(variant):
        raise ReviewError("not found", 404)
    if not rel or rel.endswith("/"):
        rel = (rel or "") + "index.html"
    if "\x00" in rel or "\\" in rel:
        raise ReviewError("not found", 404)
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise ReviewError("not found", 404)
    base = (review_dir(review_id) / "rounds" / str(int(round_n)) / variant).resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ReviewError("not found", 404)
    if not target.is_file():
        raise ReviewError("not found", 404)
    return target
