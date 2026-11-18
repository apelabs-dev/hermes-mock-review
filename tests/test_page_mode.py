"""Page mode: an artifact's display:page and a prototype round's full-bleed frame.

The card measured a 1080 px column, a frame that did not reach the window, and a decide row buried under the sticky
footer. These are real-browser tests: geometry from the board page, a real 1x1 screenshot pixel for the visual claim,
and the frame's own document for fixed/sticky content, pinning and media. Every test fails on the PAGE base (a48293d).
"""

import base64
import json
import math
import struct
import time
import zlib

import pytest

import browserkit as bc
from mockreview import store
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)
from test_media_range import _pcm_wav

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
PAGE_BG = (248, 248, 243)       # the test page's own body background, #f8f8f3
BOARD_BG = (250, 250, 250)      # the board's light canvas, #fafafa

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:#f8f8f3;color:#202020}
#sticky{position:sticky;top:0;height:44px;background:#eee7d8;font:14px sans-serif}
#tall{height:3000px}
#copy{width:300px;height:60px;margin:20px;font:16px sans-serif}
#dlg{position:fixed;top:40%;left:24px;width:240px;height:120px;background:#fff;border:1px solid #333;display:none}
</style></head><body>
<div id="sticky">Sticky header</div>
<p id="copy">Review this total now</p>
<button id="open-dlg" type="button">Open dialog</button>
<div id="tall">tall page</div>
<div id="dlg">Dialog</div>
<script>
document.getElementById('open-dlg').addEventListener('click', function () {
  document.getElementById('dlg').style.display = 'block';
});
</script></body></html>"""

AUDIO_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:#f8f8f3}
</style></head><body>
<audio id="a" controls preload="metadata" src="assets/tone.wav"></audio>
<script>
var a = document.getElementById('a'), p = {state: 'loading'};
window.__audioProbe = function () { return JSON.stringify(p); };
a.addEventListener('loadedmetadata', function () {
  p = {state: 'metadata', duration: a.duration, seekable: a.seekable.length,
       seekableEnd: a.seekable.length ? a.seekable.end(0) : 0};
  a.currentTime = a.duration / 2;
});
a.addEventListener('seeked', function () { p.state = 'seeked'; p.after = a.currentTime; });
a.addEventListener('error', function () { p = {state: 'error', code: a.error && a.error.code}; });
setTimeout(function () { if (p.state !== 'seeked') { p.state = p.state === 'error' ? 'error' : 'timeout'; } }, 15000);
</script></body></html>"""

ART_PAGE = """<!doctype html><html><head><style>
html,body{margin:0;background:#eef3ff;height:2500px}
</style></head><body><h1>artifact page</h1></body></html>"""

ART_SMALL = """<!doctype html><html><head><style>
html,body{margin:0;background:#eef3ff;height:400px}h1{margin:0}
</style></head><body><h1>artifact frame</h1></body></html>"""

# A classic 15 px scrollbar, forced after load: macOS Chrome uses overlay scrollbars, Linux/Windows Chrome reserves
# the gutter, and only the reserved gutter shows a full bleed that measures `vw` instead of the layout viewport.
CLASSIC_SCROLLBAR = ("(function(){var s=document.createElement('style');"
                     "s.textContent='::-webkit-scrollbar{width:15px;height:15px} html{overflow-y:scroll}';"
                     "document.head.appendChild(s);return true;})()")

# The same scrollbar installed before the board's module script can take its first `--page-w` measurement. A
# document-start hook runs before the parser creates the document element (both `document.head` and
# `document.documentElement` are null there), so it waits for the root and appends the style during parsing.
# `__sbwAt` records the readyState, proving the scrollbar beat the board's first layout.
CLASSIC_SCROLLBAR_FROM_START = (
    "(function(){var css='::-webkit-scrollbar{width:15px;height:15px} html{overflow-y:scroll}';"
    "function add(){var root=document.head||document.documentElement;if(!root)return false;"
    "var s=document.createElement('style');s.textContent=css;root.appendChild(s);"
    "window.__sbwAt=document.readyState;return true;}"
    "if(!add()){var mo=new MutationObserver(function(){if(add())mo.disconnect();});"
    "mo.observe(document,{childList:true,subtree:true});}})()")

# The page-mode footer's icons frozen from the base build: the layout changes, the icons do not.
BASE_ICONS = [
    '<svg viewBox="0 0 24 24" aria-hidden="true" data-icon="check" class="ico" stroke-width="1.5"><path d="M20 6 9 17l-5-5"></path></svg>',
    '<svg viewBox="0 0 24 24" aria-hidden="true" data-icon="pencil" class="ico" stroke-width="1.5"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"></path> <path d="m15 5 4 4"></path></svg>',
    '<svg viewBox="0 0 24 24" aria-hidden="true" data-icon="skip-forward" class="ico" stroke-width="1.5"><path d="M21 4v16"></path> <path d="M6.029 4.285A2 2 0 0 0 3 6v12a2 2 0 0 0 3.029 1.715l9.997-5.998a2 2 0 0 0 .003-3.432z"></path></svg>',
]
BASE_SEND_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true" data-icon="send" class="ico" stroke-width="1.5"><path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"></path> <path d="m21.854 2.147-10.94 10.939"></path></svg>'

# The footer's visible text: text nodes under .proto-row, skipping anything inside .vh or [aria-hidden] (the
# screen-reader status and the icon SVGs).
VISIBLE_FOOT_TEXT = r"""JSON.stringify((function(){
  var root = document.querySelector('.proto-row'), out = [];
  function hidden(n) {
    for (; n && n !== root; n = n.parentElement) {
      if (n.classList.contains('vh') || n.hasAttribute('aria-hidden')) return true;
    }
    return false;
  }
  (function walk(el) {
    for (var c = el.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) { if (!hidden(c.parentElement)) out.push(c.nodeValue); }
      else if (c.nodeType === 1) walk(c);
    }
  })(root);
  return out.join(' ').replace(/\s+/g, ' ').trim();
})())"""


def _proto_review(env, tmp_path, page=PAGE, name="index.html", look=None):
    d = tmp_path / "proto"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(page, encoding="utf-8")
    return store.create_review("Page mode", "", ORIGIN, "local", store.parse_round_spec(prototype=str(d / name)),
                               look=look, wake=False)


def _proto_review_rounds(env, tmp_path, rounds):
    data = _proto_review(env, tmp_path)
    page = tmp_path / "proto" / "index.html"   # _proto_review already wrote the page
    for _ in range(rounds - 1):
        store.add_round(data["id"], store.parse_round_spec(prototype=str(page)))
    return data


def _open(b, rid, server, wait="!!document.querySelector('#dz-proto[data-ready=true]')"):
    return Board(b, f"{server}/r/{rid}/", wait=wait)


def _geometry(b):
    expr = """JSON.stringify((function(){
      function r(e){if(!e)return null;var b=e.getBoundingClientRect();
        return {left:b.left, top:b.top, width:b.width, height:b.height, bottom:b.bottom, right:b.right};}
      function rsel(sel){return r(document.querySelector(sel));}
      function visibleToggle(e){var q=e.getBoundingClientRect();
        return getComputedStyle(e).display !== 'none' && q.width > 0 && q.height > 0;}
      var row = document.querySelector('.proto-row'), roundsEl = document.querySelector('#dzfoot .rounds');
      return {pageMode: document.body.classList.contains('page-mode'),
              innerW: innerWidth, innerH: innerHeight, scrollY: scrollY,
              scrollW: document.documentElement.scrollWidth,
              scrollH: document.documentElement.scrollHeight,
              bodyBg: getComputedStyle(document.body).backgroundColor,
              bar: rsel('.dzbar'), frame: rsel('.proto iframe'), dock: rsel('#dzfoot'),
              protoFoot: rsel('.proto-foot'),
              roundTab: rsel('#dzfoot .round-tab'),
              roundTabs: [].slice.call(document.querySelectorAll('#dzfoot .round-tab')).map(r),
              roundOn: rsel('#dzfoot .round-tab.on'),
              roundOnText: (function(){var e=document.querySelector('#dzfoot .round-tab.on');
                return e ? e.textContent.trim() : null;})(),
              rounds: roundsEl ? {box: r(roundsEl), scrollWidth: roundsEl.scrollWidth,
                clientWidth: roundsEl.clientWidth, scrollLeft: roundsEl.scrollLeft,
                overflowX: getComputedStyle(roundsEl).overflowX} : null,
              footToggles: [].slice.call(document.querySelectorAll('#dzfoot [data-comment-toggle]'))
                .filter(visibleToggle).map(r),
              toggles: [].slice.call(document.querySelectorAll('[data-comment-toggle]'))
                .filter(visibleToggle).map(r),
              rowChildren: row ? [].slice.call(row.children).map(function(e){
                return {tag:e.tagName.toLowerCase(), cls:e.className, id:e.getAttribute('data-id')};}) : [],
              segGrp: rsel('#dzfoot .decide[data-id=proto] .grp'),
              segGrpClientH: (function(){var e=document.querySelector('#dzfoot .decide[data-id=proto] .grp');
                return e ? e.clientHeight : null;})(),
              footerNote: rsel('#dzfoot textarea.dnote'),
              tallyPill: rsel('#dzfoot .sendbar-inline .sum-pick'),
              sendBtn: rsel('#dzfoot .btn-send'),
              sendbar: rsel('#dzfoot .sendbar'),
              dockHasDecide: !!document.querySelector('#dzfoot .decide[data-id=proto] .sbtn'),
              decideButtons: [].slice.call(document.querySelectorAll('.decide[data-id=proto] .sbtn')).map(function (x) {
                var q = x.getBoundingClientRect();
                var hitB = document.elementFromPoint(q.left + q.width / 2, q.bottom - 1);
                var hitC = document.elementFromPoint(q.left + q.width / 2, q.top + q.height / 2);
                var s = x.querySelector('svg');
                return {left:q.left, top:q.top, width:q.width, height:q.height,
                        right:q.right, bottom:q.bottom, inDock: !!x.closest('#dzfoot'),
                        hitsBottom: !!(hitB && (hitB === x || x.contains(hitB))),
                        hitsCentre: !!(hitC && (hitC === x || x.contains(hitC))),
                        svg: s ? (function(){var z=s.getBoundingClientRect();
                          return {left:z.left, right:z.right, top:z.top, bottom:z.bottom,
                                  width:z.width, height:z.height};})() : null};
              })};
    })())"""
    return json.loads(b.js(expr))


def _rows(g):
    """Rows: the four docked controls share one height (40 px, 44 px on a phone) and are vertically centred, so
    controls on one row share a top. The round tabs zone is counted with them: a footer whose tabs fell onto their
    own row is three rows, not two, which the four decided controls alone could not tell apart."""
    tops = {round(g[k]["top"]) for k in ("segGrp", "footerNote", "tallyPill", "sendBtn")}
    tops.add(round(g["rounds"]["box"]["top"]))
    return len(tops)


def _same_row(a, b, tolerance=2):
    return abs(a["top"] - b["top"]) <= tolerance


def _assert_disjoint(a, b, label_a, label_b):
    assert a["right"] <= b["left"] + 0.5 or b["right"] <= a["left"] + 0.5, \
        f"{label_a} and {label_b} overlap: {label_a}={a} {label_b}={b}"


def _rgba(text):
    return tuple(int(float(x)) for x in text[text.index("(") + 1:text.index(")")].split(",")[:3])


def _png_pixel(raw):
    """The first pixel of a PNG, unfiltered: zlib is stdlib, so a screenshot can be read without an image library."""
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", raw[:8]
    pos, width, kind, idat = 8, 0, 0, b""
    while pos < len(raw):
        size = struct.unpack(">I", raw[pos:pos + 4])[0]
        name = raw[pos + 4:pos + 8]
        chunk = raw[pos + 8:pos + 8 + size]
        if name == b"IHDR":
            width, _, _, kind = struct.unpack(">IIBB", chunk[:10])
        elif name == b"IDAT":
            idat += chunk
        pos += 12 + size
    data = zlib.decompress(idat)
    depth = {0: 1, 2: 3, 4: 2, 6: 4}[kind]
    stride = width * depth
    prev, row, at = bytearray(stride), bytearray(stride), 0
    for _ in range(max(1, len(data) // (stride + 1))):
        filt = data[at]
        at += 1
        row = bytearray(data[at:at + stride])
        at += stride
        for x in range(stride):
            a = row[x - depth] if x >= depth else 0
            b = prev[x]
            c = prev[x - depth] if x >= depth else 0
            if filt == 1:
                row[x] = (row[x] + a) & 255
            elif filt == 2:
                row[x] = (row[x] + b) & 255
            elif filt == 3:
                row[x] = (row[x] + ((a + b) >> 1)) & 255
            elif filt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[x] = (row[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = row
        break
    return tuple(row[:depth])


def _pixel(b, x, y):
    png = b.cdp.call("Page.captureScreenshot",
                     {"format": "png", "clip": {"x": x, "y": y, "width": 1, "height": 1, "scale": 1}},
                     session=b.sid)
    return _png_pixel(base64.b64decode(png["data"]))


def _near(pixel, want, tolerance=1):
    return len(pixel) >= 3 and all(abs(pixel[i] - want[i]) <= tolerance for i in range(3))


def _frame_session(b, suffix="index.html"):
    targets = b.cdp.call("Target.getTargets")["targetInfos"]
    rid = b.review["id"]
    frame = next(t for t in targets
                 if t["type"] == "iframe" and rid in t["url"] and t["url"].split("?")[0].endswith(suffix))
    sid = b.cdp.call("Target.attachToTarget", {"targetId": frame["targetId"], "flatten": True})["sessionId"]
    b.cdp.call("Runtime.enable", session=sid)
    return sid


def _in_frame(b, sid, expr):
    got = b.cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session=sid)
    return got["result"].get("value")


def test_a_prototype_page_fills_the_window_and_the_canvas_takes_the_pages_colour(env, server, tmp_path, chrome):
    data = _proto_review(env, tmp_path)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    assert b.until("getComputedStyle(document.querySelector('.proto')).backgroundColor === 'rgb(248, 248, 243)'"), \
        "the probe's background colour never reached the canvas"
    g = _geometry(b)
    assert g["pageMode"], "a prototype round must be in page mode"
    assert abs(g["frame"]["width"] - 1440) <= 1, g
    assert abs(g["frame"]["left"]) <= 1, g
    assert abs(g["frame"]["bottom"] - g["dock"]["top"]) <= 1, g
    assert g["scrollW"] <= g["innerW"], g
    for x in (20, 1420):
        pixel = _pixel(b, x, 450)
        assert _near(pixel, PAGE_BG), f"the pixel at ({x}, 450) is {pixel}, not the page's #f8f8f3"
        board_bg = _rgba(g["bodyBg"])
        assert not _near(pixel, board_bg), f"({x}, 450) is the board canvas colour {pixel}"


def test_mobile_page_is_the_mobile_width_centred_with_the_page_colour_around_it(env, server, tmp_path, chrome):
    data = _proto_review(env, tmp_path)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.press('.dzbar .wseg .seg-btn[data-value="phone"]')
    assert b.until("document.querySelector('.proto iframe').getBoundingClientRect().width === 390"), \
        "the frame never took the mobile width"
    assert b.until("!!document.querySelector('#dz-proto[data-ready=true]') && "
                   "Math.abs(document.querySelector('.proto iframe').getBoundingClientRect().bottom - "
                   "document.querySelector('#dzfoot').getBoundingClientRect().top) <= 1"), "the page never settled"
    g = _geometry(b)
    assert g["pageMode"], "a prototype round must be in page mode"
    assert abs(g["frame"]["width"] - 390) <= 1, g
    assert abs(g["frame"]["left"] - (g["innerW"] - 390) / 2) <= 1, g
    assert abs(g["frame"]["height"] - (g["innerH"] - g["bar"]["height"] - g["dock"]["height"])) <= 2, g
    left = _pixel(b, 100, 450)
    assert _near(left, PAGE_BG), f"the canvas at (100, 450) is {left}, not the page's own background"


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_the_decide_bar_is_one_docked_surface_that_never_covers_the_frame(env, server, tmp_path, chrome,
                                                                         width, height, mobile):
    data = _proto_review(env, tmp_path)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    assert b.until("document.querySelectorAll('#dzfoot .decide[data-id=proto] .sbtn').length === 3"), \
        "the approve/change/skip row is not in the docked footer"
    assert b.until("document.querySelectorAll('#dzfoot .decide[data-id=proto] textarea.dnote').length === 1"), \
        "the note field is not in the docked footer"
    g = _geometry(b)
    assert g["pageMode"], g
    assert g["dockHasDecide"] and len(g["decideButtons"]) == 3 and all(x["inDock"] for x in g["decideButtons"]), g
    assert g["protoFoot"] is None, "the old in-flow decide row must be gone"
    assert g["frame"]["bottom"] <= g["dock"]["top"] + 0.5, g
    assert abs(g["dock"]["bottom"] - g["innerH"]) <= 1, g
    assert abs(g["dock"]["top"] - g["frame"]["bottom"]) <= 1, "the page must account for exactly the dock"
    covered = b.js("""JSON.stringify([].slice.call(document.querySelectorAll('#dzfoot .decide[data-id=proto] .sbtn'))
      .map(function (x) { var r = x.getBoundingClientRect(), hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return !!hit && (hit === x || x.contains(hit)); }))""")
    assert json.loads(covered) == [True, True, True], covered


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_a_prototype_decide_click_updates_the_status_without_page_errors(env, server, tmp_path, chrome,
                                                                         width, height, mobile):
    data = _proto_review(env, tmp_path)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.js("window.__pageErrors = []; addEventListener('error', function (e) {"
         "window.__pageErrors.push(String(e.message)); });")
    assert b.until("(document.getElementById('proto-status') || {}).textContent === 'Open'"), \
        "the docked status must read Open before any click"
    b.click_sel("#dzfoot .decide[data-id=proto] .s-change")
    assert b.until("(document.getElementById('proto-status') || {}).textContent === 'Change'"), \
        "Change must update the docked status"
    assert b.js("document.querySelector('.decide[data-id=proto]').classList.contains('marked')"), \
        "Change must mark the decide surface"
    b.click_sel("#dzfoot .decide[data-id=proto] .s-approve")
    assert b.until("(document.getElementById('proto-status') || {}).textContent === 'Approved'"), \
        "Approve must update the docked status"
    assert b.js("document.querySelector('.decide[data-id=proto]').classList.contains('marked')"), \
        "Approve must keep the decide surface marked"
    assert json.loads(b.js("JSON.stringify(window.__pageErrors)")) == [], "the decide clicks raised a page error"
    hidden = json.loads(b.js("""JSON.stringify((function(){var e=document.getElementById('proto-status'),
      r=e.getBoundingClientRect();return {display:getComputedStyle(e).display,width:r.width,height:r.height};})())"""))
    assert hidden["display"] != "none", hidden
    assert hidden["width"] <= 1 and hidden["height"] <= 1, hidden


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_the_missing_note_warning_does_not_squeeze_the_docked_note_field(env, server, tmp_path, chrome,
                                                                         width, height, mobile):
    data = _proto_review(env, tmp_path, look={"devices": ["desktop", "mobile"], "require_note_on_change": True})
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.js("window.__pageErrors = []; addEventListener('error', function (e) {"
         "window.__pageErrors.push(String(e.message)); });")
    assert b.until("(document.getElementById('proto-status') || {}).textContent === 'Open'"), \
        "the docked status must read Open before any click"
    b.click_sel("#dzfoot .decide[data-id=proto] .s-change")
    warned = """(function(){var el=document.querySelector('.decide[data-id=proto]'), msg=document.getElementById('ft-msg');
      return !!el && el.classList.contains('needs-note') && !!msg && !msg.hidden &&
             getComputedStyle(msg).display !== 'none' && (msg.textContent || '').indexOf('Add a note') >= 0;})()"""
    assert b.until(warned), "the missing-note state never reached the docked footer"
    measured = json.loads(b.js("""JSON.stringify((function(){
      function r(e){var q=e.getBoundingClientRect();
        return {left:q.left, top:q.top, width:q.width, height:q.height, bottom:q.bottom, right:q.right};}
      var decide=document.querySelector('#dzfoot .decide[data-id=proto]'), msg=document.getElementById('ft-msg');
      return {decide:r(decide), dnote:r(decide.querySelector('textarea.dnote')),
              needDisplay:getComputedStyle(decide.querySelector('.need-note')).display,
              footMsg:{hidden:msg.hidden, display:getComputedStyle(msg).display, height:r(msg).height,
                       text:(msg.textContent || '')}};
    })())"""))
    # The shipped fix hides the duplicate in-row warning; the docked footer message (#ft-msg) and the note field's
    # warn border still say a note is missing, so the width branch is the one that passes here. The display branch
    # would accept a future fix that lays the warning out without squeezing the field instead.
    assert measured["dnote"]["width"] >= 0.5 * measured["decide"]["width"] or measured["needDisplay"] == "none", \
        ("the missing-note warning squeezed the note field: dnote %.1f px, decide %.1f px, ratio %.2f, need-note %s"
         % (measured["dnote"]["width"], measured["decide"]["width"],
            measured["dnote"]["width"] / measured["decide"]["width"], measured["needDisplay"]))
    assert not measured["footMsg"]["hidden"], "the docked footer message must not be hidden: %r" % measured["footMsg"]
    assert measured["footMsg"]["display"] != "none" and measured["footMsg"]["height"] > 0, \
        "the docked footer message must still be visible: %r" % measured["footMsg"]
    assert "Add a note" in measured["footMsg"]["text"], \
        "the docked footer message must still say a note is missing: %r" % measured["footMsg"]
    assert json.loads(b.js("JSON.stringify(window.__pageErrors)")) == [], "the missing-note state raised a page error"
    layout = json.loads(b.js("""JSON.stringify((function(){
      var f=document.querySelector('.proto iframe').getBoundingClientRect();
      var d=document.getElementById('dzfoot').getBoundingClientRect();
      return {frameBottom:f.bottom, dockTop:d.top, scrollW:document.documentElement.scrollWidth, innerW:innerWidth};
    })())"""))
    assert layout["frameBottom"] <= layout["dockTop"] + 0.5, "the frame must stay above the dock: %r" % layout
    assert layout["scrollW"] <= layout["innerW"], "the board must not scroll sideways: %r" % layout


CASES = [
    (1440, 900, False, 1),
    (1024, 800, False, 1),
    (1024, 800, False, 8),
    (720, 800, False, 1),
    (720, 800, False, 8),
    (390, 844, True, 1),
    (390, 844, True, 8),
]
CASE_IDS = ["%d-r%d" % (w, n) for w, _, _, n in CASES]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width,height,mobile,rounds", CASES, ids=CASE_IDS)
def test_the_page_footer_is_one_row_of_three_zones_that_keeps_send_on_screen(env, server, tmp_path, chrome, theme,
                                                                            width, height, mobile, rounds):
    data = _proto_review_rounds(env, tmp_path, rounds)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.js("document.documentElement.setAttribute('data-theme', %s)" % json.dumps(theme))
    b.cdp.pump(0.2)
    assert b.until("document.querySelectorAll('#dzfoot .decide[data-id=proto] .sbtn').length === 3"), \
        "the approve/change/skip row is not in the docked footer"
    assert b.until("document.querySelectorAll('#dzfoot .round-tab').length === %d" % rounds), \
        "the footer does not show every round"
    g = _geometry(b)
    for key, label in (("segGrp", "segmented control"), ("footerNote", "note"), ("tallyPill", "tally pill"),
                       ("sendBtn", "Send")):
        assert g[key], f"the {label} is missing from the page-mode footer: {g}"
    assert g["roundTabs"] and len(g["roundTabs"]) == rounds, g["roundTabs"]
    assert g["rounds"] and g["rounds"]["box"], g
    controls = [("round tab #%d" % (i + 1), x) for i, x in enumerate(g["roundTabs"])]
    controls += [("segmented control", g["segGrp"]), ("note", g["footerNote"]),
                 ("tally pill", g["tallyPill"]), ("Send", g["sendBtn"])]
    controls += [("Comment toggle", x) for x in g["footToggles"]]
    want_h = 44 if width <= 600 else 40
    for label, rect in controls:
        if width <= 600 and label == "segmented control":
            assert rect["height"] >= want_h, \
                f"the {label} is only {rect['height']} px tall, not at least {want_h}: {rect}"
            continue
        assert abs(rect["height"] - want_h) <= 1, \
            f"the {label} is {rect['height']} px tall, not {want_h}: {rect}"
    if width <= 600:
        segs = g["decideButtons"]
        assert len(segs) == 3 and all(x["inDock"] for x in segs), \
            "the phone footer must show exactly three decide segments: %r" % (segs,)
        for i, seg in enumerate(segs):
            assert g["segGrpClientH"] >= seg["height"], \
                ("the segmented control clips decide segment %d: clientHeight %r, segment height %r"
                 % (i + 1, g["segGrpClientH"], seg["height"]))
            assert seg["hitsBottom"], \
                ("the bottom of decide segment %d is not tappable (the segmented control clips it): %r"
                 % (i + 1, seg))
            assert seg["hitsCentre"], \
                "the centre of decide segment %d is not tappable: %r" % (i + 1, seg)
            assert seg["width"] >= 44, \
                "decide segment %d is only %s px wide, not at least 44: %r" % (i + 1, seg["width"], seg)
            assert seg["height"] >= 44, \
                "decide segment %d is only %s px tall, not at least 44: %r" % (i + 1, seg["height"], seg)
            assert seg["svg"], \
                "decide segment %d does not render its icon: %r" % (i + 1, seg)
            icon_offset = ((seg["svg"]["left"] + seg["svg"]["right"])
                           - (seg["left"] + seg["right"])) / 2
            assert abs(icon_offset) <= 1, \
                ("the icon of decide segment %d is %s px off centre (icon %r, segment %r)"
                 % (i + 1, icon_offset, seg["svg"], seg))
    assert (len(g["footToggles"]) == 1) == (width <= 600), \
        "the docked footer carries exactly one Comment toggle, and only on a phone: %r" % g["footToggles"]
    rows = _rows(g)
    want = 1 if width >= 720 else 2
    assert rows == want, "the footer is not %d row(s): tops %r" % (
        want, {k: round(g[k]["top"]) for k in ("segGrp", "footerNote", "tallyPill", "sendBtn")})
    for ka, kb in (("segGrp", "footerNote"), ("segGrp", "tallyPill"), ("segGrp", "sendBtn"),
                   ("footerNote", "tallyPill"), ("footerNote", "sendBtn"), ("tallyPill", "sendBtn")):
        if _same_row(g[ka], g[kb]):
            _assert_disjoint(g[ka], g[kb], ka, kb)
    rounds_box = g["rounds"]["box"]
    if want == 1:
        assert rounds_box["right"] <= g["segGrp"]["left"] + 0.5, \
            f"the round tabs zone overlaps the decide control: rounds={rounds_box} grp={g['segGrp']}"
    else:
        assert g["sendbar"], g
        assert rounds_box["right"] <= g["sendbar"]["left"] + 0.5, \
            f"the round tabs zone overlaps the send zone: rounds={rounds_box} sendbar={g['sendbar']}"
    assert (g["rounds"]["scrollWidth"] <= g["rounds"]["clientWidth"] + 1
            or g["rounds"]["overflowX"] in ("auto", "scroll", "hidden")), \
        "the round tabs neither fit nor scroll: %r" % g["rounds"]
    assert g["sendBtn"]["right"] <= g["innerW"] + 0.5, f"Send is clipped: {g['sendBtn']} vs {g['innerW']}"
    assert g["scrollW"] <= g["innerW"], g
    if width == 1440:
        assert g["footerNote"]["width"] >= 300, f"the note field is only {g['footerNote']['width']} px: {g}"
    elif width >= 720:
        assert g["footerNote"]["width"] >= 80, f"the note field is only {g['footerNote']['width']} px: {g}"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (600, 844, True), (390, 844, True)],
                         ids=["desktop", "600", "phone"])
def test_page_mode_keeps_a_visible_comment_toggle_on_a_phone(env, server, tmp_path, chrome, theme, width, height,
                                                             mobile):
    data = _proto_review(env, tmp_path)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.js("document.documentElement.setAttribute('data-theme', %s)" % json.dumps(theme))
    b.cdp.pump(0.2)
    probe = """JSON.stringify((function(){
      var foot = document.getElementById('dzfoot'), head = document.getElementById('comment-toggle');
      function rect(e){var r=e.getBoundingClientRect();
        return {id:e.id, pressed:e.getAttribute('aria-pressed'), display:getComputedStyle(e).display,
                width:r.width, height:r.height};}
      return {foot: [].slice.call(foot.querySelectorAll('[data-comment-toggle]')).map(rect),
              head: head ? rect(head) : null};})())"""
    if width > 600:
        got = json.loads(b.js(probe))
        assert got["foot"] == [], f"the desktop dock must not carry a Comment toggle: {got}"
        assert got["head"] and got["head"]["display"] != "none" and got["head"]["width"] > 0, \
            "the header Comment toggle is the one at desktop width: %r" % got
        return
    got = json.loads(b.js(probe))
    assert len(got["foot"]) == 1, f"the docked footer has no single Comment toggle: {got}"
    toggle = got["foot"][0]
    assert toggle["id"] == "comment-toggle-mobile", toggle
    assert toggle["display"] != "none" and abs(toggle["width"] - 44) <= 1 and abs(toggle["height"] - 44) <= 1, toggle
    assert toggle["pressed"] == "false", toggle
    assert got["head"] and got["head"]["display"] == "none", \
        "the header toggle must be hidden where the docked one shows: %r" % got
    b.click_sel("#dzfoot [data-comment-toggle]")
    assert b.until("(document.querySelector('#dzfoot [data-comment-toggle]') || {}).getAttribute"
                   "('aria-pressed') === 'true'"), "a tap on the docked Comment toggle did not turn comment mode on"
    b.click_sel("#dzfoot [data-comment-toggle]")
    assert b.until("(document.querySelector('#dzfoot [data-comment-toggle]') || {}).getAttribute"
                   "('aria-pressed') === 'false'"), "a second tap did not leave comment mode"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_the_page_footer_shows_the_status_as_the_highlighted_segment_not_a_loose_word(env, server, tmp_path, chrome,
                                                                                      theme, width, height, mobile):
    data = _proto_review(env, tmp_path)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.js("document.documentElement.setAttribute('data-theme', %s)" % json.dumps(theme))
    b.cdp.pump(0.2)
    assert b.until("!!document.getElementById('proto-status')")

    def status():
        return json.loads(b.js("""JSON.stringify((function(){var e=document.getElementById('proto-status'),
          r=e.getBoundingClientRect();return {text:e.textContent,display:getComputedStyle(e).display,
                                              width:r.width,height:r.height};})())"""))

    def no_status_word():
        text = b.js(VISIBLE_FOOT_TEXT)
        for word in ("Open", "Approved", "Skipped"):
            assert word not in text, f"the visible footer shows the loose status word {word!r}: {text!r}"
        return text

    no_status_word()
    st = status()
    assert st["text"] == "Open", st
    assert st["display"] != "none" and st["width"] <= 1 and st["height"] <= 1, st

    b.click_sel("#dzfoot .decide[data-id=proto] .s-change")
    assert b.until("(document.getElementById('proto-status') || {}).textContent === 'Change'"), \
        "Change must update the docked status"
    no_status_word()
    st = status()
    assert st["display"] != "none" and st["width"] <= 1 and st["height"] <= 1, st

    b.click_sel("#dzfoot .decide[data-id=proto] .s-approve")
    assert b.until("(document.getElementById('proto-status') || {}).textContent === 'Approved'"), \
        "Approve must update the docked status"
    no_status_word()
    st = status()
    assert st["text"] == "Approved", st
    assert st["display"] != "none" and st["width"] <= 1 and st["height"] <= 1, st

    pressed = json.loads(b.js("""JSON.stringify((function(){
      function bg(sel){return getComputedStyle(document.querySelector(sel)).backgroundColor;}
      return {approve:bg('#dzfoot .decide[data-id=proto] .s-approve'),
              change:bg('#dzfoot .decide[data-id=proto] .s-change'),
              skip:bg('#dzfoot .decide[data-id=proto] .s-skip'),
              state:[].slice.call(document.querySelectorAll('#dzfoot .decide[data-id=proto] .sbtn'))
                .map(function(x){return x.getAttribute('aria-pressed')})};})())"""))
    assert pressed["state"] == ["true", "false", "false"], pressed
    assert pressed["approve"] != pressed["change"] and pressed["approve"] != pressed["skip"], pressed


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (600, 844, True), (390, 844, True)],
                         ids=["desktop", "600", "phone"])
def test_the_page_footer_keeps_the_pre_change_icons(env, server, tmp_path, chrome, theme, width, height, mobile):
    data = _proto_review(env, tmp_path)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.js("document.documentElement.setAttribute('data-theme', %s)" % json.dumps(theme))
    b.cdp.pump(0.2)
    structure = _geometry(b)["rowChildren"]
    if width <= 600:
        assert [c["tag"] for c in structure] == ["button", "nav", "div", "div"], structure
        assert structure[0]["cls"].split() == ["ctoggle", "foot-comment"], structure[0]
        assert structure[0]["id"] is None, structure[0]
        parts = structure[1:]
    else:
        assert [c["tag"] for c in structure] == ["nav", "div", "div"], structure
        parts = structure
    assert parts[0]["cls"].split() == ["rounds"], parts[0]
    assert "decide" in parts[1]["cls"].split() and parts[1]["id"] == "proto", parts[1]
    assert "sendbar" in parts[2]["cls"].split() and "sendbar-inline" in parts[2]["cls"].split(), parts[2]
    icons = json.loads(b.js("""JSON.stringify({
      buttons: [].slice.call(document.querySelectorAll('#dzfoot .decide[data-id=proto] .sbtn svg.ico'))
        .map(function(s){return s.outerHTML;}),
      send: document.querySelector('#dzfoot .btn-send svg.ico').outerHTML})"""))
    assert icons["buttons"] == BASE_ICONS, icons["buttons"]
    assert icons["send"] == BASE_SEND_ICON, icons["send"]


def _wait_round_tab_in_strip(b, timeout=3.0):
    """Scroll sync runs on the next frame; pump() is not a sleep, so poll for the settled layout."""
    return b.until("""(function(){
      var s = document.querySelector('#dzfoot .rounds'), o = s && s.querySelector('.round-tab.on');
      if (!o) return false;
      var a = s.getBoundingClientRect(), c = o.getBoundingClientRect();
      return c.left >= a.left - 0.5 && c.right <= a.right + 0.5;})()""", timeout=timeout)


def _assert_round_tab_in_strip(g, expect=None):
    assert g["rounds"] and g["rounds"]["box"], f"the footer has no round strip: {g}"
    box, on = g["rounds"]["box"], g["roundOn"]
    assert on is not None, f"no round tab is marked on in the footer: {g}"
    if expect is not None:
        assert g["roundOnText"] == expect, f"the viewed round is {g['roundOnText']!r}, not {expect!r}: {g}"
    assert on["left"] >= box["left"] - 0.5 and on["right"] <= box["right"] + 0.5, \
        ("the viewed round tab (%s) is not fully inside the round strip: strip=%r on=%r scrollLeft=%r "
         "scrollWidth=%r clientWidth=%r" % (g["roundOnText"], box, on, g["rounds"]["scrollLeft"],
                                            g["rounds"]["scrollWidth"], g["rounds"]["clientWidth"]))


ROUND_CASES = [
    (390, 844, True, 2),
    (390, 844, True, 8),
    (720, 800, False, 8),
    (1024, 800, False, 8),
    (1440, 900, False, 8),
]
ROUND_CASE_IDS = ["390-r2", "390-r8", "720-r8", "1024-r8", "1440-r8"]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width,height,mobile,rounds", ROUND_CASES, ids=ROUND_CASE_IDS)
def test_the_viewed_round_tab_stays_inside_the_round_strip(env, server, tmp_path, chrome, theme, width, height,
                                                           mobile, rounds):
    data = _proto_review_rounds(env, tmp_path, rounds)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.js("document.documentElement.setAttribute('data-theme', %s)" % json.dumps(theme))
    assert b.until("document.querySelectorAll('#dzfoot .round-tab').length === %d" % rounds), \
        "the footer does not show every round"
    assert _wait_round_tab_in_strip(b), ("the viewed round tab did not settle inside the round strip within 3 s: %r"
                                         % (_geometry(b),))
    _assert_round_tab_in_strip(_geometry(b))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_fractional_round_strip_keeps_the_last_tab_fully_inside(env, server, tmp_path, chrome, theme):
    """The runner's 1024 layout gave the strip a 321.546875 px box; scrollWidth and clientWidth are integers there, so
    at max scroll the last tab ended 0.515625 px past the strip's fractional right (a settled layout, not a slow one).
    Force that exact fraction so the integer clamp is exercised on every machine: the strip's trailing slack pixel is
    what keeps the viewed round inside instead of waiting out the timeout."""
    data = _proto_review_rounds(env, tmp_path, 8)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1024, height=800,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.js("document.documentElement.setAttribute('data-theme', %s)" % json.dumps(theme))
    assert b.until("document.querySelectorAll('#dzfoot .round-tab').length === 8"), \
        "the footer does not show every round"
    assert b.js("(function(){var s=document.querySelector('#dzfoot .rounds');"
                "s.style.flex='0 0 321.546875px';s.style.width='321.546875px';return true})()")
    assert b.until("""(function(){
      var s = document.querySelector('#dzfoot .rounds'), o = s && s.querySelector('.round-tab.on');
      if (!o) return false;
      s.scrollLeft = 1e6;
      var a = s.getBoundingClientRect(), c = o.getBoundingClientRect();
      return c.left >= a.left - 0.5 && c.right <= a.right + 0.5;})()""", timeout=3), \
        ("the last tab is not inside a fractional strip at max scroll: %r" % (_geometry(b),))
    _assert_round_tab_in_strip(_geometry(b), "R8")


def test_a_resize_keeps_the_viewed_round_tab_inside_the_round_strip(env, server, tmp_path, chrome):
    data = _proto_review_rounds(env, tmp_path, 8)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.cdp.pump(0.2)
    _assert_round_tab_in_strip(_geometry(b), "R8")
    b.cdp.call("Emulation.setDeviceMetricsOverride",
               {"width": 900, "height": 800, "deviceScaleFactor": 1, "mobile": False}, session=b.sid)
    assert b.until("innerWidth === 900", timeout=3.0), \
        "the browser did not take the 900 px viewport: %r" % (b.js("[innerWidth, innerHeight]"),)
    assert _wait_round_tab_in_strip(b), ("the viewed round tab did not settle inside the round strip within 3 s: %r"
                                         % (_geometry(b),))
    _assert_round_tab_in_strip(_geometry(b), "R8")


def _click_round_tab(b, label):
    """Real-click the visible part of a round tab: once the strip is scrolled its older tabs can be clipped, and the
    centre of a clipped tab lands on whatever sits beside the strip."""
    point = json.loads(b.js("""JSON.stringify((function(){
      var s = document.querySelector('#dzfoot .rounds');
      var t = [].slice.call(document.querySelectorAll('#dzfoot .round-tab'))
        .filter(function (x) { return x.textContent.trim() === %s; })[0];
      if (!s || !t) return null;
      var sr = s.getBoundingClientRect(), tr = t.getBoundingClientRect();
      var left = Math.max(sr.left, tr.left), right = Math.min(sr.right, tr.right);
      if (right - left < 2) return null;
      return [(left + right) / 2, (tr.top + tr.bottom) / 2];})())""" % json.dumps(label)))
    assert point, f"the {label} tab is not visible inside the round strip"
    b.click(*point)


def test_switching_rounds_keeps_the_viewed_round_tab_inside_the_round_strip(env, server, tmp_path, chrome):
    data = _proto_review_rounds(env, tmp_path, 2)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=390, height=844, mobile=True,
              wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    b.review = data
    b.cdp.pump(0.2)
    assert b.until("(document.querySelector('#dzfoot .round-tab.on') || {}).textContent === 'R2'"), \
        "the phone does not open on the current round"
    _click_round_tab(b, "R1")
    assert b.until("(document.querySelector('#dzfoot .round-tab.on') || {}).textContent === 'R1'"), \
        "the R1 tab did not switch the view"
    _click_round_tab(b, "R2")
    assert b.until("(document.querySelector('#dzfoot .round-tab.on') || {}).textContent === 'R2'"), \
        "the R2 tab did not switch the view back"
    assert _wait_round_tab_in_strip(b), ("the viewed round tab did not settle inside the round strip within 3 s: %r"
                                         % (_geometry(b),))
    _assert_round_tab_in_strip(_geometry(b), "R2")


def test_fixed_dialogs_and_sticky_headers_behave_like_a_browser_tab(env, server, tmp_path, chrome):
    data = _proto_review(env, tmp_path)
    b = _open(chrome, data["id"], server)
    b.review = data
    g = _geometry(b)
    assert g["pageMode"], "a prototype round must be in page mode"
    assert abs(g["frame"]["height"] - (g["innerH"] - g["bar"]["height"] - g["dock"]["height"])) <= 2, g
    assert b.js("document.documentElement.scrollHeight <= innerHeight + 1"), \
        "the outer board page must not scroll in page mode"
    sid = _frame_session(b)
    assert _in_frame(b, sid, "(document.getElementById('open-dlg').click(), 1)")
    b.cdp.pump(0.2)
    dialog = json.loads(_in_frame(b, sid, """JSON.stringify((function(){var d=document.getElementById('dlg'),
      r=d.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,
      shown:getComputedStyle(d).display!=='none',vh:innerHeight,vw:innerWidth};})())"""))
    assert dialog["shown"], dialog
    assert 0 <= dialog["top"] and dialog["bottom"] <= dialog["vh"], f"the fixed dialog left the frame viewport: {dialog}"
    assert 0 <= dialog["left"] and dialog["right"] <= dialog["vw"], dialog
    _in_frame(b, sid, "window.scrollTo(0, 1200)")
    b.cdp.pump(0.2)
    deadline = time.monotonic() + 5
    seen = None
    while time.monotonic() < deadline:
        seen = json.loads(_in_frame(b, sid, """JSON.stringify((function(){var s=document.getElementById('sticky'),
          r=s.getBoundingClientRect();return {top:r.top,height:r.height,y:scrollY,docH:document.documentElement.scrollHeight};})())"""))
        if seen["y"] >= 1200:
            break
        b.cdp.pump(0.1)
    assert seen and seen["y"] >= 1200 and seen["docH"] > seen["y"], seen
    assert abs(seen["top"]) <= 1, f"the sticky header did not stay at the top of the frame: {seen}"
    assert seen["height"] > 0, seen


def test_audio_in_a_page_mode_frame_still_seeks(env, server, tmp_path, chrome):
    d = tmp_path / "media"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "tone.wav").write_bytes(_pcm_wav())
    (d / "index.html").write_text(AUDIO_PAGE, encoding="utf-8")
    data = store.create_review("Page audio", "", ORIGIN, "local", store.parse_round_spec(prototype=str(d / "index.html")),
                               wake=False)
    b = _open(chrome, data["id"], server)
    b.review = data
    g = _geometry(b)
    assert g["pageMode"], "the audio page is not in page mode"
    assert abs(g["frame"]["height"] - (g["innerH"] - g["bar"]["height"] - g["dock"]["height"])) <= 2, g
    sid = _frame_session(b)
    assert b.until("true")
    deadline = time.monotonic() + 20
    probe = None
    while time.monotonic() < deadline:
        raw = _in_frame(b, sid, "window.__audioProbe ? window.__audioProbe() : ''")
        if raw:
            probe = json.loads(raw)
            if probe.get("state") in ("seeked", "error", "timeout"):
                break
        b.cdp.pump(0.2)
    assert probe is not None and probe.get("state") == "seeked", probe
    assert math.isfinite(probe["duration"]) and abs(probe["duration"] - 5) <= 0.1, probe
    assert probe["seekable"] >= 1, probe
    assert probe["seekableEnd"] >= probe["duration"] - 0.1, probe
    assert abs(probe["after"] - probe["duration"] / 2) <= 0.1, probe


def test_a_pin_inside_the_page_opens_a_thread_anchored_to_the_element(env, server, tmp_path, chrome):
    data = _proto_review(env, tmp_path)
    b = _open(chrome, data["id"], server)
    b.review = data
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    sid = _frame_session(b)
    point = json.loads(_in_frame(b, sid, """JSON.stringify((function(){var r=document.getElementById('copy').getBoundingClientRect();
      return [r.left+r.width/2, r.top+r.height/2];})())"""))
    frame = _geometry(b)["frame"]
    b.cdp.pump(0.2)   # let the board's state message reach the probe before the tap
    b.click(frame["left"] + point[0], frame["top"] + point[1])
    assert b.until("!!document.querySelector('.popover')"), "the tap on the page opened no pin composer"
    assert b.js("(document.querySelector('.pop-where') || {}).textContent || ''").startswith("on "), \
        b.js("(document.querySelector('.pop-where') || {}).textContent || ''")
    b.js("(function(t){t.value='Please adjust this';t.dispatchEvent(new Event('input'));return 1})"
         "(document.querySelector('.popover textarea'))")
    b.press(".popover .composer-actions .btn-round")
    draft_expr = ("Object.values(localStorage).map(function(v){try{return JSON.parse(v)}catch(e){return null}})"
                  ".find(function(v){return v&&v.pins&&v.pins.length})")
    assert b.until("!!(%s)" % draft_expr, timeout=10)
    pin = json.loads(b.js("JSON.stringify((%s).pins[0])" % draft_expr))
    assert pin["block"] == "proto" and pin["target"] and pin["target"]["path"], pin
    assert "copy" in pin["target"]["path"], pin
    b.press("#pins-proto .pin-row")
    assert b.until("!!document.querySelector('.tpop')"), "the pin did not open its thread"
    where = b.js("(document.querySelector('.tpop .tp-where') || {}).textContent || ''")
    assert where.startswith("on ") and where != "on ", where
    assert b.js("(document.querySelector('.tpop') || {}).textContent || ''").find("Please adjust this") >= 0
    g = _geometry(b)
    assert g["pageMode"] and g["frame"]["height"] > 0, "the pin was made on a frame that is not page mode"


# The wait right after Page.navigate: document-start scripts can run while the new document has no documentElement
# yet, so the null guard must be the first term (a null .clientWidth read throws and the run dies).
FIRST_PAGE_WAIT_BODY = ("document.documentElement.clientWidth < innerWidth && window.__sbwAt === 'loading' && "
                        "!!document.querySelector('#dz-film iframe.art-frame') && "
                        "!!document.querySelector('#dz-small iframe.art-frame')")
FIRST_PAGE_WAIT = "!!document.documentElement && " + FIRST_PAGE_WAIT_BODY

FULL_BLEED_SETTLED = """(function(){var f=document.querySelector('#dz-film iframe'),
    s=document.querySelector('#dz-small iframe'), bar=document.querySelector('.dzbar'),
    foot=document.querySelector('#dzfoot');
    if(!f||!s||!bar||!foot)return false;
    var h=innerHeight-bar.getBoundingClientRect().height-foot.getBoundingClientRect().height;
    return Math.abs(f.getBoundingClientRect().height-h)<=2 &&
           Math.abs(f.getBoundingClientRect().width-document.documentElement.clientWidth)<=1 &&
           Math.abs(s.getBoundingClientRect().height-400)<=2;})()"""

FULL_BLEED_DUMP = """JSON.stringify((function(){function g(sel){var e=document.querySelector(sel);
    if(!e)return null;var r=e.getBoundingClientRect();return [r.left,r.top,r.width,r.height];}
    return {inner:[innerWidth,innerHeight],clientWidth:document.documentElement.clientWidth,
            film:g('#dz-film iframe'),small:g('#dz-small iframe'),
            filmStyle:(document.querySelector('#dz-film iframe')||{}).style&&
                      document.querySelector('#dz-film iframe').style.height,
            smallStyle:(document.querySelector('#dz-small iframe')||{}).style&&
                       document.querySelector('#dz-small iframe').style.height,
            foot:g('#dzfoot'),bar:g('.dzbar'),
            barh:getComputedStyle(document.documentElement).getPropertyValue('--barh'),
            footh:getComputedStyle(document.documentElement).getPropertyValue('--footh')};})())"""

# Removes the display bar and footer whenever board.js adds them: the state the first settle evaluation can hit.
WITHHOLD_DISPLAY_CHROME = """(function(){
  function strip(){var els=document.querySelectorAll('.dzbar,#dzfoot');for(var i=0;i<els.length;i++)els[i].remove();}
  new MutationObserver(strip).observe(document, {childList:true, subtree:true});
  strip();
})()"""


def test_an_artifact_display_page_gets_the_full_bleed_frame_and_the_plain_one_is_unchanged(env, server, tmp_path, chrome):
    d = tmp_path / "dec"
    d.mkdir()
    (d / "decisions.json").write_text(json.dumps({"blocks": [
        {"type": "artifact", "id": "film", "title": "Film", "html": ART_PAGE, "display": "page"},
        {"type": "artifact", "id": "small", "title": "Small", "html": ART_SMALL},
    ]}), encoding="utf-8")
    data = store.create_review("Page artifacts", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(d / "decisions.json")), wake=False)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-film iframe.art-frame') && "
                   "!!document.querySelector('#dz-small iframe.art-frame')")
    b.review = data
    url = f"{server}/r/{data['id']}/"
    b.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": CLASSIC_SCROLLBAR_FROM_START}, session=b.sid)
    b.cdp.call("Page.navigate", {"url": url}, session=b.sid)
    assert b.until(FIRST_PAGE_WAIT, timeout=60), \
        "the classic scrollbar was not active from the first layout: %s" % b.js(
            "JSON.stringify({inner:innerWidth,clientWidth:document.documentElement.clientWidth,"
            "sbwAt:window.__sbwAt})")
    settled = FULL_BLEED_SETTLED
    assert b.until(settled, timeout=20), b.js(FULL_BLEED_DUMP)
    page = json.loads(b.js("""JSON.stringify((function(){var s=document.querySelector('#dz-film'), f=s.querySelector('iframe'),
      r=f.getBoundingClientRect(), art=f.parentElement, cs=getComputedStyle(art);
      return {width:r.width,height:r.height,left:r.left,
              clientWidth:document.documentElement.clientWidth,section:s.className,
              radius:cs.borderRadius,outline:cs.outlineStyle+' '+cs.outlineWidth};})())"""))
    plain = json.loads(b.js("""JSON.stringify((function(){var s=document.querySelector('#dz-small'), f=s.querySelector('iframe'),
      r=f.getBoundingClientRect(), art=f.parentElement, cs=getComputedStyle(art);
      return {width:r.width,height:r.height,section:s.className,
              radius:cs.borderRadius,outline:cs.outlineStyle+' '+cs.outlineWidth};})())"""))
    assert "blk-art-page" in page["section"], page
    assert abs(page["width"] - page["clientWidth"]) <= 1 and abs(page["left"]) <= 1, page
    assert page["height"] > 700, page
    assert page["radius"] == "0px" and page["outline"].startswith("none"), page
    assert "blk-art-page" not in plain["section"], plain
    assert plain["width"] < 1000 and abs(plain["height"] - 400) <= 2, plain
    assert plain["radius"] == "14px" and plain["outline"].startswith("solid"), plain


def test_the_full_bleed_wait_treats_a_missing_display_bar_as_not_yet(env, server, tmp_path, chrome):
    """board.js creates .dzbar and #dzfoot after the artifact iframes; the wait must say "not yet", not throw."""
    d = tmp_path / "dec"
    d.mkdir()
    (d / "decisions.json").write_text(json.dumps({"blocks": [
        {"type": "artifact", "id": "film", "title": "Film", "html": ART_PAGE, "display": "page"},
        {"type": "artifact", "id": "small", "title": "Small", "html": ART_SMALL},
    ]}), encoding="utf-8")
    data = store.create_review("Page artifacts", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(d / "decisions.json")), wake=False)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-film iframe.art-frame') && "
                   "!!document.querySelector('#dz-small iframe.art-frame')")
    b.review = data
    b.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": WITHHOLD_DISPLAY_CHROME}, session=b.sid)
    b.cdp.call("Page.navigate", {"url": f"{server}/r/{data['id']}/"}, session=b.sid)
    assert b.until("!!document.querySelector('#dz-film iframe.art-frame') && "
                   "!!document.querySelector('#dz-small iframe.art-frame')", timeout=60), \
        "the artifact frames never loaded"
    assert b.js(FULL_BLEED_SETTLED) is False, "a page with no display bar cannot be settled"
    dump = json.loads(b.js(FULL_BLEED_DUMP))
    assert dump["bar"] is None and dump["foot"] is None, dump
    assert b.js("1 + 1") == 2, "the board page itself must still answer"


def test_the_first_wait_treats_a_document_without_a_document_element_as_not_yet(chrome):
    """A document-start hook runs while the new document is being created; the first wait must say "not yet"."""
    b = Board(chrome, "about:blank", wait="true")
    b.js("document.removeChild(document.documentElement)")
    assert b.js(FIRST_PAGE_WAIT) is False, "the guarded first wait must not throw on a null documentElement"
    assert b.until(FIRST_PAGE_WAIT, timeout=1) is None, "a page with no documentElement cannot be settled"
    with pytest.raises(bc.BrowserCheckError) as ei:      # a PageScriptError on HEAD: the wait names the throw
        b.until(FIRST_PAGE_WAIT_BODY, timeout=1)
    message = str(ei.value)
    assert FIRST_PAGE_WAIT_BODY[:60] in message and "after 1s" in message, message


def test_a_classic_scrollbar_does_not_make_the_page_artifact_overhang_the_page(env, server, tmp_path, chrome):
    d = tmp_path / "dec"
    d.mkdir()
    (d / "decisions.json").write_text(json.dumps({"blocks": [
        {"type": "artifact", "id": "film", "title": "Film", "html": ART_PAGE, "display": "page"},
        {"type": "artifact", "id": "small", "title": "Small", "html": ART_SMALL},
    ]}), encoding="utf-8")
    data = store.create_review("Page artifacts", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(d / "decisions.json")), wake=False)
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900,
              wait="!!document.querySelector('#dz-film iframe.art-frame') && "
                   "!!document.querySelector('#dz-small iframe.art-frame')")
    b.review = data
    assert b.js(CLASSIC_SCROLLBAR)
    settled = """(function(){var f=document.querySelector('#dz-film iframe');if(!f)return false;
      var r=f.getBoundingClientRect(), w=document.documentElement.clientWidth;
      return Math.abs(r.left)<=1 && Math.abs(r.width-w)<=1;})()"""
    ok = b.until(settled, timeout=5)
    page = json.loads(b.js("""JSON.stringify((function(){var f=document.querySelector('#dz-film iframe');
      var r=f.getBoundingClientRect();
      return {width:r.width,height:r.height,left:r.left,inner:innerWidth,
              clientWidth:document.documentElement.clientWidth,
              scrollW:document.documentElement.scrollWidth};})())"""))
    assert ok, "the full-bleed frame still measures the viewport, not the layout viewport: %s" % page
    assert abs(page["width"] - page["clientWidth"]) <= 1, page
    assert abs(page["left"]) <= 1, page
