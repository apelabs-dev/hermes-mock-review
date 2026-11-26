"""An artifact with display "canvas": a full-length, inline page for presentational bodies.

A frame is a card capped at 4000 px, a page is one viewport tall and scrolls inside. A canvas is as tall as its own page
and scrolls with the board, so the board's header and footer (the essential controls) stay where they are. These are
real-browser tests for the geometry, plus a plain test for the validator.
"""

import json

import pytest

from mockreview import store
from mockreview.store import ReviewError
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}

# Four sections of 1500 px: the document is 6000 px tall, far past the 4000 px cap of a frame.
TALL = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:#101820;color:#eef0f6;font:16px sans-serif}
section{height:1500px;box-sizing:border-box;padding:24px;border-bottom:1px solid #2a3340}
</style></head><body><section id="s1">One</section><section id="s2">Two</section>
<section id="s3">Three</section><section id="s4">Four</section></body></html>"""


def _review(tmp_path, blocks):
    d = tmp_path / "dec"
    d.mkdir()
    (d / "decisions.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")
    return store.create_review("Canvas", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(d / "decisions.json")), wake=False)


def _open(chrome, server, data, width=1440, height=900):
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height,
              wait="!!document.querySelector('#dz-cv iframe.art-frame') && !!document.querySelector('#dz-fr iframe.art-frame')")
    b.review = data
    return b


def test_the_validator_accepts_canvas_and_names_the_three_displays(tmp_path):
    def spec(display):
        d = tmp_path / display
        d.mkdir()
        (d / "decisions.json").write_text(json.dumps({"blocks": [
            {"type": "artifact", "id": "cv", "title": "Deck", "html": TALL, "display": display}]}), encoding="utf-8")
        return store.parse_round_spec(decisions=str(d / "decisions.json"))

    for display in ("frame", "page", "canvas"):
        blocks = spec(display)["decisions"]["blocks"]
        assert blocks[0]["display"] == display
    with pytest.raises(ReviewError) as e:
        spec("stage")
    assert '"frame", "page" or "canvas"' in str(e.value), e.value


def test_a_canvas_is_as_tall_as_its_page_and_a_frame_is_still_capped(env, server, tmp_path, chrome):
    data = _review(tmp_path, [
        {"type": "artifact", "id": "cv", "title": "Canvas", "html": TALL, "display": "canvas"},
        {"type": "artifact", "id": "fr", "title": "Frame", "html": TALL},
    ])
    b = _open(chrome, server, data)
    assert b.until("(function(){var f=document.querySelector('#dz-cv iframe');return !!f && f.getBoundingClientRect().height>4000})()",
                   timeout=20), b.js("document.querySelector('#dz-cv iframe').style.height")
    g = json.loads(b.js("""JSON.stringify((function(){
      function r(sel){var e=document.querySelector(sel).getBoundingClientRect();return {w:e.width,h:e.height,l:e.left};}
      var s=document.querySelector('#dz-cv');
      return {cv:r('#dz-cv iframe'),fr:r('#dz-fr iframe'),cw:document.documentElement.clientWidth,cls:s.className,
              art:getComputedStyle(document.querySelector('#dz-cv .art')).borderRadius};})())"""))
    assert abs(g["cv"]["h"] - 6000) <= 2, g
    assert abs(g["cv"]["w"] - g["cw"]) <= 1 and abs(g["cv"]["l"]) <= 1, g   # full bleed, like a page
    assert "blk-art-canvas" in g["cls"] and g["art"] == "0px", g
    assert abs(g["fr"]["h"] - 4000) <= 2, g                                  # the plain frame keeps its cap


def test_the_header_and_footer_stay_on_screen_over_a_long_canvas(env, server, tmp_path, chrome):
    data = _review(tmp_path, [
        {"type": "artifact", "id": "cv", "title": "Canvas", "html": TALL, "display": "canvas"},
        {"type": "artifact", "id": "fr", "title": "Frame", "html": TALL},
    ])
    b = _open(chrome, server, data)
    assert b.until("document.querySelector('#dz-cv iframe').getBoundingClientRect().height>4000", timeout=20)
    shell = """JSON.stringify((function(){
      function box(sel){var e=document.querySelector(sel);if(!e)return null;var r=e.getBoundingClientRect();
        return {top:r.top,bottom:r.bottom,w:r.width,h:r.height};}
      return {inner:innerHeight,bar:box('.dzbar'),foot:box('#dzfoot'),send:box('#dzfoot .sendbar'),
              tab:box('#dzfoot .round-tab'),comment:box('#comment-toggle,#comment-toggle-mobile'),y:scrollY};})())"""
    for y in (0, 3000, "max"):
        b.js("window.scrollTo(0, %s)" % ("document.documentElement.scrollHeight" if y == "max" else y))
        b.cdp.pump(0.4)
        g = json.loads(b.js(shell))
        assert g["bar"] and g["bar"]["top"] >= -1 and g["bar"]["bottom"] <= g["inner"], (y, g)
        assert g["foot"] and g["foot"]["bottom"] <= g["inner"] + 1 and g["foot"]["h"] > 20, (y, g)
        for part in ("send", "tab", "comment"):
            assert g[part] and g[part]["w"] > 0, (y, part, g)


def test_a_pin_far_down_a_canvas_opens_its_composer_near_the_tap(env, server, tmp_path, chrome):
    """Taps are measured in the frame, which is the page's whole height: the 4000 px clamp put the composer ~1000 px away."""
    data = _review(tmp_path, [{"type": "artifact", "id": "cv", "title": "Canvas", "html": TALL, "display": "canvas"},
                              {"type": "artifact", "id": "fr", "title": "Frame", "html": TALL}])
    b = _open(chrome, server, data)
    assert b.until("document.querySelector('#dz-cv iframe').getBoundingClientRect().height>5000", timeout=20)
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    top = b.js("document.querySelector('#dz-cv iframe').getBoundingClientRect().top + scrollY")
    b.js("window.scrollTo(0, %d)" % (top + 5000 - 500))   # page y 5000 lands at viewport y ~500
    b.cdp.pump(0.6)
    frame_top = b.js("document.querySelector('#dz-cv iframe').getBoundingClientRect().top")
    y = frame_top + 5000
    assert 150 < y < 750, y
    b.click(300, y)
    assert b.until("!!document.querySelector('.popover')", timeout=10), "the tap opened no pin composer"
    top_of_popover = b.js("document.querySelector('.popover').getBoundingClientRect().top")
    assert abs(top_of_popover - y) < 450, f"composer at {top_of_popover}, tap at {y}"
