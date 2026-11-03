"""The "Try it live" round must be usable: a click on the page works the page, and pinning is opt-in.

Before 1.1.7 a prototype round inherited the board's Comment tool, so the probe inside the frame swallowed every click
and turned it into a pin; the "Use it | Pin" switch sat under the page. These tests drive a real headless browser with
real mouse events (Input.dispatchMouseEvent), because attribute/DOM-only checks passed while clicks were dead (1.1.2).
They skip when no Chrome, Chromium or Edge is installed.
"""
import json
import time

import pytest

from mockreview import browsercheck as bc, store

ORIGIN = {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}

# The button posts to the board window, so the test can count clicks that really reached the page's own handler.
PAGE = ('<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0;font:16px sans-serif}'
        '#go{position:absolute;left:20px;top:20px;width:220px;height:90px}</style></head><body>'
        '<button id="go" type="button">Do the thing</button>'
        '<script>var n=0;document.getElementById("go").addEventListener("click",function(){n++;'
        'parent.postMessage({proto_click:n},"*");});'
        'addEventListener("load",function(){parent.postMessage({proto_ready:1},"*");});</script></body></html>')

RECORDER = ('if (window.top === window) { window.__clicks = 0; window.__ready = 0; addEventListener("message", function (e) {'
            ' if (e.data && e.data.proto_ready) window.__ready = 1;'
            ' if (e.data && e.data.proto_click) window.__clicks = e.data.proto_click; }); }')


@pytest.fixture
def chrome():
    try:
        bc.find_browser()
    except bc.BrowserUnavailable:
        pytest.skip("no Chrome/Chromium/Edge installed")
    with bc.Chrome() as (host, port, path):
        ws = bc.WebSocket(host, port, path)
        try:
            yield bc.Cdp(ws, bc.Observer())
        finally:
            ws.close()


@pytest.fixture
def live_review(env, tmp_path):
    page = tmp_path / "proto" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(PAGE, encoding="utf-8")
    data = store.create_review("Live", "", ORIGIN, "tailscale", store.parse_round_spec(prototype=str(page)))
    return data["id"]


class Board:
    def __init__(self, cdp, url):
        self.cdp = cdp
        tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        for m in ("Page.enable", "Runtime.enable"):
            cdp.call(m, session=self.sid)
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1,
                                                        "mobile": False}, session=self.sid)
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER}, session=self.sid)
        cdp.call("Page.navigate", {"url": url}, session=self.sid)
        assert self.until("!!document.querySelector('.proto iframe') && window.__ready === 1", timeout=60), \
            "the prototype round never loaded"
        time.sleep(0.5)  # the probe's hello reaches the board, which answers with the tool state

    def js(self, expr):
        return self.cdp.evaluate(expr, session=self.sid)

    def until(self, expr, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = self.js(expr)
            if got:
                return got
            self.cdp.pump(0.1)
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

    def tool(self):
        return self.js("(document.querySelector('#proto-tool .seg-btn.on')||{}).textContent||''")

    def clicks(self):
        return self.js("window.__clicks")

    def pins(self):
        return self.js("document.querySelectorAll('#pins-proto li').length")

    def popover(self):
        return self.js("!!document.querySelector('.popover')")


def test_live_round_opens_on_use_it_and_clicks_work_the_page(server, live_review, chrome):
    b = Board(chrome, f"{server}/r/{live_review}/")
    assert b.tap_page() == "page", "a first click on the live page must reach the page's handler, not drop a pin"
    assert b.clicks() == 1 and not b.popover() and b.pins() == 0
    assert b.tool() == "Use it"
    assert b.js("!!document.querySelector('#dzbar #proto-tool')"), "the switch must live in the header"
    assert not b.js("!!document.querySelector('.proto-foot .seg')"), "no second switch under the page"


def test_pin_is_opt_in_and_drops_back_to_use_it(server, live_review, chrome):
    b = Board(chrome, f"{server}/r/{live_review}/")
    # Pin, then save: a pin is added, the page's handler never ran, and the next click works the page again.
    b.click_sel("#proto-tool .seg-btn:nth-child(2)")
    assert b.until("document.body.classList.contains('proto-pin')") and b.tool() == "Pin"
    assert b.tap_page() == "pin", "a click in Pin mode must open a pin"
    assert b.clicks() == 0, "in Pin mode the page's handler must not run"
    b.js("(function(t){t.value='make this bigger';t.dispatchEvent(new Event('input'));return 1})(document.querySelector('.popover textarea'))")
    b.press(".popover .btn-primary")
    assert b.until("document.querySelectorAll('#pins-proto li').length === 1")
    assert b.tool() == "Use it" and not b.js("document.body.classList.contains('proto-pin')")
    assert b.tap_page() == "page", "after a pin the page must be clickable again"
    assert b.clicks() == 1 and b.pins() == 1 and not b.popover()
    # Pin, then cancel: no pin, back to Use it.
    b.click_sel("#proto-tool .seg-btn:nth-child(2)")
    assert b.until("document.body.classList.contains('proto-pin')")
    assert b.tap_page() == "pin"
    b.press(".popover .pop-actions .btn-ghost")
    assert b.until("!document.querySelector('.popover')")
    assert b.tool() == "Use it" and b.pins() == 1
    assert b.tap_page() == "page"
    assert b.clicks() == 2
