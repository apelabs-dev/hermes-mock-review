"""Artifact page state and the in-page clipboard, in a real browser. The page is untrusted: it may write only its own
block's state, only on the round the board is showing, within the caps; a flood is ignored, a wrong frame or a stale
round is not obeyed, and a clipboard write needs a real tap no older than a second."""

import json
import re
import threading
import time

import pytest

import browserkit as bc
from conftest import http
from browserkit import BrowserCheckError, BrowserUnavailable, Cdp, Chrome, Observer, WebSocket
from mockreview import config, server as board_server, store

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}
ROUND = 1
FRAME_TOKEN = re.compile(r"(/rounds/[0-9]+)/~[^/]+(/[a-z]/)")


def untokenized(url):
    """The URL with the round-frame token segment removed: the path shape these tests are about is today's."""
    return FRAME_TOKEN.sub(r"\1\2", str(url or ""))


class Harness:
    def __init__(self, cdp, rec, sid, board, tmp):
        self.cdp, self.rec, self.sid, self.board, self.tmp = cdp, rec, sid, board, tmp

    def navigate(self, rid, n=None):
        self.rec.frames.clear()
        self.rec.contexts.clear()
        url = f"{self.board}/r/{rid}/" + (f"?v={time.time_ns()}#round-{n}" if n else "")
        self.cdp.call("Page.navigate", {"url": url}, session=self.sid)
        return url

    def top(self, expr):
        return self.cdp.evaluate(expr, session=self.sid)

    def await_top(self, expr):
        return self.eval(self.sid, None, expr, await_=True)

    def wait(self, predicate, timeout=15.0):
        try:
            return self.cdp.wait(predicate, time.monotonic() + timeout, 0.2)
        except BrowserUnavailable:
            raise

    def live(self, suffix, deadline):
        """The frame and execution context of the document now at `suffix`, ready for page script.

        A sandboxed artifact frame is an OOPIF: its context is reported across sessions, can be about:blank or already
        gone while the frame id still says the block, and appears before the page's own script has run. Verify the
        document URL and that parsing finished before handing the context to a test expression.

        The event map is not enough on its own: a context id can be refused as stale after a process swap, and the
        creation event that would replace it may already have been consumed. When a matching frame has no usable
        context, ask the browser to report its live contexts again (Cdp.reannounce_contexts) and re-resolve."""
        while True:
            self.cdp.flush()                       # enable sessions the browser attached (the OOPIF) before reading events
            lost = False
            for fid, (u, _) in list(self.rec.frames.items()):
                if not untokenized(u.split("?", 1)[0]).endswith(suffix):
                    continue
                ctx = self.rec.contexts.get(fid)
                if not ctx:
                    lost = True
                    continue
                try:
                    state = json.loads(self.eval(ctx[0] or self.sid, ctx[1],
                                                 "JSON.stringify({href: location.href, ready: document.readyState})"))
                except BrowserCheckError as e:
                    if "Cannot find context with specified id" not in str(e):
                        raise
                    self.rec.contexts.pop(fid, None)
                    lost = True
                    continue
                except AssertionError:
                    self.rec.contexts.pop(fid, None)
                    lost = True
                    continue
                if not (isinstance(state, dict) and isinstance(state.get("href"), str)
                        and untokenized(state["href"].split("?", 1)[0]).endswith(suffix)):
                    self.rec.contexts.pop(fid, None)
                    lost = True
                    continue
                if state.get("ready") == "loading":
                    break                          # still parsing: keep the mapping and look again shortly
                return fid, ctx
            if lost:
                self.cdp.reannounce_contexts([self.sid] + self.rec.sessions())
            if time.monotonic() >= deadline:
                return None
            self.cdp.pump(0.05)

    def eval(self, session, context, expr, await_=False):
        params = {"expression": expr, "returnByValue": True}
        if await_:
            params["awaitPromise"] = True
        if context is not None:
            params["contextId"] = context
        res = self.cdp.call("Runtime.evaluate", params, session=session)
        if res.get("exceptionDetails"):
            desc = ((res["exceptionDetails"].get("exception") or {}).get("description")
                    or res["exceptionDetails"].get("text") or "page script failed")
            raise AssertionError(desc)
        return (res.get("result") or {}).get("value")

    def in_frame(self, suffix, expr, await_=False):
        # A context read a moment ago can be destroyed before the evaluation (an OOPIF moves between sessions as
        # about:blank is replaced and the process swap settles). Re-resolve and retry that one refusal; another error,
        # or a frame that never comes back, still fails the test. The deadline is the frame's own discovery window,
        # the same one live() uses, so a slow board is not turned into a failure.
        deadline = time.monotonic() + 15.0
        last = None
        while True:
            got = self.live(suffix, deadline)
            if got is None:
                if last is not None:
                    raise last
                raise AssertionError(f"no frame for {suffix}")
            fid, ctx = got
            try:
                return self.eval(ctx[0] or self.sid, ctx[1], expr, await_)
            except BrowserCheckError as e:
                if "Cannot find context with specified id" not in str(e):
                    raise
                last = e
                self.rec.contexts.pop(fid, None)
                if time.monotonic() >= deadline:
                    raise

    def await_in_frame(self, suffix, expr):
        return self.in_frame(suffix, expr, await_=True)

    def click_in_frame(self, rid, block, selector, n=ROUND):
        """A real click at the centre of `selector`, after that document is live and the element exists.

        The frame is re-resolved for every step (a process swap can replace the context between the read and the
        dispatch) and a click refused because the context went away is retried, so a click is never silently lost."""
        suffix = f"/r/{rid}/rounds/{n}/a/blocks/{block}.html"
        deadline = time.monotonic() + 15.0
        while True:
            got = self.live(suffix, deadline)
            assert got, f"no frame for {suffix}"
            fid, (session, context) = got
            fsid = session or self.sid
            try:
                box = self.eval(fsid, context,
                                "JSON.stringify((function(){var e=document.querySelector(%s);"
                                "return e ? e.getBoundingClientRect() : null;})())" % json.dumps(selector))
            except BrowserCheckError as e:
                if "Cannot find context with specified id" not in str(e):
                    raise
                self.rec.contexts.pop(fid, None)
                if time.monotonic() >= deadline:
                    raise
                continue
            br = json.loads(box) if box and box != "null" else None
            if br is None:
                if time.monotonic() >= deadline:
                    raise AssertionError(f"{selector} never appeared in the frame for {suffix}")
                self.cdp.pump(0.05)
                continue
            self.top("(function(){var f=document.querySelector('[data-id=" + json.dumps(block) +
                     "] iframe.art-frame'); if(f) f.scrollIntoView({block:'center'}); return true})()")
            cx, cy = br["x"] + br["width"] / 2, br["y"] + br["height"] / 2
            try:
                self.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy, "button": "none"},
                              session=fsid)
                for typ in ("mousePressed", "mouseReleased"):
                    self.cdp.call("Input.dispatchMouseEvent",
                                  {"type": typ, "x": cx, "y": cy, "button": "left", "clickCount": 1}, session=fsid)
            except BrowserCheckError as e:
                if "Cannot find context with specified id" not in str(e) and "Session with given id not found" not in str(e):
                    raise
                self.rec.contexts.pop(fid, None)
                if time.monotonic() >= deadline:
                    raise
                continue
            return


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    try:
        bc.find_browser()
    except BrowserUnavailable as e:
        pytest.skip(e.message)
    tmp = tmp_path_factory.mktemp("page-data")
    mp = pytest.MonkeyPatch()
    for k, v in (("HOME", tmp / "home"), ("HERMES_HOME", tmp / "hermes"), ("MOCKREVIEW_HOME", tmp / "data"),
                 ("MOCKREVIEW_CONFIG", tmp / "no-config.yaml")):
        mp.setenv(k, str(v))
    mp.setenv("MOCKREVIEW_OWNER", "self")
    mp.setenv("MOCKREVIEW_PUBLIC_BASE", "https://board.example.test:8790")
    httpd = board_server.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    board = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with Chrome() as (host, port, path):
            ws = WebSocket(host, port, path)
            try:
                cdp, rec = Cdp(ws, Observer()), None
                rec = cdp.obs
                tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
                sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
                for m, p in (("Page.enable", {}), ("Network.enable", {}), ("Runtime.enable", {}),
                             ("Network.setCacheDisabled", {"cacheDisabled": True}),
                             ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}),
                             ("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 1000, "deviceScaleFactor": 1,
                                                                     "mobile": False})):
                    cdp.call(m, p, session=sid)
                yield Harness(cdp, rec, sid, board, tmp)
            finally:
                ws.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        mp.undo()


def review(tmp, name, blocks, extra_round=None):
    f = tmp / name / "decisions.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"blocks": blocks}), encoding="utf-8")
    data = store.create_review(name, "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(f)))
    if extra_round:
        f2 = tmp / (name + "-2") / "decisions.json"
        f2.parent.mkdir(parents=True)
        f2.write_text(json.dumps({"blocks": extra_round}), encoding="utf-8")
        store.add_round(data["id"], store.parse_round_spec(decisions=str(f2)))
    return data


def feedback_md(rid, n=ROUND):
    return (config.data_root() / rid / "rounds" / str(n) / "feedback.md").read_text(encoding="utf-8")


def load_round(rid, n=ROUND):
    return store.load(rid)["rounds"][n - 1]


def feedback_ready(rid, n=ROUND):
    """The record and both feedback files exist: what a Send is actually acknowledged by, so a wait on review.json
    alone can never race the markdown the caller is about to read."""
    d = config.data_root() / rid / "rounds" / str(n)
    try:
        stored = load_round(rid, n).get("feedback") is not None
    except (OSError, ValueError):
        return False
    return stored and (d / "feedback.md").is_file() and (d / "feedback.json").is_file()


def test_round_trip_survives_reload_and_reaches_feedback_json_and_markdown(h):
    data = review(h.tmp, "roundtrip", [
        {"type": "artifact", "id": "pick", "title": "Pick", "height": 300,
         "html": "<script>window.D = window.mockreview.use('data'); window.READY = true;</script><p>pick</p>"}])
    rid = data["id"]
    h.navigate(rid)
    h.await_in_frame(f"/r/{rid}/rounds/1/a/blocks/pick.html", "window.READY === true")
    assert h.await_in_frame(f"/r/{rid}/rounds/1/a/blocks/pick.html",
                            "window.D.set('pick','b').then(function(){return 'ok'},function(e){return e.message})") == "ok"
    time.sleep(0.5)                                    # the draft save is debounced
    h.navigate(rid)
    suffix = f"/r/{rid}/rounds/1/a/blocks/pick.html"
    h.await_in_frame(suffix, "window.D && window.READY === true")
    assert h.await_in_frame(suffix, "window.D.get('pick')") == "b"
    h.top("document.querySelector('.btn-send').click()")
    assert h.wait(lambda: feedback_ready(rid)), "no feedback stored"
    fb = load_round(rid)["feedback"]
    assert fb["page_state"] == {"pick": {"pick": "b"}}
    md = feedback_md(rid)
    assert "## Page state (1 block)" in md and '  > pick: "b"' in md


def test_page_state_alone_enables_send_without_a_reload_and_remove_disables_it(h):
    data = review(h.tmp, "page-send", [
        {"type": "artifact", "id": "pick", "title": "Pick", "height": 300,
         "html": "<script>window.D = window.mockreview.use('data'); window.READY = true;</script><p>pick</p>"}])
    rid = data["id"]
    h.navigate(rid)
    suffix = f"/r/{rid}/rounds/1/a/blocks/pick.html"
    h.await_in_frame(suffix, "window.READY === true")
    assert h.await_in_frame(suffix, "window.D.set('pick','b').then(function(){return 'ok'},function(e){return e.message})") == "ok"
    bar = json.loads(h.top("JSON.stringify({disabled: document.querySelector('.btn-send').disabled,"
                           " text: document.querySelector('.sendbar').textContent})"))
    assert bar["disabled"] is False, bar
    assert "Nothing marked yet" not in bar["text"], bar
    h.top("document.querySelector('.btn-send').click()")
    assert h.wait(lambda: feedback_ready(rid)), "no feedback stored"
    assert load_round(rid)["feedback"]["page_state"] == {"pick": {"pick": "b"}}

    fresh = review(h.tmp, "page-send-remove", [
        {"type": "artifact", "id": "solo", "title": "Solo", "height": 300,
         "html": "<script>window.D = window.mockreview.use('data'); window.READY = true;</script><p>solo</p>"}])
    rid = fresh["id"]
    h.navigate(rid)
    suffix = f"/r/{rid}/rounds/1/a/blocks/solo.html"
    h.await_in_frame(suffix, "window.READY === true")
    assert h.await_in_frame(suffix, "window.D.set('pick','b').then(function(){return 'ok'},function(e){return e.message})") == "ok"
    assert h.top("document.querySelector('.btn-send').disabled") is False
    assert h.await_in_frame(suffix, "window.D.remove('pick').then(function(){return 'ok'},function(e){return e.message})") == "ok"
    assert h.top("document.querySelector('.btn-send').disabled") is True


def test_limits_and_a_flood_are_refused_and_leave_the_board_healthy(h):
    data = review(h.tmp, "limits", [
        {"type": "artifact", "id": "limits", "title": "Limits", "height": 300,
         "html": "<script>window.D = window.mockreview.use('data'); window.READY = true;</script>"}])
    rid = data["id"]
    h.navigate(rid)
    suffix = f"/r/{rid}/rounds/1/a/blocks/limits.html"
    h.await_in_frame(suffix, "window.READY === true")
    big = h.await_in_frame(suffix, "window.D.set('big','x'.repeat(9000)).then(function(){return 'ok'},function(e){return e.message})")
    assert big == "value over 8 KB", big
    keys = h.await_in_frame(suffix, """(async function(){
      function pause() { return new Promise(function (r) { setTimeout(r, 35); }); }
      for (var i=0;i<64;i++) { await window.D.set('k'+i, i); await pause(); }
      try { await window.D.set('k64', 64); return 'ok'; } catch (e) { return e.message; } })()""")
    assert keys == "block has 64 keys", keys
    h.await_in_frame(suffix, """(async function(){
      function pause() { return new Promise(function (r) { setTimeout(r, 35); }); }
      for (var i=0;i<64;i++) { await window.D.remove('k'+i); await pause(); }
      return true; })()""")
    h.in_frame(suffix, """(function(){for (var i=0;i<300;i++) parent.postMessage({__mockreview:1, type:'data', op:'set',
      key:'f'+i, value:i, id:10000+i}, '*'); return true})()""")
    time.sleep(1.2)
    listed = h.await_in_frame(suffix, "window.D.list()")
    assert len(listed) <= 80, listed                   # the per-frame bucket (40/s), not all 300
    assert http("GET", h.board + "/healthz")[0] == 200
    assert h.top("document.querySelectorAll('.blk').length") >= 1
    assert load_round(rid).get("submission") is None


def test_a_page_that_floods_data_settles_every_promise_and_the_board_stays_healthy(h):
    data = review(h.tmp, "settles", [
        {"type": "artifact", "id": "settles", "title": "Settles", "height": 300, "markable": True,
         "html": "<script>window.D = window.mockreview.use('data'); window.M = window.mockreview.use('marks');"
                 "window.RES = []; window.MRES = []; window.READY = true;</script>"}])
    rid = data["id"]
    h.navigate(rid)
    suffix = f"/r/{rid}/rounds/1/a/blocks/settles.html"
    assert h.wait(lambda: h.in_frame(suffix, "window.READY === true"), 10)
    h.in_frame(suffix, """(function(){
      for (var i=0;i<60;i++) (function (i) {
        window.D.set('f'+i, i).then(function(){ window.RES.push('ok'); }, function(e){ window.RES.push(e.message); });
      })(i);
      return true; })()""")
    assert h.wait(lambda: h.in_frame(suffix, "window.RES.length") == 60, 6), \
        "over-budget set() promises never settled"
    outcomes = json.loads(h.in_frame(suffix, "JSON.stringify(window.RES)"))
    assert outcomes.count("ok") >= 1, outcomes
    assert set(outcomes) == {"ok", "rate limited"}, sorted(set(outcomes))
    time.sleep(1.2)
    h.in_frame(suffix, """(function(){
      for (var i=0;i<60;i++) (function () {
        window.M.set({status:'approve'}).then(function(){ window.MRES.push('ok'); }, function(e){ window.MRES.push(e.message); });
      })();
      return true; })()""")
    assert h.wait(lambda: h.in_frame(suffix, "window.MRES.length") == 60, 6), \
        "over-budget marks.set() promises never settled"
    marks = json.loads(h.in_frame(suffix, "JSON.stringify(window.MRES)"))
    assert marks.count("ok") >= 1, marks
    assert set(marks) == {"ok", "rate limited"}, sorted(set(marks))
    time.sleep(1.2)
    assert h.await_in_frame(suffix, "window.D.set('after','ok').then(function(){return 'ok'},function(e){return e.message})") == "ok"
    assert http("GET", h.board + "/healthz")[0] == 200
    assert load_round(rid).get("submission") is None


def test_a_second_frame_and_a_forged_sender_cannot_write_or_read_another_blocks_state(h):
    data = review(h.tmp, "frames", [
        {"type": "artifact", "id": "alpha", "title": "Alpha", "height": 200,
         "html": "<script>window.A = window.mockreview.use('data');</script><p>a</p>"},
        {"type": "artifact", "id": "beta", "title": "Beta", "height": 200,
         "html": "<script>window.B = window.mockreview.use('data');</script><p>b</p>"}])
    rid = data["id"]
    h.navigate(rid)
    a = f"/r/{rid}/rounds/1/a/blocks/alpha.html"
    b = f"/r/{rid}/rounds/1/a/blocks/beta.html"
    h.await_in_frame(a, "window.A && true")
    h.await_in_frame(b, "window.B && true")
    assert h.await_in_frame(a, "window.A.set('private','secret').then(function(){return 'ok'},function(e){return e.message})") == "ok"
    assert h.await_in_frame(b, "window.B.get('private')") is None
    assert h.await_in_frame(b, "window.B.set('private','stolen').then(function(){return 'ok'},function(e){return e.message})") == "ok"
    h.in_frame(b, "parent.postMessage({__mockreview:1, type:'data', op:'set', key:'private', value:'beta-forged', id:7}, '*')")
    h.top("window.postMessage({__mockreview:1, type:'data', op:'set', key:'private', value:'top-forged', id:8}, '*')")
    time.sleep(0.4)
    assert h.await_in_frame(a, "window.A.get('private')") == "secret"
    beta = h.await_in_frame(b, "window.B.get('private')")
    assert beta in ("stolen", "beta-forged") and beta != "secret", beta
    assert load_round(rid).get("submission") is None


def test_a_data_message_tagged_with_the_previous_round_is_ignored(h):
    data = review(h.tmp, "rounds", [
        {"type": "artifact", "id": "one", "title": "One", "height": 200,
         "html": "<script>window.D1 = window.mockreview.use('data');</script>"}])
    rid = data["id"]
    h.navigate(rid, 1)
    one = f"/r/{rid}/rounds/1/a/blocks/one.html"
    h.await_in_frame(one, "window.D1 && true")
    assert h.await_in_frame(one, "window.D1.set('old','v1').then(function(){return 'ok'},function(e){return e.message})") == "ok"
    time.sleep(0.5)                                    # round 1 is current while this draft is saved
    f2 = h.tmp / "rounds-2" / "decisions.json"
    f2.parent.mkdir(parents=True)
    f2.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "two", "title": "Two", "height": 200,
                                          "html": "<script>window.D2 = window.mockreview.use('data');</script>"}]}),
                  encoding="utf-8")
    store.add_round(rid, store.parse_round_spec(decisions=str(f2)))
    h.navigate(rid, 2)
    two = f"/r/{rid}/rounds/2/a/blocks/two.html"
    h.await_in_frame(two, "window.D2 && true")
    assert h.await_in_frame(two, "window.D2.set('fresh','v2').then(function(){return 'ok'},function(e){return e.message})") == "ok"
    h.in_frame(two, "parent.postMessage({__mockreview:1, type:'data', op:'set', key:'stale', value:'x', id:9, round:1}, '*')")
    time.sleep(0.5)
    assert h.await_in_frame(two, "window.D2.list()") == ["fresh"]
    h.navigate(rid, 1)
    h.await_in_frame(one, "window.D1 && true")
    assert h.await_in_frame(one, "window.D1.get('old')") == "v1"
    assert h.await_in_frame(one, "window.D1.get('stale')") is None


def test_clipboard_an_in_page_copy_button_writes_the_exact_text_and_needs_a_recent_tap(h):
    data = review(h.tmp, "clipboard", [
        {"type": "artifact", "id": "copy", "title": "Copy", "height": 300,
         "html": """<button id="copy" data-mr-live style="position:fixed;left:20px;top:20px;width:120px;height:40px">Copy</button>
<script>
window.C = window.mockreview.use('clipboard');
document.addEventListener('pointerdown', function () { window.TAPPED = 'yes'; }, true);
document.getElementById('copy').addEventListener('click', function () {
  window.CLICKED = 'yes';
  window.C.writeText('copied-exact-text').then(function () { window.COPY = 'ok'; }, function (e) { window.COPY = e.message; });
});
</script>"""}])
    h.cdp.call("Browser.grantPermissions", {"permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
                                            "origin": h.board})
    rid = data["id"]
    h.navigate(rid)
    suffix = f"/r/{rid}/rounds/1/a/blocks/copy.html"
    h.await_in_frame(suffix, "window.C && true")
    h.click_in_frame(rid, "copy", "#copy")
    state = h.in_frame(suffix, "JSON.stringify({copy: window.COPY, tapped: window.TAPPED, clicked: window.CLICKED})")
    assert h.wait(lambda: h.in_frame(suffix, "window.COPY") == "ok", 5), state
    assert h.await_top("navigator.clipboard.readText()") == "copied-exact-text"
    time.sleep(1.2)
    age = h.await_in_frame(suffix, "window.C.writeText('nope').then(function(){return 'ok'},function(e){return e.message})")
    assert age == "no recent tap", age
    assert h.await_top("navigator.clipboard.readText()") == "copied-exact-text"


def test_clipboard_focus_is_the_real_boundary_and_a_direct_write_skips_the_tap_rule(h):
    data = review(h.tmp, "clip-focus", [
        {"type": "artifact", "id": "alpha", "title": "Alpha", "height": 300,
         "html": """<button id="focus" data-mr-live style="position:fixed;left:20px;top:20px;width:120px;height:40px">Focus</button>
<script>
window.C = window.mockreview.use('clipboard');
window.READY = true;
</script>"""},
        {"type": "artifact", "id": "beta", "title": "Beta", "height": 300, "html": "<p>beta never got a tap</p>"}])
    h.cdp.call("Browser.grantPermissions", {"permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
                                            "origin": h.board})
    rid = data["id"]
    h.navigate(rid)
    a = f"/r/{rid}/rounds/1/a/blocks/alpha.html"
    b = f"/r/{rid}/rounds/1/a/blocks/beta.html"
    assert h.wait(lambda: h.in_frame(a, "window.READY === true"), 10)
    h.await_in_frame(b, "document.body && true")
    h.click_in_frame(rid, "alpha", "#focus")
    assert h.wait(lambda: h.in_frame(a, "document.hasFocus()") is True, 5)
    assert h.in_frame(b, "document.hasFocus()") is False
    h.await_top("navigator.clipboard.writeText('seed')")
    time.sleep(1.2)
    # the probe's wrapper still enforces its 1-second trusted-tap rule
    assert h.await_in_frame(a, "window.C.writeText('wrapped').then(function(){return 'ok'},function(e){return e.message})") == "no recent tap"
    assert h.await_top("navigator.clipboard.readText()") == "seed"
    # a direct Clipboard API call from the same focused frame skips that rule: focus, not the tap, is the boundary
    assert h.await_in_frame(a, "navigator.clipboard.writeText('BYPASS-no-tap').then(function(){return 'ok'},function(e){return e.name+': '+e.message})") == "ok"
    assert h.await_top("navigator.clipboard.readText()") == "BYPASS-no-tap"
    read = h.await_in_frame(a, "navigator.clipboard.readText().then(function(){return 'ok'},function(e){return e.name})")
    assert read == "NotAllowedError", read                # the frame has write only, never read
    # a second artifact frame that never received focus cannot write at all
    denied = h.await_in_frame(b, "navigator.clipboard.writeText('from-beta').then(function(){return 'ok'},function(e){return e.name+': '+e.message})")
    assert denied.startswith("NotAllowedError: ") and "focus" in denied.lower(), denied
    assert h.await_top("navigator.clipboard.readText()") == "BYPASS-no-tap"


def test_the_three_screen_pattern_keeps_three_picks_and_three_notes_through_a_reload_and_send(h):
    page = """<style>[data-screen]{display:none}[data-screen].on{display:block}</style>
<div data-screen="s1" class="on"><label><input type="radio" name="s1" value="approve" data-mr-live> Approve</label>
<label><input type="radio" name="s1" value="change" data-mr-live> Change</label>
<label><input type="radio" name="s1" value="skip" data-mr-live> Skip</label><textarea data-note="s1" data-mr-live></textarea></div>
<div data-screen="s2"><label><input type="radio" name="s2" value="approve" data-mr-live> Approve</label>
<label><input type="radio" name="s2" value="change" data-mr-live> Change</label>
<label><input type="radio" name="s2" value="skip" data-mr-live> Skip</label><textarea data-note="s2" data-mr-live></textarea></div>
<div data-screen="s3"><label><input type="radio" name="s3" value="approve" data-mr-live> Approve</label>
<label><input type="radio" name="s3" value="change" data-mr-live> Change</label>
<label><input type="radio" name="s3" value="skip" data-mr-live> Skip</label><textarea data-note="s3" data-mr-live></textarea></div>
<script>
window.D = window.mockreview.use('data');
['s1','s2','s3'].forEach(function (id) {
  document.querySelectorAll('input[name="' + id + '"]').forEach(function (el) {
    el.addEventListener('change', function () { window.D.set(id + '_status', el.value); });
  });
  var ta = document.querySelector('[data-note="' + id + '"]');
  ta.addEventListener('input', function () { window.D.set(id + '_note', ta.value); });
  window.D.get(id + '_status').then(function (v) {
    if (v) document.querySelector('input[name="' + id + '"][value="' + v + '"]').checked = true;
  });
  window.D.get(id + '_note').then(function (v) { if (v) ta.value = v; });
});
window.READY = true;
</script>"""
    data = review(h.tmp, "screens", [
        {"type": "artifact", "id": "screens", "title": "Admin screens", "height": 500, "html": page}])
    rid = data["id"]
    h.navigate(rid)
    suffix = f"/r/{rid}/rounds/1/a/blocks/screens.html"
    h.await_in_frame(suffix, "window.READY === true")
    filled = h.await_in_frame(suffix, """(async function(){
      var picks = {s1:'approve', s2:'change', s3:'skip'};
      for (var id in picks) {
        var el = document.querySelector('input[name="' + id + '"][value="' + picks[id] + '"]');
        el.click();
        var ta = document.querySelector('[data-note="' + id + '"]');
        ta.value = 'note for ' + id;
        ta.dispatchEvent(new Event('input', {bubbles:true}));
        await window.D.set(id + '_status', picks[id]);
        await window.D.set(id + '_note', ta.value);
      }
      return 'filled';
    })()""")
    assert filled == "filled"
    time.sleep(0.5)
    h.navigate(rid)
    h.await_in_frame(suffix, "window.READY === true")
    h.wait(lambda: h.in_frame(suffix, "document.querySelector('[data-note=s1]').value") == "note for s1", 5)
    restored = h.await_in_frame(suffix, """JSON.stringify({
      s1: document.querySelector('input[name=s1]:checked') && document.querySelector('input[name=s1]:checked').value,
      s2: document.querySelector('input[name=s2]:checked') && document.querySelector('input[name=s2]:checked').value,
      s3: document.querySelector('input[name=s3]:checked') && document.querySelector('input[name=s3]:checked').value,
      n1: document.querySelector('[data-note=s1]').value, n2: document.querySelector('[data-note=s2]').value,
      n3: document.querySelector('[data-note=s3]').value})""")
    assert json.loads(restored) == {"s1": "approve", "s2": "change", "s3": "skip",
                                    "n1": "note for s1", "n2": "note for s2", "n3": "note for s3"}
    h.top("document.querySelector('.btn-send').click()")
    assert h.wait(lambda: feedback_ready(rid)), "no feedback stored"
    ps = load_round(rid)["feedback"]["page_state"]["screens"]
    assert ps == {"s1_note": "note for s1", "s1_status": "approve", "s2_note": "note for s2",
                  "s2_status": "change", "s3_note": "note for s3", "s3_status": "skip"}
    md = feedback_md(rid)
    assert "## Page state (1 block)" in md
    for bit in ('s1_status: "approve"', 's2_status: "change"', 's3_status: "skip"',
                's1_note: "note for s1"', 's2_note: "note for s2"', 's3_note: "note for s3"'):
        assert bit in md, bit


def test_marks_let_a_markable_page_set_its_own_mark_and_refuse_other_blocks(h):
    data = review(h.tmp, "marks", [
        {"type": "artifact", "id": "vote", "title": "Vote", "height": 200, "markable": True,
         "html": "<script>window.M = window.mockreview.use('marks');</script><p>vote</p>"},
        {"type": "artifact", "id": "plain", "title": "Plain", "height": 100,
         "html": "<script>window.PM = window.mockreview.use('marks');</script><p>plain</p>"}])
    rid = data["id"]
    h.navigate(rid)
    vote = f"/r/{rid}/rounds/1/a/blocks/vote.html"
    plain = f"/r/{rid}/rounds/1/a/blocks/plain.html"
    h.await_in_frame(vote, "window.M && true")
    h.await_in_frame(plain, "window.PM && true")
    assert h.await_in_frame(plain, "window.PM.set({status:'approve'}).then(function(){return 'ok'},function(e){return e.message})") == "this block is not markable"
    assert h.await_in_frame(vote, "window.M.set({status:'maybe'}).then(function(){return 'ok'},function(e){return e.message})") == "status must be approve, change or skip"
    assert h.await_in_frame(vote, "window.M.set({status:'approve', note:'page says yes'}).then(function(){return 'ok'},function(e){return e.message})") == "ok"
    # a page that sends only a status must leave the note already on the mark alone (the feedback below proves it)
    assert h.await_in_frame(vote, "window.M.set({status:'approve'}).then(function(){return 'ok'},function(e){return e.message})") == "ok"
    assert h.wait(lambda: h.top("(document.querySelector('#dz-vote .status') || {}).getAttribute && "
                                "document.querySelector('#dz-vote .status').getAttribute('data-s')") == "approve", 5)
    h.top("document.querySelector('.btn-send').click()")
    assert h.wait(lambda: feedback_ready(rid)), "no feedback stored"
    fb = load_round(rid)["feedback"]
    assert fb["marks"]["vote"] == {"status": "approve", "option": None, "note": "page says yes"}
    assert "[vote] Approve" in feedback_md(rid)


def test_use_returns_null_outside_an_artifact_block_frame(h):
    page = h.tmp / "proto" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text("<p>prototype</p><script>window.PROTO = true;</script>", encoding="utf-8")
    data = store.create_review("Proto", "", ORIGIN, "tailscale", store.parse_round_spec(prototype=str(page)))
    rid = data["id"]
    h.navigate(rid)
    suffix = f"/r/{rid}/rounds/1/a/index.html"
    assert h.wait(lambda: h.in_frame(suffix, "window.PROTO === true"), 10)
    got = h.await_in_frame(suffix, "JSON.stringify({"
                            "data: window.mockreview.use('data'), marks: window.mockreview.use('marks'),"
                            "clipboard: window.mockreview.use('clipboard'), other: window.mockreview.use('nope'),"
                            "comments: typeof window.mockreview.comments.anchorFor, path: location.pathname})")
    state = json.loads(got)
    assert untokenized(state["path"]) == f"/r/{rid}/rounds/1/a/index.html"
    assert state["data"] is None and state["marks"] is None and state["clipboard"] is None, state
    assert state["other"] is None, state
    assert state["comments"] == "function", state


def test_a_frame_context_lost_after_a_refusal_is_resolved_again(h):
    """A process swap can refuse the recorded context, and dropping it leaves no entry: its creation event was already
    consumed, so a resolver that only reads events waits out the whole window (the CI failure). The harness asks the
    browser to report its live contexts again, so the next lookup sees the frame instead of a 15 s failure."""
    data = review(h.tmp, "lost-context", [
        {"type": "artifact", "id": "copy", "title": "Copy", "height": 300,
         "html": "<button id='copy' data-mr-live>Copy</button><script>window.C = 1;</script>"}])
    rid = data["id"]
    h.navigate(rid)
    suffix = f"/r/{rid}/rounds/1/a/blocks/copy.html"
    assert h.await_in_frame(suffix, "window.C") == 1
    h.rec.contexts.clear()                    # exactly the map state in_frame leaves after popping a refused context
    assert h.in_frame(suffix, "window.C") == 1
