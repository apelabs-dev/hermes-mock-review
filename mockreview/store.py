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
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import config, locks, passwords, settings

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
VARIANT_IDS = "abcd"
VARIANT_RE = re.compile(r"^[a-d]$")
MODES = ("tailscale", "local", "herenow")
MAX_VARIANTS = 4
MAX_TITLE = 160
MAX_BRIEF = 4000
MAX_LABEL = 60
MAX_STANCE = 240
MAX_ASSETS_BYTES = 50 * 1024 * 1024

# Injected into every variant page (see board/probe.js): reports page size and
# comment-mode taps to the board via postMessage and draws pin markers.
PROBE_MARKER = "mockreview-probe"


def _probe() -> str:
    js = (config.BOARD_DIR / "probe.js").read_text(encoding="utf-8")
    return "<script>/*" + PROBE_MARKER + "*/\n" + js + "</script>"


class ReviewError(Exception):
    """An error with an HTTP-ish status code and a message safe to show users."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def load(review_id: str) -> Dict[str, Any]:
    p = review_dir(review_id) / "review.json"
    if not p.is_file():
        raise ReviewError("review not found", 404)
    return json.loads(p.read_text(encoding="utf-8"))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def save(review_id: str, data: Dict[str, Any]) -> None:
    data["updated_at"] = now_iso()
    atomic_write(review_dir(review_id) / "review.json", json.dumps(data, indent=2, ensure_ascii=False))


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
BLOCK_TYPES = ("text", "recap", "decision", "cards", "callout", "mock", "table")
TONES = ("neutral", "ok", "warn", "err", "accent")
BLOCK_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
MAX_CARDS = 12
MAX_COLUMNS = 6
MAX_ROWS = 30


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
    return out


def save_design(name: str, src: Path) -> Dict[str, Any]:
    """Copy a design profile dir into designs/<name>/ (replacing the old one)."""
    if not DESIGN_RE.match(name):
        raise ReviewError("design name must be lowercase letters, digits and dashes")
    src = Path(src).expanduser()
    if not src.is_dir():
        raise ReviewError(f"design dir not found: {src}")
    info = validate_design(src)
    dest = designs_root() / name
    tmp = designs_root() / f".{name}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    _copy_assets(src, tmp)
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


def validate_decisions(raw: Any) -> Dict[str, Any]:
    """decisions.json: the content of a decision round. The board renders it; mocks are static HTML strings.
    Only `decision` blocks (and a markable `recap`) are voted on; text / cards / callout / mock / table are context."""
    if not isinstance(raw, dict):
        raise ReviewError("decisions.json must be an object")
    blocks = raw.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ReviewError("decisions.json: blocks must be a non-empty list")
    if len(blocks) > MAX_BLOCKS:
        raise ReviewError(f"decisions.json: at most {MAX_BLOCKS} blocks")
    ids = set()   # decision + context-mock ids: one namespace (pins, DOM ids)
    recap_ids = set()
    out_blocks = []
    for i, b in enumerate(blocks):
        where = f"blocks[{i}]"
        if not isinstance(b, dict) or b.get("type") not in BLOCK_TYPES:
            raise ReviewError(f"{where}: type must be one of {BLOCK_TYPES}")
        t = b["type"]
        ob: Dict[str, Any] = {"type": t, "title": _clean_line(b.get("title") or "", 200)}
        if t in ("decision", "mock"):
            did = b.get("id")
            if not isinstance(did, str) or not BLOCK_ID_RE.fullmatch(did):
                raise ReviewError(f"{where}: {t} id must be short lowercase (d1, d2, ...)")
            if did in ids:
                raise ReviewError(f"{where}: duplicate block id {did}")
            ids.add(did)
        if t == "text":
            ob["text"] = _txt(b.get("text"), 6000)
        elif t == "recap":
            items = b.get("items") if isinstance(b.get("items"), list) else []
            ob["items"] = [_clean_line(x, 600) for x in items if isinstance(x, str)][:40]
            ob["id"] = _clean_line(b.get("id") or "recap", 24)
            ob["markable"] = bool(b.get("markable", True))
            if ob["markable"]:
                recap_ids.add(ob["id"])
        elif t == "cards":
            ob.update(_cards(b, where))
        elif t == "callout":
            ob.update(_callout(b, where))
        elif t == "table":
            ob.update(_table(b, where))
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
                       "options": oo, "desktop_width": _width(b, where)})
        out_blocks.append(ob)
    clash = ids & recap_ids
    if clash:
        raise ReviewError(f"decisions.json: block id {sorted(clash)[0]} is used twice (a recap and a decision/mock)")
    return {"intro": _txt(raw.get("intro"), 3000), "next": _txt(raw.get("next"), 600),
            "extra_css": raw.get("extra_css") if isinstance(raw.get("extra_css"), str) else "",
            "blocks": out_blocks}


def decision_index(dec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What feedback validation + markdown need: every markable block, its title and its options."""
    out: List[Dict[str, Any]] = []
    for b in dec["blocks"]:
        if b["type"] == "decision":
            out.append({"id": b["id"], "title": b["title"], "tag": b["tag"], "nav": b["nav"], "layout": b["layout"],
                        "options": [{"id": o["id"], "label": o["label"], "recommended": o["recommended"]}
                                    for o in b["options"]]})
        elif b["type"] == "recap" and b.get("markable"):
            out.append({"id": b["id"], "title": b["title"] or "Recap", "tag": "", "nav": "Recap", "layout": "recap",
                        "options": []})
    return out


def mock_index(dec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Context mocks: pins may land on them (block = their id), but they are not decisions (no marks, no tally)."""
    return [{"id": b["id"], "title": b["title"]} for b in dec["blocks"] if b["type"] == "mock"]


def round_mocks(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (r.get("mocks") or []) if r.get("kind") == "decisions" else []


PROTOTYPE_INDEX = [{"id": "proto", "title": "Prototype", "tag": "", "nav": "", "layout": "prototype", "options": []}]


def round_index(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    if r.get("kind") == "decisions":
        return r.get("decisions") or []
    if r.get("kind") == "prototype":
        return PROTOTYPE_INDEX
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


def _copy_assets(src_dir: Path, dest_dir: Path) -> None:
    """Copy a variant's sibling assets/ dir. Symlinks are skipped, size is capped."""
    total = 0
    for root, dirs, files in os.walk(src_dir, followlinks=False):
        rootp = Path(root)
        dirs[:] = [d for d in dirs if not (rootp / d).is_symlink()]
        for name in files:
            f = rootp / name
            if f.is_symlink() or not f.is_file():
                continue
            total += f.stat().st_size
            if total > MAX_ASSETS_BYTES:
                raise ReviewError(f"assets dir {src_dir} is larger than 50 MB")
            target = dest_dir / f.relative_to(src_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)


def _install_round(review_id: str, n: int, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = review_dir(review_id) / "rounds" / str(n)
    if base.exists():
        shutil.rmtree(base)
    variants: List[Dict[str, Any]] = []
    if spec["kind"] == "decisions":
        vdir = base / "a"
        vdir.mkdir(parents=True)
        (vdir / "decisions.json").write_text(json.dumps(spec["decisions"], ensure_ascii=False), encoding="utf-8")
        assets = spec["src"].parent / "assets"
        if assets.is_dir() and not assets.is_symlink():
            _copy_assets(assets, vdir / "assets")
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
            (vdir / "index.html").write_text(inject_probe(html), encoding="utf-8")
            assets = p["src"].parent / "assets"
            if assets.is_dir() and not assets.is_symlink():
                _copy_assets(assets, vdir / "assets")
            variants.append({"id": vid, "label": p["label"], "stance": p["stance"],
                             "file": f"rounds/{n}/{vid}/index.html", "source": str(p["src"])})
    if spec.get("design"):
        _copy_assets(spec["design"], base / "a" / "design")
    return variants


def _round_record(n: int, spec: Dict[str, Any], variants: List[Dict[str, Any]]) -> Dict[str, Any]:
    r: Dict[str, Any] = {"n": n, "kind": spec["kind"], "created_at": now_iso(), "variants": variants,
                         "feedback": None, "submission": None}
    if spec["kind"] == "decisions":
        r["decisions"] = decision_index(spec["decisions"])
        mocks = mock_index(spec["decisions"])
        if mocks:
            r["mocks"] = mocks
    if spec.get("design"):
        r["design"] = validate_design(spec["design"])
    return r


def normalize_origin(origin: Any, wake: bool = True) -> Dict[str, str]:
    """Validate an explicit delivery origin; never infer a platform from an identifier."""
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
    return out


def create_review(title: str, brief: str, origin: Any, mode: str, spec: Any,
                  look: Optional[Dict[str, Any]] = None, wake: bool = True,
                  password: Optional[bool] = None) -> Dict[str, Any]:
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
        if not wake:
            data["wake"] = False       # a board nobody is woken by: Send stores feedback, sends nothing
        if mode == "herenow":
            enabled = settings.get("herenow_password", password)
            data["herenow"] = {"slug": None, "site_url": None, "processed_records": [], "last_poll": None,
                               "password_enabled": enabled, "password": passwords.generate() if enabled else None,
                               "password_applied": None, "password_status": "pending"}
        save(rid, data)
    return data


def add_round(review_id: str, spec: Any, look: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`look`: replaces the review's stored settings (only when the caller passed per-review overrides)."""
    spec = _as_spec(spec)
    with locked(review_id):
        data = load(review_id)
        if data["status"] != "open":
            raise ReviewError("review is closed", 409)
        n = len(data["rounds"]) + 1
        if not spec.get("design"):
            prev = review_dir(review_id) / "rounds" / str(n - 1) / "a" / "design"
            if prev.is_dir():
                spec = dict(spec, design=prev)   # a new round keeps the review's design unless told otherwise
        variants = _install_round(review_id, n, spec)
        data["rounds"].append(_round_record(n, spec, variants))
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


def review_url(data: Dict[str, Any]) -> str:
    if data["mode"] == "herenow":
        hn = data.get("herenow") or {}
        if "password_enabled" in hn and hn.get("password_status") != "applied":
            return ""
        return hn.get("site_url") or ""
    if data["mode"] == "local":
        return f"{config.local_link_base()}/r/{data['id']}"
    base = config.public_base()
    return f"{base}/r/{data['id']}" if base else ""


def public_state(data: Dict[str, Any], submit_kind: str = "local") -> Dict[str, Any]:
    """What the board sees. No thread id, no filesystem paths."""
    rounds = []
    for r in data["rounds"]:
        sub = r.get("submission") or None
        rounds.append({
            "n": r["n"],
            "kind": r.get("kind", "variants"),
            "design": r.get("design"),
            "decisions": round_index(r),
            "created_at": r["created_at"],
            "variants": [{k: v[k] for k in ("id", "label", "stance", "file")} for v in r["variants"]],
            "feedback": r.get("feedback"),
            "submission": None if not sub else {
                "status": sub.get("status"),
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
        "token": data["token"],
        "current_round": data["rounds"][-1]["n"],
        "rounds": rounds,
        "submit": {"kind": submit_kind},
        "settings": data.get("settings"),
        "generated_at": now_iso(),
    }
    if submit_kind == "sitedata":
        state["submit"].update({"collection": "feedback", "max_payload": 15000})
    return state


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
