"""The export serializer: allowlisted static HTML, every block type, artifacts, prototypes and comments."""

import base64
from html import unescape
import json
import re
import sys
import types

import pytest

from authkit import account
from mockreview import export, store, threads, users

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
SCRIPT_PAYLOAD = "window.__exportPwned = true"


def decisions_round():
    blocks = [
        {"type": "text", "title": "Prose", "text": "Frames the round."},
        {"type": "recap", "title": "Agreed so far", "items": ["One", "Two"]},
        {"type": "decision", "id": "d1", "title": "Layout", "today": "One column.",
         "options": [{"id": "a", "label": "Single", "html": "<p>single</p>"},
                     {"id": "b", "label": "Steps", "recommended": True, "html": "<p>steps</p>"}]},
        {"type": "cards", "title": "What changes", "items": [{"title": "Header", "text": "Smaller", "pill": "new"}]},
        {"type": "callout", "tone": "warn", "title": "Constraint", "text": "Phone first."},
        {"type": "mock", "id": "m1", "title": "Today", "caption": "Current screen",
         "html": '<div class="x">Mock</div><script>window.__mockPwned = 1</script>'
                 '<img src="rounds/1/a/assets/pixel.png" onerror="window.__onerrorPwned = 1">'
                 '<img src="https://evil.example.test/x.png"><a href="javascript:alert(1)">bad link</a>'},
        {"type": "table", "title": "Compare", "columns": ["", "A", "B"],
         "rows": [["Taps", 3, 2], ["Risk", "low", "medium"]], "highlight_col": 2},
        {"type": "settled", "title": "Already decided",
         "items": [{"title": "Offline first", "quote": "On the train.", "rule": "Save locally first."}]},
        {"type": "defaults", "title": "How I read the rest", "text": "Where silent:",
         "items": ["Sync every 5 minutes"]},
        {"type": "question", "id": "q1", "title": "How long?", "text": "Pick a window.",
         "recommendation": {"answer": "30 days", "why": "Bounded storage."}},
        {"type": "piece", "id": "p1", "title": "Change log", "tag": "P0", "text": "One log",
         "items": ["Per notebook"]},
        {"type": "artifact", "id": "art", "title": "Click sound", "caption": "Take 3",
         "html": ('<style>.inside-artifact{color:red}</style>'
                  '<div class="inside-artifact">Artifact body</div>'
                  '<img src="data:image/png;base64,iVBORw0KGgo=">'
                  '<script>window.__artifactPwned = 1</script>')},
    ]
    return {"preset": "design", "intro": "Intro prose", "blocks": blocks}


@pytest.fixture
def decisions_review(env, tmp_path):
    src = tmp_path / "decisions"
    (src / "assets").mkdir(parents=True)
    (src / "assets" / "pixel.png").write_bytes(PIXEL)
    path = src / "decisions.json"
    path.write_text(json.dumps(decisions_round()), encoding="utf-8")
    return store.create_review("Checkout review", "Read this first", ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(path)), wake=False)


def build(review, comments=False, **kw):
    return export.build_export_html(store.load(review["id"]), 1, comments,
                                    generated_at="2026-11-13 12:00 UTC", **kw)


NO_EXTERNAL = re.compile(r"(?i)(https?:|mailto:|javascript:|blob:|cid:)")


def test_the_document_is_static_and_self_contained(decisions_review):
    html = build(decisions_review)
    assert html.startswith("<!doctype html>")
    assert "<script" not in html.lower()
    assert re.search(r"\son[a-z]+\s*=", html, re.I) is None
    assert NO_EXTERNAL.search(html) is None
    assert "//" not in html
    assert 'http-equiv="Content-Security-Policy"' in html
    assert export.EXPORT_CSP.replace('"', "&quot;") in html
    assert "<style>" in html and "@media print" in html
    assert "data:image/png;base64," in html
    assert "Checkout review" in html and "Read this first" in html
    assert "Generated 2026-11-13 12:00 UTC" in html


def test_every_block_type_is_rendered(decisions_review):
    html = build(decisions_review)
    for block_type in store.BLOCK_TYPES:
        assert 'data-block="%s"' % block_type in html, block_type


def test_mock_markup_cannot_bring_script_handlers_or_external_urls(decisions_review):
    html = build(decisions_review)
    assert "window.__mockPwned" not in html
    assert "window.__onerrorPwned" not in html
    assert "alert(1)" not in html
    assert "evil.example.test" not in html
    assert 'class="x"' in html


def test_artifact_is_a_figure_with_its_static_page(decisions_review):
    html = build(decisions_review)
    assert '<figure class="mr-artifact mr-artifact-d-frame">' in html
    assert "<figcaption>" in html and "Click sound" in html and "Take 3" in html
    assert "Artifact body" in html
    assert re.search(r"\.mr-aw-\d+-art \.inside-artifact\{color:red\}", html), "the page's CSS is scoped to its wrapper"
    assert "window.__artifactPwned" not in html


def comments():
    return [
        {"anchor": {"label": "on Checkout layout"}, "author": {"name": "Ada", "kind": "member"},
         "at": "2026-11-13T10:00:00Z", "body": "Use two columns.", "resolved": True,
         "replies": [{"author": {"name": "Blue Fox", "kind": "guest"}, "at": "2026-11-13T10:05:00Z",
                      "body": "Agreed."},
                     {"author": {"name": "Old Timer", "kind": "former"}, "at": "2026-11-13T10:06:00Z",
                      "body": "Former take."}]},
        {"anchor": {"label": "on the header"}, "author": {"name": "Bea", "kind": "guest"},
         "at": "2026-11-13T11:00:00Z", "body": "Tighten the spacing.", "resolved": False, "replies": []},
    ]


def test_comments_section_renders_only_when_asked(decisions_review):
    off = build(decisions_review, False)
    on = build(decisions_review, True, threads=comments())
    assert 'class="mr-comments"' not in off and "id=\"comments\"" not in off
    assert '<section class="mr-comments" id="comments">' in on
    assert "<h2>Comments</h2>" in on
    assert "Ada" in on and "Blue Fox (guest)" in on and "Former member" in on
    assert "Resolved" in on and "Tighten the spacing." in on
    assert '<div class="mr-reply">' in on
    assert "Use two columns." in on


def test_an_empty_thread_list_still_shows_the_section(decisions_review):
    html = build(decisions_review, True, threads=[])
    assert '<section class="mr-comments" id="comments">' in html
    assert "No comments." in html


def test_comment_text_is_escaped_not_interpreted(decisions_review):
    html = build(decisions_review, True, threads=[{
        "anchor": {"label": "<script>bad()</script>"}, "author": {"name": "<b>Eve</b>", "kind": "member"},
        "at": "", "body": "<img src=x onerror=alert(1)>", "resolved": False, "replies": []}])
    assert "<script>bad()" not in html
    assert "<b>Eve</b>" not in html
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_export_threads_without_the_optional_module(monkeypatch):
    monkeypatch.delitem(sys.modules, "mockreview.threads", raising=False)
    assert export.export_threads("some-review") == []


def test_export_threads_maps_and_normalises_a_stubbed_module(monkeypatch):
    fake = types.ModuleType("mockreview.threads")
    seen = []
    fake.threads_for_export = lambda rid: seen.append(rid) or [
        {"anchor": {"label": "on a card"}, "author": {"name": "Ada", "kind": "member"},
         "at": "2026-11-13T10:00:00Z", "body": "Hello", "resolved": 1,
         "replies": [{"author": {"name": "Bea", "kind": "guest"}, "at": "later", "body": "Hi"}]},
        {"body": "   "},
        {"body": "Kept"},
    ]
    monkeypatch.setitem(sys.modules, "mockreview.threads", fake)
    out = export.export_threads("rid-1")
    assert seen == ["rid-1"]
    assert [t["body"] for t in out] == ["Hello", "Kept"]
    assert out[0]["resolved"] is False
    assert out[0]["replies"][0]["author"] == {"name": "Bea", "kind": "guest"}
    assert out[1]["anchor"] == {"label": ""} and out[1]["replies"] == []


def test_export_threads_with_a_module_that_has_no_adapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "mockreview.threads", types.ModuleType("mockreview.threads"))
    assert export.export_threads("rid") == []


@pytest.fixture
def prototype_review(env, tmp_path):
    src = tmp_path / "proto"
    (src / "assets").mkdir(parents=True)
    (src / "assets" / "style.css").write_text(".proto{color:green}", encoding="utf-8")
    (src / "assets" / "pixel.png").write_bytes(PIXEL)
    (src / "index.html").write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="assets/style.css"></head>'
        '<body><h1>Prototype</h1><img src="assets/pixel.png">'
        '<img src="https://evil.example.test/x.png"><script>window.__protoPwned=1</script></body></html>',
        encoding="utf-8")
    return store.create_review("Try it live", "", ORIGIN, "local",
                               store.parse_round_spec(prototype=str(src / "index.html")), wake=False)


def test_prototype_stylesheet_and_image_are_inlined(prototype_review):
    html = build(prototype_review)
    assert ".proto{color:green}" in html
    assert "data:image/png;base64," in html
    assert "evil.example.test" not in html
    assert "window.__protoPwned" not in html
    assert "Prototype" in html and "Try it live" in html


def test_export_filename_is_a_safe_slug():
    data = {"title": "Checkout — Spring '26 / v2!"}
    name = export.export_filename(data, 3, "md")
    assert name == "checkout-spring-26-v2-round-3.md"
    long = export.export_filename({"title": "x" * 500}, 1, "png")
    assert len(long.split("-round-")[0]) <= 60


def test_missing_round_is_a_review_error(decisions_review):
    with pytest.raises(store.ReviewError):
        export.build_export_html(store.load(decisions_review["id"]), 99, False)


CSS_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))\s*\)""", re.I)


def css_urls(text):
    return [next(g for g in m.groups() if g is not None).strip() for m in CSS_URL.finditer(text)]


def safe_urls_only(text):
    return all(url == "" or url.startswith("#") or url.startswith("data:image/") for url in css_urls(text))


def test_css_escapes_are_decoded_before_the_inline_and_stylesheet_allowlists():
    source = r'<div id="css-escape" style="background-image:\75rl(\2f\2f export-leak.example.test/pixel)">safe prose</div>'
    css = r'.x{background-image:\75rl(\2f\2f export-leak.example.test/pixel)}'
    out = export._safe_fragment(source, export._Options())
    assert "export-leak" not in out and "safe prose" in out
    assert 'id="css-escape"' in out
    assert safe_urls_only(unescape(out))
    styles = export._escape_css(css)
    assert "export-leak" not in styles
    assert safe_urls_only(styles)


def test_every_escaped_or_commented_external_url_is_cleared_in_inline_and_stylesheet_css():
    inline = [
        r'background-image:\000075rl(\2f\2f h/p)',
        r'background-image:u\72l(//h/p)',
        r'background-image:\5c75rl(\5c2f\5c2f h/p)',
        r"background-image:url(  '//h/p'  )",
        r'background-image:/*x*/url(//h/p)',
        r'background-image:expression(alert(1))',
        r'background-image:url(data:text/html;base64,PHNjcmlwdD4=)',
        r'-moz-binding:url(//h/x.xml)',
    ]
    for payload in inline:
        out = export._safe_fragment(f'<div class="x" style="{payload}">safe prose</div>', export._Options())
        assert "//h" not in out and "export-leak" not in out, payload
        assert "safe prose" in out
        assert safe_urls_only(unescape(out)), payload
    sheets = inline + [r'@import url(//h/p);', r'@import url("//h/p") screen;']
    for payload in sheets:
        out = export._escape_css(".x{" + payload + "}" if not payload.startswith("@import") else payload)
        assert "//h" not in out and "export-leak" not in out, payload
        assert safe_urls_only(out), payload


def test_the_safe_css_controls_still_survive():
    sheet = (
        ".a{color:red;background:#fff;grid-template-columns:repeat(2,1fr);border:1px solid #e4e4df}"
        "@media (max-width:600px){.b{color:red}}"
        ".c{clip-path:url(#clip)}"
        ".d{background:url(data:image/png;base64,iVBORw0KGgo=)}"
    )
    out = export._escape_css(sheet)
    for piece in ("color:red", "background:#fff", "grid-template-columns:repeat(2,1fr)",
                  "border:1px solid #e4e4df", "@media (max-width:600px)", "url(#clip)",
                  "data:image/png;base64,iVBORw0KGgo="):
        assert piece in out, piece
    fragment = export._safe_fragment(
        '<div style="color:red;background:#fff;grid-template-columns:repeat(2,1fr);'
        'border:1px solid #e4e4df">safe prose</div>', export._Options())
    assert "color:red" in fragment and "background:#fff" in fragment
    assert "grid-template-columns:repeat(2,1fr)" in fragment
    assert "border:1px solid #e4e4df" in fragment


def test_a_variant_stylesheet_and_inline_style_are_sanitized_with_browser_semantics(env, tmp_path):
    src = tmp_path / "hostile"
    src.mkdir()
    payload = r"\75rl(\2f\2f export-leak.example.test/pixel)"
    (src / "index.html").write_text(
        "<!doctype html><html><head><style>"
        ".x{background:url(//h/p)}.y{background-image:" + payload + "}"
        "</style></head><body><div class=\"safe\" style=\"background-image:" + payload + "\">safe prose</div>"
        "</body></html>", encoding="utf-8")
    review = store.create_review("Hostile styles", "", ORIGIN, "local",
                                 store.parse_round_spec(prototype=str(src / "index.html")), wake=False)
    doc = build(review)
    assert "export-leak" not in doc and "//h" not in doc
    assert "safe prose" in doc
    assert safe_urls_only(unescape(doc))


def test_export_threads_passes_the_visible_rounds_and_drops_threads_outside_them(monkeypatch):
    fake = types.ModuleType("mockreview.threads")
    seen = {}

    def backend(rid, *, rounds=None):
        seen.update(rid=rid, rounds=rounds)
        return [{"body": "two", "round": 2}, {"body": "three", "round": 3},
                {"body": "two-n", "round_n": "2"}, {"body": "three-n", "round_n": 3},
                {"body": "no-round"}]

    fake.threads_for_export = backend
    monkeypatch.setitem(sys.modules, "mockreview.threads", fake)
    out = export.export_threads("rid-1", rounds=[2])
    assert seen == {"rid": "rid-1", "rounds": [2]}
    assert [t["body"] for t in out] == ["two", "two-n", "no-round"]


def test_thread_backend_errors_are_not_silent_empty_exports(monkeypatch):
    fake = types.ModuleType("mockreview.threads")

    def broken(rid):
        raise RuntimeError("synthetic thread storage failure")

    fake.threads_for_export = broken
    monkeypatch.setitem(sys.modules, "mockreview.threads", fake)
    with pytest.raises(RuntimeError, match="synthetic thread storage failure"):
        export.export_threads("rid")


def test_the_real_thread_store_has_an_export_view(review):
    """threads_for_export maps the real read model and honours an exact round allowlist (the export's contract)."""
    rid = review["id"]
    owner = users.owner_user()
    actor = f"user:{owner['id']}"
    first = threads.create(rid, actor, owner["name"], "owner", "Export body",
                           anchor={"block": "a", "x": 0.4, "y": 0.6,
                                   "target": {"path": "#alpha", "sig": {"tag": "h1", "h": "abcd"},
                                              "label": "Alpha heading"}})
    threads.reply_comment(rid, first["id"], actor, owner["name"], "owner", "Export reply")
    resolved = threads.create(rid, "guest:g_export", "Guest Name", "commenter", "Resolved body",
                              anchor={"block": "b", "x": 0.1, "y": 0.2})
    threads.set_status(rid, resolved["id"], "resolved", actor)
    past = account(review_id=rid, role="commenter", name="Former Person")
    threads.create(rid, f"user:{past['user_id']}", "Former Person", "commenter", "Former body")
    users.delete_user(actor, past["user_id"])

    out = {t["body"]: t for t in threads.threads_for_export(rid)}
    assert set(out) == {"Export body", "Resolved body", "Former body"}
    body = out["Export body"]
    assert body["anchor"] == {"label": "Alpha heading"}
    assert body["author"] == {"name": owner["name"], "kind": "member"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", body["at"])
    assert body["resolved"] is False and body["round"] == 1
    assert [(r["body"], r["author"]) for r in body["replies"]] == [
        ("Export reply", {"name": owner["name"], "kind": "member"})]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", body["replies"][0]["at"])
    assert out["Resolved body"]["resolved"] is True and out["Resolved body"]["anchor"] == {"label": "b"}
    assert out["Resolved body"]["author"] == {"name": "Guest Name", "kind": "guest"}
    assert out["Former body"]["author"] == {"name": "Former member", "kind": "former"}

    assert threads.threads_for_export(rid, rounds=[]) == []
    assert threads.threads_for_export(rid, rounds=[2]) == []
    assert {t["body"] for t in threads.threads_for_export(rid, rounds=[1, "nope", ""])} == set(out)


def test_the_export_view_uses_the_first_live_comment_and_skips_empty_bodies(review):
    rid = review["id"]
    owner = users.owner_user()
    actor = f"user:{owner['id']}"
    kept = threads.create(rid, actor, owner["name"], "owner", "Deleted first comment")
    threads.reply_comment(rid, kept["id"], actor, owner["name"], "owner", "Kept reply")
    threads.delete_comment(rid, kept["comments"][0]["id"], actor)

    out = threads.threads_for_export(rid)
    assert [(t["body"], t["replies"]) for t in out] == [("Kept reply", [])]


def _artifact_doc(tmp_path, blocks):
    spec = tmp_path / "decisions.json"
    spec.write_text(json.dumps({"intro": False, "outro": False, "blocks": blocks}), encoding="utf-8")
    review = store.create_review("Scope", "", {"platform": "discord", "chat_id": "test-chat"}, "local",
                                 store.parse_round_spec(decisions=str(spec)), wake=False)
    return export.build_export_html(store.load(review["id"]), 1, False)


def test_a_long_chain_of_root_selectors_is_rewritten_in_one_pass(env, tmp_path):
    """The rewrite of `html`, `body` and `:root` inside an artifact's CSS must not rescan the sheet once per token: a
    chain of 20 000 of them took tens of seconds when each pass removed one."""
    import time
    css = "html " * 120000 + "h1{color:red}"
    page = "<!doctype html><style>" + css + "</style><h1>x</h1>"
    started = time.monotonic()
    html = _artifact_doc(tmp_path, [{"type": "artifact", "id": "map", "title": "T", "html": page}])
    assert time.monotonic() - started < 3, "the root-selector rewrite is not linear"
    scope = re.search(r'class="mr-aw (mr-aw-[\w-]+)"', html).group(1)
    assert "." + scope + " h1{color:red}" in html or "." + scope + "h1" not in html
    assert not re.search(r"\." + re.escape(scope) + r"\s+html", html), "a root selector survived the rewrite"


def test_two_artifacts_whose_ids_differ_only_by_a_hyphen_keep_their_own_wrapper(env, tmp_path):
    blocks = [{"type": "artifact", "id": i, "title": i, "html": "<style>h1{color:red}</style><h1>a</h1>"}
              for i in ("map-a", "mapa")]
    html = _artifact_doc(tmp_path, blocks)
    wrappers = re.findall(r'class="mr-aw (mr-aw-[\w-]+)"', html)
    assert len(wrappers) == 2 and wrappers[0] != wrappers[1], wrappers
