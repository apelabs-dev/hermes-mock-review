"""The export of a design-profile round: the profile CSS, fonts and extra_css in one scoped <style>, the board's
`.cq > .app[data-theme]` box, the widened CSS sanitizer and the HTML/SVG allowlist it relies on."""

import base64
import json
import re

import pytest

from mockreview import export, store

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
FONT = b"wOF2" + bytes(range(256)) * 4
NO_EXTERNAL = re.compile(r"(?i)(https?:|mailto:|javascript:|blob:|cid:|ftp:|file:)")

MOCK_CSS = """
.app{--bg:#123456;background:var(--bg)}
.app > .shell > nav{display:flex}
.app .a + .b ~ .c{color:red}
@media (max-width: 600px){.app .only-wide{display:none}}
@media (prefers-reduced-motion: reduce){.app *{animation:none}}
@keyframes mo-fade{from{opacity:0}50%{opacity:.5}to{opacity:1}}
@property --angle{syntax:"<angle>";inherits:false;initial-value:0deg}
.app .spin{animation:mo-fade 1s}
"""
FONTS_CSS = '@font-face{font-family:"Sample";font-weight:100 900;src:url("fonts/Sample.woff2") format("woff2")}'


def sheet(css, scope=None):
    return export._sanitize_css(css, sheet=True, scope=scope) or ""


def fragment(markup):
    return export._safe_fragment(markup, export._Options())


def profile(tmp_path, name="profile", mock_css=MOCK_CSS, fonts_css=FONTS_CSS, default_theme="dark", font=FONT):
    d = tmp_path / name
    (d / "fonts").mkdir(parents=True)
    spec = {"name": "sample", "default_theme": default_theme, "mock_css": "mock.css",
            "themes": {"dark": {"bg": "#123456", "surface": "#1a2b3c", "text": "#f0f0f0", "accent": "#88aaff"},
                       "light": {"bg": "#fedcba", "surface": "#ffffff", "text": "#101010", "accent": "#3355cc"}}}
    (d / "mock.css").write_text(mock_css, encoding="utf-8")
    if fonts_css is not None:
        spec["fonts_css"] = "fonts.css"
        (d / "fonts.css").write_text(fonts_css, encoding="utf-8")
        (d / "fonts" / "Sample.woff2").write_bytes(font)
    (d / "design.json").write_text(json.dumps(spec), encoding="utf-8")
    return d


def designed_review(tmp_path, *, extra_css="", html='<div class="shell"><nav><a>One</a></nav></div>', look=None,
                    **kw):
    prof = profile(tmp_path, **kw)
    src = tmp_path / "round" / "decisions.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps({"extra_css": extra_css, "blocks": [
        {"type": "mock", "id": "m1", "title": "Designed", "html": html},
        {"type": "decision", "id": "d1", "title": "Pick", "options": [
            {"id": "a", "label": "A", "html": '<p class="only-wide">wide</p>'}]}]}), encoding="utf-8")
    return store.create_review("Designed", "", ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(src), design=str(prof)), wake=False, look=look)


def build(review):
    return export.build_export_html(store.load(review["id"]), 1, False, generated_at="2026-11-13 12:00 UTC")


def style_of(doc):
    return doc[doc.index("<style>") + 7:doc.index("</style>")]


# ---------------------------------------------------------------- the sanitizer


def test_child_combinators_keyframes_and_width_queries_survive():
    out = sheet(MOCK_CSS)
    for piece in (".app > .shell > nav{display:flex}", ".app .a + .b ~ .c{color:red}",
                  "@media (max-width: 600px){.app .only-wide{display:none}}",
                  "@keyframes mo-fade{from{opacity:0}50%{opacity:.5}to{opacity:1}}",
                  ".app{--bg:#123456;background:var(--bg)}", "@media (prefers-reduced-motion: reduce)"):
        assert piece in out, piece


def test_property_rules_are_dropped_alone():
    out = sheet(MOCK_CSS)
    assert "@property" not in out and "<angle>" not in out and "<" not in out
    assert ".app .spin{animation:mo-fade 1s}" in out


def test_container_supports_and_font_face_are_kept():
    out = sheet("@container mock (min-width: 400px){.a{color:red}}"
                "@supports (display:grid){.b{display:grid}}"
                "@font-face{font-family:X;src:url(data:font/woff2;base64,AAAA) format('woff2')}")
    assert "@container mock (min-width: 400px){.a{color:red}}" in out
    assert "@supports (display:grid){.b{display:grid}}" in out
    assert '@font-face{font-family:X;src:url("data:font/woff2;base64,AAAA") format("woff2")}' in out


@pytest.mark.parametrize("hostile", [
    ".a{color:red}</style><script>window.__pwned=1</script>.b{color:blue}",
    '.a{content:"</style><script>window.__pwned=1</script>"}',
    r".a{content:'\3c/style\3e'}",
    "/* </style> */.a{color:red}",
    ".a > .b{color:red}</style ><img src=x onerror=alert(1)>",
    "@media (max-width:600px){</style>.a{color:red}}",
    '@font-face{font-family:"</style>";src:url(data:font/woff2;base64,AAAA)}',
    '@keyframes k{from{content:"</style>"}}',
])
def test_style_end_tags_and_angle_brackets_are_impossible(hostile):
    out = sheet(hostile, scope=".mr-d1")
    assert "<" not in out and "</style" not in out.lower() and "script" not in out


def test_a_bad_rule_drops_that_rule_not_the_sheet():
    out = sheet(".a{color:red} .b<x{color:blue} .c{width:calc(1px > 2px);color:green} @import url(x.css); "
                "@page{margin:0} .d{color:navy}")
    assert ".a{color:red}" in out and ".d{color:navy}" in out
    assert ".c{color:green}" in out              # `>` in a value drops that declaration
    assert ".b" not in out and "@page" not in out and "@import" not in out


@pytest.mark.parametrize("wrap", [
    ".a{{{}}}",
    "@media (max-width: 600px){{.a{{{}}}}}",
    "@container mock (min-width: 400px){{.a{{{}}}}}",
    "@supports (display:grid){{.a{{{}}}}}",
    "@keyframes k{{from{{{}}}to{{opacity:1}}}}",
    "@font-face{{font-family:X;{}}}",
    "@media (max-width: 600px){{@supports (display:grid){{.a{{{}}}}}}}",
])
@pytest.mark.parametrize("decl", [
    "background:url(https://evil.example.test/x.png)",
    "background:url(//evil.example.test/x.png)",
    r"background:\75rl(\2f\2f evil.example.test/x)",
    "src:url(https://evil.example.test/f.woff2) format('woff2')",
    "src:url(//evil.example.test/f.woff2)",
    "src:url(data:text/css;base64,QQ==)",
    "src:local(Arial)",
    "background:url(data:font/woff2;base64,AAAA)",
])
def test_remote_urls_are_refused_in_every_at_rule(wrap, decl):
    out = sheet(wrap.format(decl))
    assert "evil.example.test" not in out and "//" not in out and "data:text" not in out
    if out.startswith("@font-face"):
        assert "local(" not in out
    else:
        assert "data:font" not in out          # a data: font survives only as an @font-face src


def test_remote_urls_in_at_rule_preludes_are_refused():
    for css in ("@media url(https://evil.example.test/x){.a{color:red}}",
                "@supports (background:url(//evil.example.test/x)){.a{color:red}}",
                "@container mock (min-width: 400px) and url(x){.a{color:red}}",
                '@keyframes "a//b"{from{opacity:0}}'):
        out = sheet(css)
        assert "evil" not in out and "url(" not in out and "//" not in out, css


def test_scope_prefixes_every_selector_and_no_rule_escapes_it():
    out = sheet(".app .x,:is(.a,.b) > .c{color:red}}.mr-header{display:none}"
                "@media (max-width: 600px){h1{color:red}}@keyframes k{from{opacity:0}}", scope=".mr-d1")
    assert ".mr-d1 .app .x,.mr-d1 :is(.a,.b) > .c{color:red}" in out
    assert "@media (max-width: 600px){.mr-d1 h1{color:red}}" in out
    assert "@keyframes k{from{opacity:0}}" in out
    for selector in re.findall(r"(?:^|})([^{}@]+)\{", out):
        if selector.strip() not in ("from", "to"):
            assert all(p.strip().startswith(".mr-d1 ") for p in export._css_split(selector, ",")), selector


@pytest.mark.parametrize("selector", ["~ .mr-caption", "+ p", "> .x", ".app,~ .mr-tag", "  ~.mr-caption"])
def test_a_selector_opening_with_a_combinator_cannot_hang_off_the_scope(selector):
    out = sheet(selector + "{display:none}.app > .ok{color:red}", scope=".mr-d1")
    assert out == ".mr-d1 .app > .ok{color:red}", out
    assert "@media (max-width: 9px){.mr-d1 .ok{color:red}}" == sheet(
        "@media (max-width: 9px){" + selector + "{display:none}.ok{color:red}}", scope=".mr-d1")


def test_the_inline_style_allowlist_still_refuses_angle_brackets():
    assert export._sanitize_css("color:red;width:calc(1px > 2px)", sheet=False) is None


# ---------------------------------------------------------------- the HTML and SVG allowlist


def test_app_elements_and_data_aria_role_attributes_are_kept():
    out = fragment('<nav role="tablist" data-pg="overview" aria-label="Main"><button type="button" disabled '
                   'aria-pressed="true">Go</button><label for="q">Q</label><input id="q" name="q" type="search" '
                   'placeholder="Find" value="x" checked><select name="s"><option selected value="1">One</option>'
                   '</select><textarea rows="2">t</textarea><progress value="3" max="5"></progress>'
                   '<meter value="1" min="0" max="2"></meter><time datetime="2026-01-01">Jan</time><mark>m</mark>'
                   '<sup>1</sup><sub>2</sub><s>s</s><u>u</u><abbr title="t">a</abbr><kbd>k</kbd>'
                   '<aside hidden>side</aside><main>main</main></nav>')
    for piece in ('<nav role="tablist" data-pg="overview" aria-label="Main">', '<button type="button" disabled=""',
                  'aria-pressed="true"', '<label for="q">', 'placeholder="Find"', 'value="x"', 'checked=""',
                  '<option selected="" value="1">', '<textarea rows="2">', '<progress value="3" max="5">',
                  '<time datetime="2026-01-01">', "<mark>", "<sup>", "<sub>", "<s>", "<u>", "<abbr", "<kbd>",
                  "<aside hidden=\"\">", "<main>"):
        assert piece in out, piece


def test_handlers_links_and_form_actions_are_still_stripped():
    out = fragment('<form action="https://evil.example.test/post"><button formaction="https://evil.example.test/x" '
                   'onclick="window.__pwned=1" data-x="1">Go</button><input type="image" src="https://evil.example.test/i">'
                   '<a href="https://evil.example.test/" class="nav on" data-pg="a" onmouseover="x()">Link</a></form>')
    assert "evil.example.test" not in out and "onclick" not in out and "onmouseover" not in out
    assert "action" not in out and "<form" not in out and "src=" not in out and "href" not in out
    assert 'data-x="1"' in out and '<a class="nav on" data-pg="a">Link</a>' in out


def test_inline_svg_subset_keeps_shapes_and_svg_case():
    out = fragment('<svg viewBox="0 0 10 10" width="10" height="10" xmlns="http://www.w3.org/2000/svg">'
                   '<defs><linearGradient id="g" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#f00"/>'
                   '</linearGradient><clipPath id="c" clipPathUnits="objectBoundingBox"><rect width="1" height="1"/>'
                   '</clipPath><symbol id="i" viewBox="0 0 4 4"><circle cx="2" cy="2" r="2"/></symbol></defs>'
                   '<g clip-path="url(#c)"><path d="M0 0h10v10z" fill="url(#g)"/><polyline points="0,0 5,5"/>'
                   '<use href="#i" x="1"/><use xlink:href="#i"/></g><text x="1" y="9">Chart &lt;1&gt;</text>'
                   '<title>Icon</title></svg>')
    for piece in ('<svg viewBox="0 0 10 10" width="10" height="10">', '<linearGradient id="g" gradientUnits=',
                  '<stop offset="0" stop-color="#f00"/>', '<clipPath id="c" clipPathUnits="objectBoundingBox">',
                  '<symbol id="i" viewBox="0 0 4 4">', '<g clip-path="url(#c)">', 'fill="url(#g)"',
                  '<polyline points="0,0 5,5"/>', '<use href="#i" x="1"/>', '<use href="#i"/>',
                  '<text x="1" y="9">Chart &lt;1&gt;</text>', "<title>Icon</title>"):
        assert piece in out, piece
    assert "xmlns" not in out and "w3.org" not in out


def test_svg_links_foreign_objects_scripts_and_animation_are_stripped():
    out = fragment('<svg viewBox="0 0 10 10" onload="window.__pwned=1">'
                   '<use href="https://evil.example.test/s.svg#i"/><use xlink:href="//evil.example.test/s.svg#i"/>'
                   '<use href="data:image/svg+xml;base64,PHN2Zz4="/>'
                   '<foreignObject><div onclick="x()">html inside</div><iframe></iframe></foreignObject>'
                   '<script>window.__pwned=1</script><style>rect{fill:url(https://evil.example.test/p)}</style>'
                   '<image href="https://evil.example.test/i.png"/><animate attributeName="x" to="5"/>'
                   '<set attributeName="fill" to="red"/><animateTransform type="rotate"/>'
                   '<a href="https://evil.example.test/"><rect width="1" height="1" '
                   'fill="url(https://evil.example.test/p)" stroke="url(#ok)" onclick="x()" '
                   'style="fill:url(//evil.example.test/p)"/></a>'
                   '<filter id="f"><feImage href="https://evil.example.test/x"/></filter></svg>')
    assert "evil.example.test" not in out and "onload" not in out and "onclick" not in out
    for gone in ("foreignObject", "html inside", "<iframe", "<script", "__pwned", "<style", "<image", "<animate",
                 "<set", "animateTransform", "<a", "<filter", "feImage", "data:image/svg"):
        assert gone not in out, gone
    assert '<rect width="1" height="1" stroke="url(#ok)"/>' in out


# ---------------------------------------------------------------- the export document


def test_a_design_round_inlines_the_profile_css_fonts_and_extra_css(env, tmp_path):
    doc = build(designed_review(tmp_path, extra_css=".app .extra > i{color:teal}"))
    css = style_of(doc)
    assert "<script" not in doc.lower() and re.search(r"\son[a-z]+\s*=", doc, re.I) is None
    assert NO_EXTERNAL.search(doc) is None
    assert export.EXPORT_CSP.replace('"', "&quot;") in doc and doc.count("Content-Security-Policy") == 1
    assert doc.count("<style>") == 1 and doc.count("</style>") == 1
    assert ".mr-d1 .app > .shell > nav{display:flex}" in css
    assert ".mr-d1 .app .extra > i{color:teal}" in css
    assert "@container mock (max-width: 600px){.mr-d1 .app .only-wide{display:none}}" in css
    assert "@media (prefers-reduced-motion: reduce)" in css      # not a width query: stays a media query
    assert "@property" not in css
    font = base64.b64encode(FONT).decode("ascii")
    assert css.count('src:url("data:font/woff2;base64,' + font + '") format("woff2")') == 1
    assert "fonts/Sample.woff2" not in doc
    assert ('<div class="mr-mock mr-designed mr-d1"><div class="cq"><div class="app" data-theme="dark">'
            in doc)
    assert doc.count('class="mr-mock mr-designed mr-d1"') == 2      # the mock and the decision option


def test_the_theme_follows_the_reviews_default_theme_like_the_board(env, tmp_path):
    doc = build(designed_review(tmp_path, look={"default_theme": "light"}))
    assert '<div class="app" data-theme="light">' in doc


def test_design_css_cannot_restyle_the_export_chrome(env, tmp_path):
    doc = build(designed_review(tmp_path, extra_css="}body{display:none}.mr-header{display:none}"
                                                    "@media (max-width: 600px){html{color:red}}"))
    css = style_of(doc)
    design = css[css.index(".mr-d1"):]
    for selector in re.findall(r"(?:^|})([^{}@]+)\{", design):
        if selector.strip() not in ("from", "to") and not re.fullmatch(r"\s*\d+%\s*", selector):
            assert all(p.strip().startswith(".mr-d1 ") for p in export._css_split(selector, ",")), selector


def test_hostile_design_files_cannot_end_the_style_element(env, tmp_path):
    payload = "</style><script>window.__pwned=1</script>"
    doc = build(designed_review(tmp_path, extra_css=".a{color:red}" + payload,
                                mock_css=MOCK_CSS + payload + r".x{content:'\3c/style\3e'}",
                                fonts_css=FONTS_CSS + payload + "@font-face{src:url(https://evil.example.test/f)}"))
    assert doc.count("</style") == 1 and "<script" not in doc.lower() and "__pwned" not in doc
    assert "evil.example.test" not in doc


def test_a_profile_file_name_cannot_leave_the_round(env, tmp_path):
    review = designed_review(tmp_path)
    secret = store.review_dir(review["id"]) / "rounds" / "1" / "a" / "design" / "fonts" / "Secret.woff2"
    secret.write_bytes(b"secret-bytes")
    with store.locked(review["id"]):
        data = store.load(review["id"])
        data["rounds"][0]["design"]["mock_css"] = "../decisions.json"
        data["rounds"][0]["design"]["fonts_css"] = "/etc/hosts"
        store.save(review["id"], data)
    doc = build(review)
    assert "decisions.json" not in style_of(doc) and '"blocks"' not in style_of(doc)
    assert "@font-face" not in style_of(doc)
    assert base64.b64encode(b"secret-bytes").decode() not in doc


def test_fonts_are_charged_to_the_inline_budget():
    budget = export._Budget(remaining=10)
    designs = export._Designs(budget)
    assert designs._font_uri("x" * 8, 1, "a", "fonts/Sample.woff2") is None
    assert budget.remaining == 10


def test_identical_designs_are_inlined_once_and_different_ones_stay_apart(env, tmp_path):
    review = designed_review(tmp_path)
    rid, rnd = review["id"], store.load(review["id"])["rounds"][0]
    designs = export._Designs(export._Budget())
    first = designs.add(rid, 1, "a", rnd["design"], "")
    again = designs.add(rid, 1, "a", rnd["design"], "")
    other = designs.add(rid, 1, "a", rnd["design"], ".app .only-here{color:red}")
    assert first == again == "mr-d1" and other == "mr-d2"
    css = designs.css()
    font = base64.b64encode(FONT).decode("ascii")
    assert css.count(font) == 1 and css.count("@font-face") == 1
    assert css.count(".mr-d1 .app > .shell > nav{display:flex}") == 1
    assert ".mr-d2 .app .only-here{color:red}" in css and ".mr-d1 .app .only-here" not in css


def test_a_font_over_the_budget_drops_its_font_face(env, tmp_path):
    review = designed_review(tmp_path)
    rid, rnd = review["id"], store.load(review["id"])["rounds"][0]
    designs = export._Designs(export._Budget(remaining=len(FONT) - 1))
    designs.add(rid, 1, "a", rnd["design"], "")
    assert "@font-face" not in designs.css() and "data:font" not in designs.css()


@pytest.mark.parametrize("nested", [":not(&)", ":is(&, .mr-header)", ":where(body, &)", ":not(&)[data-x]"])
def test_a_nested_selector_with_amp_only_inside_parentheses_is_dropped(nested):
    out = sheet(".app{.k{color:red}" + nested + "{display:none}}", scope=".mr-d1")
    assert "display:none" not in out
    assert ".mr-d1 .app{.k{color:red}}" in out


@pytest.mark.parametrize("nested", ["&:hover", ".x", "& + .y", "body &", "&.on"])
def test_nested_selectors_anchored_on_amp_or_relative_are_kept(nested):
    out = sheet(".app{" + nested + "{color:teal}}", scope=".mr-d1")
    assert nested + "{color:teal}" in out


def test_the_export_can_leave_the_fonts_out(env, tmp_path):
    review = designed_review(tmp_path)
    doc = export.build_export_html(store.load(review["id"]), 1, False, fonts=False)
    assert "data:font/" not in doc and "@font-face" not in style_of(doc)
    assert ".mr-d1 .app > .shell > nav{display:flex}" in style_of(doc)


def test_the_desk_view_drops_the_fonts_before_it_gives_up(env, tmp_path, monkeypatch):
    from mockreview import api
    review = designed_review(tmp_path, font=b"wOF2" + bytes(range(256)) * 400)
    with_fonts = len(api.desk_view(review["id"])["data_url"])
    monkeypatch.setattr(api, "DESK_DATA_URL_LIMIT", with_fonts - 1)
    payload = api.desk_view(review["id"])
    assert payload["too_large"] is False
    doc = base64.b64decode(payload["data_url"].split(",", 1)[1]).decode("utf-8")
    assert "data:font/" not in doc and ".mr-d1 .app" in doc
    monkeypatch.setattr(api, "DESK_DATA_URL_LIMIT", 100)
    assert api.desk_view(review["id"])["too_large"] is True
