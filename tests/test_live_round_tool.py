"""One Comment toggle, OFF at first, on every live surface.

Before 1.1.7 a prototype round inherited the board's Comment tool, so the probe inside the frame swallowed every click
and turned it into a pin; later a "Use it | Pin" picker sat under the page, and the board had three separate pickers for
the same idea. These tests drive a real headless browser with real mouse events (Input.dispatchMouseEvent), because
attribute/DOM-only checks passed while clicks were dead (1.1.2). They skip when no Chrome, Chromium or Edge is
installed.
"""
import json
import time

import pytest

import browserkit as bc
from mockreview import store

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}

# The button posts to the board window, so the test can count clicks that really reached the page's own handler.
PAGE = ('<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0;font:16px sans-serif}'
        '#go{position:absolute;left:20px;top:20px;width:220px;height:90px}</style></head><body>'
        '<button id="go" type="button">Do the thing</button><p id="copy" style="position:absolute;left:20px;top:140px">Review this text</p>'
        '<script>var n=0;document.getElementById("go").addEventListener("click",function(){n++;'
        'parent.postMessage({proto_click:n},"*");});'
        'addEventListener("load",function(){parent.postMessage({proto_ready:1},"*");});</script></body></html>')

RECORDER = ('if (window.top === window) { window.__clicks = 0; window.__ready = 0; window.__escapes = 0;'
            ' addEventListener("message", function (e) {'
            ' if (e.data && e.data.proto_ready) window.__ready = 1;'
            ' if (e.data && e.data.proto_click) window.__clicks = e.data.proto_click;'
            ' if (e.data && e.data.__mockreview === 1 && e.data.type === "escape") window.__escapes = (window.__escapes||0)+1; }); }')

# A native control that needs no script; scripts and inline handlers are stripped by the board.
DECISIONS = {"blocks": [{"type": "mock", "id": "m1",
                         "html": '<details id="d"><summary id="s">More</summary><p>hidden</p></details>'}]}

# Real fields in the mock markup. The board renders them in the mock's shadow root, where a key event that bubbles to
# the document is retargeted to .mock-host before the board's own `c` handler sees it.
FIELDS = {"blocks": [{"type": "mock", "id": "m1",
                      "html": '<input id="mock-input"><textarea id="mock-textarea"></textarea>'}]}


@pytest.fixture
def chrome():
    try:
        bc.find_browser()
    except bc.BrowserUnavailable:
        pytest.skip("no Chrome/Chromium/Edge installed")
    # A cold browser on a busy CI runner (Windows especially) can take well over the production check's 20 s to open
    # its debugging port, so give it longer and one relaunch. A launch failure is a rig problem, not the board's.
    for attempt in (1, 2):
        browser = bc.Chrome(startup=60)
        try:
            host, port, path = browser.__enter__()
            break
        except bc.BrowserCheckError:
            browser.__exit__(None, None, None)
            if attempt == 2:
                raise
    try:
        ws = bc.WebSocket(host, port, path)
        try:
            yield bc.Cdp(ws, bc.Observer())
        finally:
            ws.close()
    finally:
        browser.__exit__(None, None, None)


@pytest.fixture
def live_review(env, tmp_path):
    page = tmp_path / "proto" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(PAGE, encoding="utf-8")
    data = store.create_review("Live", "", ORIGIN, "tailscale", store.parse_round_spec(prototype=str(page)))
    return data["id"]


@pytest.fixture
def decisions_review(env, tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Live", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(path)))
    return data["id"]


@pytest.fixture
def fields_review(env, tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(FIELDS), encoding="utf-8")
    data = store.create_review("Live", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(path)))
    return data["id"]


class Board:
    PROTO_READY = "!!document.querySelector('.proto iframe') && window.__ready === 1"

    def __init__(self, cdp, url, wait=None, width=1280, height=900, mobile=False):
        self.cdp = cdp
        tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        for m in ("Page.enable", "Runtime.enable"):
            cdp.call(m, session=self.sid)
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1,
                                                        "mobile": mobile}, session=self.sid)
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER}, session=self.sid)
        cdp.call("Page.navigate", {"url": url}, session=self.sid)
        assert self.until(wait or self.PROTO_READY, timeout=60), "the board never loaded"
        time.sleep(0.5)  # the probe's hello reaches the board, which answers with the tool state

    def js(self, expr):
        return self.cdp.evaluate(expr, session=self.sid)

    def until(self, expr, timeout=15.0):
        end = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < end:
            try:
                got = self.js(expr)
            except bc.PageScriptError as e:
                last_error = e      # the page is mid-navigation or its DOM is not built yet: not yet, not a crash
                got = None
            else:
                last_error = None
            if got:
                return got
            self.cdp.pump(0.1)
        if last_error is not None:
            # The last poll threw: name the expression and the throw instead of a bare timeout, so the next CI log
            # tells a typo or a broken expression from a condition that simply did not happen.
            expression = " ".join(expr.split())[:240]
            reason = " ".join(str(last_error).split())[:280]
            raise bc.PageScriptError("the wait timed out after %ss with the page still throwing: %s: %s"
                                     % (timeout, expression, reason))
        return None

    def press(self, sel):
        # The pin popover's own buttons sit over the cross-process frame, and right after the popover appears Chrome's
        # hit-test data can still send a synthetic click to the frame underneath (a person is never that fast).
        # Those buttons are not under test, so press them directly; every click on the page itself is a real one.
        assert self.js("(function(e){if(!e)return false;e.click();return true})(document.querySelector(%s))"
                       % json.dumps(sel)), f"{sel} is not on the page"

    def click(self, x, y):
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            p = {"type": kind, "x": x, "y": y, "button": "left" if kind != "mouseMoved" else "none", "clickCount": 1}
            self.cdp.call("Input.dispatchMouseEvent", p, session=self.sid)

    def click_sel(self, sel):
        r = json.loads(self.js("(function(){var e=document.querySelector(%s);if(!e)return 'null';"
                               "var r=e.getBoundingClientRect();return JSON.stringify([r.left+r.width/2,r.top+r.height/2]);})()"
                               % json.dumps(sel)))
        assert r, f"{sel} is not on the page"
        self.click(*r)

    def key(self, key, code=None, vk=None, text=None):
        down = {"type": "keyDown", "key": key}
        if code:
            down["code"] = code
        if vk:
            down["windowsVirtualKeyCode"] = vk
        if text is not None:
            down["text"] = text
        self.cdp.call("Input.dispatchKeyEvent", down, session=self.sid)
        up = {"type": "keyUp", "key": key}
        if code:
            up["code"] = code
        if vk:
            up["windowsVirtualKeyCode"] = vk
        self.cdp.call("Input.dispatchKeyEvent", up, session=self.sid)

    def tap_page(self, tries=4):
        """A real click on the page's button. Returns 'page' when the page's own handler ran, 'pin' when the board
        opened a pin instead. A click that did neither (a slow runner still compositing the frame) is retried; it is
        never counted as either outcome, so the bug (a pin) and the fix (the handler) stay distinguishable."""
        before = self.clicks()
        for _ in range(tries):
            r = json.loads(self.js("JSON.stringify((function(r){return [r.left,r.top]})("
                                   "document.querySelector('.proto iframe').getBoundingClientRect()))"))
            self.click(r[0] + 130, r[1] + 65)   # the centre of #go inside the frame
            got = self.until("window.__clicks > %d ? 'page' : document.querySelector('.popover') ? 'pin' : ''" % before,
                             timeout=4)
            if got:
                return got
        return "missed"

    def tap_text(self):
        r = json.loads(self.js("JSON.stringify((function(r){return [r.left,r.top]})("
                               "document.querySelector('.proto iframe').getBoundingClientRect()))"))
        self.click(r[0] + 80, r[1] + 160)
        return self.until("document.querySelector('.popover') ? 'pin' : ''", timeout=4)

    def aria(self):
        return self.js("(document.querySelector('#comment-toggle')||{}).getAttribute('aria-pressed')")

    def label(self):
        return self.js("(document.querySelector('#comment-toggle .lb')||{}).textContent||''")

    def clicks(self):
        return self.js("window.__clicks")

    def pins(self):
        return self.js("document.querySelectorAll('#pins-proto li').length")

    def popover(self):
        return self.js("!!document.querySelector('.popover')")

    def summary_center(self):
        self.js("(function(){var s=document.querySelector('.mock-host').shadowRoot.querySelector('summary');"
                "if(s)s.scrollIntoView({block:'center'});return 1})()")
        self.cdp.pump(0.1)
        r = self.js("(function(){var s=document.querySelector('.mock-host').shadowRoot.querySelector('summary');"
                    "var r=s.getBoundingClientRect();return JSON.stringify([r.left+r.width/2,r.top+r.height/2]);})()")
        return json.loads(r)

    def details_open(self):
        return self.js("document.querySelector('.mock-host').shadowRoot.querySelector('details').open")


def test_live_round_opens_interactive_and_clicks_work_the_page(server, live_review, chrome):
    b = Board(chrome, f"{server}/r/{live_review}/")
    assert b.js("!!document.querySelector('#dzbar #comment-toggle')"), "the comment toggle must live in the header"
    assert b.aria() == "false" and b.label() == "Comment"
    assert not b.js("document.body.classList.contains('proto-pin')")
    assert not b.js("!!document.querySelector('#dzbar .seg:not(.wseg)')"), "the old Tap/Pin picker must be gone"
    assert b.tap_page() == "page", "a first click on the live page must reach the page's handler, not drop a pin"
    assert b.clicks() == 1 and not b.popover() and b.pins() == 0


def test_comment_mode_stays_on_after_a_pin_is_saved_or_cancelled(server, live_review, chrome):
    b = Board(chrome, f"{server}/r/{live_review}/")
    b.click_sel("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    assert b.label() == "Exit comment mode" and b.js("document.body.classList.contains('proto-pin')")
    assert b.tap_page() == "page", "native buttons stay live in comment mode"
    assert b.tap_text() == "pin", "noninteractive text opens a pin"
    assert b.clicks() == 1, "the native button handler must run in comment mode"
    b.js("(function(t){t.value='make this bigger';t.dispatchEvent(new Event('input'));return 1})"
         "(document.querySelector('.popover textarea'))")
    b.press(".popover .btn-primary")
    assert b.until("document.querySelectorAll('#pins-proto li').length === 1")
    assert b.aria() == "true" and b.label() == "Exit comment mode"
    assert b.js("document.body.classList.contains('proto-pin')"), "comment mode must stay on after a pin is saved"
    assert b.tap_text() == "pin", "the very next text tap must still pin"
    b.press(".popover .pop-close")
    assert b.until("!document.querySelector('.popover')")
    assert b.aria() == "true" and b.js("document.body.classList.contains('proto-pin')") and b.pins() == 1
    # Esc pressed inside the frame: the probe forwards it, the board leaves comment mode and the page works again.
    b.js("document.querySelector('.proto iframe').focus()")
    b.key("Escape", "Escape", 27)
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'false'")
    assert not b.js("document.body.classList.contains('proto-pin')")
    assert (b.js("window.__escapes || 0") or 0) >= 1, "the probe must forward Esc from the frame"
    assert b.tap_page() == "page" and b.clicks() == 2
    # Two real clicks: on, then off.
    b.click_sel("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    b.click_sel("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'false'")
    # c is ignored while a textarea is focused...
    b.js("(function(){var t=document.createElement('textarea');t.id='tmp-ta';document.body.appendChild(t);t.focus();return 1})()")
    b.key("c", "KeyC", 67, text="c")
    assert b.aria() == "false", "c must not toggle while a textarea is focused"
    b.js("(function(){var t=document.getElementById('tmp-ta');t.remove();"
         "if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();return 1})()")
    # ...and turns comment mode on from the page.
    b.key("c", "KeyC", 67, text="c")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'"), \
        "c must turn comment mode on"


def test_decisions_round_click_works_the_mock_until_comment_mode(server, decisions_review, chrome):
    b = Board(chrome, f"{server}/r/{decisions_review}/",
              wait="!!document.querySelector('.mock-host') && !!document.querySelector('.mock-host').shadowRoot")
    assert b.details_open() is False
    b.click(*b.summary_center())
    assert b.until("document.querySelector('.mock-host').shadowRoot.querySelector('details').open === true"), \
        "a click must reach the mock's own control"
    assert not b.popover()
    b.click_sel("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    # Close it again first, so "the mock did not react" is a real observation and not just the state we left behind.
    b.js("document.querySelector('.mock-host').shadowRoot.querySelector('details').open = false")
    b.click(*b.summary_center())
    assert b.until("document.querySelector('.mock-host').shadowRoot.querySelector('details').open === true"), "native summary stays live"
    assert not b.popover()
    r = json.loads(b.js("(function(){var r=document.querySelector('.mock-host').shadowRoot.querySelector('p').getBoundingClientRect();return JSON.stringify([r.left+10,r.top+5])})()"))
    b.click(*r)
    assert b.until("!!document.querySelector('.popover')"), "noninteractive text must pin a note"
    b.key("Escape", "Escape", 27)   # first Esc closes the popover
    assert b.until("!document.querySelector('.popover')")
    b.key("Escape", "Escape", 27)   # second Esc leaves comment mode
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'false'")
    assert not b.js("document.body.classList.contains('tool-comment')")
    b.click(*b.summary_center())
    assert b.until("document.querySelector('.mock-host').shadowRoot.querySelector('details').open === false"), \
        "a click must work the mock again once comment mode is off"


@pytest.mark.parametrize("field", ["mock-input", "mock-textarea"])
def test_c_types_in_a_mock_field_instead_of_toggling(server, fields_review, chrome, field):
    b = Board(chrome, f"{server}/r/{fields_review}/",
              wait="!!document.querySelector('.mock-host') && !!document.querySelector('.mock-host').shadowRoot")
    assert b.aria() == "false"
    b.js("document.querySelector('.mock-host').shadowRoot.getElementById(%s).focus()" % json.dumps(field))
    b.key("x", "KeyX", 88, text="x")
    b.key("c", "KeyC", 67, text="c")
    value = b.js("document.querySelector('.mock-host').shadowRoot.getElementById(%s).value" % json.dumps(field))
    assert value == "xc", f"typing in the mock's {field} must not lose c to the comment toggle"
    assert b.aria() == "false", f"c typed in the mock's {field} must not turn comment mode on"


def test_full_screen_close_tooltip_follows_comment_mode(server, review, chrome):
    b = Board(chrome, f"{server}/r/{review['id']}/",
              wait="!!document.querySelector('[aria-label=\"Open variant A full size\"]')")
    assert b.aria() == "false"
    b.press('[aria-label="Open variant A full size"]')
    assert b.until("!!document.querySelector('.modal')")
    close_title = "document.querySelector('.modal [aria-label=\"Close\"]').title"
    assert b.js(close_title) == "Close (Esc)"
    b.js("document.querySelector('.modal').setAttribute('data-keep', '1')")
    b.press('.modal [data-comment-toggle]')
    assert b.until(close_title + " === 'Close'")
    assert b.js("document.querySelector('.modal').getAttribute('data-keep')") == "1", "the modal must not be rebuilt"
    b.press('.modal [data-comment-toggle]')
    assert b.until(close_title + " === 'Close (Esc)'")
    assert b.js("document.querySelector('.modal').getAttribute('data-keep')") == "1", "the modal must not be rebuilt"
