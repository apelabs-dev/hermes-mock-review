"""Comment mode pins a click on anything in a mock or a framed page, its buttons, links and fields included.

Owner decision (2026-11-22): in comment mode a click on a mock control becomes a pin anchored to that control and does
not act (no handler, no navigation, no toggle, no focus); Enter or Space on a focused control does nothing. Only the
board's own decision controls, anything a page marks `[data-mr-live]`, and the pin markers keep their clicks. The
reviewer turns comment mode off to use a mock's controls. The live bug was an inline `<button><span>…</span></button>`
that the hover outlined but a click did not pin.

These drive a real headless browser with real mouse and key events (Input.dispatchMouseEvent / dispatchKeyEvent).
They skip when no Chrome, Chromium or Edge is installed.
"""
import itertools
import json

import pytest

from mockreview import store
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
ROOT = "document.querySelector('#dz-m1 .mock-host').shadowRoot"
APP = ROOT + ".querySelector('.app')"
DRAFT = ("Object.values(localStorage).map(v=>{try{return JSON.parse(v)}catch(_){return null}})"
         ".find(v=>v&&v.pins&&v.pins.length)")

# The owner's repro is the first two elements: a heading and a picker toggle whose label is a span.
MOCK = ('<section style="padding:16px"><h2>New category</h2>'
        '<button class="picker-toggle" type="button" style="padding:14px 24px"><span>Show all 100 icons</span></button>'
        '<p><a id="docs" href="https://example.test/docs" style="display:inline-block;padding:6px">'
        '<span>Read the docs</span></a></p>'
        '<details id="more"><summary id="more-sum">More options</summary><p>Hidden</p></details>'
        '<p><input id="name" placeholder="Name"></p>'
        '<div data-mr-live style="padding:8px"><button id="keep" type="button">Keep live</button></div>'
        '<p id="copy">Plain copy</p></section>')

DECISIONS = {"blocks": [
    {"type": "mock", "id": "m1", "title": "Category picker", "html": MOCK},
    {"type": "decision", "id": "d1", "title": "Layout",
     "options": [{"id": "a", "label": "Grid", "html": "<p>Grid</p>"},
                 {"id": "b", "label": "List", "html": "<p>List</p>"}]},
]}

BUTTON = "New category > button “Show all 100 icons”"


@pytest.fixture
def controls_review(env, tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Controls", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)), wake=False)
    return data["id"]


def board(chrome, server, rid):
    b = Board(chrome, f"{server}/r/{rid}/",
              wait="!!document.querySelector('#dz-m1 .mock-host')?.shadowRoot?.querySelector('.picker-toggle')")
    b.js("import('./anchors.js').then(m=>window.__anchors=m)")
    assert b.until("!!window.__anchors")
    return b


def on(b, want=True):
    if b.aria() != ("true" if want else "false"):
        b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === %s"
                   % json.dumps("true" if want else "false"))


def point(b, sel, root=ROOT):
    got = b.js("(()=>{let e=%s.querySelector(%s);if(!e)return 'null';e.scrollIntoView({block:'center'});"
               "let r=e.getBoundingClientRect();return JSON.stringify([r.left+r.width/2,r.top+r.height/2])})()"
               % (root, json.dumps(sel)))
    assert got != "null", sel
    b.cdp.pump(0.05)
    return json.loads(got)


def where(b):
    """The open new-pin composer's `on …` line."""
    assert b.until("!!document.querySelector('.popover .pop-where')", timeout=5), "the new-pin composer did not open"
    return b.js("document.querySelector('.popover .pop-where').textContent")


def close(b):
    b.press(".popover .pop-close")
    assert b.until("!document.querySelector('.popover')", timeout=5)


def save(b, count):
    b.js("document.querySelector('.popover textarea').value='Please adjust this'")
    b.press(".popover .btn-primary")
    assert b.until("!document.querySelector('.popover')", timeout=5)
    assert b.until("(%s)?.pins.length === %d" % (DRAFT, count), timeout=5), "the pin did not reach the draft"
    return json.loads(b.js("JSON.stringify(%s)" % DRAFT))["pins"][count - 1]


def tabs(b):
    return {t["targetId"] for t in b.cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page"}


def resolves_to(b, target, sel):
    return b.js("window.__anchors.resolve(%s, %s) === %s.querySelector(%s)"
                % (APP, json.dumps(target), ROOT, json.dumps(sel)))


INLINE = (  # (where the real click lands, the control it must pin, the composer label)
    (".picker-toggle span", ".picker-toggle", BUTTON),
    ("#docs span", "#docs", "New category > a “Read the docs”"),
    ("#more-sum", "#more-sum", "New category > summary “More options”"),
    ("#name", "#name", "New category > input"),
)


def test_inline_click_pins_each_control_and_does_not_work_it(server, controls_review, chrome):
    b = board(chrome, server, controls_review)
    # A window listener runs after the board's own, so it sees whether the board cancelled each click.
    b.js("(()=>{let r=%s;window.__seen=[];window.__focus=0;window.__toggles=0;"
         "addEventListener('click',e=>{let t=e.composedPath()[0];if(t&&t.closest&&t.closest('section'))"
         "window.__seen.push(e.defaultPrevented)});"
         "r.querySelector('#name').addEventListener('focus',()=>window.__focus++);"
         "r.querySelector('#more').addEventListener('toggle',()=>window.__toggles++);return 1})()" % ROOT)
    before = tabs(b)
    on(b)
    for n, (spot, control, label) in enumerate(INLINE, 1):
        b.click(*point(b, spot))
        assert where(b) == "on " + label
        pin = save(b, n)
        assert pin["target"]["label"] == label
        assert resolves_to(b, pin["target"], control), "the saved target must resolve to %s" % control
    b.cdp.pump(0.3)
    opened = tabs(b) - before
    for t in opened:
        b.cdp.call("Target.closeTarget", {"targetId": t})
    state = json.loads(b.js("JSON.stringify({seen:window.__seen,focus:window.__focus,toggles:window.__toggles,"
                            "open:%s.querySelector('#more').open,active:(%s.activeElement||{}).id||''})" % (ROOT, ROOT)))
    assert state == {"seen": [True] * 4, "focus": 0, "toggles": 0, "open": False, "active": ""}, state
    assert not opened, "a comment-mode click on a mock link must not open it"


def test_owner_repro_span_inside_a_button_pins_the_button_and_survives_reload(server, controls_review, chrome):
    b = board(chrome, server, controls_review)
    on(b)
    b.click(*point(b, ".picker-toggle span"))
    assert where(b) == "on " + BUTTON
    target = save(b, 1)["target"]
    assert resolves_to(b, target, ".picker-toggle")
    assert b.until("document.querySelectorAll('#dz-m1 .mpin').length === 1")
    b.cdp.call("Page.reload", session=b.sid)
    assert b.until("document.querySelectorAll('#dz-m1 .mpin').length === 1", timeout=30), "the pin did not come back"
    b.js("import('./anchors.js').then(m=>window.__anchors=m)")
    assert b.until("!!window.__anchors")
    assert not b.js("document.querySelector('#dz-m1 .mpin').classList.contains('det')"), \
        "the reloaded pin must re-resolve to the button, not detach"
    assert resolves_to(b, target, ".picker-toggle")


def test_live_mock_element_and_board_decision_controls_keep_their_clicks(server, controls_review, chrome):
    b = board(chrome, server, controls_review)
    b.js("(()=>{window.__keep=0;%s.querySelector('#keep').addEventListener('click',()=>window.__keep++);return 1})()"
         % ROOT)
    on(b)
    b.click(*point(b, "#keep"))
    assert b.until("window.__keep === 1"), "a [data-mr-live] mock element must keep its click in comment mode"
    b.cdp.pump(0.2)
    assert not b.popover(), "a [data-mr-live] mock element must never pin"
    for sel in ('#dz-d1 .vbtn[data-o="b"]', "#dz-d1 .sbtn.s-approve", "#dz-d1 .sbtn.s-change", "#dz-d1 .sbtn.s-skip"):
        b.click(*point(b, sel, "document"))
        assert b.until("document.querySelector(%s).getAttribute('aria-pressed') === 'true'" % json.dumps(sel)), sel
        assert not b.popover(), "%s must stay a board control, not a pin" % sel
    b.click(*point(b, "#dz-d1 .dnote", "document"))
    assert b.until("document.activeElement === document.querySelector('#dz-d1 .dnote')")
    b.key("o", "KeyO", 79, text="o")
    b.key("k", "KeyK", 75, text="k")
    assert b.js("document.querySelector('#dz-d1 .dnote').value") == "ok" and not b.popover()
    assert b.aria() == "true"


def visible_hover(b):
    return b.js("JSON.stringify((()=>{let o=[...document.querySelectorAll('.mr-hover')].find(o=>!o.hidden);"
                "if(!o)return null;let r=o.getBoundingClientRect();"
                "return {rect:[r.left,r.top,r.width,r.height],label:o.querySelector('.mr-hover-label').textContent}})())")


def move(b, xy):
    for dx in (-1, 0):   # two moves: a same-position move may coalesce
        b.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": xy[0] + dx, "y": xy[1], "button": "none"},
                   session=b.sid)


def test_hover_outlines_the_control_a_click_would_pin_and_skips_live(server, controls_review, chrome):
    b = board(chrome, server, controls_review)
    on(b)
    move(b, point(b, ".picker-toggle span"))
    want = json.loads(b.js("(()=>{let r=%s.querySelector('.picker-toggle').getBoundingClientRect();"
                           "return JSON.stringify([r.left,r.top,r.width,r.height])})()" % ROOT))
    assert b.until("[...document.querySelectorAll('.mr-hover')].some(o=>!o.hidden)"), "no hover outline over the button"
    got = json.loads(visible_hover(b))
    assert all(abs(a - w) <= 1 for a, w in zip(got["rect"], want)), (got, want)
    assert got["label"] == BUTTON, got
    move(b, point(b, "#keep"))
    assert b.until("![...document.querySelectorAll('.mr-hover')].some(o=>!o.hidden)"), \
        "a [data-mr-live] element keeps its click, so the hover must not outline it: %s" % visible_hover(b)


def test_inline_keys_do_not_press_a_mock_control_in_comment_mode(server, controls_review, chrome):
    b = board(chrome, server, controls_review)
    b.js("(()=>{window.__presses=0;%s.querySelector('.picker-toggle').addEventListener('click',()=>window.__presses++);"
         "return 1})()" % ROOT)
    on(b)
    b.js("%s.querySelector('.picker-toggle').focus()" % ROOT)
    b.key("Enter", "Enter", 13, text="\r")
    b.key(" ", "Space", 32, text=" ")
    b.js("%s.querySelector('#more-sum').focus()" % ROOT)
    b.key("Enter", "Enter", 13, text="\r")
    b.cdp.pump(0.3)
    assert b.js("window.__presses") == 0, "Enter or Space must not press a mock button in comment mode"
    assert not b.js("%s.querySelector('#more').open" % ROOT), "Enter must not open a mock summary in comment mode"
    assert not b.popover(), "a key makes no pin"
    assert b.aria() == "true"
    on(b, False)
    b.js("%s.querySelector('.picker-toggle').focus()" % ROOT)
    b.key("Enter", "Enter", 13, text="\r")
    assert b.until("window.__presses === 1"), "with comment mode off Enter presses the mock's button"
    b.key(" ", "Space", 32, text=" ")
    assert b.until("window.__presses === 2"), "with comment mode off Space presses the mock's button"


# ---- framed pages: an artifact (probe first) and a prototype (probe last) ----

FRAME_PAGE = '''<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font:16px sans-serif;min-height:640px}
h2{margin:0;padding:12px 20px}
#go{position:absolute;left:20px;top:60px;width:220px;height:60px}
#hash{position:absolute;left:20px;top:150px;padding:6px}
#more{position:absolute;left:20px;top:200px}
#field{position:absolute;left:20px;top:260px;width:180px}
#keep{position:absolute;left:300px;top:60px;width:180px;height:60px}
#copy{position:absolute;left:300px;top:150px;margin:0}
</style></head><body>
<h2>Frame page</h2>
<button id="go" type="button"><span>Do the thing</span></button>
<a id="hash" href="#moved">Jump</a>
<details id="more"><summary id="more-sum">More</summary><p>Hidden</p></details>
<input id="field">
<button id="keep" type="button" data-mr-live>Keep live</button>
<p id="copy">Plain copy</p>
<script>
var log = [];
function say(m) { m.fx = 1; parent.postMessage(m, '*'); }
function note(kind) { return function () { log.push(kind); }; }
var go = document.getElementById('go');
['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(function (t) { go.addEventListener(t, note('go-' + t)); });
document.getElementById('keep').addEventListener('click', note('keep-click'));
document.getElementById('field').addEventListener('focus', note('field-focus'));
document.getElementById('more').addEventListener('toggle', note('more-toggle'));
addEventListener('hashchange', note('hashchange'));
addEventListener('mousemove', function (e) {
  say({hover: e.composedPath().map(function (n) { return n.id || ''; }).filter(Boolean), x: e.clientX, y: e.clientY});
});
addEventListener('message', function (e) {
  var d = e.data || {};
  if (d.fxCommand === 'state') {
    var points = {};
    ['go', 'hash', 'more-sum', 'field', 'keep', 'copy'].forEach(function (id) {
      var r = document.getElementById(id).getBoundingClientRect();
      points[id] = [r.left + r.width / 2, r.top + r.height / 2];
    });
    say({state: d.n, log: log.slice(), hash: location.hash, open: document.getElementById('more').open,
         active: document.activeElement ? document.activeElement.id : '', points: points,
         comment: document.documentElement.classList.contains('mr-comment')});
  }
  if (d.fxCommand === 'clear') log = [];
  if (d.fxCommand === 'focus') document.getElementById(d.id).focus();
});
addEventListener('load', function () { parent.postMessage({proto_ready: 1}, '*'); });
</script></body></html>'''

FRAMES = {"artifact": ".art-frame", "prototype": ".proto iframe"}
LISTEN = ("window.__fx=null;window.__hover=null;addEventListener('message',e=>{let d=e.data;if(!d||!d.fx)return;"
          "if('state' in d)window.__fx=d;if(d.hover)window.__hover=d})")


class Frame:
    def __init__(self, b, sel):
        self.b, self.sel, self.n = b, json.dumps(sel), itertools.count(1)
        b.js(LISTEN)

    def post(self, message):
        return ("(function(){try{document.querySelector(%s).contentWindow.postMessage(%s,'*')}catch(e){}})()"
                % (self.sel, json.dumps(message)))

    def state(self, until="true", timeout=20):
        """The page's own report, re-asked until `until` (a JS expression over `s`) holds."""
        n = next(self.n)
        ask = self.post({"fxCommand": "state", "n": n})
        got = self.b.until("(function(){%s;let s=window.__fx;return !!s && s.state>=%d && (%s)})()" % (ask, n, until),
                           timeout=timeout)
        assert got, "the framed page never reported (%s): %s" % (until, self.b.js("JSON.stringify(window.__fx)"))
        return json.loads(self.b.js("JSON.stringify(window.__fx)"))

    def rect(self):
        return json.loads(self.b.js("JSON.stringify((()=>{let r=document.querySelector(%s).getBoundingClientRect();"
                                    "return [r.left,r.top]})())" % self.sel))

    def click(self, id):
        """A real click on #id once a real pointer move at that point has landed on it inside the frame.

        A cold compositor can still map a pointer event to an earlier layout, so the click waits (bounded) until the
        page itself reports a move over the element at that same point."""
        for _ in range(10):
            pt = self.state()["points"][id]
            fr = self.rect()
            self.b.js("window.__hover=null")
            move(self.b, (fr[0] + pt[0], fr[1] + pt[1]))
            if self.b.until("window.__hover && window.__hover.hover.indexOf(%s) >= 0 && Math.abs(window.__hover.x-%r)"
                            "<=1.5 && Math.abs(window.__hover.y-%r)<=1.5" % (json.dumps(id), pt[0], pt[1]), timeout=2):
                self.b.click(fr[0] + pt[0], fr[1] + pt[1])
                return
        raise AssertionError("a real pointer move never landed on #%s in the frame" % id)

    def key(self, id, *key):
        self.b.js("document.querySelector(%s).focus()" % self.sel)
        self.b.js(self.post({"fxCommand": "focus", "id": id}))
        self.state("s.active === %s" % json.dumps(id))
        self.b.key(*key)


@pytest.fixture(params=sorted(FRAMES))
def framed(request, env, server, chrome, tmp_path):
    kind = request.param
    page = tmp_path / "page" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(FRAME_PAGE, encoding="utf-8")
    if kind == "prototype":
        spec = store.parse_round_spec(prototype=str(page))
        wait = Board.PROTO_READY
    else:
        path = tmp_path / "artifact.json"
        path.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "a1", "title": "Frame artifact",
                                                "markable": True, "html": FRAME_PAGE}]}), encoding="utf-8")
        spec = store.parse_round_spec(decisions=str(path))
        wait = "!!document.querySelector('.art-frame')"
    data = store.create_review("Framed controls", "", ORIGIN, "local", spec, wake=False)
    b = Board(chrome, f"{server}/r/{data['id']}/", wait=wait)
    f = Frame(b, FRAMES[kind])
    f.state()
    if kind == "artifact":   # an auto-height artifact starts small and grows to the probe's hello
        assert b.until("document.querySelector('.art-frame').getBoundingClientRect().height > 300", timeout=10)
    b.js("document.querySelector(%s).scrollIntoView({block:'center'})" % json.dumps(FRAMES[kind]))
    return b, f


ENTER = ("Enter", "Enter", 13, "\r")
SPACE = (" ", "Space", 32, " ")


def test_frame_controls_pin_in_comment_mode_and_work_without_it(framed):
    b, f = framed
    on(b)
    f.state("s.comment === true")
    for id, label in (("go", "Frame page > button “Do the thing”"), ("hash", "Frame page > a “Jump”"),
                      ("more-sum", "Frame page > summary “More”"), ("field", "Frame page > input")):
        f.click(id)
        assert where(b) == "on " + label
        close(b)
    b.cdp.pump(0.3)
    s = f.state()
    assert (s["log"], s["hash"], s["open"], s["active"]) == ([], "", False, ""), \
        "a comment-mode click must not reach the page's handlers, navigate, toggle or focus: %s" % s
    # [data-mr-live] keeps its click and never pins.
    f.click("keep")
    assert f.state("s.log.indexOf('keep-click') >= 0", timeout=5)["log"] == ["keep-click"]
    assert not b.popover(), "a [data-mr-live] element in a frame must never pin"
    # Enter and Space on a focused control press nothing and pin nothing.
    f.b.js(f.post({"fxCommand": "clear"}))
    f.key("go", *ENTER)
    f.key("go", *SPACE)
    b.cdp.pump(0.3)
    assert f.state()["log"] == [], "Enter or Space must not press a framed button in comment mode"
    assert not b.popover() and b.aria() == "true"

    on(b, False)
    f.state("s.comment === false")
    f.key("go", *ENTER)
    assert f.state("s.log.indexOf('go-click') >= 0", timeout=5), "with comment mode off Enter presses the button"
    f.b.js(f.post({"fxCommand": "clear"}))
    f.click("go")
    s = f.state("s.log.indexOf('go-click') >= 0", timeout=5)
    assert s["log"] == ["go-pointerdown", "go-mousedown", "go-mouseup", "go-click"], s
    f.click("hash")
    assert f.state("s.hash === '#moved'", timeout=5)
    f.click("more-sum")
    assert f.state("s.open === true", timeout=5)
    f.click("field")
    assert f.state("s.active === 'field'", timeout=5)
    assert not b.popover(), "with comment mode off no click pins"
