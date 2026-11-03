"""Prototype bundling for password-gated here.now boards: bundle.py itself, and where api/cli/tools apply it."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_plugin
from mockreview import api, browsercheck, bundle, cli, config, herenow, store
from mockreview.store import ReviewError
from test_plugin import FakeCtx

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
    html = bundle.bundle_html(proj / "index.html")
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
            bundle.bundle_html(proj / "index.html")


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
    html = bundle.bundle_html(proj / "index.html")
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
    assert bundle.bundle_html(proj / "index.html") == src


# ---------------------------------------------------------------- failures are explicit

def test_missing_assets_fail_and_name_every_one(proj):
    write(proj, "assets/main.css", "body{background:url(img/gone.png)}")
    page(proj, '<img src="assets/missing.png"><img srcset="assets/m2.png 1x">',
         '<link rel="stylesheet" href="assets/main.css"><link rel="stylesheet" href="assets/nope.css">'
         '<script src="assets/nope.js"></script>')
    with pytest.raises(ReviewError) as ei:
        bundle.bundle_html(proj / "index.html")
    msg = ei.value.message
    for name in ("assets/missing.png", "assets/m2.png", "assets/nope.css", "assets/nope.js", "img/gone.png"):
        assert name in msg
    assert msg.count("file not found") == 5 and ei.value.status == 400


def test_script_string_pointing_into_a_known_folder_must_exist(proj):
    write(proj, "assets/ok.png", b"P", binary=True)
    page(proj, '<script>var a = "assets/ok.png", b = "assets/gone.png";</script>')
    with pytest.raises(ReviewError, match=r"assets/gone\.png .*file not found"):
        bundle.bundle_html(proj / "index.html")


@pytest.mark.parametrize("code,named", [
    ("img.src = 'missing.png';", "missing.png"),                                  # a bare file name
    ("img.src = 'lost/poster.png';", "lost/poster.png"),                         # a folder that does not exist
    ('const frames = ["a/b.webp"];', "a/b.webp"),
    ("x = ['../outside.png'];", "../outside.png"),
])
def test_script_string_naming_a_missing_asset_is_an_error_not_a_skip(proj, code, named):
    page(proj, f"<script>{code}</script>")
    with pytest.raises(ReviewError) as ei:
        bundle.bundle_html(proj / "index.html")
    assert named in ei.value.message and "(a script string)" in ei.value.message
    assert ("leaves the prototype's folder" if named.startswith("..") else "file not found") in ei.value.message


def test_script_strings_that_are_not_missing_assets_stay_as_written(proj):
    code = ("var u = 'https://example.test/lib/a.png', v = '//cdn.example.test/b.png', w = 'data:image/png;base64,AAAA';"
            "var prose = 'see notes about poster.png here', nope = 'index.html', obj = {'x.png': 1}, k = o['y.png'];"
            "a.download = 'chart.png'; b.setAttribute('download', 'report.png'); var f = 'export.json', t = 'notes.txt';"
            "var r = `api/items/${id}`, h = '#top', e = 'poster.png.bak';")
    page(proj, f"<script>{code}</script>")
    html = bundle.bundle_html(proj / "index.html")
    assert code in html and "__mrA" not in html


def test_a_data_attribute_naming_a_missing_asset_is_an_error_too(proj):
    page(proj, '<div data-image="gone/poster.png"></div>')
    with pytest.raises(ReviewError, match=r"gone/poster\.png .*file not found"):
        bundle.bundle_html(proj / "index.html")
    page(proj, '<div data-note="gone/poster.png is where it lives" data-file="export.json"></div>')
    assert bundle.bundle_html(proj / "index.html")


@pytest.mark.parametrize("literal,msg", [
    ("`nowhere/poster-${n}.webp`", r"nowhere/poster-\$\{\.\.\.\}.*folder nowhere/ not found"),
    ("`assets/poster-${n}.webp`", r"no local \.webp file under assets/"),            # a folder with nothing matching
])
def test_template_literal_with_a_missing_folder_or_no_files_is_an_error(proj, literal, msg):
    write(proj, "assets/other.png", b"P", binary=True)
    page(proj, f"<script>img.src = {literal};</script>")
    with pytest.raises(ReviewError, match=msg):
        bundle.bundle_html(proj / "index.html")


def test_template_literals_that_are_not_asset_paths_are_left_alone(proj):
    code = "var a = `api/items/${id}`, b = `Loading/${n} of ${m}`, c = `${base}/x.png`;"
    page(proj, f"<script>{code}</script>")
    assert code in bundle.bundle_html(proj / "index.html")


def test_srcset_with_data_and_local_candidates_bundles_every_local_one(proj):
    write(proj, "assets/a.png", b"AAA", binary=True)
    write(proj, "assets/b.png", b"BBB", binary=True)
    page(proj, '<img srcset="data:image/gif;base64,R0lGOA== 1x, assets/a.png 2x, https://example.test/c.png 3x, assets/b.png">'
               '<picture><source srcset="assets/a.png 480w,assets/b.png 800w" sizes="100vw"></picture>')
    html = bundle.bundle_html(proj / "index.html")
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
        bundle.bundle_html(proj / "index.html")


def test_path_traversal_root_absolute_and_nul_are_refused(proj, tmp_path):
    write(tmp_path, "secret.png", b"SECRET", binary=True)
    page(proj, '<img src="../secret.png"><img src="/etc/hosts.png"><img src="assets/../../secret.png">')
    with pytest.raises(ReviewError) as ei:
        bundle.bundle_html(proj / "index.html")
    assert ei.value.message.count("leaves the prototype's folder") == 2
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
        bundle.bundle_html(proj / "index.html")
    assert ei.value.message.count("symbolic link") == 2


def test_size_bounds(proj, monkeypatch):
    write(proj, "assets/big.png", b"B" * 5000, binary=True)
    write(proj, "assets/also.png", b"C" * 5000, binary=True)
    page(proj, '<img src="assets/big.png">')
    monkeypatch.setattr(bundle, "MAX_ASSET_BYTES", 4000)
    with pytest.raises(ReviewError, match="too big to inline"):
        bundle.bundle_html(proj / "index.html")
    monkeypatch.setattr(bundle, "MAX_ASSET_BYTES", 10_000)
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", 8000)
    page(proj, '<img src="assets/big.png"><img src="assets/also.png">')
    with pytest.raises(ReviewError, match="larger than .* MB once inlined"):
        bundle.bundle_html(proj / "index.html")
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", 10 ** 9)
    monkeypatch.setattr(bundle, "MAX_BUNDLE_BYTES", 9000)       # the same asset used four times blows the output cap
    page(proj, '<img src="assets/big.png">' * 4)
    with pytest.raises(ReviewError, match="larger than .* MB"):
        bundle.bundle_html(proj / "index.html")


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
        bundle.bundle_html(proj / "index.html")


# ---------------------------------------------------------------- scripts: runtime map, template literals, modules

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
    html = bundle.bundle_html(proj / "index.html")
    no_local_refs(html)
    mapjs = re.search(r"window\.__MR_A=(\{.*?\});\(function", html, re.S).group(1)
    table = json.loads(mapjs)
    assert set(table) == {"assets/shots/poster-a-1.webp", "assets/shots/poster-b-2.webp", "assets/lazy.js", "assets/data.json"}
    assert decode(table["assets/shots/poster-a-1.webp"]) == b"W" * 3000
    # the megabytes live once: the image is referenced three times in scripts and appears once in the whole bundle
    assert html.count(table["assets/shots/poster-a-1.webp"]) == 1
    page_js = decode(re.search(r'<script defer src="(data:[^"]+)"', html).group(1)).decode()
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
    html = bundle.bundle_html(proj / "index.html")
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
    html = bundle.bundle_html(proj / "index.html")
    body = re.search(r"<script>var m.*?</script>", html, re.S).group(0)
    assert '{"assets/a.png": 1,' in body and 'm["assets/a.png"]' in body and 'case "assets/a.png"' in body
    assert 'ok: window.__mrA("assets/b.png")' in body and '[window.__mrA("assets/b.png")]' in body
    assert 'c ? window.__mrA("assets/a.png") : window.__mrA("assets/b.png")' in body
    assert "data:image/png;base64" in body                              # substring refs (markup, url()) are inlined


def test_es_modules_become_an_import_map_and_keep_working(proj):
    write(proj, "js/main.js", 'import { a } from "./lib/a.js"; import "./side.js"; const m = await import("./lib/b.js");'
                              'export * from "./lib/a.js"; console.log(a, "assets/x.png");')
    write(proj, "js/lib/a.js", 'import { b } from "./b.js"; export const a = 1;')
    write(proj, "js/lib/b.js", 'import { a } from "./a.js"; export const b = 2;')      # a cycle: fine, keys not bytes
    write(proj, "js/side.js", "window.side = 1;")
    write(proj, "assets/x.png", b"X", binary=True)
    page(proj, '<script type="module" src="js/main.js"></script><script type="module">import "./js/side.js";</script>')
    html = bundle.bundle_html(proj / "index.html")
    imap = json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1))["imports"]
    assert set(imap) == {"mr-module/js/lib/a.js", "mr-module/js/lib/b.js", "mr-module/js/side.js"}
    assert html.index('type="importmap"') < html.index('type="module"')              # before any module script
    a = decode(imap["mr-module/js/lib/a.js"]).decode()
    assert 'from "mr-module/js/lib/b.js"' in a and "./b.js" not in a
    entry = decode(re.search(r'<script type="module" src="(data:[^"]+)"', html).group(1)).decode()
    assert 'from "mr-module/js/lib/a.js"' in entry and 'import "mr-module/js/side.js"' in entry
    assert 'import("mr-module/js/lib/b.js")' in entry and "window.__mrA(" in entry
    assert 'import "mr-module/js/side.js";' in re.findall(r"<script type=\"module\">(.*?)</script>", html, re.S)[0]


def test_module_import_of_missing_file_and_own_importmap_fail(proj):
    write(proj, "main.js", 'import "./gone.js";')
    page(proj, '<script type="module" src="main.js"></script>')
    with pytest.raises(ReviewError, match=r"import \./gone\.js: file not found"):
        bundle.bundle_html(proj / "index.html")
    write(proj, "gone.js", "1")
    page(proj, '<script type="importmap">{"imports":{"lodash":"https://example.test/l.js"}}</script>'
               '<script type="module" src="main.js"></script>')
    with pytest.raises(ReviewError, match="its own import map"):
        bundle.bundle_html(proj / "index.html")


def test_script_attributes_survive_and_integrity_is_dropped(proj):
    write(proj, "a.js", "window.a=1</script><b>")
    page(proj, '<script src="a.js" defer async integrity="sha384-x" crossorigin id="s1" data-k="v"></script>')
    html = bundle.bundle_html(proj / "index.html")
    tag = re.search(r"<script[^>]*>", html).group(0)
    assert all(w in tag for w in ("defer", "async", 'id="s1"', 'data-k="v"')) and "integrity" not in tag
    assert decode(re.search(r'src="(data:[^"]+)"', tag).group(1)).decode() == "window.a=1</script><b>"
    assert "</script><b>" not in html                      # the dangerous text lives only inside base64


def test_a_leading_utf8_bom_is_stripped_from_inlined_css_and_js(proj):
    bom = b"\xef\xbb\xbf"
    write(proj, "a.css", bom + b".a{color:red}", binary=True)
    write(proj, "a.js", bom + b"window.a=1;", binary=True)
    page(proj, '<script src="a.js"></script>', '<link rel="stylesheet" href="a.css">')
    html = bundle.bundle_html(proj / "index.html")
    style = re.search(r"<style[^>]*>(.*?)</style>", html, re.S).group(1)
    assert ".a{color:red}" in style and "﻿" not in style
    js = decode(re.search(r'<script[^>]*src="(data:[^"]+)"', html).group(1)).decode("utf-8")
    assert js == "window.a=1;"                                   # intact, and no BOM left at the start


def test_inline_script_cannot_close_itself_and_style_text_is_safe(proj):
    write(proj, "x.css", 'a::after{content:"</style><b>"}')
    page(proj, "", '<link rel="stylesheet" href="x.css">')
    html = bundle.bundle_html(proj / "index.html")
    assert html.count("</style>") == 1


def test_prelude_goes_after_meta_charset_and_without_head(proj):
    write(proj, "p.png", b"P", binary=True)
    page(proj, '<script>var u="p.png";</script>')
    html = bundle.bundle_html(proj / "index.html")
    assert html.index("charset") < html.index("__MR_A") < html.index("<body>")
    write(proj, "index.html", '<!doctype html><script>var u="p.png";</script>')
    html = bundle.bundle_html(proj / "index.html")
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
    html = bundle.bundle_html(proj / "index.html")
    uri = re.search(r'src="(data:[^"]+)"', html).group(1)
    assert uri.startswith("data:image/webp;base64,") and decode(uri) == b"WEBPDATA"


@pytest.mark.parametrize("kw,size", [({}, 100_000), ({"fail": True}, 400_000), ({"animated": True}, 400_000),
                                     ({"out": b"Z" * 500_000}, 400_000)])
def test_still_is_inlined_as_is_when_small_or_unconvertible(proj, monkeypatch, kw, size):
    _fake_pillow(monkeypatch, **kw)
    write(proj, "a.png", b"S" * size, binary=True)
    page(proj, '<img src="a.png">')
    uri = re.search(r'src="(data:[^"]+)"', bundle.bundle_html(proj / "index.html")).group(1)
    assert uri.startswith("data:image/png;base64,") and decode(uri) == b"S" * size


def test_without_pillow_big_stills_are_inlined_as_is(proj, monkeypatch):
    monkeypatch.setattr(bundle, "_load_pillow", lambda: None)
    write(proj, "a.png", b"S" * 400_000, binary=True)
    page(proj, '<img src="a.png">')
    assert 'src="data:image/png;base64,' in bundle.bundle_html(proj / "index.html")


def test_bundle_module_has_no_third_party_import():
    src = Path(bundle.__file__).read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import) (\w+)", src, re.M))
    assert imports <= {"__future__", "base64", "html", "importlib", "io", "json", "posixpath", "re", "urllib", "pathlib",
                       "typing"}
    assert "PIL" in src and 'import_module("PIL.Image")' in src       # loaded lazily, only when installed


# ---------------------------------------------------------------- where it applies: gated here.now prototype rounds only

@pytest.fixture
def hn(env, tmp_path, monkeypatch):
    """here.now with doubles for the publish helper, the account API and the browser check."""
    calls = {"publish": 0, "verify": []}
    monkeypatch.setattr(config, "herenow_publish_script", lambda: tmp_path / "publish.sh")

    def run(argv, **kw):
        calls["publish"] += 1
        return SimpleNamespace(returncode=0, stderr="publish_result.slug=test-site-ab12\n"
                               "publish_result.site_url=https://test-site-ab12.here.now/\n"
                               "publish_result.auth_mode=authenticated\n")
    monkeypatch.setattr(herenow.subprocess, "run", run)
    monkeypatch.setattr(herenow, "_api", lambda *a, **kw: {})

    def verify(site_url, password, round_n, variant="a", **kw):
        calls["verify"].append((site_url, round_n, variant))
        return {}
    monkeypatch.setattr(browsercheck, "verify_board", verify)
    monkeypatch.setenv("MOCKREVIEW_OFFER_LIVE_DEMO", "true")
    return calls


def _proto(tmp_path, name="p"):
    root = tmp_path / name
    write(root, "assets/main.css", "body{background:url(logo.svg)}")
    write(root, "assets/logo.svg", SVG)
    write(root, "assets/app.js", "window.ok = 'assets/logo.svg';")
    page(root, '<h1>Proto</h1><img src="assets/logo.svg">',
         '<link rel="stylesheet" href="assets/main.css"><script src="assets/app.js" defer></script>')
    return str(root / "index.html")


def _round_dir(rid, n=1):
    return store.review_dir(rid) / "rounds" / str(n) / "a"


def test_gated_herenow_create_bundles_the_prototype(hn, tmp_path):
    res = api.create("Gated", ORIGIN, mode="herenow", prototype=_proto(tmp_path), password=True)
    out = (_round_dir(res["review_id"]) / "index.html").read_text(encoding="utf-8")
    assert "<h1>Proto</h1>" in out and "mockreview-probe" in out         # the board's probe is still injected
    no_local_refs(out)
    assert not (_round_dir(res["review_id"]) / "assets").exists()        # nothing to serve, nothing to 401
    assert hn["verify"] == [("https://test-site-ab12.here.now/", 1, "a")]
    assert res["url"] and res["password"]


def test_password_off_herenow_local_and_tailnet_are_unchanged(hn, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("bundled a board that is not password-gated")
    monkeypatch.setattr(bundle, "bundle_html", boom)
    src = Path(_proto(tmp_path)).read_text(encoding="utf-8")
    for mode, pw in (("herenow", False), ("local", True), ("tailscale", True)):
        res = api.create(f"Plain {mode}", ORIGIN, mode=mode, prototype=_proto(tmp_path), password=pw, wake=False)
        d = _round_dir(res["review_id"])
        assert "assets/main.css" in (d / "index.html").read_text(encoding="utf-8")
        assert (d / "assets" / "main.css").is_file() and src
    assert hn["verify"] == []                                              # nothing gated, nothing to load


def test_only_prototype_rounds_are_bundled(hn, tmp_path, monkeypatch):
    from test_store import _decisions
    seen = []
    real = bundle.bundle_html
    monkeypatch.setattr(bundle, "bundle_html", lambda p: seen.append(p) or real(p))
    dec = str(_decisions(tmp_path))
    res = api.create("Decisions", ORIGIN, mode="herenow", decisions=dec, password=True)
    api.add_round(res["review_id"], decisions=dec)
    assert seen == [] and hn["verify"] == []
    api.add_round(res["review_id"], prototype=_proto(tmp_path))
    assert len(seen) == 1 and hn["verify"] == [("https://test-site-ab12.here.now/", 3, "a")]


def test_add_round_bundles_for_a_gated_review_only(hn, tmp_path):
    from test_store import _decisions
    dec = str(_decisions(tmp_path))
    gated = api.create("G", ORIGIN, mode="herenow", password=True, decisions=dec)
    api.add_round(gated["review_id"], prototype=_proto(tmp_path))
    no_local_refs((_round_dir(gated["review_id"], 2) / "index.html").read_text(encoding="utf-8"))
    open_ = api.create("O", ORIGIN, mode="herenow", password=False, wake=False, decisions=dec)
    api.add_round(open_["review_id"], prototype=_proto(tmp_path))
    d = _round_dir(open_["review_id"], 2)
    assert "assets/main.css" in (d / "index.html").read_text(encoding="utf-8") and (d / "assets" / "app.js").is_file()


def test_bundle_failure_on_create_leaves_nothing_and_never_publishes(hn, tmp_path):
    proto = Path(_proto(tmp_path))
    (proto.parent / "assets" / "logo.svg").unlink()
    with pytest.raises(ReviewError, match="assets/logo.svg"):
        api.create("Broken", ORIGIN, mode="herenow", prototype=str(proto), password=True)
    assert store.list_ids() == [] and hn["publish"] == 0 and hn["verify"] == []


def test_bundle_failure_on_add_round_keeps_the_review_as_it_was(hn, tmp_path):
    from test_store import _decisions
    res = api.create("G", ORIGIN, mode="herenow", password=True, decisions=str(_decisions(tmp_path)))
    before = (store.review_dir(res["review_id"]) / "review.json").read_text(encoding="utf-8")
    publishes = hn["publish"]
    proto = Path(_proto(tmp_path))
    (proto.parent / "assets" / "main.css").unlink()
    with pytest.raises(ReviewError, match="assets/main.css"):
        api.add_round(res["review_id"], prototype=str(proto))
    assert (store.review_dir(res["review_id"]) / "review.json").read_text(encoding="utf-8") == before
    assert not (store.review_dir(res["review_id"]) / "rounds" / "2").exists() and hn["publish"] == publishes


def test_cli_and_tool_use_the_same_bundling(hn, tmp_path, monkeypatch, capsys):
    seen = []
    real = bundle.bundle_spec
    monkeypatch.setattr(bundle, "bundle_spec", lambda spec: seen.append(spec["kind"]) or real(spec))
    proto = _proto(tmp_path)
    assert cli.main(["create", "--title", "CLI", "--platform", "discord", "--chat-id", "test-chat", "--mode", "herenow",
                     "--password", "--prototype", proto]) == 0
    rid = json.loads(capsys.readouterr().out)["review_id"]
    assert cli.main(["add-round", rid, "--prototype", proto]) == 0
    capsys.readouterr()
    ctx = FakeCtx()
    load_plugin().register(ctx)
    res = json.loads(ctx.tools["mock_review_create"]["handler"](
        {"title": "Tool", "mode": "herenow", "password": True, "origin_platform": "discord",
         "origin_chat_id": "test-chat", "prototype": proto}))
    assert res["password"]
    res2 = json.loads(ctx.tools["mock_review_add_round"]["handler"]({"review_id": res["review_id"], "prototype": proto}))
    assert res2["round"] == 2
    assert seen == ["prototype"] * 4
    for rid_, n in ((rid, 1), (rid, 2), (res["review_id"], 1), (res["review_id"], 2)):
        no_local_refs((_round_dir(rid_, n) / "index.html").read_text(encoding="utf-8"))


def test_tool_reports_a_bundling_failure_as_an_error(hn, tmp_path):
    proto = Path(_proto(tmp_path))
    (proto.parent / "assets" / "app.js").unlink()
    ctx = FakeCtx()
    load_plugin().register(ctx)
    res = json.loads(ctx.tools["mock_review_create"]["handler"](
        {"title": "Tool", "mode": "herenow", "password": True, "origin_platform": "discord",
         "origin_chat_id": "test-chat", "prototype": str(proto)}))
    assert "assets/app.js" in res["error"] and store.list_ids() == []
