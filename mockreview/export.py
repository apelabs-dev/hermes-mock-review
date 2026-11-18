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
p div span a b i em strong small code pre ul ol li table thead tbody tr td th h1 h2 h3 h4 h5 h6 img figure
figcaption section article header footer dl dt dd blockquote hr br details summary
""".split())
VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())
# These are dropped together with everything inside them: their "text" is code/markup, not prose.
DROP_SUBTREE = frozenset("""
script style iframe frame frameset object embed applet svg math template noscript canvas video audio source
link meta base title head
""".split())
ALLOWED_ATTRS = frozenset("class id style alt title width height colspan rowspan".split())
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
_CSS_PROPERTY = re.compile(r"-?[a-z][a-z0-9-]*", re.I)
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
        if _CSS_DATA_IMAGE.fullmatch(payload):
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
    if "\\" in value or "<" in value or ">" in value or not _css_balanced(value):
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


def _css_declarations(text: str) -> Optional[str]:
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
        value = _css_value(pair[1])
        if value is None:
            continue
        out.append(prop + ":" + value.strip())
    return ";".join(out)


def _css_prelude_ok(prelude: str) -> bool:
    text = prelude.strip()
    if not text:
        return True
    if text.startswith("@"):
        if not re.match(r"(?i)@media\b", text):
            return False
        text = re.sub(r"(?i)^@media\b", "", text, count=1)
    if "\\" in text or "<" in text or ">" in text or "@" in text or "//" in text:
        return False
    if "url(" in text.lower() or not _css_balanced(text):
        return False
    return not any(token in text.lower() for token in _CSS_DANGER)


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


def _css_rules(css: str) -> Optional[str]:
    out = []
    i = 0
    while i < len(css):
        open_at = css.find("{", i)
        if open_at < 0:
            prelude = css[i:]
            if not _css_prelude_ok(prelude):
                return None
            out.append(prelude)
            break
        prelude = css[i:open_at]
        if not _css_prelude_ok(prelude):
            return None
        close_at = _css_block_end(css, open_at + 1)
        if close_at is None:
            return None
        inner = css[open_at + 1:close_at]
        out.append(prelude + "{")
        if "{" in inner:
            nested = _css_rules(inner)
            if nested is None:
                return None
            out.append(nested)
        else:
            out.append(_css_declarations(inner) or "")
        out.append("}")
        i = close_at + 1
    return "".join(out)


def _sanitize_css(text: str, *, sheet: bool) -> Optional[str]:
    css = _css_normalize(text)
    if "\\" in css or "<" in css or ">" in css:
        return None
    css = _css_strip_danger(_css_replace_urls(css))
    css = re.sub(r"@import\b[^;{}]*(?:\{[^{}]*\}|;|$)", "", css, flags=re.I | re.S)
    out = _css_rules(css) if sheet else _css_declarations(css)
    if out is None:
        return None
    if sheet:
        masked = _CSS_URL.sub(lambda m: "" if _css_safe_url(_css_url_payload(m)) else "\0", out)
        if "\0" in masked or "url(" in masked.lower() or "//" in masked:
            return None
        masked = re.sub(r"(?i)@media\b", "", masked)
        if "\\" in masked or "<" in masked or ">" in masked or "@" in masked:
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


@dataclass
class _Options:
    review_id: str = ""
    round_n: int = 1
    variant: str = "a"
    image: Optional[Callable[[str], Optional[str]]] = None
    budget: _Budget = field(default_factory=_Budget)

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
    attrs = []
    if tag == "a":
        # An anchor keeps its text, never an href.
        for k, v in node.attrs:
            if k in ALLOWED_ATTRS and k not in ("class", "id", "style"):
                attrs.append((k, v))
    else:
        for k, v in node.attrs:
            if k not in ALLOWED_ATTRS:
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
            body += '<div class="mr-mock">' + _safe_fragment(str(block.get("html")), opts) + "</div>"
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
                             + '<div class="mr-mock">' + _safe_fragment(str(opt.get("html") or ""), opts)
                             + "</div></div>")
        body = today + intro + '<div class="mr-options">' + "".join(opts_html) + "</div>"
    elif t == "mock":
        body = ('<div class="mr-mock">' + _safe_fragment(str(block.get("html") or ""), opts) + "</div>"
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
    style_tag = ""
    if styles:
        css = _escape_css("\n".join(styles))
        style_tag = "<style>" + css + "</style>"
    caption = '<figcaption><strong>' + _escape_text(block.get("title") or "Artifact") + "</strong>"
    if block.get("caption"):
        caption += " <span>" + _escape_text(block.get("caption")) + "</span>"
    caption += "</figcaption>"
    return ('<figure class="mr-artifact">' + style_tag + '<div class="mr-artifact-page">' + body
            + "</div>" + caption + "</figure>")


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
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0 auto;max-width:960px;padding:32px 24px 48px;background:var(--b);color:var(--t);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",sans-serif;
overflow-wrap:anywhere}
h1{font-size:20px;line-height:1.25;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:16px;line-height:1.3;margin:20px 0 8px}
h3{font-size:14px;margin:12px 0 4px}
p{margin:6px 0}
ul,ol{margin:8px 0;padding-left:20px}
li{margin:4px 0}
a{color:var(--a)}
img{max-width:100%;height:auto}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--s2);border:1px solid var(--l);border-radius:var(--r);padding:12px}
blockquote{margin:8px 0;padding:2px 12px;border-left:3px solid var(--l);color:var(--m)}
hr{border:0;border-top:1px solid var(--l);margin:20px 0}
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
.mr-mock img{display:block}
.mr-artifact{border:1px solid var(--l);border-radius:var(--r);margin:12px 0;background:var(--s2);overflow:hidden}
.mr-artifact-page{padding:12px;background:var(--s);overflow:hidden}
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
                      csp: str = EXPORT_CSP) -> str:
    """The one static export document. Pure data in, HTML out; no listener and no script."""
    rnd = _round_of(data, int(round_n))
    rid = str(data.get("id") or "")
    kind = rnd.get("kind") or ("decisions" if rnd.get("decisions") else "variants")
    label = "Decisions" if kind == "decisions" else "Prototype" if kind == "prototype" else "Variants"
    if kind == "prototype":
        variants = rnd.get("variants") or []
        if variants and variants[0].get("label"):
            label = str(variants[0]["label"])
    budget = _Budget()
    opts = _Options(review_id=rid, round_n=int(round_n), variant="a",
                    image=_image_reader(rid, int(round_n), "a", budget), budget=budget)
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
            '<title>' + _escape_text(title) + " · mock review</title><style>" + _STYLE + "</style></head>"
            '<body data-export-name="' + _escape_attr(name) + '" data-export-filename="'
            + _escape_attr(name + ".png") + '">'
            + header + '<main class="mr-body">' + content + "</main></body></html>")
