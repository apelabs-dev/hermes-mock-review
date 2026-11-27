"""An artifact with display "long": a full-length, inline page for presentational bodies.

A frame is a card capped at 4000 px, a page is one viewport tall and scrolls inside. A long page is as tall as its own page
and scrolls with the board, so the board's header and footer (the essential controls) stay where they are. These are
real-browser tests for the geometry, plus a plain test for the validator.
"""

import json
import time

import pytest

from mockreview import store
from mockreview.store import ReviewError
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}

# The draft the board keeps for this round, whichever review it belongs to.
DRAFT = ("Object.values(localStorage).map(v=>{try{return JSON.parse(v)}catch(_){return null}})"
         ".find(v=>v&&v.pins&&v.pins.length)")

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
    return store.create_review("Long page", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(d / "decisions.json")), wake=False)


def _open(chrome, server, data, width=1440, height=900):
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height,
              wait="!!document.querySelector('#dz-cv iframe.art-frame') && !!document.querySelector('#dz-fr iframe.art-frame')")
    b.review = data
    return b


def test_the_validator_accepts_long_and_names_the_three_displays(tmp_path):
    def spec(display):
        d = tmp_path / display
        d.mkdir()
        (d / "decisions.json").write_text(json.dumps({"blocks": [
            {"type": "artifact", "id": "cv", "title": "Deck", "html": TALL, "display": display}]}), encoding="utf-8")
        return store.parse_round_spec(decisions=str(d / "decisions.json"))

    for display in ("frame", "page", "long"):
        blocks = spec(display)["decisions"]["blocks"]
        assert blocks[0]["display"] == display
    with pytest.raises(ReviewError) as e:
        spec("stage")
    assert '"frame", "page" or "long"' in str(e.value), e.value


def test_a_long_page_is_as_tall_as_its_page_and_a_frame_is_still_capped(env, server, tmp_path, chrome):
    data = _review(tmp_path, [
        {"type": "artifact", "id": "cv", "title": "Long page", "html": TALL, "display": "long"},
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
    assert "blk-art-long" in g["cls"] and g["art"] == "0px", g
    assert abs(g["fr"]["h"] - 4000) <= 2, g                                  # the plain frame keeps its cap


def test_the_header_and_footer_stay_on_screen_over_a_long_page(env, server, tmp_path, chrome):
    data = _review(tmp_path, [
        {"type": "artifact", "id": "cv", "title": "Long page", "html": TALL, "display": "long"},
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


def test_a_pin_far_down_a_long_page_opens_its_composer_near_the_tap(env, server, tmp_path, chrome):
    """Taps are measured in the frame, which is the page's whole height: the 4000 px clamp put the composer ~1000 px away."""
    data = _review(tmp_path, [{"type": "artifact", "id": "cv", "title": "Long page", "html": TALL, "display": "long"},
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


def _add_pin(b, block, page_y, text="Please adjust this"):
    """Tap the artifact page at `page_y` and save the pin, so the draft holds a thread pin there."""
    if b.js("!!document.querySelector('.popover')"):
        b.press(".popover .pop-close, .popover .tp-close")
        assert b.until("!document.querySelector('.popover')", timeout=10)
    top = b.js("document.querySelector('#dz-%s iframe').getBoundingClientRect().top + scrollY" % block)
    b.js("window.scrollTo(0, %d)" % round(top + page_y - 500))
    b.cdp.pump(0.4)
    y = b.js("document.querySelector('#dz-%s iframe').getBoundingClientRect().top" % block) + page_y
    assert 150 < y < 750, y
    b.click(300, y)
    assert b.until("!!document.querySelector('.popover textarea')", timeout=10), "the tap opened no pin composer"
    assert b.js("(document.querySelector('.popover').getAttribute('aria-label')||'').indexOf('New pin')===0"), \
        b.js("document.querySelector('.popover').getAttribute('aria-label')")
    b.js("(function(t){t.value=%s;t.dispatchEvent(new Event('input'));return 1})"
         "(document.querySelector('.popover textarea'))" % json.dumps(text))
    b.press(".popover .btn-primary")
    assert b.until("!!(%s)" % DRAFT, timeout=10), "the saved pin never reached the draft"
    time.sleep(0.3)   # saveDraft writes 250 ms after the change


def _pin_viewport_y(block):
    """A JS expression for where the block's pin page point sits in the viewport: a long-page frame is the page at scale 1."""
    return ("(function(){var p=(%s).pins.find(p=>p.block===%s);"
            "var r=document.querySelector('#dz-%s iframe').getBoundingClientRect();"
            "return r.top + p.y*r.height})()" % (DRAFT, json.dumps(block), block))


def test_a_markable_long_page_and_page_are_full_bleed_and_a_markable_frame_is_not(env, server, tmp_path, chrome):
    """A markable artifact keeps its decision row but must break out of the column exactly like a plain one."""
    data = _review(tmp_path, [
        {"type": "artifact", "id": "mc", "title": "Markable long page", "html": TALL, "display": "long", "markable": True},
        {"type": "artifact", "id": "mp", "title": "Markable page", "html": TALL, "display": "page", "markable": True},
        {"type": "artifact", "id": "mf", "title": "Markable frame", "html": TALL, "markable": True},
    ])
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-mc iframe.art-frame') && "
                   "!!document.querySelector('#dz-mp iframe.art-frame') && "
                   "!!document.querySelector('#dz-mf iframe.art-frame')")
    b.review = data
    assert b.until("document.querySelector('#dz-mc iframe').getBoundingClientRect().height>5000", timeout=20)
    g = json.loads(b.js("""JSON.stringify((function(){
      function box(sel){var s=document.querySelector(sel),f=s.querySelector('iframe'),r=f.getBoundingClientRect(),
        a=f.parentElement,cs=getComputedStyle(a);
        return {cls:s.className,left:r.left,width:r.width,height:r.height,radius:cs.borderRadius,
                outline:cs.outlineStyle,head:!!s.querySelector('.ihead h3'),decide:!!s.querySelector('.decide'),
                pins:!!s.querySelector('.ipins')};}
      return {clientWidth:document.documentElement.clientWidth,mc:box('#dz-mc'),mp:box('#dz-mp'),mf:box('#dz-mf')};})())"""))
    mc, mp, mf = g["mc"], g["mp"], g["mf"]
    assert "blk-art-long" in mc["cls"], mc
    assert "blk-art-page" in mp["cls"], mp
    assert "blk-art-long" not in mf["cls"] and "blk-art-page" not in mf["cls"], mf
    for named in (mc, mp):
        assert abs(named["left"]) <= 1 and abs(named["width"] - g["clientWidth"]) <= 1, named   # full bleed
        assert named["radius"] == "0px" and named["outline"].startswith("none"), named
        assert named["head"] and named["decide"] and named["pins"], named                     # still a decision
    assert abs(mc["height"] - 6000) <= 2, mc
    assert mp["height"] > 700, mp
    assert mf["radius"] == "14px" and mf["width"] < 1000, mf     # the markable frame is unchanged: a card in the column
    assert abs(mf["height"] - 4000) <= 2, mf
    b.press("#dz-mc .decide .s-approve")
    assert b.until("document.querySelector('#dz-mc .ihead .status').textContent === 'Approved'"), \
        b.js("document.querySelector('#dz-mc .ihead .status').textContent")


def test_a_pin_list_row_brings_a_far_long_page_pin_into_view(env, server, tmp_path, chrome):
    """A long page has no inner scroll: the pin's row must scroll the board to the pin, and a frame must not change."""
    data = _review(tmp_path, [
        {"type": "artifact", "id": "cv", "title": "Long page", "html": TALL, "display": "long"},
        {"type": "artifact", "id": "fr", "title": "Frame", "html": TALL},
    ])
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-cv iframe.art-frame') && "
                   "!!document.querySelector('#dz-fr iframe.art-frame')")
    b.review = data
    assert b.until("document.querySelector('#dz-cv iframe').getBoundingClientRect().height>5000", timeout=20)
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='true'")
    _add_pin(b, "cv", 5000, "far down the long page")
    assert b.until("document.querySelectorAll('#pins-cv li').length===1", timeout=10)

    b.js("window.scrollTo(0, 0)")
    b.cdp.pump(0.3)
    assert b.js("scrollY") == 0
    b.press("#pins-cv .pin-row")
    assert b.until("!!document.querySelector('.popover.tpop')", timeout=10), "the row opened no thread"
    at = _pin_viewport_y("cv")
    assert b.until("(function(){var y=%s;return y>0 && y<innerHeight})()" % at, timeout=10), \
        "the pin is at viewport y %s" % b.js(at)
    assert b.js("scrollY") > 1000

    # A frame is unchanged: a thread pin's row opens the thread and never moves the board.
    _add_pin(b, "fr", 1000, "up on the frame")
    b.js("window.scrollTo(0, 0)")
    b.cdp.pump(0.3)
    assert b.js("scrollY") == 0
    b.press("#pins-fr .pin-row")
    assert b.until("!!document.querySelector('.popover.tpop')", timeout=10)
    time.sleep(0.5)   # a scroll, if one were started, would have moved by now
    assert b.js("scrollY") == 0, "a frame pin's row must not scroll the board"


def _reload_with_threadless_pin(b, block, page_y):
    """Reload over a draft holding one pin with no thread.

    No server thread exists for it, so no thread marker is drawn over it: the marker the test taps is the pin's own.
    The guideline field makes the board write its draft, which is where the key comes from.
    """
    b.js("(function(){var t=document.querySelector('.blk-else .field');if(!t)return 0;"
         "t.value='seed';t.dispatchEvent(new Event('input'));return 1})()")
    time.sleep(0.35)   # saveDraft writes 250 ms after the change
    page_h = b.js("document.querySelector('#dz-%s iframe').getBoundingClientRect().height" % block)
    old = json.loads(b.js(
        "JSON.stringify((()=>{let e=Object.entries(localStorage).find(([k,v])=>{try{return JSON.parse(v).pins}"
        "catch(_){return false}});let d=JSON.parse(e[1]);"
        "d.pins=[{block:%s,option:null,w:'desktop',x:%s,y:%s,text:'far down'}];d.guidelines='';"
        "return [e[0],JSON.stringify(d)]})())" % (json.dumps(block), round(300 / 1440, 5), round(page_y / page_h, 5))))
    script = b.cdp.call("Page.addScriptToEvaluateOnNewDocument",
                        {"source": "localStorage.setItem(%s,%s)" % (json.dumps(old[0]), json.dumps(old[1]))},
                        session=b.sid)
    b.cdp.call("Page.reload", session=b.sid)
    return script


def test_a_pin_list_row_brings_a_threadless_far_long_page_pin_into_view(env, server, tmp_path, chrome):
    """A pin whose thread never reached the server takes the same row path: scroll the board before the flash."""
    data = _review(tmp_path, [{"type": "artifact", "id": "cv", "title": "Long page", "html": TALL, "display": "long"}])
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-cv iframe.art-frame')")
    b.review = data
    assert b.until("document.querySelector('#dz-cv iframe').getBoundingClientRect().height>5000", timeout=20)
    seed = _reload_with_threadless_pin(b, "cv", 5000)
    try:
        assert b.until("document.querySelector('#dz-cv iframe') && "
                       "document.querySelector('#dz-cv iframe').getBoundingClientRect().height>5000", timeout=30)
        assert b.until("document.querySelectorAll('#pins-cv li').length===1", timeout=10)
        b.js("window.scrollTo(0, 0)")
        b.cdp.pump(0.3)
        assert b.js("scrollY") == 0
        b.press("#pins-cv .pin-row")
        assert b.until("!!document.querySelector('.popover textarea')", timeout=10), "the row opened no pin editor"
        assert b.js("(document.querySelector('.popover').getAttribute('aria-label')||'').indexOf('Pin 1')===0"), \
            b.js("document.querySelector('.popover').getAttribute('aria-label')")
        at = _pin_viewport_y("cv")
        assert b.until("(function(){var y=%s;return y>0 && y<innerHeight})()" % at, timeout=10), \
            "the pin is at viewport y %s" % b.js(at)
        assert b.js("scrollY") > 1000
    finally:
        b.cdp.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": seed["identifier"]}, session=b.sid)


def test_a_marker_tap_far_down_a_long_page_opens_the_editor_near_the_marker(env, server, tmp_path, chrome):
    """The 'pin' message from an existing marker is clamped like the first tap: a 4000 px clamp puts the editor ~1000 px away."""
    data = _review(tmp_path, [{"type": "artifact", "id": "cv", "title": "Long page", "html": TALL, "display": "long"}])
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-cv iframe.art-frame')")
    b.review = data
    assert b.until("document.querySelector('#dz-cv iframe').getBoundingClientRect().height>5000", timeout=20)
    seed = _reload_with_threadless_pin(b, "cv", 5200)
    try:
        assert b.until("document.querySelector('#dz-cv iframe') && "
                       "document.querySelector('#dz-cv iframe').getBoundingClientRect().height>5000", timeout=30)
        assert b.until("document.querySelectorAll('#pins-cv li').length===1", timeout=10)
        time.sleep(0.5)   # the probe redraws its markers from the state the board pushes after load
        info = json.loads(b.js("""JSON.stringify((function(){var p=(%s).pins[0],
          r=document.querySelector('#dz-cv iframe').getBoundingClientRect();
          return {y:p.y,w:p.x,top:r.top+scrollY,h:r.height};})())""" % DRAFT))
        b.js("window.scrollTo(0, %d)" % round(info["top"] + info["y"] * info["h"] - 500))
        b.cdp.pump(0.4)
        top = b.js("document.querySelector('#dz-cv iframe').getBoundingClientRect().top")
        tap_y = top + info["y"] * info["h"] - 14          # the middle of the 28 px marker
        tap_x = info["w"] * b.js("document.querySelector('#dz-cv iframe').getBoundingClientRect().width") + 14
        assert 300 < tap_y < 700, tap_y
        b.click(tap_x, tap_y)
        assert b.until("!!document.querySelector('.popover textarea')", timeout=10), "the marker tap opened no editor"
        assert b.js("(document.querySelector('.popover').getAttribute('aria-label')||'').indexOf('Pin 1')===0"), \
            b.js("document.querySelector('.popover').getAttribute('aria-label')")
        top_of_popover = b.js("document.querySelector('.popover').getBoundingClientRect().top")
        assert abs(top_of_popover - tap_y) < 200, f"editor at {top_of_popover}, marker at {tap_y}"
    finally:
        b.cdp.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": seed["identifier"]}, session=b.sid)
