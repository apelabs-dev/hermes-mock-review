"""Pins must paint above z-indexed content on every surface (PINZ, mock-review 2.0.0).

A pin dropped where a mock positions its own content with a z-index (a modal, a popover, a sticky bar) used to be
saved, listed and counted, but painted underneath it: `.mock-host` created no stacking context, so the mock's
internal `z-index: 100` outranked the sibling `.pin-layer`. These tests drive a real headless browser with real
mouse events (Input.dispatchMouseEvent) and hit-test the marker's own centre. The probe adds `.mock-host.use`
while it measures, because `document.elementFromPoint` skips mock content that is not hit-testable and would
otherwise report a pin "on top" even when it is painted underneath.

They skip when no Chrome, Chromium or Edge is installed.
"""
import json
import time

import pytest

from mockreview import store
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}

# The mock is a real app surface: ordinary copy, an absolutely positioned layer with a large z-index, and a native
# control. None of the absolute children has a positioned ancestor, so they resolve against `.mock-inner` today;
# C4 fails if a fix changes that containing block.
MOCK_HTML = (
    '<div id="plain" style="position:absolute;left:24px;top:24px;width:200px;height:50px;margin:0">Plain copy</div>'
    '<a id="lnk" href="https://example.com/go" style="position:absolute;left:240px;top:78px;width:120px;'
    'height:22px">A mock link</a>'
    '<div id="stacked" style="position:absolute;left:20%;top:25%;width:60%;height:50%;background:#2bd;z-index:100">'
    'Stacked layer</div>'
    '<details id="more" style="position:absolute;left:24px;top:340px;margin:0"><summary id="more-sum">More</summary>'
    '<p>hidden copy</p></details>'
)

DECISIONS = {
    "intro": "Pin the stacked layer, the plain copy, and a table header.",
    "blocks": [
        {"type": "mock", "id": "m1", "title": "Checkout", "html": MOCK_HTML},
        {"type": "table", "title": "Rates", "columns": ["Plan", "Price"],
         "rows": [["Basic", "4"], ["Pro", "9"], ["Team", "19"]]},
        {"type": "defaults", "id": "df", "title": "Everything else", "text": "No other changes.",
         "items": ["Keep the rest as it is."]},
    ],
}

DESIGN = {
    "name": "pinprobe", "label": "Pin probe", "default_theme": "light", "mock_css": "mock.css",
    "themes": {
        "light": {"bg": "#ffffff", "surface": "#f4f4f6", "text": "#111111", "accent": "#3355dd"},
        "dark": {"bg": "#0b0b0f", "surface": "#16161c", "text": "#eeeeee", "accent": "#8899ff"},
    },
}

MOCK_CSS = (".app{min-height:420px;background:#fff;color:#111}"
            ".app[data-theme=dark]{background:#101014;color:#eee}")

LOOK = {"devices": ["desktop", "mobile"], "default_device": "auto", "default_theme": "light",
        "show_theme_toggle": True, "pins_enabled": True, "require_note_on_change": False, "max_pins": 40,
        "footer_button": "send", "interactive_elements": True}

COMBOS = [("desktop", 1280, 900, False), ("phone", 390, 844, True)]
THEMES = ["light", "dark"]

PROTO_HTML = ('<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0}'
              '#stacked{position:absolute;left:20px;top:20px;width:300px;height:200px;background:#2bd;z-index:100}'
              '</style></head><body><div id="stacked">Stacked layer</div>'
              '<script>addEventListener("load",function(){parent.postMessage({proto_ready:1},"*");});</script>'
              '</body></html>')

PROBE = r"""
(function(){
  var pt = __POINT__;
  var hosts = [].slice.call(document.querySelectorAll('.mock-host'));
  var added = [];
  hosts.forEach(function(h){ if (!h.classList.contains('use')) { h.classList.add('use'); added.push(h); } });
  var el = document.elementFromPoint(pt[0], pt[1]);
  var list = document.elementsFromPoint(pt[0], pt[1]);
  var host = document.querySelector('.mock-host');
  var inShadow = null;
  if (host && host.shadowRoot && host.shadowRoot.elementFromPoint) {
    var s = host.shadowRoot.elementFromPoint(pt[0], pt[1]);
    inShadow = s ? (s.tagName.toLowerCase() + (s.id ? '#' + s.id : '')) : null;
  }
  added.forEach(function(h){ h.classList.remove('use'); });
  function name(e){ if (!e) return null;
    var c = (typeof e.className === 'string' && e.className.trim()) ? '.' + e.className.trim().split(/\s+/).join('.') : '';
    return e.tagName.toLowerCase() + c; }
  var surface = document.querySelector(__SURFACE__);
  return JSON.stringify({top: name(el), isPin: !!el && !!el.closest('.mpin'),
    isSurface: !!el && !!surface && (el === surface || surface.contains(el)),
    shadowTop: inShadow, stack: list.slice(0, 6).map(name)});
})()
"""

GHOST_PROBE = r"""
(function(){
  var g = document.querySelector('.mpin.ghost');
  if (!g) return JSON.stringify({error: 'no ghost'});
  var r = g.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
  var hosts = [].slice.call(document.querySelectorAll('.mock-host'));
  var added = [];
  hosts.forEach(function(h){ if (!h.classList.contains('use')) { h.classList.add('use'); added.push(h); } });
  var list = document.elementsFromPoint(x, y);
  added.forEach(function(h){ h.classList.remove('use'); });
  var surface = document.querySelector(__SURFACE__);
  var gi = list.indexOf(g), si = surface ? list.indexOf(surface) : -1;
  function name(e){ if (!e) return null;
    var c = (typeof e.className === 'string' && e.className.trim()) ? '.' + e.className.trim().split(/\s+/).join('.') : '';
    return e.tagName.toLowerCase() + c; }
  return JSON.stringify({x: x, y: y, ghostIndex: gi, surfaceIndex: si,
    aboveSurface: gi >= 0 && (si < 0 || gi < si), top: name(list[0]),
    stack: list.slice(0, 8).map(name)});
})()
"""

GEOM = r"""
(function(){
  var inner = document.querySelector('.mock-inner').getBoundingClientRect();
  var host = document.querySelector('.mock-host');
  var layer = host.shadowRoot.querySelector('#stacked').getBoundingClientRect();
  var want = {left: inner.left + inner.width * 0.20, top: inner.top + inner.height * 0.25,
    width: inner.width * 0.60, height: inner.height * 0.50};
  return JSON.stringify({inner: {left: inner.left, top: inner.top, width: inner.width, height: inner.height},
    layer: {left: layer.left, top: layer.top, width: layer.width, height: layer.height}, want: want});
})()
"""

DRAFT = ("Object.values(localStorage).map(v=>{try{return JSON.parse(v)}catch(_){return null}})"
         ".find(v=>v&&v.pins&&v.pins.length)")


def review(tmp_path, theme):
    d = tmp_path / ("design-" + theme)
    d.mkdir()
    (d / "design.json").write_text(json.dumps(DESIGN), encoding="utf-8")
    (d / "mock.css").write_text(MOCK_CSS, encoding="utf-8")
    src = tmp_path / ("decisions-" + theme + ".json")
    src.write_text(json.dumps(DECISIONS), encoding="utf-8")
    return store.create_review("Pin stack", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(src), design=str(d)),
                               look=dict(LOOK, default_theme=theme), wake=False)


@pytest.fixture(params=[(w, t) for w in COMBOS for t in THEMES],
                ids=["%s-%s" % (w[0], t) for w in COMBOS for t in THEMES])
def board(request, env, server, chrome, tmp_path):
    label, width, height, mobile = request.param[0]
    theme = request.param[1]
    data = review(tmp_path, theme)
    b = Board(chrome, server + "/r/" + data["id"] + "/",
              wait="!!document.querySelector('.mock-host') && !!document.querySelector('.mock-inner')",
              width=width, height=height, mobile=mobile)
    if mobile:
        b.cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}, session=b.sid)
    assert b.until("document.querySelector('.mock-inner').getBoundingClientRect().height > 100", timeout=10)
    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='true'")
    time.sleep(.3)
    b.review = data
    b.label = label
    b.theme = theme
    return b


def rect_centre(b, expr):
    got = b.js("(function(){var e = %s;if(!e)return 'null';e.scrollIntoView({block:'center'});"
               "var r=e.getBoundingClientRect();return JSON.stringify([r.left+r.width/2,r.top+r.height/2])})()"
               % expr)
    assert got != "null", expr
    b.cdp.pump(0.05)
    return json.loads(got)


def shadow_centre(b, selector):
    return rect_centre(b, "document.querySelector('.mock-host').shadowRoot.querySelector(%s)" % json.dumps(selector))


def centre(b, selector):
    return rect_centre(b, "document.querySelector(%s)" % json.dumps(selector))


def marker_centre(b, index=0):
    return rect_centre(b, "document.querySelectorAll('.mpin')[%d]" % index)


def point_in(b, selector, fx, fy, shadow=True):
    root = ("document.querySelector('.mock-host').shadowRoot" if shadow else "document")
    got = b.js("(function(){var e=%s.querySelector(%s);if(!e)return 'null';"
               "e.scrollIntoView({block:'center'});var r=e.getBoundingClientRect();"
               "return JSON.stringify([r.left+r.width*%s,r.top+r.height*%s])})()"
               % (root, json.dumps(selector), fx, fy))
    assert got != "null", selector
    b.cdp.pump(0.05)
    return json.loads(got)


GHOST_AND_COMPOSER = ("JSON.stringify({ghost: document.querySelectorAll('.mpin.ghost').length, "
                      "composer: !!document.querySelector('.popover')})")


def after_a_scroll(b):
    """Scroll a few pixels with a new pin's composer open and report the ghost and the composer. A scroll redraws the
    pin layers on the next frame (and re-measures for up to six more); that redraw used to drop the ghost while the
    composer stayed open, and the scroll in point_in could land it after a slow runner's click."""
    b.js("window.scrollBy(0, 5)")
    time.sleep(.4)
    return json.loads(b.js(GHOST_AND_COMPOSER))


def probe(b, point, surface='.mock-host'):
    js = PROBE.replace('__POINT__', json.dumps(point)).replace('__SURFACE__', json.dumps(surface))
    return json.loads(b.js(js))


def ghost_probe(b, surface='.mock-host'):
    return json.loads(b.js(GHOST_PROBE.replace('__SURFACE__', json.dumps(surface))))


def save(b, text, pins_after=None):
    assert b.until("!!document.querySelector('.popover textarea')", timeout=5), "the pin popover did not open"
    b.js("document.querySelector('.popover textarea').value=%s" % json.dumps(text))
    b.press('.popover .btn-primary')
    assert b.until("!document.querySelector('.popover')", timeout=5)
    if pins_after is not None:
        assert b.until("document.querySelectorAll('.mpin').length === %d" % pins_after, timeout=10), \
            "the saved pin was not drawn"


def draft(b):
    got = b.js("JSON.stringify(%s)" % DRAFT)
    return json.loads(got) if got and got != "null" else None


def report(b, surface, where, result):
    print("MEASURE %-8s %-6s %-8s %-22s top=%s pin=%s surface=%s shadow=%s aboveSurface=%s"
          % (b.label, b.theme, surface, where, result.get("top"), result.get("isPin"),
             result.get("isSurface"), result.get("shadowTop"), result.get("aboveSurface")))


def test_pins_paint_above_z_indexed_mock_content(board):
    b = board
    state = json.loads(b.js(
        "(function(){var h=document.querySelector('.mock-host');var a=h&&h.shadowRoot.querySelector('.app');"
        "return JSON.stringify({doc:document.documentElement.getAttribute('data-theme'),"
        "mock:a?a.getAttribute('data-theme'):null})})()"))
    assert state == {"doc": b.theme, "mock": b.theme}, state

    # C4: the fix must not change the containing block of the fixture's absolutely positioned layer. Passes on BASE.
    geo = json.loads(b.js(GEOM))
    for key in ("left", "top", "width", "height"):
        assert abs(geo["layer"][key] - geo["want"][key]) <= 2, (key, geo)
    print("MEASURE %-8s %-6s %-8s %-22s layer=%s" % (b.label, b.theme, "mock", "C4 geometry",
                                                     {k: round(geo["layer"][k], 1) for k in geo["layer"]}))

    # Setup guard: without a pin the mock content is already hit-testable at this point. On BASE the top is the
    # mock host; a selector or sanitizer problem fails here, not in the stacking assertion.
    pre = probe(b, shadow_centre(b, "#stacked"))
    assert pre["isSurface"] and not pre["isPin"], pre

    # C1: a real click on the z-indexed layer, saved through the popover, and its marker is the top at its centre.
    b.click(*shadow_centre(b, "#stacked"))
    save(b, "Stacked layer note", 1)
    c1 = probe(b, marker_centre(b, 0))
    report(b, "mock", "C1 saved marker", c1)
    assert c1["isPin"], c1
    assert not c1["isSurface"], c1

    # C5: ordinary content with no z-index still gets a marker on top at its centre.
    b.click(*shadow_centre(b, "#plain"))
    save(b, "Plain copy note", 2)
    c5 = probe(b, marker_centre(b, 1))
    report(b, "mock", "C5 plain marker", c5)
    assert c5["isPin"], c5

    # C3: the marker rebuilt from the draft after a reload is still on top.
    assert b.until("(%s) && (%s).pins.length===2" % (DRAFT, DRAFT), timeout=5), "the draft did not save"
    url = b.js("location.href")
    b.cdp.call("Page.navigate", {"url": url}, session=b.sid)
    assert b.until("document.querySelectorAll('.mpin').length === 2", timeout=60), "the draft pins did not come back"
    time.sleep(.4)
    c3 = probe(b, marker_centre(b, 0))
    report(b, "mock", "C3 after reload", c3)
    assert c3["isPin"], c3
    assert not c3["isSurface"], c3

    # C6: the pin layer is not a click shield. In Comment mode a native control inside the mock and a board control
    # under no pin both take a real click.
    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='true'")
    b.click(*shadow_centre(b, "#more-sum"))
    assert b.until("document.querySelector('.mock-host').shadowRoot.querySelector('details#more').open"), \
        "a real click on the mock's summary was swallowed"
    assert not b.popover(), "the summary click must not become a pin"
    b.click(*centre(b, "#dz-df .sbtn.s-approve"))
    assert b.until("document.querySelector('#dz-df .sbtn.s-approve').getAttribute('aria-pressed')==='true'"), \
        "a real click on a board control under no pin was swallowed"


def test_ghost_marker_paints_above_z_indexed_mock_content(board):
    """A fresh ghost is above the same z-indexed content, independent of any saved pin."""
    b = board
    b.click(*point_in(b, "#stacked", 0.35, 0.35))
    assert b.until("!!document.querySelector('.mpin.ghost')", timeout=5), "no ghost marker"
    g = ghost_probe(b, ".mock-host")
    report(b, "mock", "C2 ghost", g)
    assert g.get("surfaceIndex", -1) >= 0, g
    assert g["aboveSurface"], g
    after = after_a_scroll(b)
    assert after == {"ghost": 1, "composer": True}, "a redraw dropped the ghost of the pin being written: %s" % after


def test_mock_link_and_controls_take_clicks_in_tap_mode(board):
    """In Tap mode a real mock link, the z-indexed layer and the mock's summary take clicks; none becomes a pin."""
    b = board
    b.click(*shadow_centre(b, "#stacked"))
    save(b, "Stacked layer note", 1)

    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='false'")
    assert b.js("document.querySelector('.mock-host').classList.contains('use')")

    # The click below only proves anything if the anchor is still a link: the sanitizer drops the href of a mock
    # <a> unless it is absolute http(s), so a relative one would leave an inert element that still takes clicks.
    assert b.js("(function(){var a=document.querySelector('.mock-host').shadowRoot.querySelector('#lnk');"
                "return a ? a.getAttribute('href') : 'missing'})()") == "https://example.com/go", \
        "the mock's link lost its href (a relative href is stripped when the mock is sanitized)"

    b.js("(function(){"
         "var r = document.querySelector('.mock-host').shadowRoot;"
         "window.__hits = {lnk: 0, layer: 0};"
         "r.querySelector('#lnk').addEventListener('click', function(e){ e.preventDefault(); window.__hits.lnk++; });"
         "r.querySelector('#stacked').addEventListener('click', function(){ window.__hits.layer++; });"
         "return 1;"
         "})()")

    b.click(*shadow_centre(b, "#lnk"))
    assert b.until("window.__hits.lnk === 1", timeout=5), b.js("JSON.stringify(window.__hits)")
    assert not b.popover(), "a Tap-mode click on a mock link must not become a pin"

    b.click(*point_in(b, "#stacked", 0.22, 0.28))
    assert b.until("window.__hits.layer === 1", timeout=5), b.js("JSON.stringify(window.__hits)")
    assert not b.popover(), "a Tap-mode click on the mock's z-indexed layer must not become a pin"

    b.click(*shadow_centre(b, "#more-sum"))
    assert b.until("document.querySelector('.mock-host').shadowRoot.querySelector('#more').open"), \
        "a real click on the mock's summary was swallowed"
    assert not b.popover(), "a Tap-mode click on the mock's summary must not become a pin"


def test_pins_paint_above_z_indexed_canvas_content(board):
    """A canvas pin on a table header used to sit under the sticky `th` (z-index 1-3)."""
    b = board
    th = centre(b, ".blk-table thead th")
    b.click(*th)
    assert b.until("!!document.querySelector('.popover textarea')", timeout=5), "the pin popover did not open"
    after = after_a_scroll(b)
    assert after == {"ghost": 1, "composer": True}, "a redraw dropped the canvas ghost: %s" % after
    save(b, "Rates note", 1)
    marker = b.js("!!document.querySelector('.blk-table > .pin-layer .mpin')")
    assert marker, "the canvas pin was not drawn on the table section"
    got = probe(b, marker_centre(b, 0), ".blk-table thead th.c0")
    report(b, "canvas", "saved marker", got)
    assert got["isPin"], got


@pytest.fixture
def proto_page(env, server, chrome, tmp_path):
    page = tmp_path / "proto" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(PROTO_HTML, encoding="utf-8")
    data = store.create_review("Pin stack proto", "", ORIGIN, "local",
                               store.parse_round_spec(prototype=str(page)), wake=False)
    b = Board(chrome, server + "/r/" + data["id"] + "/", wait=Board.PROTO_READY)
    b.review = data
    return b


def test_prototype_pins_are_drawn_above_page_content(proto_page):
    """Prototype/artifact/variant pins live inside the frame in the probe's own host (z-index 2147483647)."""
    b = proto_page
    b.press('#comment-toggle')
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed')==='true'")
    r = json.loads(b.js("JSON.stringify((function(r){return [r.left,r.top]})(document.querySelector('.proto iframe')"
                        ".getBoundingClientRect()))"))
    b.click(r[0] + 170, r[1] + 120)          # inside #stacked in the frame
    save(b, "Proto note")
    assert b.until("!!(%s) && (%s).pins.some(function(p){return p.block==='proto'})" % (DRAFT, DRAFT), timeout=10)
    pin = [p for p in draft(b)["pins"] if p["block"] == "proto"][0]

    targets = b.cdp.call("Target.getTargets")["targetInfos"]
    target = next(x for x in targets if x["type"] == "iframe" and b.review["id"] in x["url"])
    sid = b.cdp.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["sessionId"]
    b.cdp.call("Runtime.enable", session=sid)
    got = b.cdp.call("Runtime.evaluate", {"expression":
        "(function(){var p=%s,w=Math.max(document.documentElement.scrollWidth,document.body.scrollWidth,1),"
        "h=Math.max(document.documentElement.scrollHeight,document.body.scrollHeight,1);"
        "var x=p.x*w+14-scrollX,y=p.y*h-14-scrollY;var host=document.querySelector('mr-overlay');"
        "var top=document.elementFromPoint(x,y);return JSON.stringify({x:x,y:y,"
        "top:top?top.tagName.toLowerCase():null,hostIsTop:top===host,"
        "z:host?getComputedStyle(host).zIndex:null,stackedTop:(top&&top.id)||null})})()"
        % json.dumps({"x": pin["x"], "y": pin["y"]}), "returnByValue": True}, session=sid)["result"]["value"]
    print("MEASURE proto    -      proto    saved marker           %s" % got)
    assert json.loads(got)["hostIsTop"], got
    assert json.loads(got)["z"] == "2147483647", got
