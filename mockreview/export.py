"""One static, script-free export document per (review, round, comments on/off).

Everything else is derived from this document: the HTML download is the document itself, the PDF is the same
document served by a thin print page, the Markdown is the document converted in the browser with vendored
Turndown, and the PNG is the document rendered by vendored SnapDOM with comments off. That keeps the four
formats from drifting and makes "PNG never contains comment UI" structural rather than a CSS trick.

Nothing here imports http.server. The only data it trusts is store.load() plus the review's own files, resolved
through store.safe_variant_file(); a path in JSON is never read. Mock, prototype and artifact HTML is rebuilt
through an allowlist parser (HTMLParser), so the output holds no script, no event handler, no external URL and
only data: images. Comments are plain text.
"""

from __future__ import annotations

import base64
import hashlib
import html
import importlib
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import store

EXPORT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; "
              "form-action 'none'; base-uri 'none'; frame-src 'none'")
EXPORT_SCRIPT_CSP = EXPORT_CSP + "; script-src 'self'"
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_INLINE_BYTES = 20 * 1024 * 1024
IMAGE_MIMES = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}

ALLOWED_TAGS = frozenset("""
p div span a b i em strong small code pre ul ol li table thead tbody tfoot tr td th caption colgroup col h1 h2 h3
h4 h5 h6 img figure figcaption section article header footer dl dt dd blockquote hr br details summary
nav aside main button label select option input textarea progress meter time mark sup sub s u abbr kbd
""".split())
VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())
# These are dropped together with everything inside them: their "text" is code/markup, not prose. `svg` is not here:
# it goes through the inline SVG subset (_serialize_svg).
DROP_SUBTREE = frozenset("""
script style iframe frame frameset object embed applet math template noscript canvas video audio source
link meta base title head
""".split())
ALLOWED_ATTRS = frozenset("""
class id style alt title width height colspan rowspan span role type value placeholder checked disabled selected for
name hidden open min max low high optimum readonly multiple rows cols datetime
""".split())
# data-* and aria-* keep their (escaped) value so a design's attribute selectors still match.
_DATA_ARIA_ATTR = re.compile(r"(?:data-[a-z0-9_.-]{1,60}|aria-[a-z]{1,30})")

# The inline SVG subset: static shapes, text and paint servers. No foreignObject, script, style, image, animation,
# filter or link element; those go with everything inside them. The HTML parser lowercases names, so each maps back
# to its SVG spelling.
SVG_TAGS = {t.lower(): t for t in """
svg g path circle rect line polyline polygon ellipse defs linearGradient radialGradient stop clipPath mask symbol use
title text tspan
""".split()}
SVG_LEAF_TAGS = frozenset("path circle rect line polyline polygon ellipse stop use".split())
SVG_UNWRAP = frozenset(("a", "switch"))
SVG_ATTRS = {a.lower(): a for a in """
id class style role width height x y x1 y1 x2 y2 cx cy r rx ry d points pathLength viewBox preserveAspectRatio
transform fill fill-opacity fill-rule stroke stroke-width stroke-linecap stroke-linejoin stroke-dasharray
stroke-dashoffset stroke-opacity stroke-miterlimit opacity color display visibility overflow clip-path clip-rule mask
vector-effect shape-rendering paint-order gradientUnits gradientTransform spreadMethod fx fy fr offset stop-color
stop-opacity clipPathUnits maskUnits maskContentUnits refX refY dx dy rotate text-anchor dominant-baseline
alignment-baseline baseline-shift font-family font-size font-weight font-style letter-spacing word-spacing
textLength lengthAdjust focusable
""".split()}
_SVG_FRAGMENT = re.compile(r"#[A-Za-z_][A-Za-z0-9_.:-]{0,100}")
_SVG_URL_FRAGMENT = re.compile(r"""url\(\s*(["']?)#[A-Za-z_][A-Za-z0-9_.:-]{0,100}\1\s*\)""", re.I)
_URL_SCHEME = re.compile(r"(?i)\b(https?|mailto|javascript|blob|cid|ftp|file):")
_DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=\s]+)$", re.I)
MAX_BASE64 = (MAX_IMAGE_BYTES * 4) // 3 + 4


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: Optional[str], attrs: Optional[List[Tuple[str, str]]]):
        self.tag = tag
        self.attrs = attrs or []
        self.children: List[Any] = []

    def text(self) -> str:
        out = []
        for c in self.children:
            out.append(c if isinstance(c, str) else c.text())
        return "".join(out)

    def find_all(self, tag: str) -> List["_Node"]:
        out = []
        for c in self.children:
            if isinstance(c, _Node):
                if c.tag == tag:
                    out.append(c)
                out.extend(c.find_all(tag))
        return out


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node(None, [])
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        node = _Node(tag.lower(), [(str(k).lower(), str(v or "")) for k, v in attrs])
        self.stack[-1].children.append(node)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.stack[-1].children.append(
            _Node(tag.lower(), [(str(k).lower(), str(v or "")) for k, v in attrs]))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _tree(markup: str) -> _Node:
    parser = _TreeParser()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:
        return parser.root if hasattr(parser, "root") else _Node(None, [])
    return parser.root


def _body_of(root: _Node) -> Optional[_Node]:
    for n in root.find_all("body"):
        return n
    return None


def _text_of(node: _Node) -> str:
    return node.text()


def _escape_text(value: Any) -> str:
    """Escaped text with URL schemes and double slashes entity-encoded: the raw document then contains no
    http://, https://, mailto:, javascript: or protocol-relative //, while the rendered text is unchanged."""
    out = html.escape(str(value if value is not None else ""), quote=False)
    out = _URL_SCHEME.sub(lambda m: m.group(1) + "&#58;", out)
    return out.replace("//", "&#47;&#47;")


def _escape_attr(value: Any) -> str:
    out = html.escape(str(value if value is not None else ""), quote=True)
    out = _URL_SCHEME.sub(lambda m: m.group(1) + "&#58;", out)
    return out.replace("//", "&#47;&#47;")


_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.S)
_CSS_HEX_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\r\n\f]?")
_CSS_CHAR_ESCAPE = re.compile(r"\\([^0-9a-fA-F\r\n])")
_CSS_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))\s*\)""", re.I)
_CSS_DATA_IMAGE = re.compile(r"data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+", re.I)
_CSS_DATA_FONT = re.compile(r"data:font/(woff2|woff);base64,[A-Za-z0-9+/=]+", re.I)
_CSS_PROPERTY = re.compile(r"--[a-z0-9_-]{1,80}|-?[a-z][a-z0-9-]*", re.I)   # custom properties too
_CSS_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.I)
_CSS_DANGER = ("expression(", "javascript:", "vbscript:", "-moz-binding", "behavior", "image-set(", "src(")


def _css_hex_char(m: re.Match) -> str:
    code = int(m.group(1), 16)
    if code == 0 or 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
        return "\ufffd"
    return chr(code)


def _css_normalize(text: str) -> str:
    """CSS text with comments stripped and escapes decoded to a fixpoint, so a browser and this parser agree."""
    css = _CSS_COMMENT.sub("", str(text or ""))
    for _ in range(4):
        decoded = _CSS_HEX_ESCAPE.sub(_css_hex_char, css)
        decoded = _CSS_CHAR_ESCAPE.sub(lambda m: m.group(1), decoded)
        if decoded == css:
            break
        css = decoded
    return css


def _css_url_payload(m: re.Match) -> str:
    for group in (m.group(1), m.group(2), m.group(3)):
        if group is not None:
            return group.strip()
    return ""


def _css_safe_url(payload: str) -> bool:
    return (len(payload) > 1 and payload.startswith("#")) or bool(_CSS_DATA_IMAGE.fullmatch(payload))


def _css_replace_urls(text: str) -> str:
    def repl(m: re.Match) -> str:
        payload = _css_url_payload(m)
        if len(payload) > 1 and payload.startswith("#"):
            return m.group(0)
        if _CSS_DATA_IMAGE.fullmatch(payload) or _CSS_DATA_FONT.fullmatch(payload):
            # A data: font survives only into an @font-face src (_css_font_src); every other value refuses it.
            return 'url("' + payload + '")'
        return 'url("")'
    return _CSS_URL.sub(repl, text)


def _css_strip_danger(text: str) -> str:
    text = re.sub(r"expression\s*\(", "", text, flags=re.I)
    text = re.sub(r"(?:javascript|vbscript)\s*:", "", text, flags=re.I)
    for token in ("-moz-binding", "behavior", "image-set(", "src("):
        text = re.sub(re.escape(token), "", text, flags=re.I)
    return text


def _css_balanced(text: str) -> bool:
    quote = ""
    depth = 0
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
            elif ch in "\r\n\f":
                return False      # a browser ends a string at a newline (a bad string): never emit one
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return not quote and depth == 0


def _css_split(text: str, sep: str, maxsplit: int = -1) -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    quote = ""
    depth = 0
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == sep and depth == 0 and (maxsplit < 0 or len(parts) < maxsplit):
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _css_value(value: str) -> Optional[str]:
    if "\\" in value or "<" in value or ">" in value:
        return None
    value = _css_strip_danger(_css_replace_urls(value))
    if "\\" in value or "<" in value or ">" in value or "{" in value or "}" in value or not _css_balanced(value):
        return None
    remaining = _CSS_URL.sub(lambda m: "" if _css_safe_url(_css_url_payload(m)) else "\0", value)
    if "\0" in remaining or "url(" in remaining.lower():
        return None
    low = remaining.lower()
    if "@" in remaining or "//" in remaining or _CSS_SCHEME.search(remaining):
        return None
    if any(token in low for token in _CSS_DANGER):
        return None
    return value


def _css_declarations(text: str, font_face: bool = False) -> Optional[str]:
    out = []
    for part in _css_split(text, ";"):
        if not part.strip():
            continue
        pair = _css_split(part, ":", 1)
        if len(pair) != 2:
            continue
        prop = pair[0].strip()
        if not _CSS_PROPERTY.fullmatch(prop):
            continue
        value = _css_font_src(pair[1]) if font_face and prop.lower() == "src" else _css_value(pair[1])
        if value is None:
            continue
        out.append(prop + ":" + value.strip())
    return ";".join(out)


_CSS_FONT_SOURCE = re.compile(
    r"""\s*url\(\s*(["']?)(data:font/(?:woff2|woff);base64,[A-Za-z0-9+/=]+)\1\s*\)"""
    r"""(?:\s+format\(\s*(["']?)(woff2|woff)\3\s*\))?\s*""", re.I)


def _css_font_src(value: str) -> Optional[str]:
    """An @font-face src: data:font/woff2 and data:font/woff URLs only; local() and every other source is dropped."""
    keep = []
    for item in _css_split(value, ","):
        m = _CSS_FONT_SOURCE.fullmatch(item)
        if m:
            keep.append('url("' + m.group(2) + '")' + (' format("' + m.group(4).lower() + '")' if m.group(4) else ""))
    return ", ".join(keep) or None


def _css_text_ok(text: str) -> bool:
    """Prelude text: no escape, `<`, at-sign, brace, semicolon, comment, `//`, url() or dangerous token; balanced."""
    if any(c in text for c in "\\<@{};") or "//" in text or "/*" in text:
        return False
    low = text.lower()
    if "url(" in low or any(token in low for token in _CSS_DANGER):
        return False
    return _css_balanced(text)


_CSS_CONDITION = re.compile(r"[A-Za-z0-9\s().:,%=>-]*")
_CSS_KEYFRAME_SELECTOR = re.compile(r"\s*(?:from|to|\d{1,3}(?:\.\d+)?%)\s*(?:,\s*(?:from|to|\d{1,3}(?:\.\d+)?%)\s*)*",
                                    re.I)
_CSS_KEYFRAMES_NAME = re.compile(r"""\s*(?:-?[A-Za-z_][A-Za-z0-9_-]*|"[A-Za-z0-9 _-]{1,60}")\s*""")
_CSS_GROUP_RULES = ("media", "container", "supports")


def _css_at_rule(prelude: str) -> Tuple[Optional[str], str]:
    """(kind, rebuilt prelude) for an at-rule this sanitizer keeps, (None, "") for one whose rule is dropped:
    @property (its syntax descriptor carries `<`), @import, @namespace, @page, @layer and anything unknown."""
    m = re.match(r"@(-?[A-Za-z][A-Za-z-]*)(.*)$", prelude, re.S)
    if not m:
        return None, ""
    name, rest = m.group(1).lower(), m.group(2)
    if rest and not rest[0].isspace() and rest[0] not in "(\"":
        return None, ""
    if name in ("media", "container"):
        if rest.strip() and _CSS_CONDITION.fullmatch(rest) and _css_text_ok(rest):
            return name, "@" + name + " " + rest.strip()
        return None, ""
    if name == "supports":
        if rest.strip() and _css_text_ok(rest):
            return name, "@supports " + rest.strip()
        return None, ""
    if name in ("keyframes", "-webkit-keyframes"):
        if _CSS_KEYFRAMES_NAME.fullmatch(rest):
            return "keyframes", "@keyframes " + rest.strip()
        return None, ""
    if name == "font-face" and not rest.strip():
        return "font-face", "@font-face"
    return None, ""


def _css_scope(selector: str, scope: Optional[str]) -> Optional[str]:
    """A selector list with every complex selector placed under `scope` (no scope: unchanged)."""
    parts = [p.strip() for p in _css_split(selector, ",")]
    if any(not p for p in parts):
        return None
    if scope is None:
        return ",".join(parts)
    # A selector that opens with a combinator would hang off the scope itself (`.mr-d1 ~ .x` reaches the export
    # chrome next to the mock); it is invalid at a sheet's top level anyway, so the rule goes.
    if any(p[0] in ">+~" for p in parts):
        return None
    return ",".join(scope + " " + p for p in parts)


def _css_block_end(css: str, start: int) -> Optional[int]:
    depth = 1
    i = start
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _css_rules(css: str, scope: Optional[str] = None, ctx: str = "top") -> str:
    """Rebuild a sheet rule by rule from allowlisted parts. A rule that fails a check is dropped on its own; the rest
    of the sheet stays. Every emitted prelude and value is brace-free and quote-balanced, so a browser splits the
    output into exactly these rules. ctx: "top"/"group" (style rules go under `scope`), "style" (nested in a style
    rule that already is), "keyframes" (keyframe selectors only)."""
    out = []
    i = 0
    while i < len(css):
        open_at = css.find("{", i)
        if open_at < 0:
            break      # trailing text without a block is dropped
        close_at = _css_block_end(css, open_at + 1)
        if close_at is None:
            break      # an unterminated block is dropped with everything after it
        # Statements before the block (`@charset "x";`, `@import ...;`, `@layer a, b;`) are dropped.
        prelude = _css_split(css[i:open_at], ";")[-1].strip()
        inner = css[open_at + 1:close_at]
        i = close_at + 1
        if "<" in prelude or "<" in inner:
            continue
        if ctx == "keyframes":
            if _CSS_KEYFRAME_SELECTOR.fullmatch(prelude) and "{" not in inner:
                out.append(prelude + "{" + (_css_declarations(inner) or "") + "}")
            continue
        if prelude.startswith("@"):
            kind, head = _css_at_rule(prelude)
            if kind in _CSS_GROUP_RULES:
                out.append(head + "{" + _css_rules(inner, scope, "style" if ctx == "style" else "group") + "}")
            elif kind == "keyframes" and ctx != "style":
                out.append(head + "{" + _css_rules(inner, None, "keyframes") + "}")
            elif kind == "font-face" and ctx != "style" and "{" not in inner:
                decls = _css_declarations(inner, font_face=True) or ""
                if re.search(r"(?i)(?:^|;)src:", decls):
                    out.append(head + "{" + decls + "}")
            continue
        if not prelude or not _css_text_ok(prelude):
            continue
        if ctx == "style" and scope and any("&" in p and len(_css_split(p, "&")) == 1
                                            for p in _css_split(prelude, ",")):
            continue      # `:not(&)`, `:is(&, x)`: an `&` only inside parentheses can match outside the scope
        selector = _css_scope(prelude, None if ctx == "style" else scope)
        if selector is None:
            continue
        if "{" in inner:
            out.append(selector + "{" + _css_rules(inner, scope, "style") + "}")
        else:
            out.append(selector + "{" + (_css_declarations(inner) or "") + "}")
    return "".join(out)


_CSS_KEPT_AT = re.compile(r"(?i)@(?:media|container|supports|keyframes|font-face)\b")


def _sanitize_css(text: str, *, sheet: bool, scope: Optional[str] = None) -> Optional[str]:
    """A stylesheet (sheet=True) or a declaration list, rebuilt from allowlisted parts. In a sheet a bad rule drops
    that rule only, and `scope` (a selector) is placed in front of every style rule."""
    css = _css_normalize(text)
    if "\\" in css or (not sheet and ("<" in css or ">" in css)):
        return None
    css = _css_strip_danger(_css_replace_urls(css))
    css = re.sub(r"@import\b[^;{}]*(?:\{[^{}]*\}|;|$)", "", css, flags=re.I | re.S)
    out = _css_rules(css, scope) if sheet else _css_declarations(css)
    if out is None:
        return None
    if sheet:
        # The last line of defence: whatever the rule walk kept must still pass the whole-sheet checks.
        masked = _CSS_URL.sub(lambda m: "" if _css_safe_url(_css_url_payload(m))
                              or _CSS_DATA_FONT.fullmatch(_css_url_payload(m)) else "\0", out)
        if "\0" in masked or "url(" in masked.lower() or "//" in masked:
            return None
        masked = _CSS_KEPT_AT.sub("", masked)
        if "\\" in masked or "<" in masked or "@" in masked:
            return None
    return out


def _escape_css(markup: str) -> str:
    """CSS emitted inside our own <style>: browser-equivalent escapes, allowlisted values, or no style at all."""
    return _sanitize_css(str(markup or ""), sheet=True) or ""


@dataclass
class _Budget:
    remaining: int = MAX_INLINE_BYTES

    def take(self, size: int) -> bool:
        if size <= 0 or size > self.remaining:
            return False
        self.remaining -= size
        return True


MAX_DESIGN_CSS_BYTES = 2 * 1024 * 1024
MAX_FONT_BYTES = 2 * 1024 * 1024
FONT_MIMES = {"woff2": "font/woff2", "woff": "font/woff"}


def _to_container(css: str) -> str:
    """board.js toContainer(): a width-only @media becomes `@container mock`, so the app's CSS, written for a
    viewport, lays out against the mock's box (.cq) instead."""
    def repl(m: re.Match) -> str:
        cond = re.sub(r"(?i)^(only\s+)?screen\s+and\s+", "", m.group(1).strip(), count=1)
        if re.fullmatch(r"(?i)(\(\s*(min|max)-width\s*:\s*[\d.]+px\s*\)\s*(and\s*)?)+", cond):
            return "@container mock " + cond + " {"
        return m.group(0)
    return re.sub(r"@media\s+([^{;]+)\{", repl, css)


def _css_top_rules(css: str) -> List[str]:
    """Split sanitizer output (brace-free preludes and values) into its top-level rules."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(css):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append(css[start:i + 1])
                start = i + 1
    return out


class _Designs:
    """The design CSS of one export document. A profile's mock.css + the round's extra_css become one sheet scoped
    under its own class (.mr-d1, .mr-d2, ...), so it cannot restyle the export chrome or another design's mocks; an
    identical sheet or @font-face (same content) is emitted once."""

    def __init__(self, budget: _Budget, fonts: bool = True) -> None:
        self.budget = budget
        self.fonts = fonts
        self.scopes: Dict[str, str] = {}
        self.sheets: List[str] = []
        self.font_rules: Dict[str, None] = {}
        self.font_uris: Dict[str, Optional[str]] = {}

    def _text(self, rid: str, round_n: int, variant: str, rel: str) -> str:
        try:
            f = store.safe_variant_file(rid, str(round_n), variant, rel)
            if f.stat().st_size > MAX_DESIGN_CSS_BYTES:
                return ""
            return f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def _font_uri(self, rid: str, round_n: int, variant: str, payload: str) -> Optional[str]:
        path = _url_unquote(payload.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
        if not path or path.startswith("/") or re.match(r"(?i)^[a-z][a-z0-9+.-]*:", path):
            return None
        if any(p in ("", ".", "..") for p in path.split("/")):
            return None
        mime = FONT_MIMES.get(path.rsplit(".", 1)[-1].lower())
        if mime is None:
            return None
        try:
            f = store.safe_variant_file(rid, str(round_n), variant, "design/" + path)
        except Exception:
            return None
        key = str(f)
        if key not in self.font_uris:
            uri = None
            try:
                if self.fonts and f.stat().st_size <= MAX_FONT_BYTES:
                    data = f.read_bytes()
                    if self.budget.take(len(data)):
                        uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
            except OSError:
                uri = None
            self.font_uris[key] = uri
        return self.font_uris[key]

    def add(self, rid: str, round_n: int, variant: str, design: Dict[str, Any], extra_css: str) -> str:
        """The scope class for this design + extra_css, registering its sheet and fonts on first use."""
        names = {k: design.get(k) for k in ("mock_css", "fonts_css")}
        texts = {k: self._text(rid, round_n, variant, "design/" + v) if isinstance(v, str) and v else ""
                 for k, v in names.items()}
        source = texts["mock_css"] + "\n" + (extra_css if isinstance(extra_css, str) else "")
        key = hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()
        cls = self.scopes.get(key)
        if cls is None:
            cls = self.scopes[key] = "mr-d%d" % (len(self.scopes) + 1)
            self.sheets.append(_sanitize_css(_to_container(source), sheet=True, scope="." + cls) or "")
        if texts["fonts_css"]:
            def inline(m: re.Match) -> str:
                uri = self._font_uri(rid, round_n, variant, _css_url_payload(m))
                return 'url("' + uri + '")' if uri else 'url("")'
            fonts = _sanitize_css(_CSS_URL.sub(inline, _css_normalize(texts["fonts_css"])), sheet=True,
                                  scope="." + cls) or ""
            for rule in _css_top_rules(fonts):
                if rule.startswith("@font-face"):
                    self.font_rules.setdefault(rule, None)
                else:
                    self.sheets.append(rule)
        return cls

    def css(self) -> str:
        return "".join(self.font_rules) + "".join(self.sheets)


def _design_theme(data: Dict[str, Any], design: Dict[str, Any]) -> Optional[str]:
    """board.js designTheme() without the viewer's own toggle: the review's default_theme when the profile has it,
    else the profile's default."""
    themes = design.get("themes") if isinstance(design.get("themes"), dict) else {}
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    want = settings.get("default_theme") or "project"
    theme = want if want != "project" and want in themes else design.get("default_theme")
    return theme if theme in ("dark", "light") else None


@dataclass
class _Options:
    review_id: str = ""
    round_n: int = 1
    variant: str = "a"
    image: Optional[Callable[[str], Optional[str]]] = None
    budget: _Budget = field(default_factory=_Budget)
    designs: Optional[_Designs] = None
    # (scope class, data-theme, designed): set while a decisions round with a design profile or extra_css renders.
    design: Optional[Tuple[str, Optional[str], bool]] = None

    def mock(self, markup: Any) -> str:
        """A mock's box: the bare fragment, or the board's `.cq > .app[data-theme]` structure under its design."""
        inner = _safe_fragment(str(markup or ""), self)
        if self.design is None:
            return '<div class="mr-mock">' + inner + "</div>"
        cls, theme, designed = self.design
        return ('<div class="mr-mock ' + ("mr-designed " if designed else "") + cls + '"><div class="cq">'
                '<div class="app"' + (' data-theme="' + theme + '"' if theme else "") + ">" + inner
                + "</div></div></div>")

    def image_uri(self, src: str) -> Optional[str]:
        value = (src or "").strip()
        if not value:
            return None
        m = _DATA_IMAGE.match(value)
        if m:
            if len(m.group(2)) > MAX_BASE64 or not self.budget.take((len(m.group(2)) * 3) // 4):
                return None
            return value
        if value[:5].lower() == "data:":
            return None
        if value.startswith(("//", "/")) or re.match(r"(?i)^[a-z][a-z0-9+.-]*:", value):
            return None
        if self.image is None:
            return None
        return self.image(value)


def _image_reader(rid: str, round_n: int, variant: str, budget: _Budget) -> Callable[[str], Optional[str]]:
    """Resolve a relative image against this round's variant folder and inline it as a data: URI."""

    def read(src: str) -> Optional[str]:
        path = src.split("#", 1)[0]
        path = path.split("?", 1)[0].replace("\\", "/")
        path = _url_unquote(path)
        if path.startswith("/"):
            return None
        m = re.match(r"rounds/\d{1,3}/([a-d])/(.+)$", path)
        if m:
            if m.group(1) != variant:
                return None
            path = m.group(2)
        if not path or any(p in ("", ".", "..") for p in path.split("/")):
            return None
        try:
            f = store.safe_variant_file(rid, str(round_n), variant, path)
        except Exception:
            return None
        mime = IMAGE_MIMES.get(f.suffix.lower().lstrip("."))
        if mime is None:
            return None
        try:
            if f.stat().st_size > MAX_IMAGE_BYTES:
                return None
            data = f.read_bytes()
        except OSError:
            return None
        if not budget.take(len(data)):
            return None
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    return read


def _url_unquote(value: str) -> str:
    try:
        from urllib.parse import unquote
        return unquote(value)
    except Exception:
        return value


def _serialize(node: Any, opts: _Options) -> str:
    if isinstance(node, str):
        return _escape_text(node)
    tag = node.tag
    if not tag:
        return "".join(_serialize(c, opts) for c in node.children)
    if tag == "svg":
        return _serialize_svg(node)
    if tag in DROP_SUBTREE:
        return ""
    if tag not in ALLOWED_TAGS:
        return "".join(_serialize(c, opts) for c in node.children)
    if tag == "img":
        src = next((v for k, v in node.attrs if k == "src"), "")
        uri = opts.image_uri(src)
        if not uri:
            return ""
        attrs = [("src", uri)]
        for k, v in node.attrs:
            if k in ALLOWED_ATTRS and k not in ("class", "id", "style", "alt", "title", "width", "height",
                                                "colspan", "rowspan"):
                continue
            if k in ("alt", "title", "width", "height", "class", "id"):
                attrs.append((k, v))
        return "<img" + "".join(f' {k}="{_escape_attr(v)}"' for k, v in attrs) + ">"
    # Every element keeps only allowlisted attributes, values escaped; an anchor keeps its text, never an href, and
    # nothing keeps an event handler, src (bar the inlined img above), action or formaction.
    attrs = []
    for k, v in node.attrs:
        if k not in ALLOWED_ATTRS and not _DATA_ARIA_ATTR.fullmatch(k):
            continue
        if k == "style":
            v = _sanitize_css(str(v), sheet=False)
            if not v:
                continue
        attrs.append((k, v))
    inner = "".join(_serialize(c, opts) for c in node.children)
    open_tag = "<" + tag + "".join(f' {k}="{_escape_attr(v)}"' for k, v in attrs)
    if tag in VOID_TAGS:
        return open_tag + ">"
    return open_tag + ">" + inner + "</" + tag + ">"


def _svg_attr(name: str, value: str) -> Optional[Tuple[str, str]]:
    """One SVG attribute as (SVG spelling, value), or None. href/xlink:href only as a #fragment, url() only as
    url(#fragment), style through the inline CSS allowlist, everything else a plain presentation/geometry value."""
    if name in ("href", "xlink:href"):
        value = value.strip()
        return ("href", value) if _SVG_FRAGMENT.fullmatch(value) else None
    if _DATA_ARIA_ATTR.fullmatch(name):
        return name, value
    canonical = SVG_ATTRS.get(name)
    if canonical is None:
        return None
    if name == "style":
        clean = _sanitize_css(value, sheet=False)
        return (canonical, clean) if clean else None
    rest = _SVG_URL_FRAGMENT.sub("", _css_normalize(value))
    low = rest.lower()
    if any(c in rest for c in "\\<>{};@") or "url(" in low or "//" in rest or _CSS_SCHEME.search(rest):
        return None
    if any(token in low for token in _CSS_DANGER):
        return None
    return canonical, value


def _serialize_svg(node: Any) -> str:
    """The inline SVG subset. An element outside it goes with its whole subtree (a link or switch only unwraps)."""
    if isinstance(node, str):
        return _escape_text(node)
    if node.tag in SVG_UNWRAP:
        return "".join(_serialize_svg(c) for c in node.children)
    tag = SVG_TAGS.get(node.tag or "")
    if tag is None:
        return ""
    attrs = []
    for k, v in node.attrs:
        kept = _svg_attr(k, v)
        if kept is not None:
            attrs.append(kept)
    open_tag = "<" + tag + "".join(f' {k}="{_escape_attr(v)}"' for k, v in attrs)
    inner = "".join(_serialize_svg(c) for c in node.children)
    if node.tag in SVG_LEAF_TAGS:
        # A shape has no content; anything the parser nested under an unclosed one follows it instead.
        return open_tag + "/>" + inner
    return open_tag + ">" + inner + "</" + tag + ">"


def _safe_fragment(markup: str, opts: _Options) -> str:
    return _serialize(_tree(markup), opts)


def _variant_styles(root: _Node, rid: str, round_n: int, variant: str) -> List[str]:
    out: List[str] = []
    for style in root.find_all("style"):
        out.append(_text_of(style))
    for link in root.find_all("link"):
        attrs = dict(link.attrs)
        rel = attrs.get("rel", "").lower().split()
        if "stylesheet" not in rel:
            continue
        href = attrs.get("href", "").strip()
        if not href or href.startswith(("//", "/")) or re.match(r"(?i)^[a-z][a-z0-9+.-]*:", href):
            continue
        rel_path = _url_unquote(href.split("?", 1)[0].split("#", 1)[0]).replace("\\", "/")
        if not rel_path or any(p in ("", ".", "..") for p in rel_path.split("/")):
            continue
        try:
            f = store.safe_variant_file(rid, str(round_n), variant, rel_path)
            out.append(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
    return out


def _variant_body(root: _Node, opts: _Options) -> str:
    body = _body_of(root)
    nodes = body.children if body is not None else []
    if body is None:
        def without_head(n: Any) -> str:
            if isinstance(n, _Node):
                if n.tag == "head":
                    return ""
                if n.tag in ("html", "body", None):
                    return "".join(without_head(c) for c in n.children)
            return _serialize(n, opts)
        return "".join(without_head(c) for c in root.children)
    return "".join(_serialize(c, opts) for c in nodes)


# ---------------------------------------------------------------- threads

def _accepts_rounds(fn: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any((p.name == "rounds" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY))
               or p.kind is p.VAR_KEYWORD for p in params)


def _thread_round_visible(thread: Dict[str, Any], allowed: set) -> bool:
    for key in ("round", "round_n"):
        if key not in thread:
            continue
        value = thread[key]
        number = None
        if isinstance(value, int) and not isinstance(value, bool):
            number = value
        elif isinstance(value, str) and value.strip().isdecimal():
            number = int(value.strip())
        if number is None or number not in allowed:
            return False
    return True


def export_threads(review_id: str, *, rounds: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """The thread store's export view, normalised. The threads module is optional: only an import failure or a module
    without threads_for_export() yields []; a backend failure propagates to the server, which answers 500."""
    try:
        mod = importlib.import_module("mockreview.threads")
    except ImportError:
        return []
    fn = getattr(mod, "threads_for_export", None)
    if not callable(fn):
        return []
    raw = fn(review_id, rounds=rounds) if rounds is not None and _accepts_rounds(fn) else fn(review_id)
    if not isinstance(raw, list):
        return []
    allowed = None if rounds is None else {int(n) for n in rounds}
    out = []
    for t in raw:
        item = _normal_thread(t)
        if item is None:
            continue
        if allowed is not None and isinstance(t, dict) and not _thread_round_visible(t, allowed):
            continue
        out.append(item)
    return out


def _norm_author(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {"name": "", "kind": ""}
    name = value.get("name")
    kind = value.get("kind")
    return {"name": name.strip() if isinstance(name, str) else "",
            "kind": kind.strip() if isinstance(kind, str) else ""}


def _norm_body(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _normal_thread(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    body = _norm_body(value.get("body"))
    if not body.strip():
        return None
    anchor = value.get("anchor")
    label = anchor.get("label") if isinstance(anchor, dict) else ""
    replies = []
    raw_replies = value.get("replies")
    if isinstance(raw_replies, list):
        for r in raw_replies:
            if not isinstance(r, dict):
                continue
            replies.append({"author": _norm_author(r.get("author")),
                            "at": r.get("at") if isinstance(r.get("at"), str) else "",
                            "body": _norm_body(r.get("body"))})
    return {
        "anchor": {"label": label if isinstance(label, str) else ""},
        "author": _norm_author(value.get("author")),
        "at": value.get("at") if isinstance(value.get("at"), str) else "",
        "body": body,
        "resolved": value.get("resolved") is True,
        "replies": replies,
    }


def _author_text(author: Dict[str, str]) -> str:
    kind = author.get("kind") or ""
    name = author.get("name") or ""
    if kind == "former":
        return "Former member"
    if kind == "guest":
        return (name + " (guest)") if name else "Guest"
    return name


def _comment_block(thread: Dict[str, Any]) -> str:
    label = thread["anchor"].get("label") or "the board"
    resolved = '<span class="mr-pill">Resolved</span>' if thread["resolved"] else ""
    replies = "".join(
        '<div class="mr-reply"><p class="mr-comment-meta">' + _escape_text(_author_text(r["author"]))
        + (" · " + _escape_text(r["at"]) if r["at"] else "") + "</p><p class=\"mr-comment-body\">"
        + _escape_text(r["body"]) + "</p></div>"
        for r in thread["replies"])
    return ('<article class="mr-comment">'
            '<p class="mr-comment-anchor">on <span>' + _escape_text(label) + "</span></p>"
            '<p class="mr-comment-meta"><strong>' + _escape_text(_author_text(thread["author"]))
            + "</strong>" + (" · " + _escape_text(thread["at"]) if thread["at"] else "")
            + (" " + resolved if resolved else "") + "</p>"
            '<p class="mr-comment-body">' + _escape_text(thread["body"]) + "</p>" + replies + "</article>")


# ---------------------------------------------------------------- rounds

def _round_of(data: Dict[str, Any], round_n: int) -> Dict[str, Any]:
    for r in data.get("rounds") or []:
        if r.get("n") == round_n:
            return r
    raise store.ReviewError("round %s not found" % round_n, 400)


def _variant_file(rid: str, round_n: int, variant: str, rel: str) -> Optional[Path]:
    try:
        return store.safe_variant_file(rid, str(round_n), variant, rel)
    except Exception:
        return None


def _decisions_of(rid: str, round_n: int, rnd: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    variants = rnd.get("variants") or []
    variant = str(variants[0].get("id")) if variants else "a"
    f = _variant_file(rid, round_n, variant, "decisions.json")
    if f is None:
        return None
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _block_text(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return '<p class="mr-text">' + _escape_text(text) + "</p>" if text else ""


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _block(block: Dict[str, Any], opts: _Options) -> str:
    t = block.get("type") or "text"
    title = block.get("title") or ""
    head = '<h2 class="mr-block-title">' + _escape_text(title) + "</h2>" if title else ""
    body = ""
    if t == "text":
        body = _block_text(block.get("text"))
    elif t == "callout":
        tone = block.get("tone") if block.get("tone") in ("neutral", "ok", "warn", "err", "accent") else "neutral"
        body = '<div class="mr-callout mr-tone-' + tone + '">' + _block_text(block.get("text")) + "</div>"
    elif t == "cards":
        items = []
        for it in _list(block.get("items")):
            if not isinstance(it, dict):
                continue
            tone = it.get("tone") if it.get("tone") in ("neutral", "ok", "warn", "err", "accent") else "neutral"
            pill = it.get("pill")
            items.append('<div class="mr-card mr-tone-' + tone + '">'
                         + _block_text(it.get("text")).replace('class="mr-text"', 'class="mr-card-text"')
                         + '<p class="mr-card-title">' + _escape_text(it.get("title") or "") + "</p>"
                         + ('<span class="mr-pill">' + _escape_text(pill) + "</span>" if pill else "")
                         + "</div>")
        body = '<div class="mr-cards">' + "".join(items) + "</div>"
    elif t == "table":
        cols = _list(block.get("columns"))
        hl = block.get("highlight_col")
        head_cells = "".join("<th" + (' class="mr-hl"' if hl == i else "") + ">" + _escape_text(c) + "</th>"
                             for i, c in enumerate(cols))
        rows = []
        for row in _list(block.get("rows")):
            if not isinstance(row, list):
                continue
            rows.append("<tr>" + "".join("<td" + (' class="mr-hl"' if hl == i else "") + ">"
                                         + _escape_text(c) + "</td>" for i, c in enumerate(row)) + "</tr>")
        body = ('<table class="mr-table"><thead><tr>' + head_cells + "</tr></thead><tbody>"
                + "".join(rows) + "</tbody></table>")
    elif t == "settled":
        items = []
        for it in _list(block.get("items")):
            if not isinstance(it, dict):
                continue
            quote = ('<p class="mr-quote">' + _escape_text(it.get("quote")) + "</p>") if it.get("quote") else ""
            rule = ('<p class="mr-rule">' + _escape_text(it.get("rule")) + "</p>") if it.get("rule") else ""
            items.append('<div class="mr-settled-item"><h3>' + _escape_text(it.get("title") or "") + "</h3>"
                         + quote + rule + "</div>")
        body = '<div class="mr-settled">' + "".join(items) + "</div>"
    elif t == "defaults":
        body = _block_text(block.get("text")) + '<ul class="mr-list">' + "".join(
            "<li>" + _escape_text(x) + "</li>" for x in _list(block.get("items"))) + "</ul>"
    elif t == "recap":
        body = '<ul class="mr-list">' + "".join("<li>" + _escape_text(x) + "</li>"
                                                for x in _list(block.get("items"))) + "</ul>"
    elif t == "question":
        rec = block.get("recommendation") if isinstance(block.get("recommendation"), dict) else {}
        body = _block_text(block.get("text"))
        if rec:
            body += ('<div class="mr-recommendation"><p class="mr-meta">Recommended: '
                     + _escape_text(rec.get("answer") or "") + "</p>"
                     + (_block_text(rec.get("why")) if rec.get("why") else "") + "</div>")
    elif t == "piece":
        tag = block.get("tag")
        body = (_block_text(block.get("text"))
                + ('<ul class="mr-list">' + "".join("<li>" + _escape_text(x) + "</li>"
                                                    for x in _list(block.get("items"))) + "</ul>"
                   if _list(block.get("items")) else ""))
        if block.get("html"):
            body += opts.mock(block.get("html"))
        if tag:
            head = '<span class="mr-tag">' + _escape_text(tag) + "</span>" + head
    elif t == "decision":
        today = ('<p class="mr-meta">Today: ' + _escape_text(block.get("today")) + "</p>") if block.get("today") else ""
        intro = _block_text(block.get("text"))
        opts_html = []
        for opt in _list(block.get("options")):
            if not isinstance(opt, dict):
                continue
            rec = '<span class="mr-pill">Recommended</span>' if opt.get("recommended") else ""
            opts_html.append('<div class="mr-option"><h3>' + _escape_text(opt.get("label") or "")
                             + "</h3>" + rec + _block_text(opt.get("text"))
                             + opts.mock(opt.get("html")) + "</div>")
        body = today + intro + '<div class="mr-options">' + "".join(opts_html) + "</div>"
    elif t == "mock":
        body = (opts.mock(block.get("html"))
                + _block_text(block.get("text"))
                + ('<p class="mr-caption">' + _escape_text(block.get("caption")) + "</p>"
                   if block.get("caption") else ""))
    elif t == "artifact":
        body = _artifact_figure(block, opts)
    else:
        # An unknown type still leaves a visible, non-dropped placeholder with its text.
        body = _block_text(block.get("text")) + '<p class="mr-meta">' + _escape_text(t) + "</p>"
    return ('<section class="mr-block mr-block-' + _escape_text(t) + '" data-block="' + _escape_text(t) + '">'
            + head + body + "</section>")


def _artifact_figure(block: Dict[str, Any], opts: _Options) -> str:
    rel = block.get("file")
    markup = ""
    if isinstance(rel, str):
        f = _variant_file(opts.review_id, opts.round_n, opts.variant, rel)
        if f is not None:
            try:
                markup = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                markup = ""
    styles: List[str] = []
    body = ""
    if markup:
        root = _tree(markup)
        styles = _variant_styles(root, opts.review_id, opts.round_n, opts.variant)
        body = _variant_body(root, opts)
    # The page's own CSS is written for a page of its own (`html`, `body`, `h1`, absolute boxes). Inlined bare it would
    # restyle this document's title and layout, so it is scoped to one wrapper per artifact, and the page's root
    # selectors (`html`, `body`, `:root`) become that wrapper: it is the page's body here.
    scope = "mr-aw-%d-%s" % (opts.round_n, re.sub(r"[^a-z0-9-]", "", str(block.get("id") or "x").lower())[:40] or "x")
    style_tag = ""
    if styles:
        css = _sanitize_css("\n".join(styles), sheet=True, scope="." + scope) or ""
        # One pass: the group takes a whole chain (`html body :root`), so the sheet is never rescanned.
        root_sel = re.compile(r"\." + re.escape(scope) + r"(?:\s+(?:html|body|:root)(?![\w-]))+")
        css = root_sel.sub("." + scope, css)
        if css:
            style_tag = "<style>" + css + "</style>"
    display = block.get("display") if block.get("display") in ("page", "long", "canvas") else "frame"
    caption = ""
    if block.get("header") is not False:   # a page that speaks for itself has no caption either
        caption = '<figcaption><strong>' + _escape_text(block.get("title") or "Artifact") + "</strong>"
        if block.get("caption"):
            caption += " <span>" + _escape_text(block.get("caption")) + "</span>"
        caption += "</figcaption>"
    return ('<figure class="mr-artifact mr-artifact-d-' + display + '">' + style_tag + '<div class="mr-artifact-page">'
            + '<div class="mr-aw ' + scope + '">' + body + "</div></div>" + caption + "</figure>")


def _prototype_html(variant: Dict[str, Any], opts: _Options) -> str:
    rid = opts.review_id
    vid = str(variant.get("id") or "a")
    f = _variant_file(rid, opts.round_n, vid, "index.html")
    root = _tree(f.read_text(encoding="utf-8", errors="replace")) if f is not None else _tree("")
    styles = _variant_styles(root, rid, opts.round_n, vid)
    body = _variant_body(root, opts)
    label = variant.get("label") or "Prototype"
    stance = variant.get("stance") or ""
    heading = ('<header class="mr-variant-head"><h2>' + _escape_text(label) + "</h2>"
               + ('<p class="mr-stance">' + _escape_text(stance) + "</p>" if stance else "") + "</header>")
    style_tag = "<style>" + _escape_css("\n".join(styles)) + "</style>" if styles else ""
    return ('<section class="mr-variant" data-variant="' + _escape_text(vid) + '">' + heading
            + style_tag + '<div class="mr-variant-page">' + body + "</div></section>")


def _render_round_content(data: Dict[str, Any], round_n: int, rnd: Dict[str, Any],
                          opts: _Options) -> str:
    rid = opts.review_id
    kind = rnd.get("kind") or ("decisions" if rnd.get("decisions") else "variants")
    if kind == "decisions":
        dec = _decisions_of(rid, round_n, rnd)
        if dec is None:
            return '<p class="mr-meta">This round could not be read.</p>'
        design = rnd.get("design") if isinstance(rnd.get("design"), dict) else None
        extra = dec.get("extra_css") if isinstance(dec.get("extra_css"), str) else ""
        if opts.designs is not None and (design or extra.strip()):
            # The board's mock box: the profile CSS (always in variant a, like board.js roundBase) + extra_css.
            cls = opts.designs.add(rid, round_n, "a", design or {}, extra)
            opts.design = (cls, _design_theme(data, design) if design else None, design is not None)
        intro = _block_text(dec.get("intro"))
        body = "".join(_block(b, opts) for b in _list(dec.get("blocks")) if isinstance(b, dict))
        status = dec.get("status") if isinstance(dec.get("status"), dict) else None
        status_html = ""
        if status and status.get("label"):
            status_html = '<p class="mr-round-status">' + _escape_text(status["label"]) + "</p>"
        return intro + status_html + body
    variants = [v for v in _list(rnd.get("variants")) if isinstance(v, dict)]
    return "".join(_prototype_html(v, opts) for v in variants)


# ---------------------------------------------------------------- the document

_STYLE = """
:root{--b:#fafafa;--s:#fff;--s2:#f1f1f4;--l:#e2e2e8;--t:#17171c;--m:#666672;--a:#4f46e5;--ai:#fff;
--as:rgba(79,70,229,.10);--ok:#15803d;--oks:rgba(21,128,61,.10);--wn:#a16207;--wns:rgba(161,98,7,.12);
--pin:#5b5bf0;--r:10px;--rc:12px;color-scheme:light dark}
@media(prefers-color-scheme:dark){:root{--b:#0b0b0e;--s:#141418;--s2:#1c1c22;--l:#2a2a33;--t:#ededf2;
--m:#a0a0ac;--a:#8585ff;--ai:#0b0b0e;--as:rgba(133,133,255,.16);--ok:#4ade80;--oks:rgba(74,222,128,.14);
--wn:#fbbf24;--wns:rgba(251,191,36,.14)}}
/* A design-profile mock (inside .cq) is styled by its own CSS alone, as in the board's shadow root. */
:where(:not(.cq *)){box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0 auto;max-width:960px;padding:32px 24px 48px;background:var(--b);color:var(--t);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",sans-serif;
overflow-wrap:anywhere}
h1:where(:not(.cq *)){font-size:20px;line-height:1.25;margin:0 0 4px;letter-spacing:-.01em}
h2:where(:not(.cq *)){font-size:16px;line-height:1.3;margin:20px 0 8px}
h3:where(:not(.cq *)){font-size:14px;margin:12px 0 4px}
p:where(:not(.cq *)){margin:6px 0}
ul:where(:not(.cq *)),ol:where(:not(.cq *)){margin:8px 0;padding-left:20px}
li:where(:not(.cq *)){margin:4px 0}
a:where(:not(.cq *)){color:var(--a)}
img:where(:not(.cq *)){max-width:100%;height:auto}
code:where(:not(.cq *)),pre:where(:not(.cq *)){font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
pre:where(:not(.cq *)){white-space:pre-wrap;overflow-wrap:anywhere;background:var(--s2);border:1px solid var(--l);border-radius:var(--r);padding:12px}
blockquote:where(:not(.cq *)){margin:8px 0;padding:2px 12px;border-left:3px solid var(--l);color:var(--m)}
hr:where(:not(.cq *)){border:0;border-top:1px solid var(--l);margin:20px 0}
.mr-header{padding-bottom:16px;border-bottom:1px solid var(--l);margin-bottom:20px}
.mr-meta,.mr-generated,.mr-caption,.mr-round-status,.mr-comment-meta,.mr-stance{color:var(--m);font-size:12px}
.mr-brief{color:var(--m);font-size:13px;margin-top:8px}
.mr-block{margin:0 0 24px;padding:0 0 20px;border-bottom:1px solid var(--l)}
.mr-block:last-child{border-bottom:0}
.mr-block-title{margin:0 0 8px;font-size:16px}
.mr-tag{display:inline-block;margin-right:8px;padding:2px 8px;border-radius:6px;background:var(--as);
color:var(--a);font-size:12px;vertical-align:middle}
.mr-text{white-space:pre-wrap;font-size:14px}
.mr-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:12px 0}
.mr-card{border:1px solid var(--l);border-radius:var(--rc);padding:12px 16px;background:var(--s);min-width:0}
.mr-card-title{font-weight:600;font-size:14px;margin:0 0 4px}
.mr-card-text{margin:0;color:var(--m);font-size:13px}
.mr-pill{display:inline-block;margin-left:6px;padding:2px 8px;border-radius:999px;background:var(--as);
color:var(--a);font-size:12px;font-weight:600;vertical-align:middle}
.mr-tone-ok{border-color:color-mix(in srgb,var(--ok) 35%,transparent)}
.mr-tone-warn{border-color:color-mix(in srgb,var(--wn) 35%,transparent)}
.mr-tone-err{border-color:color-mix(in srgb,#dc2626 35%,transparent)}
.mr-tone-accent{border-color:color-mix(in srgb,var(--a) 35%,transparent)}
.mr-callout{border:1px solid var(--l);border-left:4px solid var(--a);border-radius:var(--r);padding:12px 16px;background:var(--s)}
.mr-table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}
.mr-table th,.mr-table td{padding:8px 12px;border:1px solid var(--l);text-align:left;vertical-align:top}
.mr-table th{background:var(--s2)}
.mr-table .mr-hl{background:var(--s2);font-weight:600}
.mr-settled-item{border:1px solid var(--l);border-radius:var(--rc);padding:12px 16px;margin:8px 0;background:var(--s)}
.mr-quote{margin:4px 0;color:var(--m);font-style:italic}
.mr-rule{margin:4px 0}
.mr-recommendation{border:1px solid var(--l);border-radius:var(--rc);padding:12px 16px;background:var(--s);margin:8px 0}
.mr-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:12px 0}
.mr-option{border:1px solid var(--l);border-radius:var(--rc);padding:12px 16px;background:var(--s);min-width:0}
.mr-mock{border:1px solid var(--l);border-radius:var(--r);padding:12px;margin:12px 0;background:var(--s);overflow:hidden}
/* A mock is a light design the board shows as authored: keep it light in a dark export, or its own light panels
   would carry the dark theme's light text. */
.mr-mock:not(.mr-designed){color-scheme:light;background:#fff;color:#17171c}
.mr-mock:not(.mr-designed) a:where(:not(.cq *)){color:#4f46e5}
.mr-mock:not(.mr-designed) img:where(:not(.cq *)){display:block}
/* A design-profile mock is the board's box: .cq is the container its @media-turned-@container rules query and .app
   carries the profile's data-theme; the profile's CSS paints it, so no padding or forced light background here. */
.mr-mock .cq{container:mock/inline-size}
.mr-designed{padding:0;background:transparent;color:inherit;overflow-wrap:normal}
.mr-designed .app[data-theme=dark]{color-scheme:dark}
.mr-designed .app[data-theme=light]{color-scheme:light}
.mr-artifact{border:1px solid var(--l);border-radius:var(--r);margin:12px 0;background:var(--s2);overflow:hidden}
.mr-artifact-page{padding:12px;background:var(--s);overflow:auto}
/* The page's own body: a box of its own, so what the page positions or sizes stays inside it. A fixed-size page (a
   canvas, a page with a set width) scrolls inside the figure rather than widening the document. */
.mr-aw{position:relative;isolation:isolate;overflow:clip;min-width:0}
.mr-artifact-d-canvas .mr-artifact-page{padding:0}
/* A canvas page is drawn on the board's ground and usually paints none of its own, so this document gives it one: a
   light dotted ground, whatever the theme of the window around it. */
.mr-artifact-d-canvas .mr-aw{background-color:#f3f1fa;background-image:radial-gradient(rgba(31,29,24,.14) 1.1px,transparent 1.5px);
background-size:24px 24px;color:#1f1d18}
.mr-artifact figcaption{padding:8px 12px;border-top:1px solid var(--l);color:var(--m);font-size:12px}
.mr-variant{margin:0 0 28px}
.mr-variant-head h2{margin:0 0 4px}
.mr-variant-page{border:1px solid var(--l);border-radius:var(--r);padding:12px;background:var(--s);overflow:hidden}
.mr-comments{margin-top:32px;padding-top:20px;border-top:1px solid var(--l)}
.mr-comments h2{margin:0 0 12px}
.mr-comment{border:1px solid var(--l);border-radius:var(--rc);padding:12px 16px;margin:10px 0;background:var(--s)}
.mr-comment-anchor{font-size:12px;color:var(--m);margin:0 0 4px}
.mr-comment-body{white-space:pre-wrap;margin:4px 0;font-size:13px}
.mr-reply{margin:8px 0 0 16px;padding-left:12px;border-left:2px solid var(--l)}
.mr-empty{color:var(--m);font-size:13px}
@media print{
@page{margin:14mm}
body{max-width:none;padding:0}
.mr-block,.mr-card,.mr-option,.mr-mock,.mr-artifact,.mr-variant,.mr-comment,.mr-settled-item{break-inside:avoid}
a{text-decoration:none}
}
"""


def _slug(title: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return slug[:60].strip("-") or "review"


def export_filename(data: Dict[str, Any], round_n: int, ext: str) -> str:
    """<slug>-round-<n>.<ext>, ascii only; safe for a Content-Disposition name and a ZIP entry."""
    suffix = re.sub(r"[^a-z0-9]+", "", str(ext or "").lower())[:8] or "html"
    return f"{_slug(data.get('title'))}-round-{int(round_n)}.{suffix}"


def build_export_html(data: Dict[str, Any], round_n: int, include_comments: bool, *,
                      threads: Optional[List[Dict[str, Any]]] = None, generated_at: Optional[str] = None,
                      csp: str = EXPORT_CSP, fonts: bool = True) -> str:
    """The one static export document. Pure data in, HTML out; no listener and no script. fonts=False leaves the
    design profile's fonts out (each mock falls back to the next family in its stack)."""
    rnd = _round_of(data, int(round_n))
    rid = str(data.get("id") or "")
    kind = rnd.get("kind") or ("decisions" if rnd.get("decisions") else "variants")
    label = "Decisions" if kind == "decisions" else "Prototype" if kind == "prototype" else "Variants"
    if kind == "prototype":
        variants = rnd.get("variants") or []
        if variants and variants[0].get("label"):
            label = str(variants[0]["label"])
    budget = _Budget()
    designs = _Designs(budget, fonts)
    opts = _Options(review_id=rid, round_n=int(round_n), variant="a",
                    image=_image_reader(rid, int(round_n), "a", budget), budget=budget, designs=designs)
    # Every variant has its own image resolver; the base one covers decisions mocks (variant a).
    content_parts = []
    if kind == "decisions":
        content_parts.append(_render_round_content(data, int(round_n), rnd, opts))
    else:
        for v in [x for x in _list(rnd.get("variants")) if isinstance(x, dict)]:
            vid = str(v.get("id") or "a")
            vopts = _Options(review_id=rid, round_n=int(round_n), variant=vid,
                             image=_image_reader(rid, int(round_n), vid, budget), budget=budget)
            content_parts.append(_prototype_html(v, vopts))
    content = "".join(content_parts)
    if include_comments:
        items = threads if isinstance(threads, list) else []
        comments = "".join(_comment_block(t) for t in items)
        if not items:
            comments = '<p class="mr-empty">No comments.</p>'
        content += ('<section class="mr-comments" id="comments"><h2>Comments</h2>' + comments + "</section>")
    title = str(data.get("title") or "Mock review")
    meta_bits = [str(data.get("mode") or ""), str(data.get("status") or ""), "Round %d" % int(round_n), label]
    meta = " · ".join(x for x in meta_bits if x)
    brief = data.get("brief")
    generated = generated_at or time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    name = export_filename(data, int(round_n), "png")[:-4]
    header = ('<header class="mr-header"><h1>' + _escape_text(title) + "</h1>"
              + '<p class="mr-meta">' + _escape_text(meta) + "</p>"
              + (('<p class="mr-brief">' + _escape_text(brief) + "</p>") if brief else "")
              + '<p class="mr-generated">Generated ' + _escape_text(generated) + "</p></header>")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="' + str(csp).replace('&', '&amp;').replace('"', '&quot;') + '">'
            '<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex, nofollow">'
            '<title>' + _escape_text(title) + " · mock review</title><style>" + _STYLE + designs.css() + "</style></head>"
            '<body data-export-name="' + _escape_attr(name) + '" data-export-filename="'
            + _escape_attr(name + ".png") + '">'
            + header + '<main class="mr-body">' + content + "</main></body></html>")
