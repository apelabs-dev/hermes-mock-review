"""CUX item 1: a pin can name any word or element, on every board surface.

Real headless Chrome and real input events (Input.dispatchMouseEvent), because the defect these tests guard is a click
path, not markup. A round stored before canvas pins existed (or whose block count changed) has no `sections`; the
board must derive the same refs `store.section_index` would, so a click no longer falls back to the whole canvas and
dies in the server's path validation. Every assertion in this module fails on the CUX base.
"""

import json
import time

import pytest

from mockreview import store
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
MOCK = ('<section><h2>Checkout</h2><p id="copy" style="width:300px;height:60px;margin:20px">'
        'Review this total now</p></section>')
ARTIFACT = ('<!doctype html><html><body><h1 id="ahead">Artifact heading</h1>'
            '<p id="acopy" style="margin:20px">Artifact copy for the frame</p></body></html>')
DECISIONS = {
    "intro": "Click a word in this intro, or any block on the board.",
    "blocks": [
        {"type": "text", "id": "t1", "title": "Why this round",
         "text": "Two rows in the compare table still read two taps.\n\nSecond paragraph, longer than the first one."},
        {"type": "callout", "id": "c1", "title": "Constraint", "tone": "warn",
         "text": "Keep the header and the table."},
        {"type": "cards", "title": "What is changing", "items": [
            {"title": "One tap", "text": "Fewer taps on every row."},
            {"title": "Copy", "text": "Clearer words for the same choice."}]},
        {"type": "table", "title": "How the options compare", "columns": ["Aspect", "A - Today", "B - Chip"],
         "rows": [["Fits one phone screen", "Yes", "Yes"], ["Taps to comment", "3", "2"]]},
        {"type": "settled", "title": "Already decided", "items": [
            {"title": "Roadmap", "quote": "One direction this round", "rule": "No second version."}]},
        {"type": "recap", "id": "r1", "title": "Round record", "items": ["First item", "Second item"]},
        {"type": "defaults", "id": "df", "title": "Everything else", "text": "No other changes.",
         "items": ["Keep the rest as it is."]},
        {"type": "question", "id": "q1", "title": "Which one", "text": "Pick one of the two options.",
         "recommendation": {"answer": "A - Today", "why": "Simpler for everyone"}},
        {"type": "decision", "id": "d1", "title": "The pick", "options": [
            {"id": "a", "label": "Today", "html": "<p>Today</p>"},
            {"id": "b", "label": "Chip", "html": "<p>Chip</p>"}]},
        {"type": "mock", "id": "m1", "title": "Checkout", "html": MOCK},
        {"type": "artifact", "id": "a1", "title": "Artifact", "markable": True, "html": ARTIFACT},
    ],
}

DRAFT = ("Object.values(localStorage).map(function(v){try{return JSON.parse(v)}catch(e){return null}})"
         ".find(function(v){return v&&v.pins&&v.pins.length})")
ROOT = "document.querySelector('#dz-m1 .mock-host').shadowRoot"


@pytest.fixture
def cux_review(env, tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("CUX anchors", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)),
                               wake=False)
    return data["id"]


def open_board(chrome, server, rid):
    return Board(chrome, f"{server}/r/{rid}/",
                 wait="!!document.querySelector('.blk-text') && !!document.querySelector('.mock-host')")


def on(b):
    if b.js("document.querySelector('#comment-toggle').getAttribute('aria-pressed')") != "true":
        b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")


def reload(b, server, rid):
    b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
    assert b.until("!!document.querySelector('.blk-text') && !!document.querySelector('.mock-host')", timeout=60)
    time.sleep(0.4)


def fresh_reload(b, server, rid):
    """Navigate to an empty draft, even when the old document still has a debounced draft write queued.

    saveDraft writes 250 ms after a change. Clearing localStorage from the old document and reloading leaves a
    window where that write lands after the clear, so the next board resurrects the previous pin. Clearing from a
    document-start script runs after the old document (and its timers) is gone, so the new board always starts empty.
    """
    added = b.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": "localStorage.clear()"}, session=b.sid)
    try:
        reload(b, server, rid)
    finally:
        b.cdp.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": added["identifier"]}, session=b.sid)


def point(b, expr):
    got = b.js("JSON.stringify((function(){var e=%s;if(!e)return 'null';Element.prototype.scrollIntoView.call(e,{block:'center'});"
               "var r=Element.prototype.getBoundingClientRect.call(e);return [r.left+r.width/2,r.top+r.height/2]})())"
               % expr)
    assert got != "null", expr
    b.cdp.pump(0.08)
    return json.loads(got)


def click(b, expr):
    pt = point(b, expr)
    assert isinstance(pt, list) and len(pt) == 2, (expr, pt)
    b.click(*pt)


def draft(b):
    got = b.js("JSON.stringify(%s)" % DRAFT)
    return json.loads(got) if got and got != "null" else None


def surface_pin(b, expr):
    """The marker drawn in this surface's own pin layer, not a thread left on another surface."""
    got = b.js("JSON.stringify((function(){var e=%s;"
               "var sec=e.closest('[data-mr-ref]')||document.querySelector('.doc');"
               "var p=sec?sec.querySelector(':scope > .pin-layer > .mpin'):null;if(!p)return null;"
               "var r=p.getBoundingClientRect();"
               "return {det:p.classList.contains('det'),x:r.left+r.width/2+scrollX,y:r.top+r.height/2+scrollY}})())"
               % expr)
    return json.loads(got) if got and got != "null" else None


def surface_centre(b, expr):
    got = b.js("JSON.stringify((function(){var e=%s;if(!e)return null;var r=e.getBoundingClientRect();"
               "return [r.left+r.width/2+scrollX,r.top+r.height/2+scrollY]})())" % expr)
    return json.loads(got) if got and got != "null" else None


def save_pin(b, text="Please adjust this"):
    assert b.until("!!document.querySelector('.popover textarea')")
    b.js("(function(t){t.value=%s;t.dispatchEvent(new Event('input'));return 1})(document.querySelector('.popover textarea'))"
         % json.dumps(text))
    b.press(".popover .composer-actions .btn-round")
    assert b.until("!!(%s)" % DRAFT, timeout=10), "the pin did not reach the draft"


def word_points(b, expr):
    got = b.js("JSON.stringify((function(){var e=%s;if(!e)return null;Element.prototype.scrollIntoView.call(e,{block:'center'});"
               "var w=document.createTreeWalker(e,NodeFilter.SHOW_TEXT),n;"
               "while((n=w.nextNode())){var m=/[^\\s]+/.exec(n.textContent);if(!m)continue;var r=document.createRange();"
               "r.setStart(n,m.index);r.setEnd(n,m.index+m[0].length);var rs=r.getClientRects();if(!rs.length)continue;"
               "var a=rs[0],z=rs[rs.length-1];return [m[0],a.left+1,(a.top+a.bottom)/2,z.right-1,(z.top+z.bottom)/2]}"
               "return null})())" % expr)
    assert got and got != "null", expr
    return json.loads(got)


def drag_word(b, coords, session=None):
    word, x1, y1, x2, y2 = coords
    sid = session or b.sid
    b.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x1, "y": y1, "button": "none", "clickCount": 0,
                                            "buttons": 0}, session=sid)
    b.cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x1, "y": y1, "button": "left", "clickCount": 1,
                                            "buttons": 1}, session=sid)
    steps = 6
    for i in range(1, steps + 1):
        b.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x1 + (x2 - x1) * i / steps,
                                                "y": y1 + (y2 - y1) * i / steps, "button": "left", "buttons": 1},
                   session=sid)
        b.cdp.pump(0.02)
    b.cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x2, "y": y2, "button": "left",
                                            "clickCount": 1, "buttons": 0}, session=sid)
    assert b.until("!!document.querySelector('.popover')", timeout=10), "the word selection opened no composer"
    return word


def artifact_frame_session(b, rid):
    target = b.frame_target(lambda url: rid in url and url.endswith("a1.html"))
    sid = b.cdp.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["sessionId"]
    b.cdp.call("Runtime.enable", session=sid)
    return sid


def frame_word_points(b, session, expr="#acopy"):
    got = b.cdp.call("Runtime.evaluate", {"expression":
        "JSON.stringify((function(){var e=document.querySelector(%s);if(!e)return null;"
        "e.scrollIntoView({block:'center'});"
        "var w=document.createTreeWalker(e,NodeFilter.SHOW_TEXT),n;"
        "while((n=w.nextNode())){var m=/[^\\s]+/.exec(n.textContent);if(!m)continue;var r=document.createRange();"
        "r.setStart(n,m.index);r.setEnd(n,m.index+m[0].length);var rs=r.getClientRects();if(!rs.length)continue;"
        "var a=rs[0],z=rs[rs.length-1];return [m[0],a.left+1,(a.top+a.bottom)/2,z.right-1,(z.top+z.bottom)/2]}"
        "return null})())" % json.dumps(expr),
        "returnByValue": True}, session=session)["result"]["value"]
    assert got and got != "null", "the frame word was not found"
    return json.loads(got)


def frame_point(b, session, expr):
    got = b.cdp.call("Runtime.evaluate", {"expression":
        "JSON.stringify((function(){var e=document.querySelector(%s);if(!e)return null;"
        "var r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})())"
        % json.dumps(expr), "returnByValue": True}, session=session)["result"]["value"]
    assert got and got != "null", "the frame element was not found"
    return json.loads(got)


def frame_pin_host(b, rid, point):
    time.sleep(0.8)
    frames = [t for t in b.cdp.call("Target.getTargets")["targetInfos"]
              if t["type"] == "iframe" and rid in t["url"] and t["url"].endswith("a1.html")]
    assert frames, "artifact: the frame is gone after the reload"
    got = None
    for _ in range(30):
        for frame in frames:
            try:
                fsid = b.cdp.call("Target.attachToTarget", {"targetId": frame["targetId"], "flatten": True})["sessionId"]
                got = json.loads(b.cdp.call("Runtime.evaluate", {"expression":
                    "JSON.stringify((function(){var top=document.elementFromPoint(%s,%s);"
                    "return {host:!!top&&top.tagName.toLowerCase()==='mr-overlay',"
                    "overlay:!!document.querySelector('mr-overlay')}})())" % (point[0], point[1]),
                    "returnByValue": True}, session=fsid)["result"]["value"])
            except Exception:
                continue
            if got.get("host"):
                return got
        time.sleep(0.3)
    return got


# click target -> the expected anchor surface, in the order the brief lists them
SURFACES = [
    ("board header", "document.querySelector('.doc-head h1')"),
    ("board intro", "document.querySelector('.doc-head > p')"),
    ("text block paragraph", "document.querySelector('.blk-text p:nth-of-type(2)')"),
    ("callout", "document.querySelector('.blk-callout .callout-title')"),
    ("cards item", "document.querySelector('.blk-cards .card:nth-of-type(2)')"),
    ("table body cell", "document.querySelector('.blk-table tbody tr:nth-of-type(2) td:nth-of-type(2)')"),
    ("table header cell", "document.querySelector('.blk-table thead th:nth-of-type(3)')"),
    ("settled item", "document.querySelector('.blk-settled blockquote')"),
    ("recap item", "document.querySelector('#dz-r1 .recap li:nth-of-type(2)')"),
    ("defaults item", "document.querySelector('#dz-df .recap li')"),
    ("question text", "document.querySelector('#dz-q1 .itext')"),
    ("decision option caption", "document.querySelector('#dz-d1 .opt[data-o=\"b\"] .opt-cap')"),
    ("mock content", ROOT + ".querySelector('#copy')"),
]

WORD_SURFACES = [
    ("board intro", "document.querySelector('.doc-head > p')"),
    ("text block paragraph", "document.querySelector('.blk-text p:nth-of-type(2)')"),
    ("callout", "document.querySelector('.blk-callout .callout-body p')"),
    ("cards item", "document.querySelector('.blk-cards .card:nth-of-type(2) .card-text')"),
    ("table body cell", "document.querySelector('.blk-table tbody tr:nth-of-type(1) td:nth-of-type(2)')"),
    ("settled item", "document.querySelector('.blk-settled blockquote')"),
    ("recap item", "document.querySelector('#dz-r1 .recap li:nth-of-type(2)')"),
    ("defaults item", "document.querySelector('#dz-df .recap li')"),
    ("question text", "document.querySelector('#dz-q1 .itext')"),
    ("decision option caption", "document.querySelector('#dz-d1 .opt[data-o=\"b\"] .opt-cap b')"),
    ("mock content", ROOT + ".querySelector('#copy')"),
]


def test_every_element_on_every_surface_anchors_and_survives_a_reload(cux_review, server, chrome):
    rid = cux_review
    b = open_board(chrome, server, rid)
    try:
        for label, expr in SURFACES:
            fresh_reload(b, server, rid)
            on(b)
            click(b, expr)
            assert b.until("!!document.querySelector('.popover')"), f"{label}: a click opened no composer"
            where = b.js("document.querySelector('.popover .pop-where').textContent")
            assert where.startswith("on "), f"{label}: the composer where-line must name the target: {where!r}"
            save_pin(b)
            pin = draft(b)["pins"][0]
            assert pin.get("target") and pin["target"].get("path"), f"{label}: the saved pin carries no target path: {pin}"
            reload(b, server, rid)
            on(b)
            assert b.until("!!document.querySelector('.mpin')", timeout=30), f"{label}: the pin did not come back"
            assert b.js("!document.querySelector('.mpin').classList.contains('det')"), \
                f"{label}: the pin detached after a reload"
            if label != "mock content" and b.js(
                    "!!document.querySelector('.dzbar .wseg .seg-btn[data-value=\"phone\"]')"):
                b.press('.dzbar .wseg .seg-btn[data-value="phone"]')
                assert b.until("document.querySelector('.dzbar .wseg .seg-btn[data-value=\"phone\"]')"
                               ".getAttribute('aria-checked') === 'true'", timeout=30)
                b.cdp.pump(0.4)
                surface = surface_centre(b, expr)
                assert surface is not None, f"{label}: the surface is gone on Mobile"
                pin = None
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    pin = surface_pin(b, expr)
                    surface = surface_centre(b, expr) or surface
                    if pin and abs(pin["x"] - surface[0]) < 30 and abs(pin["y"] - surface[1]) < 30:
                        break
                    b.cdp.pump(0.2)
                assert pin, f"{label}: the pin vanished on Mobile"
                assert not pin["det"], f"{label}: the pin detached on Mobile"
                after = [pin["x"], pin["y"]]
                assert abs(after[0] - surface[0]) < 30 and abs(after[1] - surface[1]) < 30, \
                    f"{label}: the pin is not on its element after the width switch ({(after, surface)})"
            b.js("localStorage.clear()")
        # The artifact is its own frame: the pin is saved from the probe's own target, and the probe resolves it there.
        fresh_reload(b, server, rid)
        on(b)
        assert b.until("!!document.querySelector('.art-frame')", timeout=30)
        frame_sid = artifact_frame_session(b, rid)
        local = json.loads(b.cdp.call("Runtime.evaluate", {"expression":
            "JSON.stringify((function(){var e=document.querySelector('#acopy');var r=e.getBoundingClientRect();"
            "return [r.left+r.width/2,r.top+r.height/2]})())", "returnByValue": True}, session=frame_sid)["result"]["value"])
        # Dispatch the click in the frame's own session: its probe owns artifact anchoring, and a page-level event can
        # land on the iframe element instead after the frame grows.
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            b.cdp.call("Input.dispatchMouseEvent", {"type": kind, "x": local[0], "y": local[1],
                                                    "button": "left" if kind != "mouseMoved" else "none",
                                                    "clickCount": 1}, session=frame_sid)
        assert b.until("!!document.querySelector('.popover')"), "artifact: a click in the frame opened no composer"
        where = b.js("document.querySelector('.popover .pop-where').textContent")
        assert where.startswith("on ") and "iframe" not in where, \
            "artifact: the composer must name the frame element, not the iframe: %r" % (where,)
        save_pin(b)
        saved = draft(b)["pins"][0]["target"]
        assert saved["path"] and saved["path"] != ".art-frame", "artifact: the saved target is the iframe: %r" % (saved,)
        reload(b, server, rid)
        on(b)
        assert b.until("!!document.querySelector('.art-frame')", timeout=30)
        fsid = artifact_frame_session(b, rid)
        local = frame_point(b, fsid, "#acopy")
        got = frame_pin_host(b, rid, [local[0] + 12, local[1] - 12])
        assert got and got.get("host"), "artifact: no pin sits on the element after the reload: %r" % (got,)
    finally:
        b.js("localStorage.clear()")


# Wraps requestAnimationFrame from the first instant of the document. Once armed, it counts the board's own refresh
# callbacks (canvasLifecycle's `settle`) and, right after the second one has run, moves the paragraph 165 px with a
# stylesheet outside the doc. The first refresh measures the re-rendered doc; the second is the one the new doc's
# ResizeObserver start-up notification queues, which is the last measure a board without the settle loop ever makes.
# The shift lands inside that frame, after the measure and before the next layout, so on every run, whatever the
# machine's frame timing, the pin is measured before the shift and nothing resizes: only the settle loop's further
# frames can see it.
LATE_SHIFT_AFTER_REFRESH = """(function(){var raf=window.requestAnimationFrame.bind(window);window.__refreshes=0;
window.requestAnimationFrame=function(cb){
  if(window.__armLateShift&&cb&&cb.name==='settle'){
    var inner=cb;
    cb=function(t){
      try{inner(t)}finally{
        if(++window.__refreshes===2){window.__armLateShift=false;
          var s=document.createElement('style');s.id='mr-late-shift';
          s.textContent='.doc-head > p { margin-left: 165px !important }';
          (document.head||document.documentElement).appendChild(s);window.__lateShifted=true}}
    }
  }
  return raf(cb)};
})()"""


def test_a_late_relayout_after_the_width_switch_reanchors_the_pin(cux_review, server, chrome):
    """A width switch that settles over frames must not freeze the pin at its first measured position.

    The switch redraws the doc, and the pin is measured in the next frame. A layout that lands after that frame
    moves the surface without resizing any box the observers watch, so only a re-measure notices it. This is the
    race the Windows CI run hit: the pin kept the position from before the final layout.
    """
    rid = cux_review
    b = open_board(chrome, server, rid)
    try:
        fresh_reload(b, server, rid)
        on(b)
        expr = "document.querySelector('.doc-head > p')"
        click(b, expr)
        assert b.until("!!document.querySelector('.popover')")
        save_pin(b)
        added = b.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": LATE_SHIFT_AFTER_REFRESH},
                           session=b.sid)
        try:
            reload(b, server, rid)
        finally:
            b.cdp.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": added["identifier"]}, session=b.sid)
        on(b)
        assert b.until("!!document.querySelector('.mpin')", timeout=30)
        assert not surface_pin(b, expr)["det"]
        before = surface_centre(b, expr)
        # Armed now, the wrapper lands the 165 px shift right after the board's second refresh of the press.
        b.js("window.__armLateShift = true")
        b.press('.dzbar .wseg .seg-btn[data-value="phone"]')
        assert b.until("window.__lateShifted === true", timeout=10)
        moved = surface_centre(b, expr)
        assert moved and abs(moved[0] - before[0] - 165) < 2, (before, moved)
        pin = surface = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pin = surface_pin(b, expr)
            surface = surface_centre(b, expr) or surface
            if pin and abs(pin["x"] - surface[0]) < 30 and abs(pin["y"] - surface[1]) < 30:
                break
            b.cdp.pump(0.1)
        assert pin and abs(pin["x"] - surface[0]) < 30 and abs(pin["y"] - surface[1]) < 30, \
            f"the pin did not follow the late relayout ({(pin, surface)})"
    finally:
        b.js("localStorage.clear()")


def test_a_dragged_word_in_the_artifact_frame_is_quoted_and_pinned(cux_review, server, chrome):
    rid = cux_review
    b = open_board(chrome, server, rid)
    try:
        on(b)
        assert b.until("!!document.querySelector('.art-frame')", timeout=30)
        frame_sid = artifact_frame_session(b, rid)
        word = drag_word(b, frame_word_points(b, frame_sid), session=frame_sid)
        assert b.until("!!document.querySelector('.popover .pq-chip')", timeout=10), "artifact: no quote chip"
        chip = b.js("document.querySelector('.popover .pq-chip .pq-text').textContent")
        assert chip == word, f"artifact: the chip must quote exactly {word!r}, got {chip!r}"
        save_pin(b)
        saved = draft(b)["pins"][0]["target"]
        assert saved.get("path") and saved["path"] != ".art-frame", \
            "artifact: the saved target is the iframe: %r" % (saved,)
        assert saved.get("quote") and saved["quote"]["exact"] == word, \
            f"artifact: the saved frame target carries no quote: {saved!r}"
        reload(b, server, rid)
        on(b)
        assert b.until("!!document.querySelector('.art-frame')", timeout=30)
        fsid = artifact_frame_session(b, rid)
        again = frame_word_points(b, fsid)
        got = frame_pin_host(b, rid, [again[3] + 12, again[2] - 12])
        assert got and got.get("host"), "artifact: no pin sits on the word after the reload: %r" % (got,)
    finally:
        b.js("localStorage.clear()")


def test_the_quote_chip_drops_to_a_plain_element_pin(cux_review, server, chrome):
    rid = cux_review
    b = open_board(chrome, server, rid)
    try:
        on(b)
        word = drag_word(b, word_points(b, "document.querySelector('.doc-head > p')"))
        assert b.until("!!document.querySelector('.popover .pq-chip')", timeout=10), "no quote chip"
        assert b.js("document.querySelector('.popover .pq-chip .pq-text').textContent") == word
        assert b.js("document.querySelector('.popover .pq-chip .pq-x').textContent") == "×"
        # The chip is removable without losing the composer or the text typed beside it.
        b.js("(function(t){t.value='Keep this text';t.dispatchEvent(new Event('input'));return 1})"
             "(document.querySelector('.popover textarea'))")
        b.press(".popover .pq-chip")
        assert b.until("!document.querySelector('.popover .pq-chip')", timeout=5), "× did not drop the quote"
        assert b.js("document.querySelector('.popover textarea').value") == "Keep this text"
        assert b.until("!!document.querySelector('.popover textarea')")
        b.press(".popover .composer-actions .btn-round")
        assert b.until("!!(%s)" % DRAFT, timeout=10)
        plain = draft(b)["pins"][0]
        assert plain.get("target") and not plain["target"].get("quote"), \
            f"dropping the quote must leave a plain element pin: {plain.get('target')}"
        assert plain["text"] == "Keep this text"
    finally:
        b.js("localStorage.clear()")


@pytest.mark.parametrize("label,expr", WORD_SURFACES, ids=[x[0].replace(" ", "-") for x in WORD_SURFACES])
def test_a_dragged_word_is_quoted_chipped_and_resolves(cux_review, server, chrome, label, expr):
    rid = cux_review
    b = open_board(chrome, server, rid)
    try:
        on(b)
        word = drag_word(b, word_points(b, expr))
        assert b.until("!!document.querySelector('.popover .pq-chip')", timeout=10), f"{label}: no quote chip"
        chip = b.js("document.querySelector('.popover .pq-chip .pq-text').textContent")
        assert chip == word, f"{label}: the chip must quote exactly {word!r}, got {chip!r}"
        assert b.js("document.querySelector('.popover .pq-chip .pq-x').textContent") == "×"
        assert b.js("document.querySelector('.popover .pop-where').textContent").startswith("on "), \
            f"{label}: the word composer names no target"
        save_pin(b)
        pin = draft(b)["pins"][0]
        assert pin["target"]["quote"]["exact"] == word, f"{label}: saved quote is {pin['target'].get('quote')!r}"
        highlight = ("(window.CSS && CSS.highlights && CSS.highlights.get('mr-comments') && "
                     "CSS.highlights.get('mr-comments').size > 0) || "
                     "document.querySelectorAll('mark[data-mr-highlight]').length > 0")
        if label == "mock content":
            highlight = ("(function(){var sr=%s;var marks=sr.querySelectorAll('mark[data-mr-highlight]').length;"
                         "var reg=(window.CSS&&CSS.highlights)?CSS.highlights.get('mr-comments'):null;"
                         "var inShadow=false;if(reg){Array.from(reg).forEach(function(r){"
                         "if(sr.contains(r.commonAncestorContainer))inShadow=true;})}"
                         "return marks>0||inShadow})()" % ROOT)
        assert b.until(highlight, timeout=10), f"{label}: the quote is not highlighted where it lands"

        def pin_xy():
            return json.loads(b.js("JSON.stringify((function(){var r=document.querySelector('.mpin')"
                                   ".getBoundingClientRect();"
                                   "return [r.left+r.width/2+scrollX,r.top+r.height/2+scrollY]})())"))

        def word_xy():
            got = b.js("JSON.stringify((function(){var e=%s;var w=document.createTreeWalker(e,NodeFilter.SHOW_TEXT),n;"
                       "while((n=w.nextNode())){var m=/[^\\s]+/.exec(n.textContent);if(!m)continue;var r=document.createRange();"
                       "r.setStart(n,m.index);r.setEnd(n,m.index+m[0].length);var b=r.getBoundingClientRect();"
                       "return [b.left+b.width/2+scrollX,b.top+b.height/2+scrollY]}return null})())" % expr)
            return json.loads(got)

        drawn = b.until("!!document.querySelector('.mpin')", timeout=10)
        assert drawn, f"{label}: the saved word pin was not drawn"
        before = pin_xy()
        wxy = word_xy()
        assert abs(before[0] - wxy[0]) < 24 and abs(before[1] - word_xy()[1]) < 24, \
            f"{label}: the pin does not sit on its word ({(before, word_xy())})"

        reload(b, server, rid)
        on(b)
        assert b.until("!!document.querySelector('.mpin')", timeout=30), f"{label}: the word pin is gone after reload"
        assert b.js("!document.querySelector('.mpin').classList.contains('det')"), f"{label}: the word pin detached"
        assert abs(pin_xy()[0] - word_xy()[0]) < 24 and abs(pin_xy()[1] - word_xy()[1]) < 24, \
            f"{label}: the pin did not return to its word"

        # Switching width re-resolves the same element/word: the pin is still drawn at it, not at a stale coordinate.
        # A mock pin stays with the width it was made at, so it is not part of this cross-width check.
        if label != "mock content" and b.js(
                "!!document.querySelector('.dzbar .wseg .seg-btn[data-value=\"phone\"]')"):
            b.press('.dzbar .wseg .seg-btn[data-value="phone"]')
            assert b.until("document.querySelector('.dzbar .wseg .seg-btn[data-value=\"phone\"]').getAttribute('aria-checked') === 'true'", timeout=30)
            b.cdp.pump(0.4)
            assert b.until("!!document.querySelector('.mpin')", timeout=20), f"{label}: the pin vanished on Mobile"
            assert b.js("!document.querySelector('.mpin').classList.contains('det')"), f"{label}: the pin detached on Mobile"
            after = pin_xy()
            assert abs(after[0] - word_xy()[0]) < 30 and abs(after[1] - word_xy()[1]) < 30, \
                f"{label}: the pin is not on its word after the width switch ({(after, word_xy())})"
    finally:
        b.js("localStorage.clear()")


def test_hover_outlines_the_element_and_names_the_anchor(cux_review, server, chrome):
    rid = cux_review
    b = open_board(chrome, server, rid)
    try:
        on(b)
        for expr, label_part in [("document.querySelector('.blk-cards .card:nth-of-type(2)')", "Copy"),
                                 ("document.querySelector('.blk-table tbody tr:nth-of-type(2) td:nth-of-type(2)')", "td")]:
            x, y = point(b, expr)
            b.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none",
                                                    "clickCount": 0, "buttons": 0}, session=b.sid)
            assert b.until("(function(){var h=document.querySelector('.canvas-hover');return !!h&&!h.hidden})()", timeout=5)
            boxes = json.loads(b.js(
                "JSON.stringify((function(){var h=document.querySelector('.canvas-hover');var e=%s;"
                "var a=h.getBoundingClientRect(),z=e.getBoundingClientRect();"
                "return {h:[a.left,a.top,a.width,a.height],e:[z.left,z.top,z.width,z.height],"
                "tab:h.querySelector('.hl-tab').textContent,outline:getComputedStyle(h).outlineWidth}})())" % expr))
            for i in range(4):
                assert abs(boxes["h"][i] - boxes["e"][i]) <= 1, f"the hover box must be the element, not the block: {boxes}"
            assert boxes["outline"] == "2px", boxes
            assert label_part in boxes["tab"], f"the hover tab must name the anchor: {boxes['tab']!r}"
            # The tab text is exactly the composer's own anchor label.
            click(b, expr)
            assert b.until("!!document.querySelector('.popover')")
            where = b.js("document.querySelector('.popover .pop-where').textContent")
            assert boxes["tab"] == where[3:], (boxes["tab"], where)
            b.press(".popover .pop-close")
            assert b.until("!document.querySelector('.popover')")

        # Mocks keep their own surface but wear the same look: element box plus the anchor label.
        assert b.until("!!document.querySelector('#dz-m1 .mock-host').shadowRoot.querySelector('#copy')")
        b.js(ROOT + ".querySelector('#copy').scrollIntoView({block:'center'})")
        b.cdp.pump(0.2)
        pt = json.loads(b.js("JSON.stringify((function(){var e=" + ROOT + ".querySelector('#copy');"
                             "var r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})())"))
        b.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": pt[0], "y": pt[1], "button": "none",
                                                "clickCount": 0, "buttons": 0}, session=b.sid)
        visible = ("(function(){var h=[].filter.call(document.querySelectorAll('.mr-hover'),"
                   "function(x){return !x.hidden})[0];return !!h})()")
        assert b.until(visible, timeout=5)
        mock = json.loads(b.js(
            "JSON.stringify((function(){var h=[].filter.call(document.querySelectorAll('.mr-hover'),"
            "function(x){return !x.hidden})[0];var e=" + ROOT + ".querySelector('#copy');"
            "var a=h.getBoundingClientRect(),z=e.getBoundingClientRect();"
            "return {h:[a.left,a.top,a.width,a.height],e:[z.left,z.top,z.width,z.height],"
            "tab:h.querySelector('.mr-hover-label')&&h.querySelector('.mr-hover-label').textContent,"
            "outline:getComputedStyle(h).outlineWidth}})())"))
        for i in range(4):
            assert abs(mock["h"][i] - mock["e"][i]) <= 1, f"the mock hover must be the element: {mock}"
        assert mock["outline"] == "2px" and mock["tab"], mock
    finally:
        b.js("localStorage.clear()")


@pytest.mark.parametrize("damage", [None, "mismatch"], ids=["no-sections", "block-count-changed"])
def test_a_legacy_round_without_sections_still_anchors_its_blocks(cux_review, server, chrome, damage):
    rid = cux_review
    data = store.load(rid)
    if damage is None:
        data["rounds"][0].pop("sections", None)
    else:
        data["rounds"][0]["sections"] = data["rounds"][0]["sections"][:-1]
    store.save(rid, data)
    assert "sections" not in store.load(rid)["rounds"][0] or \
        len(store.load(rid)["rounds"][0]["sections"]) != len(store.load(rid)["rounds"][0]["decisions"])
    b = open_board(chrome, server, rid)
    try:
        on(b)
        click(b, "document.querySelector('.blk-text p')")
        assert b.until("!!document.querySelector('.popover')", timeout=10), "the legacy block click opened no composer"
        title = b.js("document.querySelector('.popover .pop-head strong').textContent")
        assert title == "New pin · Why this round", f"the pin must name the block, not the whole canvas: {title!r}"
        save_pin(b)
        pin = draft(b)["pins"][0]
        assert pin["block"] == "s1", f"derived refs must mirror store.section_index: {pin['block']!r}"
        assert pin.get("target") and pin["target"].get("path")
        assert b.until("!!document.querySelector('.mpin')")
        assert b.js("!document.querySelector('.mpin').classList.contains('det')")
        reload(b, server, rid)
        on(b)
        assert b.until("!!document.querySelector('.mpin')", timeout=30)
        assert b.js("!document.querySelector('.mpin').classList.contains('det')"), \
            "the legacy pin detached after a reload"
    finally:
        b.js("localStorage.clear()")
