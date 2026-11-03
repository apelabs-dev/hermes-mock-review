"""Bundle a prototype page into one self-contained HTML file.

A password-gated here.now site sets its unlock cookie SameSite=Lax. The board shows a prototype in a sandboxed iframe
(sandbox="allow-scripts": an opaque origin), so every request the page makes for its own CSS, scripts, images and fonts
is cross-site and goes out without the cookie: the gate answers 401 and the prototype renders unstyled. So a prototype
published behind a password is inlined first: every local stylesheet, script, image and font becomes part of the HTML as
a data: URI (nested CSS url() and @import included), and runtime asset paths in scripts go through a small lookup table
(`window.__MR_A`, read by `window.__mrA(path)`) that holds each asset once.

Only the page's own folder is read: a reference that is missing, leaves that folder, goes through a symbolic link, has an
unsupported type or exceeds the size bounds fails the whole bundle with a message naming it. Nothing is skipped
silently. Standard library only; Pillow is used, when already installed, to shrink big PNG/JPEG stills to WebP.

What is not supported, and fails instead of producing a half-working bundle: a local <iframe>, a link to another local
page, a web manifest, a <base href>, an alternate stylesheet, a page that already has an import map next to local ES
modules, Workers and importScripts, and root-absolute ("/x") local paths. Paths built at runtime are found only when
they are written as a template literal that starts with a local folder (`assets/poster-${n}.webp`).

A script string that is a whole asset path (`'missing.png'`, `'lost/poster.png'`) and names no file is a missing
reference like any other; so is a template literal ending in an asset extension whose folder is missing. Strings
that are not asset paths (external URLs, prose, object keys, a download's file name, a bare `x.json`/`x.txt`) are left
as written.
"""

from __future__ import annotations

import base64
import html as htmllib
import importlib
import io
import json
import posixpath
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .store import MAX_ASSETS_BYTES, ReviewError

MAX_ASSET_BYTES = 25 * 1024 * 1024        # one file
MAX_BUNDLE_BYTES = 64 * 1024 * 1024       # the finished HTML
REENCODE_MIN_BYTES = 300_000              # PNG/JPEG above this become WebP q85 when Pillow is installed
WEBP_QUALITY = 85
MODULE_PREFIX = "mr-module/"              # import-map key prefix for ES modules

MIME = {
    ".css": "text/css", ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
    ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif", ".ico": "image/x-icon",
    ".bmp": "image/bmp", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject", ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".txt": "text/plain", ".wasm": "application/wasm",
}
JS_EXT = (".js", ".mjs", ".cjs")
JS_TYPES = ("", "module", "text/javascript", "application/javascript", "text/ecmascript",
            "application/ecmascript", "application/x-javascript")
_EXT = "(?:" + "|".join(sorted((e[1:] for e in MIME), key=len, reverse=True)) + ")"
_SEG = r"[\w@%+~-][\w@%+.~-]*"
_PATH = r"(?:\.{1,2}/)*(?:" + _SEG + r"/)*" + _SEG + r"\." + _EXT

_ATTRS = r"""(?:\s+[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?)*"""
_HTML = re.compile(
    r"<!--.*?-->"
    r"|<(?P<raw>script|style)(?P<rattrs>" + _ATTRS + r")\s*>(?P<rbody>.*?)</(?P=raw)\s*>"
    r"|<(?P<tag>[A-Za-z][^\s/>]*)(?P<attrs>" + _ATTRS + r")(?P<slash>\s*/)?\s*>",
    re.S | re.I)
_ATTR = re.compile(r"""(\s+)([^\s"'>/=]+)(?:(\s*=\s*)("[^"]*"|'[^']*'|[^\s"'>]+))?""")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CSS = re.compile(
    r"""(?P<imp>@import\s+(?:url\(\s*(?:"(?P<i1>[^"]*)"|'(?P<i2>[^']*)'|(?P<i3>[^)\s]*))\s*\)|"(?P<i4>[^"]*)"|'(?P<i5>[^']*)')"""
    r"""\s*(?P<cond>[^;{]*);)"""
    r"""|(?P<iset>(?:-webkit-)?image-set\((?P<isc>[^()]*)\))"""
    r"""|url\(\s*(?:"(?P<u1>[^"]*)"|'(?P<u2>[^']*)'|(?P<u3>[^)\s'"]*))\s*\)""", re.I)
_CSS_NOISE = re.compile(r"""/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'""", re.S)
_CHARSET = re.compile(r"@charset\s+(?:\"[^\"]*\"|'[^']*')\s*;", re.I)
_JS_REF = re.compile(
    r"(?P<lit>(?P<q>[\"'`])(?P<lp>" + _PATH + r")(?P<ls>[?#][^\"'`\s\\]*)?(?P=q))"
    r"|(?<![\w@%./:\\$-])(?P<sp>" + _PATH + r")(?![\w@%/-])(?!\.\w)", re.I)
_JS_IMPORT = re.compile(r"""(?P<kw>\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)(?P<q>["'])(?P<spec>[^"'\n]+)(?P=q)""")
_JS_TEMPLATE = re.compile(r"`(?:\.{1,2}/)*(?:[\w@%+.~-]+/)+")
_JS_WORKER = re.compile(r"\bnew\s+(?:Shared)?Worker\s*\(|\bimportScripts\s*\(|\bserviceWorker\s*\.\s*register\s*\(")
_DOWNLOAD = re.compile(r"\bdownload[\"'`]?\s*[=,]\s*$|\bsaveAs\s*\(\s*(?:[^()]*,\s*)?$", re.I)
_LOOSE_ATTR = re.compile(r"^(?:data-|on)|src|url|img|image|bg|icon|poster|background|thumb|logo", re.I)
_HELPER = ("(function(w){var A=w.__MR_A||{};w.__mrA=function(p){var s=String(p).split('#')[0].split('?')[0],o=[],"
           "t=s.split('/'),i;for(i=0;i<t.length;i++){if(t[i]===''||t[i]==='.')continue;if(t[i]==='..')o.pop();"
           "else o.push(t[i])}var k=o.join('/');return Object.prototype.hasOwnProperty.call(A,k)?A[k]:p}})(window);")


def _load_pillow() -> Any:
    """Pillow's Image module when it is already installed, else None. Never installed or required."""
    try:
        return importlib.import_module("PIL.Image")
    except Exception:
        return None


def _webp(data: bytes, mime: str) -> Tuple[bytes, str]:
    """A big PNG/JPEG still as WebP q85 (same pixels at display size); anything else, or any failure, as is."""
    if mime not in ("image/png", "image/jpeg") or len(data) <= REENCODE_MIN_BYTES:
        return data, mime
    image_mod = _load_pillow()
    if image_mod is None:
        return data, mime
    try:
        pil_open = image_mod.open   # Pillow's image decoder, not a text file handle
        im = pil_open(io.BytesIO(data))
        if getattr(im, "is_animated", False):
            return data, mime
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if ("A" in im.mode or "transparency" in im.info) else "RGB")
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=WEBP_QUALITY, method=5)
        out = buf.getvalue()
    except Exception:
        return data, mime
    return (out, "image/webp") if 0 < len(out) < len(data) else (data, mime)


def is_local_ref(ref: str) -> bool:
    """A URL that points at a file next to the page: not empty, not an anchor, not //host, not any scheme."""
    ref = (ref or "").strip()
    return bool(ref) and not ref.startswith(("#", "//")) and not _SCHEME.match(ref)


def _split_ref(ref: str) -> Tuple[str, str]:
    """-> (path, fragment-with-#). The query is dropped: a file on disk has none."""
    path, _, frag = ref.strip().partition("#")
    path = path.split("?", 1)[0]
    return urllib.parse.unquote(path).replace("\\", "/"), ("#" + frag if frag else "")


def _asset_string(path: str, before: str) -> bool:
    """Is this whole string, which names no file in the folder, a missing asset rather than some other string? A path with
    a folder or an asset extension is. A bare .json/.txt name is as likely an output file name, and a name given to a
    download (`a.download = 'x.png'`) is one by definition: both stay as written."""
    if _DOWNLOAD.search(before[-80:]):
        return False
    bare = "/" not in path
    return not (bare and posixpath.splitext(_split_ref(path)[0])[1].lower() in (".json", ".txt"))


def _json_safe(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")


def _template_end(s: str, i: int) -> Optional[int]:
    """Index just past the template literal that opens at s[i] == '`' (nested ${} included), or None."""
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1
        elif c == "$" and s[i + 1:i + 2] == "{":
            i = _expr_end(s, i + 2)
            if i is None:
                return None
        else:
            i += 1
    return None


def _expr_end(s: str, i: int) -> Optional[int]:
    depth, n = 1, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":
            j = i + 1
            while j < n and s[j] != c:
                j += 2 if s[j] == "\\" else 1
            i = j + 1
        elif c == "`":
            i = _template_end(s, i)
            if i is None:
                return None
        elif c == "{":
            depth += 1
            i += 1
        elif c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
        else:
            i += 1
    return None


class Bundler:
    def __init__(self, index: Path):
        self.index = Path(index)
        self.root = self.index.parent.resolve()
        self.problems: List[str] = []
        self.runtime: Dict[str, Optional[str]] = {}    # runtime key -> data: URI, each asset once
        self.modules: Dict[str, Optional[str]] = {}    # import-map key -> data: URL
        self._uri: Dict[str, str] = {}
        self._busy: List[str] = []
        self._raw_total = 0
        self._emitted = 0
        self.has_importmap = False

    # ------------------------------------------------------------------ bookkeeping

    def problem(self, msg: str) -> None:
        if msg not in self.problems:
            self.problems.append(msg)

    def _use(self, text: str) -> str:
        self._emitted += len(text)
        if self._emitted > MAX_BUNDLE_BYTES:
            raise ReviewError(f"prototype bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MB once "
                              "its assets are inlined; shrink the images or reference each asset fewer times")
        return text

    def locate(self, path: str, base: str, quiet: bool = False, what: str = "") -> Optional[str]:
        """The page-relative posix path of an existing regular file, or None (recorded unless quiet). Refuses
        absolute paths, anything that leaves the page's folder and any symbolic link on the way."""
        def bad(why: str) -> None:
            if not quiet:
                self.problem(f"{what or path}: {why}")
            return None

        if "\x00" in path or not path:
            return bad("not a usable path")
        if path.startswith("/"):
            return bad("root-absolute paths cannot be bundled (use a path relative to the page)")
        rel = posixpath.normpath(posixpath.join(base, path))
        if rel == ".." or rel.startswith("../"):
            return bad("leaves the prototype's folder")
        cur = self.root
        for part in rel.split("/"):
            cur = cur / part
            if cur.is_symlink():
                return bad("goes through a symbolic link (refused)")
        try:
            cur.resolve().relative_to(self.root)
        except (ValueError, OSError):
            return bad("leaves the prototype's folder")
        if not cur.is_file():
            return bad("file not found")
        return rel

    def read(self, rel: str) -> bytes:
        p = self.root / rel
        size = p.stat().st_size
        if size > MAX_ASSET_BYTES:
            raise ReviewError(f"{rel} is larger than {MAX_ASSET_BYTES // (1024 * 1024)} MB: too big to inline")
        return p.read_bytes()

    def text(self, rel: str) -> str:
        return self.read(rel).decode("utf-8", errors="replace").lstrip("\ufeff")

    def uri_for(self, rel: str) -> Optional[str]:
        """The data: URI of a file (CSS and JS are processed first). Cached; None after a recorded problem."""
        if rel in self._uri:
            return self._uri[rel]
        ext = posixpath.splitext(rel)[1].lower()
        mime = MIME.get(ext)
        if mime is None:
            self.problem(f"{rel}: unsupported file type {ext or '(none)'} (cannot be inlined)")
            return None
        if rel in self._busy:
            self.problem(f"{rel}: circular reference between bundled files")
            return None
        self._busy.append(rel)
        try:
            if ext == ".css":
                body = self.css(self.text(rel), posixpath.dirname(rel), (rel,)).encode("utf-8")
                mime += ";charset=utf-8"
            elif ext in JS_EXT:
                body = self.js(self.text(rel), "").encode("utf-8")
                mime += ";charset=utf-8"
            else:
                body = self.read(rel)
                body, mime = _webp(body, mime)
            self._raw_total += len(body)
            if self._raw_total > MAX_ASSETS_BYTES:
                raise ReviewError(f"the prototype's assets are larger than {MAX_ASSETS_BYTES // (1024 * 1024)} MB "
                                  "once inlined")
            uri = f"data:{mime};base64," + base64.b64encode(body).decode("ascii")
        finally:
            self._busy.pop()
        self._uri[rel] = uri
        return uri

    def ref_uri(self, ref: str, base: str, what: str) -> Optional[str]:
        """data: URI (+ fragment) for a local reference, or None after a recorded problem."""
        path, frag = _split_ref(ref)
        rel = self.locate(path, base, what=f"{what} -> {ref.strip()}")
        if rel is None:
            return None
        uri = self.uri_for(rel)
        return None if uri is None else self._use(uri + frag)

    # ------------------------------------------------------------------ CSS

    def css(self, text: str, base: str, stack: Tuple[str, ...]) -> str:
        text = _CHARSET.sub("", _CSS_NOISE.sub(lambda m: "" if m.group(0).startswith("/*") else m.group(0), text))
        where = stack[-1] if stack else "<style>"

        def sub(m: "re.Match[str]") -> str:
            if m.group("imp"):
                ref = next(m.group(g) for g in ("i1", "i2", "i3", "i4", "i5") if m.group(g) is not None)
                if not is_local_ref(ref):
                    return m.group(0)
                cond = (m.group("cond") or "").strip()
                path, _ = _split_ref(ref)
                rel = self.locate(path, base, what=f"{where}: @import {ref}")
                if rel is None:
                    return m.group(0)
                if rel in stack:
                    self.problem(f"{where}: @import {ref} is circular")
                    return m.group(0)
                if posixpath.splitext(rel)[1].lower() != ".css":
                    self.problem(f"{where}: @import {ref} is not a .css file")
                    return m.group(0)
                if re.search(r"\b(?:layer|supports)\b", cond, re.I):
                    self.problem(f"{where}: @import {ref} with layer()/supports() cannot be bundled")
                    return m.group(0)
                inner = self.css(self.text(rel), posixpath.dirname(rel), stack + (rel,))
                return self._use("@media " + cond + "{" + inner + "}" if cond else inner)
            if m.group("iset"):
                def one(s: "re.Match[str]") -> str:
                    ref = s.group(1) if s.group(1) is not None else s.group(2)
                    if not is_local_ref(ref):
                        return s.group(0)
                    uri = self.ref_uri(ref, base, where)
                    return s.group(0) if uri is None else '"' + uri + '"'
                return "image-set(" + re.sub(r'"([^"]*)"|\'([^\']*)\'', one, m.group("isc")) + ")"
            ref = next((m.group(g) for g in ("u1", "u2", "u3") if m.group(g) is not None), "")
            if not is_local_ref(ref):
                return m.group(0)
            uri = self.ref_uri(ref, base, where)
            return m.group(0) if uri is None else 'url("' + uri + '")'

        return _CSS.sub(sub, text)

    # ------------------------------------------------------------------ JS

    def runtime_asset(self, rel: str) -> None:
        """Put a file in the runtime table once; lookups by path find it through window.__mrA()."""
        if rel in self.runtime:
            return
        self.runtime[rel] = None
        self.runtime[rel] = self.uri_for(rel)

    def module_key(self, rel: str) -> str:
        key = MODULE_PREFIX + rel
        if key not in self.modules:
            self.modules[key] = None
            self.modules[key] = "data:text/javascript;charset=utf-8;base64," + base64.b64encode(
                self.js(self.text(rel), posixpath.dirname(rel)).encode("utf-8")).decode("ascii")
        return key

    def js(self, text: str, base: str) -> str:
        """Rewrite a script: local ES module imports -> import-map keys, runtime asset paths -> lookups."""
        if _JS_WORKER.search(text):
            for m in _JS_WORKER.finditer(text):
                tail = text[m.end():m.end() + 160].lstrip()
                if tail[:1] in "\"'`" and is_local_ref(tail[1:].split(tail[0], 1)[0]):
                    self.problem("scripts that start a Worker, importScripts() or register a service worker from "
                                 "a local file cannot be bundled")
                    break
        text = _JS_IMPORT.sub(lambda m: self._import(m, base), text)
        text = self._templates(text)
        return _JS_REF.sub(lambda m: self._ref(m, text), text)

    def _import(self, m: "re.Match[str]", base: str) -> str:
        spec = m.group("spec")
        if not spec.startswith(("./", "../", "/")) or not spec.lower().split("?", 1)[0].endswith(JS_EXT):
            return m.group(0)
        path, _ = _split_ref(spec)
        rel = self.locate(path, base, what=f"import {spec}")
        if rel is None:
            return m.group(0)
        q = m.group("q")
        return m.group("kw") + q + self.module_key(rel) + q

    def _templates(self, text: str) -> str:
        """`assets/dir/x-${n}.webp` -> window.__mrA(`...`) with every file of assets/dir/ in the runtime table."""
        out, pos, i = [], 0, 0
        while True:
            m = _JS_TEMPLATE.search(text, i)
            if not m:
                break
            start = m.start()
            end = _template_end(text, start)
            if end is None:
                i = start + 1
                continue
            i = end                       # never look inside this template again
            chunk = text[start:end]
            if "${" not in chunk or (start and re.match(r"[\w$.)\]]", text[start - 1])):
                continue                  # a plain literal (the reference pass takes it) or a tagged template
            body = chunk[1:-1]
            prefix = body.split("${", 1)[0]
            folder = prefix[:prefix.rfind("/") + 1]
            tail = body.rsplit("}", 1)[-1]
            ext = re.search(r"(\.[A-Za-z0-9]+)(?:[?#].*)?$", tail)
            ext = ext.group(1).lower() if ext and ext.group(1).lower() in MIME else None
            found = self._folder_files(folder, ext, prefix + "${...}")
            if not found:
                continue
            for key in found:
                self.runtime_asset(key)
            out.append(text[pos:start])
            out.append("window.__mrA(" + chunk + ")")
            pos = end
        out.append(text[pos:])
        return "".join(out)

    def _folder_files(self, folder: str, ext: Optional[str], what: str) -> List[str]:
        path = posixpath.normpath(folder) if folder else "."
        if path.startswith(("/", "..")) or "\x00" in path:
            self.problem(f"template literal {what}: folder is not inside the prototype's folder")
            return []
        cur = self.root
        for part in ([] if path == "." else path.split("/")):
            cur = cur / part
            if cur.is_symlink():
                self.problem(f"template literal {what}: folder goes through a symbolic link (refused)")
                return []
        if not cur.is_dir():
            if ext:          # it ends in an asset extension: a missing asset folder, not some other string
                self.problem(f"template literal {what}: folder {folder} not found")
            return []
        files = []
        for p in sorted(cur.rglob("*")):
            rel = p.relative_to(self.root).as_posix()
            if p.is_symlink() or not p.is_file() or any(x.startswith(".") for x in rel.split("/")):
                continue
            e = p.suffix.lower()
            if e in MIME and (ext is None or e == ext):
                files.append(rel)
        if not files:
            self.problem(f"template literal {what}: no local {ext or 'asset'} file under {folder}")
        return files

    def _ref(self, m: "re.Match[str]", text: str) -> str:
        if m.group("lit"):
            path, q = m.group("lp"), m.group("q")
            if path.startswith(MODULE_PREFIX) or not self._lit_ok(text, m.start(), m.end()):
                return m.group(0)       # (an import-map key the import pass just wrote is not a file)
            rel = self.locate(_split_ref(path)[0], "", quiet=True)
            if rel is None:
                if _asset_string(path, text[:m.start()]):
                    self.locate(_split_ref(path)[0], "", what=f"{path} (a script string)")
                return m.group(0)
            self.runtime_asset(rel)
            return "window.__mrA(" + m.group(0) + ")"
        path = m.group("sp")
        if "/" not in path and text[max(0, m.start() - 4):m.start()].lower() != "url(":
            return m.group(0)
        rel = self.locate(_split_ref(path)[0], "", quiet=True)
        if rel is None:
            return m.group(0)
        uri = self.uri_for(rel)
        return m.group(0) if uri is None else self._use(uri)

    @staticmethod
    def _lit_ok(text: str, s: int, e: int) -> bool:
        """May this whole string literal become an expression? Not an object key, an index or a case label."""
        before = text[:s].rstrip()
        after = text[e:].lstrip()
        if after[:1] == ":" and before[-1:] in ("{", ","):
            return False
        if before[-1:] == "[" and after[:1] == "]" and re.match(r"[\w$)\]]", before[:-1].rstrip()[-1:] or " "):
            return False
        word = re.search(r"([A-Za-z_$][\w$]*)$", before)
        return not (word and word.group(1) in ("case", "from", "import", "export"))

    # ------------------------------------------------------------------ HTML

    def _loose(self, value: str, where: str = "data attribute") -> str:
        """A local path inside a data attribute or a non-script data island: inline it as a data: URI."""
        def sub(m: "re.Match[str]") -> str:
            path = m.group("lp") or m.group("sp")
            if "/" not in path and not m.group("lit") and value.strip() != path:
                return m.group(0)
            rel = self.locate(_split_ref(path)[0], "", quiet=True)
            if rel is None:
                if (m.group("lit") or value.strip() == path) and _asset_string(path, value[:m.start()]):
                    self.locate(_split_ref(path)[0], "", what=f"{path} (in a {where})")
                return m.group(0)
            uri = self.uri_for(rel)
            if uri is None:
                return m.group(0)
            return self._use(m.group("q") + uri + m.group("q") if m.group("lit") else uri)
        return _JS_REF.sub(sub, value)

    def _srcset(self, value: str, base: str, where: str) -> str:
        """Every candidate of a srcset, parsed the way the browser does (a URL is the run up to whitespace, so a data: URI
        keeps its commas): local ones become data: URIs, others (data:, http:) stay, mixed lists included."""
        parts, i, n = [], 0, len(value)
        while True:
            while i < n and (value[i].isspace() or value[i] == ","):
                i += 1
            if i >= n:
                break
            j = i
            while j < n and not value[j].isspace():
                j += 1
            ref, i, desc = value[i:j], j, ""
            if ref.endswith(","):
                ref = ref.rstrip(",")
            else:
                k, depth = i, 0
                while k < n and not (value[k] == "," and depth == 0):
                    depth += (value[k] == "(") - (value[k] == ")")
                    k += 1
                desc, i = value[i:k].strip(), k
            uri = self.ref_uri(ref, base, where) if is_local_ref(ref) else None
            parts.append((uri or ref) + (" " + desc if desc else ""))
        return ", ".join(parts)

    def _attr(self, tag: str, name: str, val: str, attrs: Dict[str, Optional[str]]) -> str:
        v = val.strip()
        where = f"<{tag} {name}>"
        if name == "style":
            return self.css(v, "", ())
        if name in ("srcset", "imagesrcset"):
            return self._srcset(v, "", where)
        if tag in ("a", "area") and name == "href":
            if not is_local_ref(v):
                return val
            rel = self.locate(_split_ref(v)[0], "", quiet=True)
            if rel is None:
                return val
            if posixpath.splitext(rel)[1].lower() in (".html", ".htm"):
                self.problem(f"<{tag} href> -> {v}: a link to another local page cannot be bundled (one page only)")
                return val
            return self.ref_uri(v, "", where) or val
        if tag in ("iframe", "frame") and name == "src":
            if is_local_ref(v):
                self.problem(f"<{tag} src> -> {v}: a local frame cannot be bundled")
            return val
        if name in ("href", "xlink:href") and tag == "use":
            if is_local_ref(v):
                self.problem(f"<use {name}> -> {v}: an external SVG sprite cannot be bundled; inline the SVG")
            return val
        if (name in ("src", "poster", "background") or (name == "data" and tag == "object")
                or (name in ("href", "xlink:href") and tag in ("link", "image"))):
            return (self.ref_uri(v, "", where) or val) if is_local_ref(v) else val
        if _LOOSE_ATTR.search(name):
            return self._loose(val)
        return val

    def _link(self, attrs: Dict[str, Optional[str]], tag_text: str) -> Optional[str]:
        """<link>: a stylesheet becomes <style>; hints are dropped; anything else local is inlined by _attr."""
        rels = (attrs.get("rel") or "").lower().split()
        href = (attrs.get("href") or "").strip()
        if not is_local_ref(href):
            return tag_text if "imagesrcset" not in attrs else None
        if "stylesheet" in rels:
            if "alternate" in rels:
                self.problem(f"<link rel=\"alternate stylesheet\" href> -> {href}: cannot be bundled")
                return tag_text
            rel = self.locate(_split_ref(href)[0], "", what=f"<link rel=stylesheet> -> {href}")
            if rel is None:
                return tag_text
            css = self._use(self.css(self.text(rel), posixpath.dirname(rel), (rel,)))
            media = (attrs.get("media") or "").strip()
            return "<style" + (f' media="{htmllib.escape(media)}"' if media else "") + ">" + _close_safe(css) + "</style>"
        if any(r in rels for r in ("preload", "prefetch", "modulepreload", "prerender")):
            return ""
        if "manifest" in rels:
            self.problem(f"<link rel=\"manifest\" href> -> {href}: a web manifest cannot be bundled; remove the link")
            return tag_text
        return None

    def _tag(self, m: "re.Match[str]") -> str:
        tag = m.group("tag").lower()
        attrs_text = m.group("attrs")
        found = [(a.group(2), None if a.group(4) is None else _unq(a.group(4))) for a in _ATTR.finditer(attrs_text)]
        d = {n.lower(): v for n, v in found}
        if tag == "base" and d.get("href") is not None:
            self.problem("<base href> cannot be bundled (it changes how every other path resolves)")
            return m.group(0)
        if tag == "meta" and (d.get("http-equiv") or "").lower() == "content-security-policy":
            if "data:" not in (d.get("content") or "").lower():
                self.problem("a Content-Security-Policy meta tag that does not allow data: would block the bundled "
                             "assets; remove it or add data: to its sources")
            return m.group(0)
        if tag == "link":
            r = self._link(d, m.group(0))
            if r is not None:
                return r
        new, changed = [], False
        for n, v in found:
            nv = v if v is None else self._attr(tag, n.lower(), v, d)
            changed = changed or nv != v
            new.append((n, nv))
        if not changed:
            return m.group(0)
        body = "".join(" " + n + ("" if v is None else '="' + htmllib.escape(v, quote=True) + '"') for n, v in new)
        return "<" + m.group("tag") + body + (" /" if m.group("slash") else "") + ">"

    def _raw(self, m: "re.Match[str]") -> str:
        kind, rattrs, body = m.group("raw").lower(), m.group("rattrs"), m.group("rbody")
        if kind == "style":
            return "<style" + rattrs + ">" + _close_safe(self.css(body, "", ())) + "</style>"
        found = [(a.group(2), None if a.group(4) is None else _unq(a.group(4))) for a in _ATTR.finditer(rattrs)]
        d = {n.lower(): v for n, v in found}
        typ = (d.get("type") or "").strip().lower()
        src = d.get("src")
        if src is not None:
            if not is_local_ref(src):
                return m.group(0)
            if typ not in JS_TYPES:
                self.problem(f"<script type=\"{typ}\" src> -> {src}: only JavaScript files can be bundled")
                return m.group(0)
            rel = self.locate(_split_ref(src)[0], "", what=f"<script src> -> {src}")
            if rel is None:
                return m.group(0)
            code = self._use(self.js(self.text(rel), posixpath.dirname(rel)))
            uri = "data:text/javascript;charset=utf-8;base64," + base64.b64encode(code.encode("utf-8")).decode("ascii")
            keep = "".join(" " + n + ("" if v is None else '="' + htmllib.escape(v, quote=True) + '"')
                           for n, v in found if n.lower() not in ("src", "integrity"))
            return "<script" + keep + ' src="' + uri + '"></script>'
        if typ == "importmap":
            self.has_importmap = True
            try:
                spec = json.loads(body or "{}")
            except ValueError:
                return m.group(0)
            if any(isinstance(x, str) and is_local_ref(x) for x in _strings(spec)):
                self.problem("an import map with local paths cannot be bundled; use <script type=\"module\"> imports")
            return m.group(0)
        if typ in JS_TYPES:
            return "<script" + rattrs + ">" + self.js(body, "").replace("</script", "<\\/script") + "</script>"
        return "<script" + rattrs + ">" + self._loose(body, "data block") + "</script>"

    def bundle(self) -> str:
        page = self.text(self.index.name)
        out = _HTML.sub(lambda m: self._dispatch(m), page)
        if self.has_importmap and self.modules:
            self.problem("the page has its own import map next to local ES modules; the bundle needs to own it")
        if self.problems:
            raise ReviewError(
                "cannot bundle this prototype for a password-gated here.now board (its assets would be blocked "
                "from the sandboxed frame). Fix and retry:\n - " + "\n - ".join(self.problems[:12])
                + (f"\n - ... and {len(self.problems) - 12} more" if len(self.problems) > 12 else ""))
        pre = []
        if self.modules:
            pre.append('<script type="importmap">' + _json_safe({"imports": self.modules}) + "</script>")
        if self.runtime:
            pre.append("<script>window.__MR_A=" + _json_safe(self.runtime) + ";" + _HELPER + "</script>")
        if pre:
            out = _inject_head(out, "".join(pre))
        if len(out.encode("utf-8")) > MAX_BUNDLE_BYTES:
            raise ReviewError(f"prototype bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MB")
        return out

    def _dispatch(self, m: "re.Match[str]") -> str:
        if m.group("raw"):
            return self._raw(m)
        if m.group("tag"):
            return self._tag(m)
        return m.group(0)


def _unq(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return htmllib.unescape(v)


def _close_safe(css: str) -> str:
    return re.sub(r"</(style)", r"<\\/\1", css, flags=re.I)


def _strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _inject_head(page: str, prelude: str) -> str:
    """After <meta charset> when it is there (its encoding hint must stay in the first bytes), else at the top of <head>."""
    for pat in (r"<meta\b[^>]*\bcharset[^>]*>", r"<head\b[^>]*>", r"<html\b[^>]*>", r"\s*<!doctype[^>]*>"):
        m = re.search(pat, page, re.I)
        if m:
            return page[:m.end()] + prelude + page[m.end():]
    return prelude + page


def bundle_html(index: Path) -> str:
    """The prototype's index.html as one self-contained document. Raises ReviewError naming every problem."""
    return Bundler(index).bundle()


def bundle_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """A prototype round spec with its page replaced by the bundled HTML (and its assets/ left out of the round).
    Any other kind of round is returned unchanged: only prototypes are bundled."""
    if spec.get("kind") != "prototype":
        return spec
    pages = []
    for page in spec["pages"]:
        pages.append(dict(page, html=bundle_html(page["src"]), bundled=True))
    return dict(spec, pages=pages)
