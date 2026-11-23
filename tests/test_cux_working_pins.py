"""Hermes pin activity on a live board in desktop and coarse-touch phone browsers."""

import base64
import json
import os
from pathlib import Path
import time

import pytest

from authkit import signed_in, token_of
from conftest import http
from mockreview import api, config, live, owner, service, store, threads, users
from test_board_frames_threads import frame_session
from test_cux_send_to_hermes import mock_point, on, save_new_pin
from test_live_round_tool import Board, chrome  # noqa: F401
from test_mention_wake import call_tool, pin_requests, plugin_tools, post

pytestmark = pytest.mark.browser

RECORDER = """
window.boardInstance = Math.random();
window.boardGets = [];
window.pinEvents = [];
const fetchBoard = window.fetch;
window.fetch = function(url, options) {
  if (!options || !options.method || options.method === 'GET') boardGets.push(String(url));
  return fetchBoard.apply(this, arguments);
};
window.EventSource = class extends EventSource {
  constructor(url) {
    super(url);
    window.boardEvents = this;
    this.addEventListener('hello', () => window.boardHello = true);
    this.addEventListener('pin.requests', event => pinEvents.push(JSON.parse(event.data)));
  }
};
"""
COUNT = '.hermes-working-count'
WORKING = '.mpin[data-hermes-state="working"]'
FAILED = '.mpin[data-hermes-state="failed"]'
CHIP = '.hermes-block-status'
STATUS = '.tp-typing .hermes-thread-status'
TIMER = STATUS + ' time'


def query(selector):
    return f'document.querySelector({json.dumps(selector)})'


def exists(selector):
    return f'!!{query(selector)}'


def has_text(selector, text):
    return f'({query(selector)}?.textContent || "").includes({json.dumps(text)})'


class WorkingBoard(Board):
    def __init__(self, chrome, server, review, mobile, member=None, artifact=False, init_script=""):
        self.cdp = chrome
        size = {"width": 390, "height": 844} if mobile else {"width": 1440, "height": 900}
        self.width = size["width"]
        tid = chrome.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.sid = chrome.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        chrome.call("Page.enable", session=self.sid)
        chrome.call("Emulation.setDeviceMetricsOverride", {
            **size, "deviceScaleFactor": 1, "mobile": mobile,
        }, session=self.sid)
        if mobile:
            chrome.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5},
                        session=self.sid)
        chrome.call("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-reduced-motion", "value": "reduce" if mobile else "no-preference"},
            {"name": "prefers-color-scheme", "value": "light" if mobile else "dark"},
        ]}, session=self.sid)
        if member:
            for key, value in (("mr_session", member["session"]), ("mr_csrf", member["csrf"])):
                chrome.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                            session=self.sid)
        chrome.call("Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER + init_script}, session=self.sid)
        chrome.call("Page.navigate", {"url": f"{server}/r/{review['id']}/"}, session=self.sid)
        surface = exists(".art-frame") if artifact else "!!document.querySelector('.mock-host')?.shadowRoot?.querySelector('#copy')"
        assert self.until(f"window.boardHello && boardEvents.readyState === 1 && {surface}", timeout=30)
        assert self.js("matchMedia('(pointer: coarse)').matches") is mobile
        assert self.js("innerWidth") == self.width

    def shows(self, expression, message):
        assert self.until(expression, timeout=3), message

    def reset_observation(self):
        return self.js("window.boardGets.length = 0; window.pinEvents.length = 0; boardInstance")

    def same_document(self, instance):
        assert self.js("boardInstance") == instance

    def click(self, x, y):
        if self.width > 600:
            return super().click(x, y)
        self.cdp.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
                      session=self.sid)
        self.cdp.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []}, session=self.sid)

    def screenshot(self, mobile, state):
        dest = os.environ.get("MOCKREVIEW_WORKING_PIN_SCREENSHOTS")
        if dest:
            filename = f"{'phone' if mobile else 'desktop'}-{state}.png"
            self.cdp.pump(0.3)  # let the existing popover or sheet entrance finish
            png = self.cdp.call("Page.captureScreenshot", {"format": "png"}, session=self.sid)["data"]
            Path(dest, filename).write_bytes(base64.b64decode(png))


@pytest.fixture
def working_review(env, tmp_path, monkeypatch):
    live.reset()
    threads.reset_limits()
    monkeypatch.setattr(owner, "resolve_soft", lambda: owner.Context(config.data_root(), False))
    monkeypatch.setattr(service.notify, "send", lambda *args, **kwargs: (True, "accepted"))
    monkeypatch.setattr(service.notify, "send_notice", lambda *args, **kwargs: (True, "accepted"))
    mock = tmp_path / "checkout.json"
    mock.write_text(json.dumps({"blocks": [{
        "type": "mock", "id": "m1", "title": "Order summary",
        "html": ('<section style="padding:24px;background:#f7f5f0;color:#232321;font:16px sans-serif">'
                 '<h2>Order summary</h2><p id="copy" style="width:220px;height:64px">Subtotal · $48.00</p>'
                 '<p>Shipping calculated at checkout</p></section>'),
    }]}), encoding="utf-8")
    spec = store.parse_round_spec(decisions=str(mock))
    yield lambda pin_fix=False: store.create_review(
        "Checkout review", "", {"platform": "discord", "chat_id": "test-chat"}, "local", spec,
        wake=True, look={"pin_fix": pin_fix, "pin_fix_min_seconds": 0, "pin_fix_window_seconds": 0})
    live.reset()


@pytest.fixture(params=[False, True], ids=["desktop", "phone"])
def mobile(request):
    return request.param


def mention(server, rid, idem="first", tid=None):
    status, _, body = post(server, rid, "@Hermes Clarify the subtotal", idem=idem, thread_id=tid)
    assert status == (200 if tid else 201), body
    return json.loads(body)["thread"]["id"]


def fix(server, review, headers=None, expected=200):
    code, _, body = http("POST", f"{server}/r/{review['id']}/api/pin-fix", {
        "round": 1, "token": review["token"],
        "pin": {"id": "subtotal", "block": "m1", "x": 0.62, "y": 0.35, "text": "Clarify the subtotal"},
    }, headers=headers)
    assert code == expected, body
    return json.loads(body)


def test_editable_mention_with_pin_fix_off(working_review, server, chrome, mobile, tmp_path):
    review = working_review()
    rid = review["id"]
    b = WorkingBoard(chrome, server, review, mobile)
    instance = b.reset_observation()
    on(b, b.width)
    b.click(*mock_point(b))
    assert b.until(exists(".popover .hermes-box:checked"))
    save_new_pin(b, "Clarify the subtotal")
    service.retry_thread_wakes(rid)
    assert {q["status"] for q in pin_requests(rid).values()} == {"woken"}
    b.shows(has_text(COUNT, "Hermes · 1 working"), "a delivered mention never appeared in the header")
    b.shows(exists(WORKING), "the draft's linked marker never started working")
    tid = threads.list_threads(rid)[0]["id"]
    mention(server, rid, "overlap", tid)
    service.retry_thread_wakes(rid)
    b.shows(has_text(COUNT, "Hermes · 2 working"), "a second request on the same thread was lost")
    owner_user = users.owner_user()
    threads.create(rid, f"user:{owner_user['id']}", owner_user["name"], "owner", "Ordinary comment",
                   anchor={"block": "m1", "x": 0.85, "y": 0.6})
    assert b.js(f"document.querySelectorAll({json.dumps(CHIP)}).length") == 1
    assert b.js(has_text(CHIP, "Hermes is working…"))
    assert b.js("document.documentElement.scrollWidth === innerWidth")
    b.click_sel(COUNT)
    b.shows(has_text(".tpanel .trow", "Working"), "the header did not open the working Comments list")
    assert b.js("document.querySelectorAll('.tpanel .trow').length") == 1
    b.press(".tpanel .trow")
    b.shows(has_text(STATUS, "Hermes is working on this…"), "opening the working row omitted thread activity")
    assert b.js(exists(TIMER))
    b.js("window.elapsedBefore = " + query(TIMER) + ".textContent")
    b.shows("pinEvents.length >= 4", "did not receive the two queue and two delivery events")
    b.cdp.pump(0.15)
    b.js("window.getsBefore = boardGets.length")
    b.shows(f"{query(TIMER)}.textContent !== elapsedBefore", "elapsed time did not advance")
    assert b.js("boardGets.length === getsBefore"), "the elapsed timer caused a network request"
    b.shows(exists(WORKING), "the active marker disappeared")
    motion = b.js(f"getComputedStyle({query(WORKING + ' .hermes-pin-indicator')}).animationName")
    assert (motion == "none") is mobile
    dot = json.loads(b.js("JSON.stringify((function(){var p=%s,d=p.querySelector('.hermes-pin-indicator');"
                          "var a=p.getBoundingClientRect(),r=d.getBoundingClientRect(),s=getComputedStyle(d);"
                          "return {bg:s.backgroundColor,anim:s.animationName,dx:(r.left+r.width/2)-(a.left+a.width/2),"
                          "dy:(r.top+r.height/2)-(a.top+a.height/2),w:r.width}})())" % query(WORKING)))
    red = [int(c) for c in dot["bg"][dot["bg"].index("(") + 1:dot["bg"].index(")")].split(",")[:3]]
    assert red[0] > 200 and red[1] < 130 and red[2] < 130, f"the working dot is not red: {dot}"   # --err per theme
    assert dot["dx"] > 8 and abs(dot["dy"]) < 8, f"the working dot is not on the pin's right side: {dot}"
    assert dot["w"] < 20, f"the working indicator is a ring, not a dot: {dot}"   # a 10 px dot, turned and pulsing
    assert dot["anim"] == ("none" if mobile else "hermes-pin-pulse"), dot
    first = next(iter(pin_requests(rid)))
    api.patch_block(rid, "m1", "<p>Revised subtotal</p>", round_n=1, if_rev=0, request_ids=[first])
    b.shows(has_text(COUNT, "Hermes · 1 working"), "one patch cleared both overlapping requests")
    b.press(".tpop .tp-close")
    store.add_round(rid, store.parse_round_spec(decisions=str(tmp_path / "checkout.json")))
    new_thread = mention(server, rid, "next")
    service.retry_thread_wakes(rid)
    b.shows(f"!{exists(COUNT)}", "the old round kept work after a new round opened")
    if mobile:
        b.press(".round-picker")
        b.press(".menu-round:not(.on)")
    else:
        b.press('.round-tab[title="Round 2"]')
    b.shows(exists(f'{WORKING}[data-thread="{new_thread}"]'), "switching rounds did not show the new working pin")
    assert b.js(has_text(COUNT, "Hermes · 1 working"))
    b.same_document(instance)


def test_reviewer_pin_fix_without_a_thread(working_review, server, chrome, mobile, monkeypatch):
    review = working_review(pin_fix=True)
    b = WorkingBoard(chrome, server, review, mobile)
    instance = b.reset_observation()
    request = fix(server, review)["request"]
    assert request["status"] == "woken" and not request.get("thread_id")
    b.shows(exists(WORKING + '[data-pin-request="subtotal"]'), "the standalone pin fix has no working marker")
    assert b.js(has_text(CHIP, "Hermes is working…"))
    b.click_sel(COUNT)
    b.shows(has_text(".tpanel .trow", "Working"), "the standalone pin fix was omitted from Comments")
    b.press(".tpanel .trow")
    b.shows(has_text(".popover .hermes-thread-status", "Hermes is working on this…"),
            "the standalone pin fix has no status detail")
    api.patch_block(review["id"], "m1", "<p>Updated subtotal</p>", round_n=1, if_rev=0,
                    request_ids=[request["id"]])
    b.shows(f"!{exists(WORKING)} && !{exists(COUNT)}", "patching did not clear the standalone pin fix")
    monkeypatch.setattr(service.notify, "send", lambda *args: (False, "HTTP 422: private wake detail"))
    second = fix(server, review)["request"]
    b.shows(exists(f'{FAILED}[data-pin-request="{second["id"]}"]'), "the pin fix did not show delivery failure")
    b.press(f'{FAILED}[data-pin-request="{second["id"]}"]')
    b.shows(has_text(STATUS, "Hermes couldn’t finish"), "the pin fix did not show failure in its detail")
    assert not b.js(has_text(STATUS, "retry"))
    b.same_document(instance)


def test_working_activity_uses_the_server_clock(working_review, server, chrome):
    review = working_review()
    rid = review["id"]
    tid = mention(server, rid)
    b = WorkingBoard(chrome, server, review, mobile=False, init_script="""
        const wallNow = Date.now.bind(Date);
        const monotonicNow = performance.now.bind(performance);
        window.advanceMs = 0;
        Date.now = () => wallNow() + 2 * 3600000 + advanceMs;
        performance.now = () => monotonicNow() + advanceMs;
        const realTimeout = window.setTimeout;
        window.setTimeout = function(callback, delay, ...args) {
          if (callback.name === 'pinActivityChanged') window.pinDeadline = {callback, delay};
          return realTimeout(callback, delay, ...args);
        };
    """)
    on(b, b.width)
    b.shows(exists(f'{WORKING}[data-thread="{tid}"]'), "a fast device clock hid the queued working pin")
    assert b.js(has_text(COUNT, "Hermes · 1 working"))
    assert b.js(has_text(CHIP, "Hermes is working…"))
    assert b.js("window.pinDeadline === undefined"), "a never-attempted mention scheduled an expiry"
    b.press(COUNT)
    b.shows(has_text(".tpanel .trow", "Working"), "the working pin was absent from Comments")
    b.press(".tpanel .trow")
    b.shows(has_text(STATUS, "Hermes is working on this…"), "the thread omitted its working status")
    assert b.js(f"{query(TIMER)}.textContent.startsWith('0:')")

    before = time.time()
    assert service.retry_thread_wakes(rid) == 1
    b.shows("window.pinDeadline?.delay > 800000", "delivery did not schedule expiry in server time")
    code, _, body = http("GET", f"{server}/r/{rid}/state.json")
    assert code == 200 and before <= json.loads(body)["now"] <= time.time()
    assert b.js(has_text(STATUS, "Hermes is working on this…"))
    b.js("window.getsBeforeExpiry = boardGets.length; advanceMs = pinDeadline.delay + 1000; pinDeadline.callback()")
    b.shows(exists(FAILED), "the deadline callback did not expire the pin")
    assert not b.js(exists(COUNT))
    assert not b.js(exists(CHIP))
    assert b.js(has_text(STATUS, "Hermes couldn’t finish in time."))
    assert b.js("boardGets.length === getsBeforeExpiry"), "local expiry made a network request"


@pytest.mark.parametrize("completion", ["reply", "patch", "resolve"])
def test_completion_live(working_review, server, chrome, mobile, completion, monkeypatch):
    review = working_review()
    rid = review["id"]
    tid = mention(server, rid)
    service.retry_thread_wakes(rid)
    req = next(iter(pin_requests(rid).values()))
    assert req["status"] == "woken"
    monkeypatch.setattr(live, "RECHECK_EVERY", 0.25)
    b = WorkingBoard(chrome, server, review, mobile)
    instance = b.reset_observation()
    on(b, b.width)
    b.press(f'.mpin.tpin[data-thread="{tid}"]')
    b.shows(exists(".tpop .tp-input"), "the pre-existing thread did not open")
    working = b.js(has_text(STATUS, "Hermes is working on this…"))
    b.js(f"window.replyBox = {query('.tpop .tp-input')}; replyBox.value = 'Draft reply'; replyBox.focus()")
    if completion == "reply":
        b.screenshot(mobile, "working")
        # Another process has its own registry. It can persist events but cannot reach this stream's memory queue.
        monkeypatch.setattr(live, "publish", live.Registry().publish)
        result = call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "The subtotal is correct."})
        assert result.get("closed") == 1, result
        b.shows(has_text(".tpop .tp-comments", "The subtotal is correct."), "the reply missed the open thread")
    elif completion == "resolve":
        result = call_tool(plugin_tools(), "mock_review_resolve", {"thread_id": tid})
        assert result.get("closed") == 1, result
        b.shows(has_text(".tpop .tp-note", "Reopen this thread"), "the live resolve missed the open thread")
    else:
        result = api.patch_block(rid, "m1", "<p id='copy'>The subtotal is correct.</p>", round_n=1, if_rev=0,
                                 request_ids=[req["id"]])
        assert result["done"] == [req["id"]]
    b.shows("pinEvents.some(e => e.round === 1)", f"{completion} did not deliver its pin completion event")
    assert working, "the thread had no working state before completion"
    b.shows(f"!{exists(STATUS)} && !{exists(COUNT)} && !{exists(WORKING)}", f"{completion} left activity running")
    if completion != "resolve":
        b.shows("document.activeElement === document.querySelector('.tpop .tp-input') && "
                "document.activeElement.value === 'Draft reply'", f"{completion} changed the focused reply draft")
    b.same_document(instance)
    if completion == "reply":
        b.screenshot(mobile, "done")


@pytest.mark.parametrize("outcome", ["failed", "timeout"])
def test_failure_live(working_review, server, chrome, mobile, monkeypatch, outcome):
    review = working_review()
    rid = review["id"]
    tid = mention(server, rid)
    b = WorkingBoard(chrome, server, review, mobile)
    instance = b.reset_observation()
    on(b, b.width)
    b.press(f'.mpin.tpin[data-thread="{tid}"]')
    b.shows(exists(".tpop .tp-input"), "the thread did not open")
    working = b.js(exists(WORKING))
    if outcome == "failed":
        monkeypatch.setattr(service.notify, "send", lambda *args: (False, "HTTP 422: private wake detail"))
        service.retry_thread_wakes(rid)
    else:
        # An unreachable receiver keeps the wake queued for retries. The watchdog must still finish the indicator.
        monkeypatch.setattr(service.notify, "send", lambda *args: (False, "receiver unavailable"))
        monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_MINUTES", "0")
        service.retry_thread_wakes(rid)
        service.check_stalled(now=time.time() + 16 * 60)
    assert {q["status"] for q in pin_requests(rid).values()} == {outcome}
    b.shows(exists(FAILED), f"{outcome} never reached the pin as an error")
    b.shows(has_text(STATUS, "Hermes couldn’t finish"), f"{outcome} never reached the open thread")
    assert working, "the open request had no working indicator before the error"
    assert not b.js(exists(WORKING))
    assert not b.js(exists(COUNT))
    assert not b.js(has_text(STATUS, "private wake detail"))
    assert not b.js(has_text(STATUS, "reply"))
    if outcome == "timeout":
        assert not service._due_mention_wakes(store.load(rid), now=time.time() + 17 * 60)
    b.same_document(instance)
    b.js("window.afterFailure = boardGets.length")
    b.cdp.pump(2.1)
    assert b.js("boardGets.length === afterFailure"), "the board is polling for pin activity"
    if outcome == "failed":
        b.screenshot(mobile, "failed")


def test_pinned_guest_sees_only_work_on_the_shared_round(working_review, server, chrome, mobile, tmp_path):
    review = working_review(pin_fix=True)
    rid = review["id"]
    guest = users.join_as_guest(users.resolve_share_token(token_of(users.share(
        "local", rid, guest_role="viewer", round="1")["path"])))
    b = WorkingBoard(chrome, server, review, mobile, member=guest)
    fix(server, review, signed_in(guest), expected=403)
    fix(server, review)
    b.shows(has_text(COUNT, "Hermes · 1 working"), "a viewer cannot read activity on the shared round")
    spec = store.parse_round_spec(decisions=str(tmp_path / "checkout.json"))
    store.add_round(rid, spec)
    mention(server, rid, "next", tid=None)
    service.retry_thread_wakes(rid)
    assert {q["status"] for q in pin_requests(rid).values()} == {"woken"}
    seen = b.js("pinEvents.length")
    live.publish(rid, live.PIN_REQUESTS, "", "", {"round": 1})
    b.shows(f"pinEvents.length > {seen}", "did not receive the next visible update")
    assert b.js("pinEvents.every(e => e.round === 1)")
    assert b.js("pinEvents.every(e => Object.keys(e).length === 1)")
    code, _, body = http("GET", f"{server}/r/{rid}/state.json", headers=signed_in(guest))
    assert code == 200 and [r["n"] for r in json.loads(body)["rounds"]] == [1]


def test_existing_artifact_frame_renders_a_live_working_pin(working_review, server, chrome, mobile, tmp_path):
    spec = tmp_path / "artifact.json"
    spec.write_text(json.dumps({"blocks": [{
        "type": "artifact", "id": "m1", "title": "Order summary", "html": "<p>Subtotal</p>",
    }]}), encoding="utf-8")
    review = store.create_review("Artifact review", "", {"platform": "discord", "chat_id": "test-chat"}, "local",
                                 store.parse_round_spec(decisions=str(spec)), wake=True)
    rid = review["id"]
    page = store.review_dir(rid) / "rounds" / "1" / "a" / "blocks" / "m1.html"
    # A Windows install's write_text produces CRLF; the serve-time upgrade must still match the probe by its marker.
    old_probe = page.read_bytes().replace(b"\n", b"\r\n").replace(b"if (p.hermes) {", b"if (false) {", 1)
    page.write_bytes(old_probe)
    b = WorkingBoard(chrome, server, review, mobile, artifact=True)
    sid = frame_session(b, rid)
    chrome.call("DOM.enable", session=sid)
    on(b, b.width)
    mention(server, rid)
    service.retry_thread_wakes(rid)
    assert {q["status"] for q in pin_requests(rid).values()} == {"woken"}

    def markers(node):
        attrs = dict(zip(node.get("attributes", [])[::2], node.get("attributes", [])[1::2]))
        if "h-working" in attrs.get("class", "").split():
            return True
        return any(markers(n) for n in node.get("children", []) + node.get("shadowRoots", []))

    found = chrome.wait(lambda: markers(chrome.call(
        "DOM.getDocument", {"depth": -1, "pierce": True}, session=sid)["root"]), time.monotonic() + 3)
    assert found, "the existing artifact's marker never displayed working activity"
    assert page.read_bytes() == old_probe
