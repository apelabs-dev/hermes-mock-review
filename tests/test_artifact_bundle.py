"""The artifact bundler: bundle.py's engine on an artifact block's `src` page.

Two prototype-era tests for local ES modules are not ported: an artifact page may not load a script from a data: URL,
so module imports are refused by design.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from mockreview import api, bundle, config, store
from mockreview.store import ReviewError

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
SVG = "<svg xmlns='http://www.w3.org/2000/svg' width='4' height='4'/>"


def write(root: Path, rel: str, text="x", binary=False):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if binary else text.encode("utf-8"))
    return p


def page(root: Path, body: str, head: str = "") -> Path:
    return write(root, "index.html", f'<!doctype html><html><head><meta charset="utf-8">{head}</head><body>{body}</body></html>')


def decode(uri: str) -> bytes:
    assert uri.startswith("data:") and ";base64," in uri
    return base64.b64decode(uri.split(";base64,", 1)[1])


def uris(html: str):
    return re.findall(r"data:[a-z0-9.+/-]+(?:;charset=utf-8)?;base64,[A-Za-z0-9+/=]+", html)


def no_local_refs(html: str):
    # no attribute or CSS url() still points at a file next to the page (runtime-map keys and prose are not references)
    assert not re.search(r"""(?:src|href|poster|srcset)=["'](?!data:|https?:|#)[^"']+\.(?:css|js|png|svg|woff2?|mp4|txt)""", html)
    assert not re.search(r"""url\((?:&quot;|["']?)(?!data:|https?:)[^)]*\.(?:png|svg|woff2?)""", html)


@pytest.fixture
def proj(tmp_path):
    return tmp_path / "proto"


# ---------------------------------------------------------------- CSS: nested url(), @import, fonts

def test_nested_css_import_url_and_fonts(proj):
    write(proj, "assets/img/logo.svg", SVG)
    write(proj, "assets/fonts/a.woff2", b"WOFF2-BYTES", binary=True)
    write(proj, "assets/fonts/a.woff", b"WOFF-BYTES", binary=True)
    write(proj, "assets/css/base.css", ".b{background:url(../img/logo.svg#frag)}\n@charset \"utf-8\";")
    write(proj, "assets/css/main.css",
          "@import url(\"base.css\");\n@import 'print.css' print;\n"
          "@font-face{font-family:A;src:url('../fonts/a.woff2') format('woff2'),url(../fonts/a.woff) format('woff')}\n"
          "/* url(missing-in-comment.png) */ .c{cursor:url(../img/logo.svg), auto;"
          "background-image:image-set(\"../img/logo.svg\" 1x)}\n")
    write(proj, "assets/css/print.css", "body{color:#000}")
    page(proj, "<p class=b>hi</p>", '<link rel="stylesheet" href="assets/css/main.css">')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    no_local_refs(html)
    assert "<link" not in html and "@import" not in html and "missing-in-comment" not in html
    assert "@media print{body{color:#000}}" in html
    assert b"WOFF2-BYTES" in b"".join(decode(u) for u in uris(html) if u.startswith("data:font/woff2"))
    assert b"WOFF-BYTES" in b"".join(decode(u) for u in uris(html) if u.startswith("data:font/woff;"))
    svg = [u for u in uris(html) if u.startswith("data:image/svg+xml")]
    assert len(svg) >= 3 and all(decode(u).decode() == SVG for u in svg)
    assert re.search(r'url\("data:image/svg\+xml;base64,[A-Za-z0-9+/=]+#frag"\)', html)      # fragment kept
    assert '"data:image/svg+xml' in html.split("image-set(")[1][:20]                         # image-set string form


def test_css_import_cycle_and_layer_and_non_css_fail(proj):
    write(proj, "a.css", "@import 'b.css';")
    write(proj, "b.css", "@import 'a.css';")
    write(proj, "c.css", "@import 'a.css' layer(x);")
    write(proj, "d.css", "@import 'logo.svg';")
    write(proj, "logo.svg", SVG)
    for css, msg in (("a.css", "circular"), ("c.css", "layer"), ("d.css", "not a .css")):
        page(proj, "", f'<link rel="stylesheet" href="{css}">')
        with pytest.raises(ReviewError, match=msg):
            bundle.bundle_artifact_page(proj / "index.html", "block")


def test_html_attributes_inline_style_srcset_and_icons(proj):
    write(proj, "assets/logo.svg", SVG)
    write(proj, "assets/poster.png", b"POSTER", binary=True)
    write(proj, "assets/clip.mp4", b"VID", binary=True)
    write(proj, "assets/doc.txt", "notes")
    page(proj,
         '<img src="assets/logo.svg" srcset="assets/logo.svg 1x, assets/logo.svg 2x" alt="assets/logo.svg">'
         '<div style="background:url(assets/logo.svg)" data-bg="assets/poster.png"></div>'
         '<video src="assets/clip.mp4" poster="assets/poster.png"></video>'
         '<a href="assets/doc.txt">doc</a><a href="https://example.test/x.css">remote</a><a href="#top">top</a>',
         '<link rel="icon" href="assets/logo.svg"><link rel="preload" href="assets/logo.svg" as="image">'
         '<link rel="stylesheet" href="https://example.test/x.css">')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    no_local_refs(html)
    assert 'rel="preload"' not in html                                   # a hint for a file that is now inline
    assert 'href="https://example.test/x.css"' in html and 'href="#top"' in html
    assert 'alt="assets/logo.svg"' in html                               # prose attributes are left alone
    assert re.search(r'srcset="data:image/svg\+xml;base64,[A-Za-z0-9+/=]+ 1x, data:image/svg\+xml', html)
    assert decode(re.search(r'poster="([^"]+)"', html).group(1)) == b"POSTER"
    assert decode(re.search(r'data-bg="([^"]+)"', html).group(1)) == b"POSTER"
    assert decode(re.search(r'<a href="(data:[^"]+)"', html).group(1)) == b"notes"


def test_remote_data_anchor_and_scheme_refs_are_untouched(proj):
    page(proj, '<img src="data:image/gif;base64,R0lGOA==" srcset="data:image/gif;base64,R0lGOA== 1x">'
               '<img src="https://example.test/a.png"><img src="//cdn.example.test/a.png"><a href="mailto:a@example.test">m</a>',
         '<script src="https://example.test/lib.js"></script><link rel="stylesheet" href="http://example.test/a.css">')
    src = (proj / "index.html").read_text(encoding="utf-8")
    assert bundle.bundle_artifact_page(proj / "index.html", "block") == src


# ---------------------------------------------------------------- failures are explicit

def test_missing_assets_fail_and_name_every_one(proj):
    write(proj, "assets/main.css", "body{background:url(img/gone.png)}")
    page(proj, '<img src="assets/missing.png"><img srcset="assets/m2.png 1x">',
         '<link rel="stylesheet" href="assets/main.css"><link rel="stylesheet" href="assets/nope.css">'
         '<script src="assets/nope.js"></script>')
    with pytest.raises(ReviewError) as ei:
        bundle.bundle_artifact_page(proj / "index.html", "block")
    msg = ei.value.message
    for name in ("assets/missing.png", "assets/m2.png", "assets/nope.css", "assets/nope.js", "img/gone.png"):
        assert name in msg
    assert msg.count("file not found") == 5 and ei.value.status == 400


def test_script_string_pointing_into_a_known_folder_must_exist(proj):
    write(proj, "assets/ok.png", b"P", binary=True)
    page(proj, '<script>var a = "assets/ok.png", b = "assets/gone.png";</script>')
    with pytest.raises(ReviewError, match=r"assets/gone\.png .*file not found"):
        bundle.bundle_artifact_page(proj / "index.html", "block")


@pytest.mark.parametrize("code,named", [
    ("img.src = 'missing.png';", "missing.png"),                                  # a bare file name
    ("img.src = 'lost/poster.png';", "lost/poster.png"),                         # a folder that does not exist
    ('const frames = ["a/b.webp"];', "a/b.webp"),
    ("x = ['../outside.png'];", "../outside.png"),
])
def test_script_string_naming_a_missing_asset_is_an_error_not_a_skip(proj, code, named):
    page(proj, f"<script>{code}</script>")
    with pytest.raises(ReviewError) as ei:
        bundle.bundle_artifact_page(proj / "index.html", "block")
    assert named in ei.value.message and "(a script string)" in ei.value.message
    assert ("leaves the artifact's folder" if named.startswith("..") else "file not found") in ei.value.message


def test_script_strings_that_are_not_missing_assets_stay_as_written(proj):
    code = ("var u = 'https://example.test/lib/a.png', v = '//cdn.example.test/b.png', w = 'data:image/png;base64,AAAA';"
            "var prose = 'see notes about poster.png here', nope = 'index.html', obj = {'x.png': 1}, k = o['y.png'];"
            "a.download = 'chart.png'; b.setAttribute('download', 'report.png'); var f = 'export.json', t = 'notes.txt';"
            "var r = `api/items/${id}`, h = '#top', e = 'poster.png.bak';")
    page(proj, f"<script>{code}</script>")
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    assert code in html and "__mrA" not in html


@pytest.mark.parametrize("literal,exists", [
    ("mr-module/missing.png", False),
    ("mr-module/pixel.png", True),
])
def test_quoted_mr_module_literals_stay_exactly_as_written(proj, literal, exists):
    if exists:
        write(proj, literal, b"PNG", binary=True)
    page(proj, f"<script>img.src = '{literal}';</script>")
    before = (proj / "index.html").read_bytes()
    html, used = bundle.bundle_artifact(proj / "index.html", "block")
    assert html.encode("utf-8") == before
    assert "window.__MR_A" not in html and "__mrA" not in html
    assert used == 0


def test_a_quoted_assets_literal_that_names_no_file_still_refuses(proj):
    page(proj, "<script>img.src = 'assets/missing.png';</script>")
    with pytest.raises(ReviewError) as ei:
        bundle.bundle_artifact_page(proj / "index.html", "block")
    assert ei.value.status == 400 and "(a script string)" in ei.value.message


def test_a_data_attribute_naming_a_missing_asset_is_an_error_too(proj):
    page(proj, '<div data-image="gone/poster.png"></div>')
    with pytest.raises(ReviewError, match=r"gone/poster\.png .*file not found"):
        bundle.bundle_artifact_page(proj / "index.html", "block")
    page(proj, '<div data-note="gone/poster.png is where it lives" data-file="export.json"></div>')
    assert bundle.bundle_artifact_page(proj / "index.html", "block")


@pytest.mark.parametrize("literal,msg", [
    ("`nowhere/poster-${n}.webp`", r"nowhere/poster-\$\{\.\.\.\}.*folder nowhere/ not found"),
    ("`assets/poster-${n}.webp`", r"no local \.webp file under assets/"),            # a folder with nothing matching
])
def test_template_literal_with_a_missing_folder_or_no_files_is_an_error(proj, literal, msg):
    write(proj, "assets/other.png", b"P", binary=True)
    page(proj, f"<script>img.src = {literal};</script>")
    with pytest.raises(ReviewError, match=msg):
        bundle.bundle_artifact_page(proj / "index.html", "block")


def test_template_literals_that_are_not_asset_paths_are_left_alone(proj):
    code = "var a = `api/items/${id}`, b = `Loading/${n} of ${m}`, c = `${base}/x.png`;"
    page(proj, f"<script>{code}</script>")
    assert code in bundle.bundle_artifact_page(proj / "index.html", "block")


def test_srcset_with_data_and_local_candidates_bundles_every_local_one(proj):
    write(proj, "assets/a.png", b"AAA", binary=True)
    write(proj, "assets/b.png", b"BBB", binary=True)
    page(proj, '<img srcset="data:image/gif;base64,R0lGOA== 1x, assets/a.png 2x, https://example.test/c.png 3x, assets/b.png">'
               '<picture><source srcset="assets/a.png 480w,assets/b.png 800w" sizes="100vw"></picture>')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    no_local_refs(html)
    first = re.search(r'<img srcset="([^"]+)"', html).group(1)
    assert first.startswith("data:image/gif;base64,R0lGOA== 1x, data:image/png;base64,")
    assert "https://example.test/c.png 3x" in first and "assets/" not in first
    assert [decode(u) for u in uris(first)[1:]] == [b"AAA", b"BBB"]
    second = re.search(r'<source srcset="([^"]+)"', html).group(1)
    assert second.count("data:image/png") == 2 and second.endswith(" 800w") and " 480w, " in second


def test_srcset_with_a_missing_local_candidate_names_it_even_next_to_data(proj):
    write(proj, "assets/a.png", b"AAA", binary=True)
    page(proj, '<img srcset="data:image/gif;base64,R0lGOA== 1x, assets/gone.png 2x, assets/a.png 3x">')
    with pytest.raises(ReviewError, match=r"assets/gone\.png: file not found"):
        bundle.bundle_artifact_page(proj / "index.html", "block")


def test_path_traversal_root_absolute_and_nul_are_refused(proj, tmp_path):
    write(tmp_path, "secret.png", b"SECRET", binary=True)
    page(proj, '<img src="../secret.png"><img src="/etc/hosts.png"><img src="assets/../../secret.png">')
    with pytest.raises(ReviewError) as ei:
        bundle.bundle_artifact_page(proj / "index.html", "block")
    assert ei.value.message.count("leaves the artifact's folder") == 2
    assert "root-absolute" in ei.value.message
    assert "SECRET" not in ei.value.message


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_file_and_folder_are_refused(proj, tmp_path):
    write(tmp_path, "outside/real.png", b"SECRET", binary=True)
    (proj / "assets").mkdir(parents=True)
    os.symlink(tmp_path / "outside" / "real.png", proj / "assets" / "link.png")
    os.symlink(tmp_path / "outside", proj / "linked")
    page(proj, '<img src="assets/link.png"><img src="linked/real.png">')
    with pytest.raises(ReviewError) as ei:
        bundle.bundle_artifact_page(proj / "index.html", "block")
    assert ei.value.message.count("symbolic link") == 2


def test_size_bounds(proj, monkeypatch):
    write(proj, "assets/big.png", b"B" * 5000, binary=True)
    write(proj, "assets/also.png", b"C" * 5000, binary=True)
    page(proj, '<img src="assets/big.png">')
    monkeypatch.setattr(bundle, "MAX_ASSET_BYTES", 4000)
    with pytest.raises(ReviewError, match="too big to inline"):
        bundle.bundle_artifact_page(proj / "index.html", "block")
    monkeypatch.setattr(bundle, "MAX_ASSET_BYTES", 10_000)
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", 8000)
    page(proj, '<img src="assets/big.png"><img src="assets/also.png">')
    with pytest.raises(ReviewError, match="larger than 8000 bytes once inlined"):
        bundle.bundle_artifact_page(proj / "index.html", "block")
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", 10 ** 9)
    monkeypatch.setattr(bundle, "MAX_BUNDLE_BYTES", 9000)       # the same asset used four times blows the output cap
    page(proj, '<img src="assets/big.png">' * 4)
    with pytest.raises(ReviewError, match="larger than .* MB"):
        bundle.bundle_artifact_page(proj / "index.html", "block")


@pytest.mark.parametrize("head,body,msg", [
    ("", '<iframe src="inner.html"></iframe>', "local frame"),
    ("", '<a href="other.html">next</a>', "another local page"),
    ('<link rel="manifest" href="site.webmanifest">', "", "manifest"),
    ('<base href="/x/">', "", "<base href>"),
    ('<link rel="alternate stylesheet" href="a.css">', "", "alternate"),
    ('<meta http-equiv="Content-Security-Policy" content="img-src \'self\'">', "", "Content-Security-Policy"),
    ("", '<img src="assets/model.xyz">', "unsupported file type"),
    ("", '<svg><use href="sprite.svg#a"></use></svg>', "SVG sprite"),
    ("", '<script>new Worker("w.js")</script>', "Worker"),
    ('<script type="importmap">{"imports":{"x":"./x.js"}}</script>', "", "import map"),
    ("", '<script type="text/babel" src="app.jsx"></script>', "only JavaScript"),
])
def test_unsupported_references_fail_clearly(proj, head, body, msg):
    for name in ("inner.html", "other.html", "site.webmanifest", "a.css", "w.js", "x.js", "sprite.svg", "app.jsx"):
        write(proj, name, "x")
    write(proj, "assets/model.xyz", "x")
    page(proj, body, head)
    with pytest.raises(ReviewError, match=msg):
        bundle.bundle_artifact_page(proj / "index.html", "block")


# ---------------------------------------------------------------- scripts: runtime map, template literals

def test_runtime_map_template_literals_lazy_script_each_asset_once(proj):
    write(proj, "assets/shots/poster-a-1.webp", b"W" * 3000, binary=True)
    write(proj, "assets/shots/poster-b-2.webp", b"V" * 3000, binary=True)
    write(proj, "assets/shots/notes.txt", "skip me: the template ends in .webp")
    write(proj, "assets/data.json", '{"k": "assets/shots/poster-a-1.webp"}')
    write(proj, "assets/lazy.js", 'window.lazy = "assets/shots/poster-b-2.webp"; window.t = `assets/shots/poster-${1}.webp`;')
    write(proj, "assets/page.js", 'var s=document.createElement("script"); s.src="assets/lazy.js"; '
                                  'var a=`assets/shots/poster-${l}-${f(`x${n}`)}.webp`, b="assets/shots/poster-a-1.webp", '
                                  'c="assets/shots/poster-a-1.webp", d={"assets/data.json": 1}; fetch("assets/data.json");')
    page(proj, '<script src="assets/page.js" defer></script>')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    no_local_refs(html)
    mapjs = re.search(r"window\.__MR_A=(\{.*?\});\(function", html, re.S).group(1)
    table = json.loads(mapjs)
    assert set(table) == {"assets/shots/poster-a-1.webp", "assets/shots/poster-b-2.webp", "assets/lazy.js", "assets/data.json"}
    assert decode(table["assets/shots/poster-a-1.webp"]) == b"W" * 3000
    # the megabytes live once: the image is referenced three times in scripts and appears once in the whole bundle
    assert html.count(table["assets/shots/poster-a-1.webp"]) == 1
    page_js = re.search(r"<script defer>(.*?)</script>", html, re.S).group(1)
    assert page_js.count("window.__mrA(") == 6 - 1                    # d's object key is left alone
    assert "window.__mrA(`assets/shots/poster-${l}-${f(`x${n}`)}.webp`)" in page_js
    assert '{"assets/data.json": 1}' in page_js                         # object key untouched
    lazy = decode(table["assets/lazy.js"]).decode()
    assert "window.__mrA(`assets/shots/poster-${1}.webp`)" in lazy and 'window.__mrA("assets/shots/poster-b-2.webp")' in lazy
    assert 'window.__mrA("assets/shots/poster-a-1.webp")' in page_js


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_runtime_helper_resolves_paths_like_the_browser(proj, tmp_path):
    write(proj, "assets/p-1.png", b"ONE", binary=True)
    page(proj, '<script>var u = "assets/p-1.png";</script>')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    helper = re.search(r"<script>(window\.__MR_A=.*?)</script>", html, re.S).group(1)
    js = ("var window={};" + helper + "console.log(JSON.stringify(["
          "window.__mrA('./assets/p-1.png?v=2#x').slice(0,16),window.__mrA('assets/../assets/p-1.png').slice(0,16),"
          "window.__mrA('assets/other.png')]))")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=30).stdout
    assert json.loads(out) == ["data:image/png;b", "data:image/png;b", "assets/other.png"]


def test_literal_contexts_that_must_not_become_calls(proj):
    write(proj, "assets/a.png", b"A", binary=True)
    write(proj, "assets/b.png", b"B", binary=True)
    page(proj, '<script>var m = {"assets/a.png": 1, ok: "assets/b.png"}; var i = m["assets/a.png"]; '
               'switch (x) { case "assets/a.png": break; } var arr = ["assets/b.png"]; '
               'var t = c ? "assets/a.png" : "assets/b.png"; var h = \'<img src="assets/a.png">\'; '
               'el.style.background = "url(assets/b.png)";</script>')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    body = re.search(r"<script>var m.*?</script>", html, re.S).group(0)
    assert '{"assets/a.png": 1,' in body and 'm["assets/a.png"]' in body and 'case "assets/a.png"' in body
    assert 'ok: window.__mrA("assets/b.png")' in body and '[window.__mrA("assets/b.png")]' in body
    assert 'c ? window.__mrA("assets/a.png") : window.__mrA("assets/b.png")' in body
    assert "data:image/png;base64" in body                              # substring refs (markup, url()) are inlined


def test_script_attributes_survive_and_integrity_is_dropped(proj):
    write(proj, "a.js", "window.a=1</script><b>")
    page(proj, '<script src="a.js" defer async integrity="sha384-x" crossorigin id="s1" data-k="v"></script>')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    tag = re.search(r"<script[^>]*>", html).group(0)
    assert all(w in tag for w in ("defer", "async", 'id="s1"', 'data-k="v"')) and "integrity" not in tag and "src" not in tag
    body = re.search(r"<script[^>]*>(.*?)</script>", html, re.S).group(1)
    assert body == "window.a=1<\\/script><b>"                  # the artifact CSP allows inline script only
    assert "</script><b>" not in html                      # the dangerous text lives only inside base64


def test_a_leading_utf8_bom_is_stripped_from_inlined_css_and_js(proj):
    bom = b"\xef\xbb\xbf"
    write(proj, "a.css", bom + b".a{color:red}", binary=True)
    write(proj, "a.js", bom + b"window.a=1;", binary=True)
    page(proj, '<script src="a.js"></script>', '<link rel="stylesheet" href="a.css">')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    style = re.search(r"<style[^>]*>(.*?)</style>", html, re.S).group(1)
    assert ".a{color:red}" in style and "﻿" not in style
    js = re.search(r"<script[^>]*>(.*?)</script>", html, re.S).group(1)
    assert js == "window.a=1;"                                   # intact, and no BOM left at the start


def test_inline_script_cannot_close_itself_and_style_text_is_safe(proj):
    write(proj, "x.css", 'a::after{content:"</style><b>"}')
    page(proj, "", '<link rel="stylesheet" href="x.css">')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    assert html.count("</style>") == 1


def test_prelude_goes_after_meta_charset_and_without_head(proj):
    write(proj, "p.png", b"P", binary=True)
    page(proj, '<script>var u="p.png";</script>')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    assert html.index("charset") < html.index("__MR_A") < html.index("<body>")
    write(proj, "index.html", '<!doctype html><script>var u="p.png";</script>')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    assert html.startswith("<!doctype html><script>window.__MR_A=")


# ---------------------------------------------------------------- Pillow is optional

def _fake_pillow(monkeypatch, out=b"WEBPDATA", fail=False, animated=False):
    class Img:
        mode = "RGB"
        info = {}
        is_animated = animated

        def save(self, buf, fmt, **kw):
            assert fmt == "WEBP" and kw["quality"] == 85
            if fail:
                raise OSError("boom")
            buf.write(out)

        def convert(self, mode):
            return self
    mod = types.SimpleNamespace(open=lambda f: Img())
    monkeypatch.setattr(bundle, "_load_pillow", lambda: mod)


@pytest.mark.parametrize("name,mime", [("a.png", "image/png"), ("a.jpg", "image/jpeg"), ("a.jpeg", "image/jpeg")])
def test_big_still_becomes_webp_when_pillow_is_there(proj, monkeypatch, name, mime):
    _fake_pillow(monkeypatch)
    write(proj, name, b"S" * 400_000, binary=True)
    page(proj, f'<img src="{name}">')
    html = bundle.bundle_artifact_page(proj / "index.html", "block")
    uri = re.search(r'src="(data:[^"]+)"', html).group(1)
    assert uri.startswith("data:image/webp;base64,") and decode(uri) == b"WEBPDATA"


@pytest.mark.parametrize("kw,size", [({}, 100_000), ({"fail": True}, 400_000), ({"animated": True}, 400_000),
                                     ({"out": b"Z" * 500_000}, 400_000)])
def test_still_is_inlined_as_is_when_small_or_unconvertible(proj, monkeypatch, kw, size):
    _fake_pillow(monkeypatch, **kw)
    write(proj, "a.png", b"S" * size, binary=True)
    page(proj, '<img src="a.png">')
    uri = re.search(r'src="(data:[^"]+)"', bundle.bundle_artifact_page(proj / "index.html", "block")).group(1)
    assert uri.startswith("data:image/png;base64,") and decode(uri) == b"S" * size


def test_without_pillow_big_stills_are_inlined_as_is(proj, monkeypatch):
    monkeypatch.setattr(bundle, "_load_pillow", lambda: None)
    write(proj, "a.png", b"S" * 400_000, binary=True)
    page(proj, '<img src="a.png">')
    assert 'src="data:image/png;base64,' in bundle.bundle_artifact_page(proj / "index.html", "block")


def test_bundle_module_has_no_third_party_import():
    src = Path(bundle.__file__).read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import) (\w+)", src, re.M))
    assert imports <= {"__future__", "base64", "html", "importlib", "io", "json", "posixpath", "re", "urllib", "pathlib",
                       "typing"}
    assert "PIL" in src and 'import_module("PIL.Image")' in src       # loaded lazily, only when installed


# ---------------------------------------------------------------- artifact pages (always bundled, any kind of board)

def _art(proj, body, head=""):
    return page(proj, body, head)


def test_artifact_bundle_inlines_media_and_scripts_and_survives_a_closing_script_tag(proj):
    write(proj, "assets/a.wav", b"RIFFxxxxWAVE", binary=True)
    write(proj, "app.js", "var s = '</script><b>'; window.ok = 1;")
    _art(proj, '<audio src="assets/a.wav"></audio><script src="app.js"></script>')
    out = bundle.bundle_artifact_page(proj / "index.html", "film")
    assert "data:audio/wav;base64," in out and "assets/a.wav" not in out
    assert '<script src="data:' not in out and "window.ok = 1" in out          # script-src has no data: in the artifact CSP
    assert "</script><b>" not in out                                           # the inlined code cannot end its own tag
    no_local_refs(out)


def test_artifact_bundle_refuses_local_es_module_imports(proj):
    write(proj, "m.js", "export const a = 1;")
    _art(proj, '<script type="module">import {a} from "./m.js"; console.log(a)</script>')
    with pytest.raises(ReviewError, match="artifact film") as e:
        bundle.bundle_artifact_page(proj / "index.html", "film")
    assert "m.js" in e.value.message


def test_artifact_missing_asset_fails_naming_block_and_asset(proj):
    _art(proj, '<img src="assets/nope.png">')
    with pytest.raises(ReviewError) as e:
        bundle.bundle_artifact_page(proj / "index.html", "film")
    assert "artifact film" in e.value.message and "assets/nope.png" in e.value.message
    assert "prototype" not in e.value.message


def test_artifact_oversized_asset_fails_naming_the_asset(proj, monkeypatch):
    write(proj, "assets/big.mp4", b"B" * 5000, binary=True)
    _art(proj, '<video src="assets/big.mp4"></video>')
    monkeypatch.setattr(bundle, "MAX_ASSET_BYTES", 4000)
    with pytest.raises(ReviewError, match="artifact film: assets/big.mp4 is larger than"):
        bundle.bundle_artifact_page(proj / "index.html", "film")


def test_artifact_budget_is_shared_by_a_rounds_artifacts(tmp_path, monkeypatch):
    for n in ("one", "two"):
        write(tmp_path / n, "assets/m.mp4", b"M" * 5000, binary=True)
        write(tmp_path / n, "p.html", '<video src="assets/m.mp4"></video>')
    spec = {"kind": "decisions", "decisions": {"blocks": [
        {"type": "artifact", "id": "one", "src": str(tmp_path / "one" / "p.html")},
        {"type": "artifact", "id": "two", "src": str(tmp_path / "two" / "p.html")}]}}
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", 8000)         # each fits alone, both together do not
    bundle.bundle_artifacts({"kind": "decisions", "decisions": {"blocks": spec["decisions"]["blocks"][:1]}})
    with pytest.raises(ReviewError, match=r"artifact two: .*larger than 8000 bytes once inlined \(asset assets/m.mp4\)"):
        bundle.bundle_artifacts(spec)


def test_a_repeated_asset_counts_every_emitted_copy_against_the_round_budget(env, tmp_path, monkeypatch):
    """A 1 MB clip referenced ten times emits ~14 MB of data: URIs; the round must count all ten, not one file."""
    cap = 6 * 1024 * 1024
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", cap)
    monkeypatch.setattr(store, "MAX_ASSETS_BYTES", cap)
    art = tmp_path / "art"
    write(art, "assets/clip.mp4", b"M" * (1024 * 1024), binary=True)
    f = tmp_path / "dec" / "decisions.json"
    f.parent.mkdir()

    def build(n):
        page(art, '<video src="assets/clip.mp4"></video>' * n)
        f.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "film", "src": str(art / "index.html")}]}),
                     encoding="utf-8")
        return store.create_review("t", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(f)))

    build(3)                                  # ~4 MB of data: URIs: inside the round's 6 MB budget
    with pytest.raises(ReviewError, match=r"artifact film: .*larger than 6 MB"):
        build(10)                             # the old unique-asset count saw 1 MB and let this through


def test_a_nested_stylesheet_import_counts_each_emitted_uri_once(env, tmp_path, monkeypatch):
    """A 1 MiB font reached through <link rel=stylesheet> -> @import -> @import is emitted (and counted) once: the
    stylesheet text that carries the data: URI must not be counted again on top of the URI inside it."""
    cap = 3 * 1024 * 1024
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", cap)
    monkeypatch.setattr(store, "MAX_ASSETS_BYTES", cap)
    art = tmp_path / "art"
    write(art, "assets/font.woff2", b"F" * (1024 * 1024), binary=True)
    write(art, "outer.css", '@import "mid.css";')
    write(art, "mid.css", '@import "inner.css";')
    write(art, "inner.css", '@font-face{font-family:x;src:url("assets/font.woff2")}')
    page(art, "<p>font</p>", '<link rel="stylesheet" href="outer.css">')
    f = tmp_path / "dec" / "decisions.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "font", "src": str(art / "index.html")}]}),
                 encoding="utf-8")
    # 1.4 MB of data: URI through three nesting levels: inside the 3 MB budget, and no level counted twice
    store.create_review("t", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(f)))
    out = bundle.bundle_artifacts(store.parse_round_spec(decisions=str(f)))
    html = out["decisions"]["blocks"][0]["html"]
    assert out["inlined"] == sum(len(u) for u in uris(html))


def test_a_sub_megabyte_cap_is_reported_in_bytes_never_as_0_MB(proj, monkeypatch):
    write(proj, "assets/big.png", b"B" * 5000, binary=True)
    write(proj, "assets/also.png", b"C" * 5000, binary=True)
    page(proj, '<img src="assets/big.png"><img src="assets/also.png">')
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", 8000)
    with pytest.raises(ReviewError) as e:
        bundle.bundle_artifact_page(proj / "index.html", "block")
    assert e.value.message == "artifact block: the artifact's assets are larger than 8000 bytes once inlined (asset assets/also.png)"
    assert "0 MB" not in e.value.message
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", 3 * 1024 * 1024 + 7)    # a whole-megabyte message stays in MB
    assert store.size_label(bundle.MAX_ASSETS_BYTES) == "3 MB" and store.size_label(50 * 1024 * 1024) == "50 MB"
    assert store.size_label(8000) == "8000 bytes"


def test_bundle_artifacts_replaces_src_with_html_and_leaves_other_specs_alone(tmp_path):
    write(tmp_path, "p.html", "<p>hi</p>")
    spec = {"kind": "decisions", "decisions": {"blocks": [{"type": "artifact", "id": "film", "src": str(tmp_path / "p.html")},
                                                          {"type": "artifact", "id": "inl", "html": "<p>x</p>"}]}}
    out = bundle.bundle_artifacts(spec)
    b0, b1 = out["decisions"]["blocks"]
    assert "src" not in b0 and "<p>hi</p>" in b0["html"] and b1 == {"type": "artifact", "id": "inl", "html": "<p>x</p>"}
    proto = {"kind": "prototype", "decisions": None}
    assert bundle.bundle_artifacts(proto) is proto


def test_a_huge_data_uri_in_an_artifact_is_refused_without_taking_the_server_down(tmp_path):
    """50 MB inline is over the 2 MB html cap; a 50 MB asset next to a src page is over the 25 MB asset cap."""
    huge = "data:image/png;base64," + "A" * (50 * 1024 * 1024)
    with pytest.raises(ReviewError, match="larger than 2 MB"):
        store.validate_decisions({"blocks": [{"type": "artifact", "id": "film", "html": f'<img src="{huge}">'}]})
    write(tmp_path, "assets/big.png", b"\0" * (26 * 1024 * 1024), binary=True)
    write(tmp_path, "p.html", '<img src="assets/big.png">')
    with pytest.raises(ReviewError, match="artifact film: assets/big.png is larger than 25 MB"):
        bundle.bundle_artifacts({"kind": "decisions", "decisions": {"blocks": [
            {"type": "artifact", "id": "film", "src": str(tmp_path / "p.html")}]}})
    # a src page that is itself 50 MB
    write(tmp_path, "huge.html", b"x" * (50 * 1024 * 1024), binary=True)
    with pytest.raises(ReviewError, match="larger than 2 MB"):
        store.validate_decisions({"blocks": [{"type": "artifact", "id": "film", "src": str(tmp_path / "huge.html")}]})


def test_api_bundles_artifact_src_on_a_plain_tailscale_board(env, tmp_path):
    write(tmp_path / "art", "assets/a.png", b"\x89PNG\r\n\x1a\nxx", binary=True)
    write(tmp_path / "art", "p.html", '<img src="assets/a.png">')
    dec = tmp_path / "dec" / "decisions.json"
    dec.parent.mkdir()
    dec.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "film", "src": str(tmp_path / "art" / "p.html")}]}), encoding="utf-8")
    res = api.create("t", ORIGIN, mode="tailscale", decisions=str(dec), wake=False, off_brand="demo")
    served = (config.data_root() / res["review_id"] / "rounds/1/a/blocks/film.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in served and "assets/a.png" not in served


def test_an_artifact_counts_as_interactive():
    art = {"decisions": {"blocks": [{"type": "artifact", "id": "film", "html": "<p>static</p>"}]}}
    assert api._interactive_hint(art, {"interactive_elements": True}) == {}
    static = {"decisions": {"blocks": [{"type": "mock", "id": "m", "html": "<p>static</p>"}]}}
    assert api._interactive_hint(static, {"interactive_elements": True}) == {"hint": api.INTERACTIVE_HINT}
