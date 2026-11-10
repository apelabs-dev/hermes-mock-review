"""Brand enforcement: tie a saved design profile to a project and refuse a round whose mocks stray from it.

Two halves, both free of side effects apart from one short `git` call:

* project -> profile: `project_profile(path)` finds the saved profile bound to the directory an agent works in, through
  its git toplevel and (inside a linked worktree) the main checkout.
* the off-brand check: `violations(decisions, profile)` scans the HTML and CSS of a validated decisions dict for colours
  and fonts the profile does not allow, and `enforce(...)` turns them into one ReviewError.

Only CSS contexts are read: inline `style` attributes, `<style>` text, `extra_css`, and the colour presentation attributes
of SVG/HTML. Selectors, quoted strings and `url(...)` are never read, so `#add {}` or `url(#grad)` is not a colour.
"""

from __future__ import annotations

import os
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from . import store
from .store import ReviewError

MAX_OFF_BRAND = 200
MAX_LINES = 20
GIT_TIMEOUT = 5

BOARD_TOKENS = ("--bg", "--surface", "--surface-2", "--line", "--line-strong", "--text", "--muted", "--faint",
                "--accent", "--accent-ink", "--accent-soft", "--ok", "--warn", "--err", "--font")

# ------------------------------------------------------------------ project -> profile


def norm(path: str) -> str:
    """Compare-form of a path: real, normalised, case-folded on Windows."""
    return os.path.normcase(os.path.realpath(path))


def inside(path: str, root: str) -> bool:
    """`path` equals `root` or lies below it (both already norm()ed)."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def git_roots(path: str) -> List[str]:
    """The git toplevel of `path`, and the main checkout when `path` is in a linked worktree. [] when git is missing,
    `path` is not in a repo, or git fails for any reason: never raises."""
    try:
        p = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel", "--git-common-dir"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT)
    except Exception:
        return []
    if p.returncode != 0:
        return []
    lines = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    out: List[str] = []
    if lines:
        out.append(lines[0])
    if len(lines) > 1:
        common = lines[1] if os.path.isabs(lines[1]) else os.path.join(path, lines[1])
        common = os.path.normpath(common)
        if os.path.basename(common) == ".git":
            out.append(os.path.dirname(common))
    return out


def candidates(project: str, git: Callable[[str], Sequence[str]] = git_roots) -> List[str]:
    """The project's real path, its git toplevel and the main checkout of its worktree, normalised, in that order."""
    real = os.path.realpath(project)
    seen: List[str] = []
    for p in [real, *git(real)]:
        n = norm(p)
        if n not in seen:
            seen.append(n)
    return seen


def match_binding(cands: Sequence[str], bindings: Dict[str, Sequence[str]]) -> Optional[str]:
    """The profile whose bound path equals or contains a candidate; the longest bound path wins."""
    best: Optional[Tuple[int, str]] = None
    for name in sorted(bindings):
        for bound in bindings[name]:
            b = norm(bound)
            if any(inside(c, b) for c in cands):
                key = (len(b), name)
                if best is None or key[0] > best[0]:
                    best = key
    return best[1] if best else None


def project_profile(project: str, git: Callable[[str], Sequence[str]] = git_roots) -> Optional[str]:
    """The saved profile bound to `project` (or its repo / main checkout), or None."""
    bindings = store.project_bindings()
    if not bindings:
        return None
    return match_binding(candidates(project, git), bindings)


def clean_project(project: Any) -> Optional[str]:
    if project is None or (isinstance(project, str) and not project.strip()):
        return None
    if not isinstance(project, str) or not os.path.isabs(project.strip()):
        raise ReviewError(f"project must be an absolute path to the project directory, got {project!r}")
    return project.strip()


def clean_off_brand(reason: Any) -> Optional[str]:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise ReviewError("off_brand must be a short reason (text)")
    reason = re.sub(r"[\x00-\x1f\x7f]+", " ", reason).strip()
    if not reason:
        return None
    if len(reason) > MAX_OFF_BRAND:
        raise ReviewError(f"off_brand reason is {len(reason)} characters; keep it to {MAX_OFF_BRAND} or fewer")
    return reason


def refusal(project: Optional[str], names: Sequence[str]) -> str:
    where = (f"The project checked was {os.path.realpath(project)}, and no saved profile is bound to it or to its git "
             "repository." if project else
             "No project was given, so no bound profile could apply (pass project=<abs repo path>; the CLI uses the "
             "current directory).")
    return (f"This board has no design profile, and saved profiles exist ({', '.join(names)}). {where} "
            "Do one of:\n"
            "  - pass design=\"<name>\" (CLI: --design <name>) to use a saved profile;\n"
            "  - bind the project so it applies on its own: hermes mockreview design bind <name> <abs project path>;\n"
            "  - pass no_design=true (CLI: --no-design) for a board that deliberately has no profile.")


# ------------------------------------------------------------------ colours

_NAMED = """aliceblue f0f8ff antiquewhite faebd7 aqua 00ffff aquamarine 7fffd4 azure f0ffff beige f5f5dc bisque ffe4c4
black 000000 blanchedalmond ffebcd blue 0000ff blueviolet 8a2be2 brown a52a2a burlywood deb887 cadetblue 5f9ea0
chartreuse 7fff00 chocolate d2691e coral ff7f50 cornflowerblue 6495ed cornsilk fff8dc crimson dc143c cyan 00ffff
darkblue 00008b darkcyan 008b8b darkgoldenrod b8860b darkgray a9a9a9 darkgreen 006400 darkgrey a9a9a9
darkkhaki bdb76b darkmagenta 8b008b darkolivegreen 556b2f darkorange ff8c00 darkorchid 9932cc darkred 8b0000
darksalmon e9967a darkseagreen 8fbc8f darkslateblue 483d8b darkslategray 2f4f4f darkslategrey 2f4f4f
darkturquoise 00ced1 darkviolet 9400d3 deeppink ff1493 deepskyblue 00bfff dimgray 696969 dimgrey 696969
dodgerblue 1e90ff firebrick b22222 floralwhite fffaf0 forestgreen 228b22 fuchsia ff00ff gainsboro dcdcdc
ghostwhite f8f8ff gold ffd700 goldenrod daa520 gray 808080 green 008000 greenyellow adff2f grey 808080
honeydew f0fff0 hotpink ff69b4 indianred cd5c5c indigo 4b0082 ivory fffff0 khaki f0e68c lavender e6e6fa
lavenderblush fff0f5 lawngreen 7cfc00 lemonchiffon fffacd lightblue add8e6 lightcoral f08080 lightcyan e0ffff
lightgoldenrodyellow fafad2 lightgray d3d3d3 lightgreen 90ee90 lightgrey d3d3d3 lightpink ffb6c1
lightsalmon ffa07a lightseagreen 20b2aa lightskyblue 87cefa lightslategray 778899 lightslategrey 778899
lightsteelblue b0c4de lightyellow ffffe0 lime 00ff00 limegreen 32cd32 linen faf0e6 magenta ff00ff maroon 800000
mediumaquamarine 66cdaa mediumblue 0000cd mediumorchid ba55d3 mediumpurple 9370db mediumseagreen 3cb371
mediumslateblue 7b68ee mediumspringgreen 00fa9a mediumturquoise 48d1cc mediumvioletred c71585
midnightblue 191970 mintcream f5fffa mistyrose ffe4e1 moccasin ffe4b5 navajowhite ffdead navy 000080
oldlace fdf5e6 olive 808000 olivedrab 6b8e23 orange ffa500 orangered ff4500 orchid da70d6 palegoldenrod eee8aa
palegreen 98fb98 paleturquoise afeeee palevioletred db7093 papayawhip ffefd5 peachpuff ffdab9 peru cd853f
pink ffc0cb plum dda0dd powderblue b0e0e6 purple 800080 rebeccapurple 663399 red ff0000 rosybrown bc8f8f
royalblue 4169e1 saddlebrown 8b4513 salmon fa8072 sandybrown f4a460 seagreen 2e8b57 seashell fff5ee sienna a0522d
silver c0c0c0 skyblue 87ceeb slateblue 6a5acd slategray 708090 slategrey 708090 snow fffafa springgreen 00ff7f
steelblue 4682b4 tan d2b48c teal 008080 thistle d8bfd8 tomato ff6347 turquoise 40e0d0 violet ee82ee wheat f5deb3
white ffffff whitesmoke f5f5f5 yellow ffff00 yellowgreen 9acd32"""

RGB = Tuple[int, int, int]


def _hex_rgb(h: str) -> RGB:
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


_words = _NAMED.split()
NAMED_COLORS: Dict[str, RGB] = {_words[i]: _hex_rgb(_words[i + 1]) for i in range(0, len(_words), 2)}

_HEX_RE = re.compile(r"(?<![\w#-])#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![\w-])")
_FUNC_RE = re.compile(r"(?<![\w-])(rgba?|hsla?)\(", re.I)
_WORD_RE = re.compile(r"(?<![\w#.-])([A-Za-z]+)(?![\w(-])")
_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_NUM_RE = re.compile(rf"^({_NUM})(%?)$")
_HUE_RE = re.compile(rf"^({_NUM})(deg|grad|rad|turn)?$", re.I)
_URL_RE = re.compile(r"url\(\s*(?:\"[^\"]*\"|'[^']*'|[^)]*)\)", re.I)
_STR_RE = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")
_COLOR_PROP = re.compile(r"^(?:-webkit-)?(?:color|[a-z-]*-color|background(?:-.*)?|border(?:-.*)?|outline(?:-.*)?|fill|"
                         r"stroke|box-shadow|text-shadow|text-decoration(?:-.*)?|text-fill-color|text-stroke|"
                         r"column-rule(?:-.*)?)$")


def blank(value: str) -> str:
    """The value with url(...) contents and quoted strings emptied, so neither is read as a colour."""
    return _STR_RE.sub('""', _URL_RE.sub("url()", value))


def _balanced(text: str, start: int) -> Optional[Tuple[str, int]]:
    depth, i = 1, start
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return None


def _clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _rgb_args(inner: str) -> Optional[RGB]:
    toks = [t for t in re.split(r"[\s,/]+", inner.strip()) if t]
    if len(toks) not in (3, 4):
        return None
    vals = []
    for t in toks[:3]:
        m = _NUM_RE.match(t)
        if not m:
            return None
        v = float(m.group(1))
        vals.append(_clamp(v * 255 / 100 if m.group(2) else v))
    return vals[0], vals[1], vals[2]


def _hsl_to_rgb(h: float, s: float, l: float) -> RGB:
    h = (h % 360) / 360
    s, l = max(0.0, min(1.0, s)), max(0.0, min(1.0, l))
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q

    def chan(t: float) -> float:
        t %= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    return _clamp(chan(h + 1 / 3) * 255), _clamp(chan(h) * 255), _clamp(chan(h - 1 / 3) * 255)


def _hsl_args(inner: str) -> Optional[RGB]:
    toks = [t for t in re.split(r"[\s,/]+", inner.strip()) if t]
    if len(toks) not in (3, 4):
        return None
    m = _HUE_RE.match(toks[0])
    if not m:
        return None
    hue = float(m.group(1))
    unit = (m.group(2) or "deg").lower()
    hue = {"deg": hue, "grad": hue * 0.9, "rad": hue * 57.29577951308232, "turn": hue * 360}[unit]
    sl = []
    for t in toks[1:3]:
        n = _NUM_RE.match(t)
        if not n:
            return None
        sl.append(float(n.group(1)) / 100)
    return _hsl_to_rgb(hue, sl[0], sl[1])


# (literal as written, rgb, is hsl)
Literal = Tuple[str, RGB, bool]


def colors(value: str, named: bool) -> List[Literal]:
    """Every colour literal in a declaration value (hex, rgb(a), hsl(a); CSS names when `named`), in order. A function
    with a var() or calc() argument has no fixed colour and is skipped; a literal inside a var() fallback is read."""
    v = blank(value)
    found: List[Tuple[int, Literal]] = []
    for m in _HEX_RE.finditer(v):
        h = m.group(1)
        if len(h) <= 4:
            h = "".join(c * 2 for c in h[:3])
        found.append((m.start(), (m.group(0), _hex_rgb(h[:6]), False)))
    for m in _FUNC_RE.finditer(v):
        got = _balanced(v, m.end())
        if not got or "(" in got[0]:
            continue
        inner, end = got
        hsl = m.group(1).lower().startswith("hsl")
        rgb = _hsl_args(inner) if hsl else _rgb_args(inner)
        if rgb:
            found.append((m.start(), (v[m.start():end], rgb, hsl)))
    if named:
        for m in _WORD_RE.finditer(v):
            rgb_n = NAMED_COLORS.get(m.group(1).lower())
            if rgb_n:
                found.append((m.start(), (m.group(1), rgb_n, False)))
    return [lit for _, lit in sorted(found, key=lambda x: x[0])]


def colour_prop(prop: str) -> bool:
    return not prop.startswith("--") and bool(_COLOR_PROP.match(prop))


# ------------------------------------------------------------------ fonts

GENERIC_FAMILIES = {"sans-serif", "serif", "monospace", "system-ui", "ui-sans-serif", "ui-serif", "ui-monospace",
                    "ui-rounded", "cursive", "fantasy", "emoji", "math", "fangsong"}
CSS_WIDE = {"inherit", "initial", "unset", "revert", "revert-layer"}
SYSTEM_FONTS = {"caption", "icon", "menu", "message-box", "small-caption", "status-bar"}
_SIZE_RE = re.compile(r"^(?:[+-]?(?:\d+\.?\d*|\.\d+)(?:%|[a-zA-Z]+)|xx-small|x-small|small|medium|large|x-large|"
                      r"xx-large|xxx-large|larger|smaller)(?:/.*)?$", re.I)


def _top_split(text: str, sep: Callable[[str], bool]) -> List[str]:
    """Split on separator characters outside quotes and parentheses."""
    parts: List[str] = []
    buf: List[str] = []
    depth, quote, i = 0, "", 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                i += 1
                buf.append(text[i])
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and sep(ch):
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _no_important(value: str) -> str:
    return re.sub(r"\s*!\s*important\s*$", "", value.strip(), flags=re.I)


def families(value: str) -> List[str]:
    """The family names of a font-family value: quotes stripped, unquoted words collapsed."""
    out = []
    for part in _top_split(_no_important(value), lambda c: c == ","):
        p = part.strip()
        if not p:
            continue
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1]
        out.append(re.sub(r"\s+", " ", p).strip())
    return [f for f in out if f]


def shorthand_families(value: str) -> List[str]:
    """The family part of a `font` shorthand (after the size and optional /line-height); [] for system fonts,
    CSS-wide keywords and anything this cannot split."""
    v = _no_important(value)
    toks = [t for t in _top_split(v, lambda c: c.isspace()) if t]
    if not toks or (len(toks) == 1 and toks[0].lower() in SYSTEM_FONTS | CSS_WIDE):
        return []
    for i, t in enumerate(toks):
        if _SIZE_RE.match(t):
            j = i + 1
            if t.endswith("/"):
                j += 1
            elif j < len(toks) and toks[j].startswith("/"):
                j += 2 if toks[j] == "/" else 1
            return families(" ".join(toks[j:]))
    return []


def family_ok(fam: str, allowed: Set[str]) -> bool:
    f = fam.lower()
    return f in GENERIC_FAMILIES or f in CSS_WIDE or f.startswith("var(") or f in allowed


# ------------------------------------------------------------------ CSS and HTML walking

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_PROP_RE = re.compile(r"^-{0,2}[a-z_][\w-]*$")


def declarations(css: str) -> Iterator[Tuple[str, str]]:
    """(property, value) of every declaration in CSS text or an inline style: comments gone, selectors and at-rule
    preludes skipped, nested blocks (@media, @supports) walked."""
    text = _COMMENT_RE.sub(" ", css)
    buf: List[str] = []
    depth, quote, i = 0, "", 0
    pending: List[Tuple[str, str]] = []

    def flush() -> None:
        chunk = "".join(buf).strip()
        buf.clear()
        if ":" not in chunk:
            return
        prop, _, val = chunk.partition(":")
        prop = prop.strip().lower()
        if _PROP_RE.match(prop):
            pending.append((prop, _no_important(val)))

    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                i += 1
                buf.append(text[i])
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and ch == "{":
            buf.clear()                     # a selector or an at-rule prelude, not a declaration
        elif depth == 0 and ch in ";}":
            flush()
        else:
            buf.append(ch)
        i += 1
    flush()
    return iter(pending)


class Allowed:
    """What a profile permits: normalised RGB values, and lower-cased family names."""

    def __init__(self, rgbs: Set[RGB], fams: Set[str]):
        self.rgbs = set(rgbs) | {(255, 255, 255), (0, 0, 0)}
        self.fams = fams

    def color_ok(self, rgb: RGB, hsl: bool) -> bool:
        if rgb in self.rgbs:
            return True
        return hsl and any(all(abs(a - b) <= 1 for a, b in zip(rgb, ok)) for ok in self.rgbs)


Found = Tuple[str, str]   # (path, what)


def check_declaration(prop: str, value: str, allow: Allowed) -> List[str]:
    out: List[str] = []
    for lit, rgb, hsl in colors(value, colour_prop(prop)):
        if not allow.color_ok(rgb, hsl):
            out.append(f"color {lit}")
    fams: List[str] = []
    if prop == "font-family":
        fams = families(value)
    elif prop == "font":
        fams = shorthand_families(value)
    for f in fams:
        if not family_ok(f, allow.fams):
            out.append(f'font-family "{f}"')
    return out


def scan_css(css: str, allow: Allowed) -> List[str]:
    out: List[str] = []
    for prop, value in declarations(css):
        out.extend(check_declaration(prop, value, allow))
    return out


_COLOR_ATTRS = {"fill": "fill", "stroke": "stroke", "stop-color": "stop-color", "flood-color": "flood-color",
                "lighting-color": "lighting-color", "color": "color", "bgcolor": "background-color"}


class _HtmlScan(HTMLParser):
    def __init__(self, allow: Allowed):
        super().__init__(convert_charrefs=True)
        self.allow = allow
        self.found: List[str] = []
        self._style: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value is None:
                continue
            n = name.lower()
            if n == "style":
                self.found.extend(scan_css(value, self.allow))
            elif n in _COLOR_ATTRS:
                self.found.extend(check_declaration(_COLOR_ATTRS[n], value, self.allow))
        if tag.lower() == "style":
            self._style = []

    def handle_data(self, data):
        if self._style is not None:
            self._style.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "style":
            self._flush()

    def _flush(self) -> None:
        if self._style is not None:
            self.found.extend(scan_css("".join(self._style), self.allow))
            self._style = None

    def done(self) -> List[str]:
        self._flush()
        return self.found


def scan_html(html: str, allow: Allowed) -> List[str]:
    p = _HtmlScan(allow)
    try:
        p.feed(html)
        p.close()
    except Exception:       # a parser that gives up has still reported what it read before
        pass
    return p.done()


# ------------------------------------------------------------------ the profile and the check

def read_css(d: Path, name: Optional[str]) -> str:
    if not name:
        return ""
    try:
        return (d / name).read_text(encoding="utf-8", errors="replace")[:1024 * 1024]
    except OSError:
        return ""


def allowed_for(info: Dict[str, Any], mock_css: str = "", fonts_css: str = "") -> Allowed:
    rgbs: Set[RGB] = set()
    for tokens in (info.get("themes") or {}).values():
        for key in store.TOKEN_KEYS:
            if tokens.get(key):
                rgbs.update(rgb for _, rgb, _ in colors(tokens[key], True))
    fams: Set[str] = {f.lower() for f in families(info.get("font") or "")}
    for prop, value in list(declarations(mock_css)) + list(declarations(fonts_css)):
        rgbs.update(rgb for _, rgb, _ in colors(value, colour_prop(prop)))
        if prop == "font-family":
            fams.update(f.lower() for f in families(value))
        elif prop == "font":
            fams.update(f.lower() for f in shorthand_families(value))
    return Allowed(rgbs, fams)


def violations(decisions: Dict[str, Any], allow: Allowed) -> List[Found]:
    """(path, what) for every colour or font outside `allow`, in document order, each pair once."""
    raw: List[Found] = []
    for i, b in enumerate(decisions.get("blocks") or []):
        if not isinstance(b, dict):
            continue
        if isinstance(b.get("html"), str):
            raw.extend((f"blocks[{i}].html", w) for w in scan_html(b["html"], allow))
        for j, o in enumerate(b.get("options") or []):
            if isinstance(o, dict) and isinstance(o.get("html"), str):
                oid = o.get("id") or "abcdef"[j % 6]
                raw.extend((f"blocks[{i}].options[{oid}].html", w) for w in scan_html(o["html"], allow))
    if isinstance(decisions.get("extra_css"), str):
        raw.extend(("extra_css", w) for w in scan_css(decisions["extra_css"], allow))
    seen: Set[Found] = set()
    out: List[Found] = []
    for f in raw:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def hint() -> str:
    return ("Use the profile's tokens (var(--accent), var(--text), var(--surface), var(--font), ...) instead of raw "
            "values (the board sets " + " ".join(BOARD_TOKENS) + " from the profile), or pass off_brand=\"<reason>\" / "
            "--off-brand \"<reason>\" if this round is off-brand on purpose.")


def message(found: Sequence[Found], profile: str) -> str:
    lines = [f"{path}: {what} is not in profile {profile}" for path, what in found[:MAX_LINES]]
    if len(found) > MAX_LINES:
        lines.append(f"... and {len(found) - MAX_LINES} more")
    return (f"Off-brand mock: {len(found)} value(s) outside the design profile {profile}. Nothing was written.\n"
            + "\n".join(lines) + "\n" + hint())


def enforce(decisions: Dict[str, Any], design_dir: Path) -> None:
    """Raise ReviewError listing every colour and font in the round that the profile in `design_dir` does not allow."""
    info = store.validate_design(design_dir)
    allow = allowed_for(info, read_css(design_dir, info.get("mock_css")), read_css(design_dir, info.get("fonts_css")))
    found = violations(decisions, allow)
    if found:
        raise ReviewError(message(found, info.get("name") or design_dir.name))
