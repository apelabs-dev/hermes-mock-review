"""Brand enforcement: project bindings, design auto-apply, strict refusal and the off-brand check."""

import json
import os
import shutil
import subprocess

import pytest

from conftest import load_plugin
from mockreview import api, brand, cli, config, mcp, store
from mockreview.store import ReviewError

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")

PROFILE = {
    "name": "acme", "label": "Acme", "default_theme": "light", "font": "Inter, system-ui, sans-serif",
    "mock_css": "mock.css", "fonts_css": "fonts.css",
    "themes": {"light": {"bg": "#ffffff", "surface": "#f6f6f4", "text": "#18181b", "accent": "#4f46e5"},
               "dark": {"bg": "#0f0f12", "surface": "#18181d", "text": "#ececf1", "accent": "#a5a4ff",
                        "ok": "rgb(22, 163, 74)"}},
}
MOCK_CSS = '.card { border: 1px solid #d4d4d8; background: tan; color: #369; }\n.mono { font-family: "Fira Code", monospace; }\n'
FONTS_CSS = '@font-face { font-family: "Acme Sans"; src: url(acme.woff2); }\n'


def make_profile(tmp_path, name="acme", **over):
    d = tmp_path / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "design.json").write_text(json.dumps({**PROFILE, "name": name, **over}), encoding="utf-8")
    (d / "mock.css").write_text(MOCK_CSS, encoding="utf-8")
    (d / "fonts.css").write_text(FONTS_CSS, encoding="utf-8")
    return d


def saved(tmp_path, name="acme", **over):
    store.save_design(name, make_profile(tmp_path, name, **over))
    return store.designs_root() / name


def project(tmp_path, name="app"):
    p = tmp_path / "work" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def dec(tmp_path, blocks=None, extra_css="", name="decisions.json"):
    blocks = blocks if blocks is not None else [{"type": "mock", "id": "m1", "html": '<div style="color: var(--accent)">ok</div>'}]
    f = tmp_path / "dec" / name
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps({"blocks": blocks, "extra_css": extra_css}), encoding="utf-8")
    return str(f)


def mock(html, bid="m1"):
    return {"type": "mock", "id": bid, "html": html}


def create(tmp_path, blocks=None, extra_css="", **kw):
    return api.create("Brand", ORIGIN, mode="local", wake=False, decisions=dec(tmp_path, blocks, extra_css), **kw)


def reviews_on_disk():
    root = config.data_root()
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != "designs") if root.is_dir() else []


def allow(tmp_path):
    d = make_profile(tmp_path)
    info = store.validate_design(d)
    return brand.allowed_for(info, MOCK_CSS, FONTS_CSS)


def found(tmp_path, html=None, css=None, options=None):
    blocks = []
    if html is not None:
        blocks.append(mock(html))
    if options is not None:
        blocks.append({"type": "decision", "id": "d1", "options": options})
    return brand.violations({"blocks": blocks, "extra_css": css or ""}, allow(tmp_path))


# ---------------------------------------------------------------- bind / unbind

def test_bind_and_unbind_at_store_level(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    real = os.path.realpath(p)
    assert store.bind_design("acme", str(p)) == [real]
    assert store.bind_design("acme", str(p)) == [real]                 # twice: a no-op
    assert store.project_bindings() == {"acme": [real]}
    assert store.validate_design(store.designs_root() / "acme")["projects"] == [real]
    assert store.unbind_design("acme", str(p)) == []
    assert store.project_bindings() == {}


def test_bind_refuses_bad_paths_and_a_path_bound_elsewhere(env, tmp_path):
    saved(tmp_path, "acme")
    saved(tmp_path, "beta")
    p = project(tmp_path)
    with pytest.raises(ReviewError, match="absolute"):
        store.bind_design("acme", "relative/dir")
    with pytest.raises(ReviewError, match="not found"):
        store.bind_design("acme", str(tmp_path / "missing"))
    with pytest.raises(ReviewError, match="unknown design"):
        store.bind_design("nope", str(p))
    store.bind_design("acme", str(p))
    with pytest.raises(ReviewError, match="unbind it there first"):
        store.bind_design("beta", str(p))
    with pytest.raises(ReviewError, match="not bound"):
        store.unbind_design("beta", str(p))
    with pytest.raises(ReviewError, match="not bound"):
        store.unbind_design("acme", str(tmp_path / "elsewhere"))


def test_bind_compares_real_paths(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    link = tmp_path / "link"
    try:
        link.symlink_to(p, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    store.bind_design("acme", str(link))
    assert store.bind_design("acme", str(p)) == [os.path.realpath(p)]
    assert store.unbind_design("acme", str(link)) == []


def test_validate_design_cleans_projects(env, tmp_path):
    d = make_profile(tmp_path, projects=[str(tmp_path / "a"), str(tmp_path / "a"), str(tmp_path / "b")])
    assert store.validate_design(d)["projects"] == [str(tmp_path / "a"), str(tmp_path / "b")]
    for bad in ("not-a-list", ["relative"], [3], [str(tmp_path / f"p{i}") for i in range(51)]):
        d = make_profile(tmp_path, projects=bad)
        with pytest.raises(ReviewError):
            store.validate_design(d)
    assert store.validate_design(make_profile(tmp_path, projects=None))["projects"] == []


def test_design_save_keeps_the_bindings(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    store.bind_design("acme", str(p))
    info = store.save_design("acme", make_profile(tmp_path, label="Acme v2"))      # a refresh, no projects in the source
    assert info["label"] == "Acme v2" and info["projects"] == [os.path.realpath(p)]
    assert store.project_bindings() == {"acme": [os.path.realpath(p)]}
    other = project(tmp_path, "other")
    info = store.save_design("acme", make_profile(tmp_path, projects=[str(other)]))   # the source brings its own list
    assert info["projects"] == [str(other)]
    assert store.project_bindings() == {"acme": [str(other)]}


def test_design_save_refuses_a_source_list_bound_elsewhere(env, tmp_path):
    saved(tmp_path, "acme")
    p = project(tmp_path)
    store.bind_design("acme", str(p))
    with pytest.raises(ReviewError, match="already bound"):
        store.save_design("beta", make_profile(tmp_path, "beta", projects=[str(p)]))
    assert not (store.designs_root() / "beta").exists()


def test_a_rounds_copy_of_the_profile_has_no_projects(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    store.bind_design("acme", str(p))
    res = create(tmp_path, project=str(p))
    data = store.load(res["review_id"])
    assert "projects" not in data["rounds"][0]["design"]
    assert "projects" not in store.public_state(data)["rounds"][0]["design"]
    copy = json.loads((store.review_dir(res["review_id"]) / "rounds/1/a/design/design.json").read_text(encoding="utf-8"))
    assert "projects" not in copy


def test_design_cli_bind_unbind_show_list(env, tmp_path, capsys):
    saved(tmp_path)
    p = project(tmp_path)
    real = os.path.realpath(p)
    assert cli.main(["design", "bind", "acme", str(p)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "name": "acme", "projects": [real]}
    assert cli.main(["design", "show", "acme"]) == 0
    assert json.loads(capsys.readouterr().out)["projects"] == [real]
    assert cli.main(["design", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["projects"] == [real]
    assert cli.main(["design", "bind", "acme", "rel"]) == 1
    assert "absolute" in json.loads(capsys.readouterr().err)["error"]
    assert cli.main(["design", "unbind", "acme", str(p)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "name": "acme", "projects": []}
    assert cli.main(["design", "unbind", "acme", str(p)]) == 1
    assert "not bound" in json.loads(capsys.readouterr().err)["error"]


# ---------------------------------------------------------------- resolving a project

def test_match_binding_longest_path_wins_and_inside_counts(tmp_path):
    root = str(tmp_path / "repo")
    web = str(tmp_path / "repo" / "apps" / "web")
    binds = {"acme": [root], "beta": [web]}
    assert brand.match_binding([brand.norm(web + "/src")], binds) == "beta"
    assert brand.match_binding([brand.norm(root + "/apps/api")], binds) == "acme"
    assert brand.match_binding([brand.norm(str(tmp_path / "repository"))], binds) is None
    assert brand.match_binding([brand.norm(str(tmp_path / "other"))], binds) is None


def test_candidates_use_the_injected_git(tmp_path):
    p = project(tmp_path)
    main = tmp_path / "main"
    main.mkdir()
    got = brand.candidates(str(p), git=lambda path: [str(p), str(main)])
    assert got == [brand.norm(str(p)), brand.norm(str(main))]
    assert brand.candidates(str(p), git=lambda path: []) == [brand.norm(str(p))]


def test_git_roots_never_raises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr(brand.subprocess, "run", boom)
    assert brand.git_roots(str(tmp_path)) == []


def test_project_profile_by_path_and_subdirectory(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    sub = p / "src" / "ui"
    sub.mkdir(parents=True)
    assert brand.project_profile(str(p), git=lambda x: []) is None
    store.bind_design("acme", str(p))
    assert brand.project_profile(str(p), git=lambda x: []) == "acme"
    assert brand.project_profile(str(sub), git=lambda x: []) == "acme"
    assert brand.project_profile(str(project(tmp_path, "else")), git=lambda x: []) is None


def _git(cwd, *args):
    subprocess.run([GIT, "-c", "user.name=Test", "-c", "user.email=test@example.test", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


@needs_git
def test_project_profile_from_a_git_subdirectory_and_a_linked_worktree(env, tmp_path):
    saved(tmp_path)
    repo = tmp_path / "repos" / "acme-app"
    (repo / "pkg" / "deep").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    wt = tmp_path / "repos" / "acme-app-wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    (wt / "pkg").mkdir(exist_ok=True)
    store.bind_design("acme", str(repo))                               # bound to the main checkout only
    assert brand.project_profile(str(repo / "pkg" / "deep")) == "acme"
    assert brand.project_profile(str(wt)) == "acme"                    # a linked worktree outside the checkout
    assert brand.project_profile(str(wt / "pkg")) == "acme"
    assert brand.project_profile(str(tmp_path / "repos")) is None
    res = create(tmp_path, project=str(wt / "pkg"))
    assert res["design_auto"] == "acme"


@needs_git
def test_a_binding_to_the_repo_applies_from_its_subdirectory(env, tmp_path):
    saved(tmp_path)
    repo = tmp_path / "repo2"
    (repo / "a" / "b").mkdir(parents=True)
    _git(repo, "init", "-q")
    store.bind_design("acme", str(repo / "a"))                         # bound below the toplevel
    assert brand.project_profile(str(repo / "a" / "b")) == "acme"
    assert brand.project_profile(str(repo)) is None


# ---------------------------------------------------------------- design resolution

def test_create_auto_applies_the_bound_profile(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    store.bind_design("acme", str(p))
    res = create(tmp_path, project=str(p))
    assert res["design_auto"] == "acme"
    data = store.load(res["review_id"])
    assert data["rounds"][0]["design"]["name"] == "acme"
    assert "off_brand" not in res


def test_add_round_inherits_then_auto_applies(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    store.bind_design("acme", str(p))
    rid = create(tmp_path, project=str(p))["review_id"]
    r2 = api.add_round(rid, decisions=dec(tmp_path))                     # inherited: no project, no flag
    assert "design_auto" not in r2
    assert store.load(rid)["rounds"][1]["design"]["name"] == "acme"
    # a review made before any profile existed picks the binding up on its next round
    other = project(tmp_path, "other")
    rid2 = api.create("Plain", ORIGIN, mode="local", wake=False, decisions=dec(tmp_path), project=str(other),
                      no_design=True)["review_id"]
    store.bind_design("acme", str(other))
    r = api.add_round(rid2, decisions=dec(tmp_path), project=str(other))
    assert r["design_auto"] == "acme"
    assert store.load(rid2)["rounds"][1]["design"]["name"] == "acme"


def test_explicit_design_beats_the_binding(env, tmp_path):
    saved(tmp_path, "acme")
    saved(tmp_path, "beta")
    p = project(tmp_path)
    store.bind_design("acme", str(p))
    res = create(tmp_path, project=str(p), design="beta")
    assert "design_auto" not in res
    assert store.load(res["review_id"])["rounds"][0]["design"]["name"] == "beta"


def test_most_specific_binding_wins_in_create(env, tmp_path):
    saved(tmp_path, "acme")
    saved(tmp_path, "beta")
    root = project(tmp_path, "mono")
    web = root / "web"
    web.mkdir()
    store.bind_design("acme", str(root))
    store.bind_design("beta", str(web))
    assert create(tmp_path, project=str(web))["design_auto"] == "beta"
    assert create(tmp_path, project=str(root))["design_auto"] == "acme"


def test_create_is_refused_when_profiles_exist_and_nothing_resolves(env, tmp_path):
    saved(tmp_path, "acme")
    saved(tmp_path, "beta")
    p = project(tmp_path)
    with pytest.raises(ReviewError) as e:
        create(tmp_path, project=str(p))
    msg = e.value.message
    assert os.path.realpath(p) in msg and "acme" in msg and "beta" in msg
    assert 'design="<name>"' in msg and "--design <name>" in msg
    assert "hermes mockreview design bind <name> <abs project path>" in msg
    assert "no_design=true" in msg and "--no-design" in msg
    with pytest.raises(ReviewError, match="No project was given"):
        create(tmp_path)
    assert reviews_on_disk() == []                                       # nothing written


def test_add_round_is_refused_and_writes_nothing(env, tmp_path):
    p = project(tmp_path)
    rid = create(tmp_path)["review_id"]                                  # no profiles yet: fine
    saved(tmp_path)
    with pytest.raises(ReviewError, match="saved profiles exist"):
        api.add_round(rid, decisions=dec(tmp_path), project=str(p))
    data = store.load(rid)
    assert len(data["rounds"]) == 1 and not (store.review_dir(rid) / "rounds" / "2").exists()


def test_no_refusal_without_saved_profiles(env, tmp_path):
    res = create(tmp_path, project=str(project(tmp_path)))
    assert "design_auto" not in res and store.load(res["review_id"])["rounds"][0].get("design") is None
    assert "no_design" not in store.load(res["review_id"])


def test_design_with_no_design_is_an_error(env, tmp_path):
    saved(tmp_path)
    with pytest.raises(ReviewError, match="contradict"):
        create(tmp_path, design="acme", no_design=True)
    rid = create(tmp_path, design="acme")["review_id"]
    with pytest.raises(ReviewError, match="contradict"):
        api.add_round(rid, decisions=dec(tmp_path), design="acme", no_design=True)
    assert len(store.load(rid)["rounds"]) == 1


def test_no_design_is_recorded_and_later_rounds_need_not_repeat_it(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    res = create(tmp_path, project=str(p), no_design=True)
    rid = res["review_id"]
    data = store.load(rid)
    assert data["no_design"] is True and data["rounds"][0].get("design") is None
    api.add_round(rid, decisions=dec(tmp_path), project=str(p))          # no flag, no refusal
    assert len(store.load(rid)["rounds"]) == 2 and store.load(rid)["rounds"][1].get("design") is None
    # the brief's order: a bound project's profile still applies; no_design only lifts the refusal
    store.bind_design("acme", str(p))
    assert create(tmp_path, project=str(p), no_design=True).get("design_auto") == "acme"


def test_project_must_be_absolute(env, tmp_path):
    with pytest.raises(ReviewError, match="absolute"):
        create(tmp_path, project="relative/dir")


# ---------------------------------------------------------------- the off-brand check: what is caught

OFF = "#ff6b00"
COLOUR_SYNTAXES = ["#f60", "#f60a", "#ff6b00", "#ff6b0080", "rgb(255, 107, 0)", "rgb(255 107 0 / 50%)",
                   "rgba(255,107,0,.5)", "rgb(100% 42% 0%)", "hsl(25, 100%, 50%)", "hsla(25 100% 50% / .5)", "orange"]


@pytest.mark.parametrize("c", COLOUR_SYNTAXES)
def test_every_colour_syntax_is_caught_in_a_style_attribute(tmp_path, c):
    got = found(tmp_path, html=f'<div style="background: {c}">x</div>')
    assert got == [("blocks[0].html", f"color {c}")]


@pytest.mark.parametrize("html, css", [
    ('<style>.a { color: ORANGE; }</style><p class="a">x</p>', None),
    ('<svg><rect fill="ORANGE"/></svg>', None),
    ('<svg><stop stop-color="ORANGE"/></svg>', None),
    ('<table bgcolor="ORANGE"><tr><td>x</td></tr></table>', None),
    ('<p>x</p>', '.a { color: ORANGE }'),
    ('<p>x</p>', '@media (min-width: 600px) { .a { color: ORANGE } }'),
    ('<p style="--brand: ORANGE">x</p>', None),
    ('<p style="color: var(--accent, ORANGE)">x</p>', None),
    ('<p>x</p>', ':root { --brand: ORANGE }'),
])
def test_off_brand_colours_are_caught_in_every_css_context(tmp_path, html, css):
    lit = "#ff6b00" if "--brand" in html + (css or "") or "var(" in html else "orange"
    got = found(tmp_path, html=html.replace("ORANGE", lit), css=(css or "").replace("ORANGE", lit))
    assert len(got) == 1 and got[0][1] == f"color {lit}"
    assert got[0][0] in ("blocks[0].html", "extra_css")


def test_named_colours_are_only_read_in_colour_properties(tmp_path):
    assert found(tmp_path, html='<p style="--brand: orange; animation: tan 1s; font-family: Inter">x</p>') == []
    assert found(tmp_path, css='.tan { animation-name: tan } @keyframes red { from { opacity: 0 } }') == []
    assert found(tmp_path, html='<p style="border: 1px solid orange">x</p>') == [("blocks[0].html", "color orange")]


def test_option_html_path_uses_the_option_id(tmp_path):
    got = found(tmp_path, options=[{"id": "a", "html": "<p>x</p>"}, {"id": "b", "html": f'<b style="color:{OFF}">y</b>'}])
    assert got == [("blocks[0].options[b].html", f"color {OFF}")]


def test_blocks_without_html_and_unknown_block_types_are_scanned_generically(tmp_path):
    d = {"blocks": [{"type": "text", "text": f"colour {OFF}"}, {"type": "piece", "html": f"<i style='color:{OFF}'>x</i>"}]}
    assert brand.violations(d, allow(tmp_path)) == [("blocks[1].html", f"color {OFF}")]


def test_font_family_and_the_font_shorthand(tmp_path):
    got = found(tmp_path, html='<p style="font-family: &quot;Comic Sans MS&quot;, cursive">x</p>')
    assert got == [("blocks[0].html", 'font-family "Comic Sans MS"')]
    got = found(tmp_path, css='.a { font: italic 700 14px/1.4 "Comic Sans MS", sans-serif } .b { font: 12px Arial }')
    assert got == [("extra_css", 'font-family "Comic Sans MS"'), ("extra_css", 'font-family "Arial"')]
    assert found(tmp_path, css='.a { font: 14px / 1.4 Papyrus }') == [("extra_css", 'font-family "Papyrus"')]


def test_a_font_that_is_also_a_colour_word_is_a_font_not_a_colour(tmp_path):
    assert found(tmp_path, css='.a { font-family: tan, sans-serif }') == [("extra_css", 'font-family "tan"')]


def test_findings_are_deduplicated(tmp_path):
    html = f'<p style="color:{OFF}">a</p><p style="color:{OFF}">b</p>'
    assert found(tmp_path, html=html) == [("blocks[0].html", f"color {OFF}")]


# ---------------------------------------------------------------- the off-brand check: what is allowed

def test_theme_tokens_pass_in_any_notation_and_alpha(tmp_path):
    ok = ["#4f46e5", "#4F46E5", "#4f46e5cc", "rgb(79, 70, 229)", "rgba(79 70 229 / .15)", "#18181b", "#a5a4ff",
          "#16a34a", "rgb(22 163 74)", "#f6f6f4", "#0f0f12"]
    html = "".join(f'<i style="color: {c}">x</i>' for c in ok)
    assert found(tmp_path, html=html) == []


def test_short_hex_matches_a_long_theme_token(tmp_path):
    d = make_profile(tmp_path, themes={"dark": {"bg": "#000", "surface": "#111", "text": "#eee", "accent": "#89f"}},
                     mock_css=None, fonts_css=None)
    allow_ = brand.allowed_for(store.validate_design(d))
    d_ = {"blocks": [mock('<i style="color:#8899ff;background:#111111;border-color:#EEEEEE">x</i>')]}
    assert brand.violations(d_, allow_) == []


def test_mock_css_colours_pass(tmp_path):
    assert found(tmp_path, html='<i style="color:#d4d4d8;background:tan;border-color:#336699">x</i>') == []


def test_references_white_black_and_transparent_pass(tmp_path):
    ok = ["var(--accent)", "var(--line, var(--text))", "#fff", "#ffffff", "#000", "#000000", "white", "black",
          "rgb(255 255 255)", "rgba(0,0,0,.3)", "transparent", "currentColor", "inherit", "none",
          "rgba(var(--accent-rgb), .15)", "color-mix(in srgb, var(--accent) 20%, transparent)"]
    html = "".join(f'<i style="color: {c}; background: {c}">x</i>' for c in ok)
    assert found(tmp_path, html=html) == []


def test_hsl_tolerates_rounding_of_one_per_channel(tmp_path):
    assert found(tmp_path, html='<i style="color: hsl(210, 50%, 40%)">x</i>') == []          # exactly #336699
    assert found(tmp_path, html='<i style="color: hsl(210, 50%, 40.3%)">x</i>') == []        # off by one
    assert found(tmp_path, html='<i style="color: hsl(210, 50%, 45%)">x</i>') != []


def test_profile_and_generic_fonts_pass(tmp_path):
    ok = ['Inter', '"inter", system-ui', '"Acme Sans"', "'Fira Code', monospace", "ui-rounded, emoji, math, fangsong",
          "serif", "cursive, fantasy", "var(--font)", "var(--font), sans-serif", "inherit", "-apple-system".replace("-apple-system", "system-ui")]
    html = "".join(f'<i style="font-family: {c}">x</i>' for c in ok)
    assert found(tmp_path, html=html) == []
    shorthand = '<i style="font: 12px/1 Inter">a</i><i style="font: caption">b</i><i style="font: inherit">c</i>' \
                '<i style="font: 700 14px var(--font)">d</i><i style="font: 14px/1.4 \'Acme Sans\', serif">e</i>'
    assert found(tmp_path, html=shorthand) == []


# ---------------------------------------------------------------- the off-brand check: no false positives

def test_selectors_urls_hrefs_text_and_strings_are_not_colours(tmp_path):
    html = ('<style>#add, #fab:hover { display: block } @media (min-width: 600px) { #bad { margin: 0 } }'
            '.x::after { content: "#ff6b00 orange"; background: url(#abc) } '
            '.y { background-image: url("data:image/svg+xml;utf8,<svg fill=\'#ff6b00\'/>") }</style>'
            '<a href="#top">Jump #ff6b00</a><p>orange red #ff6b00 Comic Sans MS</p>'
            '<svg><rect fill="url(#grad)" stroke="none"/></svg><p style="background: url(#grad)">x</p>'
            '<p title="color: #ff6b00" data-x="orange">x</p>')
    assert found(tmp_path, html=html, css='#add { color: var(--accent) } #c0ffee {}') == []


def test_comments_and_nesting_in_css(tmp_path):
    css = '/* #ff6b00 orange */ .a { color: var(--text); /* color: #ff6b00 */ } @supports (display: grid) { .b { gap: 1px } }'
    assert found(tmp_path, css=css) == []
    assert found(tmp_path, css='@media (min-width: 1px) { .a { color: #ff6b00 } }') == [("extra_css", f"color {OFF}")]


def test_five_digit_and_hex_like_words_are_not_colours(tmp_path):
    assert found(tmp_path, html='<p style="margin: 0; width: 12345px; animation: a#b 1s; --x: #12345">x</p>') == []


def test_named_colour_table_is_complete():
    assert len(brand.NAMED_COLORS) == 148
    assert brand.NAMED_COLORS["rebeccapurple"] == (0x66, 0x33, 0x99) and brand.NAMED_COLORS["grey"] == (128, 128, 128)
    assert "transparent" not in brand.NAMED_COLORS and "currentcolor" not in brand.NAMED_COLORS


# ---------------------------------------------------------------- the error and the blocks

def test_enforcement_in_create_lists_paths_and_the_hint_and_writes_nothing(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    store.bind_design("acme", str(p))
    blocks = [{"type": "text", "text": "x"},
              mock('<p style="font-family: &quot;Comic Sans MS&quot;">x</p>'),
              {"type": "decision", "id": "d1", "options": [{"id": "a", "html": "<p>x</p>"},
                                                         {"id": "b", "html": f'<i style="color:{OFF}">y</i>'}]}]
    with pytest.raises(ReviewError) as e:
        create(tmp_path, blocks, project=str(p))
    lines = e.value.message.splitlines()
    assert 'blocks[1].html: font-family "Comic Sans MS" is not in profile acme' in lines
    assert f"blocks[2].options[b].html: color {OFF} is not in profile acme" in lines
    assert lines[-1].startswith("Use the profile's tokens (var(--accent), var(--text), var(--surface), var(--font)")
    assert 'off_brand="<reason>"' in lines[-1] and '--off-brand "<reason>"' in lines[-1] and "--accent-soft" in lines[-1]
    assert reviews_on_disk() == []


def test_error_is_capped_at_twenty_lines(env, tmp_path):
    saved(tmp_path)
    css = " ".join(f".c{i} {{ color: #{i + 1:06x}; }}" for i in range(0x30, 0x30 + 25))
    with pytest.raises(ReviewError) as e:
        create(tmp_path, extra_css=css, design="acme")
    lines = e.value.message.splitlines()
    detail = [ln for ln in lines if ln.startswith("extra_css:")]
    assert len(detail) == 20 and "... and 5 more" in lines


def test_extra_css_is_checked_and_a_clean_round_passes(env, tmp_path):
    saved(tmp_path)
    with pytest.raises(ReviewError, match="extra_css: color #ff6b00"):
        create(tmp_path, extra_css=".a { color: #ff6b00 }", design="acme")
    res = create(tmp_path, extra_css=".a { color: var(--accent); background: #4f46e5 }", design="acme")
    assert res["review_id"]


def test_the_inherited_design_is_checked_on_add_round(env, tmp_path):
    saved(tmp_path)
    rid = create(tmp_path, design="acme")["review_id"]
    bad = dec(tmp_path, [mock(f'<i style="color:{OFF}">x</i>')], name="bad.json")
    with pytest.raises(ReviewError, match="not in profile acme"):
        api.add_round(rid, decisions=bad)
    assert len(store.load(rid)["rounds"]) == 1 and not (store.review_dir(rid) / "rounds" / "2").exists()


def test_the_auto_applied_design_is_checked_too(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    store.bind_design("acme", str(p))
    with pytest.raises(ReviewError, match="not in profile acme"):
        create(tmp_path, [mock(f'<i style="color:{OFF}">x</i>')], project=str(p))


def test_no_profile_in_effect_means_no_check(env, tmp_path):
    res = create(tmp_path, [mock(f'<i style="color:{OFF}">x</i>')])
    assert res["review_id"]


def test_prototype_rounds_are_not_scanned(env, tmp_path, variants):
    saved(tmp_path)
    rid = create(tmp_path, design="acme")["review_id"]
    page = tmp_path / "proto" / "index.html"
    page.parent.mkdir()
    page.write_text(f'<html><body style="color:{OFF};font-family:Papyrus">x</body></html>', encoding="utf-8")
    res = api.add_round(rid, prototype=str(page), allow_live_demo=True)
    assert res["round"] == 2 and store.load(rid)["rounds"][1]["kind"] == "prototype"


# ---------------------------------------------------------------- the off_brand bypass

def test_off_brand_skips_the_check_and_is_recorded(env, tmp_path):
    saved(tmp_path)
    blocks = [mock(f'<i style="color:{OFF}">x</i>')]
    res = create(tmp_path, blocks, design="acme", off_brand="  Launch teaser, not the product UI  ")
    assert res["off_brand"] == "Launch teaser, not the product UI"
    data = store.load(res["review_id"])
    assert data["off_brand"] == "Launch teaser, not the product UI"
    assert data["rounds"][0]["off_brand"] == "Launch teaser, not the product UI"
    r2 = api.add_round(res["review_id"], decisions=dec(tmp_path, blocks, name="two.json"), off_brand="Still a teaser")
    assert r2["off_brand"] == "Still a teaser"
    data = store.load(res["review_id"])
    assert data["off_brand"] == "Still a teaser" and data["rounds"][1]["off_brand"] == "Still a teaser"
    assert "off_brand" not in store.public_state(data)["rounds"][0]


def test_off_brand_empty_is_not_given_and_too_long_is_rejected(env, tmp_path):
    saved(tmp_path)
    blocks = [mock(f'<i style="color:{OFF}">x</i>')]
    with pytest.raises(ReviewError, match="not in profile"):
        create(tmp_path, blocks, design="acme", off_brand="   ")
    with pytest.raises(ReviewError, match="200"):
        create(tmp_path, blocks, design="acme", off_brand="x" * 201)
    with pytest.raises(ReviewError):
        create(tmp_path, blocks, design="acme", off_brand=5)
    assert reviews_on_disk() == []
    assert create(tmp_path, blocks, design="acme", off_brand="y" * 200)["off_brand"] == "y" * 200


def test_a_clean_round_with_no_off_brand_records_nothing(env, tmp_path):
    saved(tmp_path)
    res = create(tmp_path, design="acme")
    assert "off_brand" not in res and "off_brand" not in store.load(res["review_id"])


# ---------------------------------------------------------------- CLI, MCP and plugin surfaces

def cli_create(*extra, tmp_path):
    return cli.main(["create", "--title", "CLI", "--platform", "discord", "--chat-id", "test-chat", "--mode", "local",
                     "--no-wake", "--decisions", dec(tmp_path), *extra])


def test_cli_project_defaults_to_the_current_directory(env, tmp_path, monkeypatch, capsys):
    saved(tmp_path)
    p = project(tmp_path)
    store.bind_design("acme", str(p))
    monkeypatch.chdir(p)
    assert cli_create(tmp_path=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["design_auto"] == "acme"
    monkeypatch.chdir(tmp_path)
    assert cli_create(tmp_path=tmp_path) == 1
    assert "saved profiles exist" in json.loads(capsys.readouterr().err)["error"]
    assert cli_create("--project", str(p), tmp_path=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["design_auto"] == "acme"


def test_cli_no_design_and_off_brand(env, tmp_path, monkeypatch, capsys):
    saved(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli_create("--no-design", tmp_path=tmp_path) == 0
    rid = json.loads(capsys.readouterr().out)["review_id"]
    assert cli.main(["add-round", rid, "--decisions", dec(tmp_path)]) == 0
    capsys.readouterr()
    assert cli_create("--design", "acme", "--no-design", tmp_path=tmp_path) == 1
    assert "contradict" in json.loads(capsys.readouterr().err)["error"]
    bad = dec(tmp_path, [mock(f'<i style="color:{OFF}">x</i>')], name="bad.json")
    args = ["create", "--title", "CLI", "--platform", "discord", "--chat-id", "test-chat", "--mode", "local",
            "--no-wake", "--design", "acme", "--decisions", bad]
    assert cli.main(args) == 1
    assert "not in profile acme" in json.loads(capsys.readouterr().err)["error"]
    assert cli.main(args + ["--off-brand", "teaser"]) == 0
    assert json.loads(capsys.readouterr().out)["off_brand"] == "teaser"


def test_mcp_tools_expose_the_brand_arguments_and_do_not_default_the_project(env, tmp_path):
    tools = {t["name"]: t for t in mcp.tools()}
    for name in ("mock_review_create", "mock_review_add_round"):
        props = tools[name]["inputSchema"]["properties"]
        assert {"project", "no_design", "off_brand"} <= set(props)
        assert "project" in tools[name]["description"]
    saved(tmp_path)
    p = project(tmp_path)
    args = {"title": "MCP", "decisions": dec(tmp_path), "origin_platform": "discord", "origin_chat_id": "test-chat"}
    res = mcp.call_tool("mock_review_create", args, mcp.Options())
    assert res["isError"] is True and "saved profiles exist" in json.loads(res["content"][0]["text"])["error"]
    store.bind_design("acme", str(p))
    res = mcp.call_tool("mock_review_create", {**args, "project": str(p)}, mcp.Options())
    assert res["isError"] is False and json.loads(res["content"][0]["text"])["design_auto"] == "acme"


def test_plugin_tool_schemas_expose_the_brand_arguments(env):
    plugin = load_plugin()
    sch = plugin.schemas(True, "local", False)
    for name in ("mock_review_create", "mock_review_add_round"):
        props = sch[name]["parameters"]["properties"]
        assert {"project", "no_design", "off_brand"} <= set(props)
    assert "no_design" in sch["mock_review_create"]["parameters"]["properties"]["project"]["description"]


def test_plugin_tool_passes_the_brand_arguments_through(env, tmp_path):
    saved(tmp_path)
    p = project(tmp_path)
    store.bind_design("acme", str(p))

    class Ctx:
        def get_config(self, key, default=None):
            return default

    run = load_plugin().MockReview(Ctx()).tool("mock_review_create")
    out = json.loads(run({"title": "Plugin", "decisions": dec(tmp_path), "wake": False, "mode": "local",
                          "project": str(p)}))
    assert out["design_auto"] == "acme"
    out = json.loads(run({"title": "Plugin", "decisions": dec(tmp_path), "wake": False, "mode": "local"}))
    assert "saved profiles exist" in out["error"]
