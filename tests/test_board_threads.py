"""Real-Chrome tests for threads: the composer, the popover, the Comments/Activity overlays and typing."""

import json
import time

import pytest

from authkit import account, signed_in
from conftest import http
from mockreview import live, service, store, threads, users
from test_live import Stream
from test_live_round_tool import Board, chrome  # noqa: F401

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
MOCK = ('<section><h2>Checkout</h2><p id="copy" style="width:300px;height:50px;margin:20px">'
        'Review this total</p><p id="other" style="width:300px;height:40px;margin:20px">Other line</p></section>')
DECISIONS = {"blocks": [{"type": "mock", "id": "m1", "html": MOCK}]}
ROOT = "document.querySelector('.mock-host').shadowRoot"

# A page hook that starts the EventSource 500 ms late, so the snapshot refresh always lands first and the replayed
# history always lands after it. That makes the duplicate-count race deterministic instead of load-dependent.
DELAYED_EVENTS = """(function () {
  var Real = window.EventSource;
  if (!Real) return;
  function Delayed(url, opts) {
    var self = this;
    self._q = []; self._real = null; self._closed = false; self.readyState = 0;
    self.addEventListener = function (kind, fn, arg) { self._q.push([kind, fn, arg]); };
    self.removeEventListener = function () {};
    self.close = function () { self._closed = true; if (self._real) self._real.close(); };
    setTimeout(function () {
      if (self._closed) return;
      var es = new Real(url, opts);
      self._real = es;
      es.onopen = function (e) { self.readyState = 1; window.__esOpened = true; if (self.onopen) self.onopen.call(es, e); };
      es.onerror = function (e) { if (self.onerror) self.onerror.call(es, e); };
      self._q.forEach(function (h) { es.addEventListener(h[0], h[1], h[2]); });
    }, 500);
  }
  window.EventSource = Delayed;
})();"""

# Counts the `thread.replied` frames the page's own stream receives, so a test can hold one back and prove the UI has
# handled it: the counter's listener is added before the board's, inside the same synchronous dispatch.
COUNT_REPLY_FRAMES = """(function () {
  var Real = window.EventSource;
  if (!Real) return;
  function Counted(url, opts) {
    var es = new Real(url, opts);
    es.addEventListener('thread.replied', function () { window.__replyFrames = (window.__replyFrames || 0) + 1; });
    window.__eventStream = es;
    return es;
  }
  Counted.prototype = Real.prototype;
  window.EventSource = Counted;
})();"""


def reload_with_a_counted_event_stream(b, url):
    b.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": COUNT_REPLY_FRAMES}, session=b.sid)
    b.cdp.call("Page.navigate", {"url": url}, session=b.sid)
    assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && "
                   "document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))", timeout=60)
    assert b.until("!!window.__eventStream && window.__eventStream.readyState === 1", timeout=15), \
        "the counted event stream never opened"


def reload_with_a_delayed_event_stream(b, url):
    b.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": DELAYED_EVENTS}, session=b.sid)
    b.cdp.call("Page.navigate", {"url": url}, session=b.sid)
    assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && "
                   "document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))", timeout=60)
    assert b.until("window.__esOpened === true", timeout=15), "the delayed event stream never opened"
    time.sleep(0.8)   # the whole replay has landed by now; the badge must not have moved


@pytest.fixture
def thread_review(env, tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Threads", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)),
                               wake=False)
    threads.reset_limits()
    return data["id"]


@pytest.fixture
def hiding_review(env, tmp_path):
    """A review with hide_resolved_pins on: a resolved thread's marker leaves the board."""
    from mockreview import settings
    path = tmp_path / "threads.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Threads", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)),
                               look=settings.review_settings({"hide_resolved_pins": True}), wake=False)
    threads.reset_limits()
    return data["id"]


def open_board(chrome, server, rid):
    return Board(chrome, f"{server}/r/{rid}/", wait="!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))")


def on(b):
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")


def point(b, sel):
    return json.loads(b.js(
        "JSON.stringify((()=>{let e=%s.querySelector(%s);Element.prototype.scrollIntoView.call(e,{block:'center'});"
        "let r=Element.prototype.getBoundingClientRect.call(e);return [r.left+r.width/2,r.top+r.height/2]})())"
        % (ROOT, json.dumps(sel))))


def save_pin(b, text="Make this clearer"):
    assert b.until("!!document.querySelector('.popover textarea')")
    b.js("(function(t){t.value=%s;t.dispatchEvent(new Event('input'));return 1})(document.querySelector('.popover textarea'))"
         % json.dumps(text))
    b.press(".popover .btn-primary")
    assert b.until("document.querySelectorAll('.mpin').length === 1 && !!document.querySelector('.mpin[data-thread]')")


def server_threads(server, rid, member=None):
    code, _, body = http("GET", f"{server}/r/{rid}/api/threads",
                         headers=signed_in(member) if member else {})
    assert code == 200
    return json.loads(body)


def test_composer_pin_popover_resolve_reopen_and_reload(hiding_review, server, chrome):
    rid = hiding_review
    b = open_board(chrome, server, rid)
    on(b)
    b.click(*point(b, "#copy"))
    save_pin(b, "Make this clearer")
    listed = server_threads(server, rid)["threads"]
    assert len(listed) == 1 and listed[0]["comments"][0]["text"] == "Make this clearer"

    b.press(".mpin")
    assert b.until("!!document.querySelector('.tpop')")
    assert b.js("document.querySelector('.tpop').textContent.includes('Make this clearer')")
    b.js("(function(t){t.value='A reply from the popover';t.dispatchEvent(new Event('input'));return 1})"
         "(document.querySelector('.tpop textarea'))")
    b.press(".tpop .tp-send")
    assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2")

    b.press(".tpop .tp-resolve")
    assert b.until("!document.querySelector('.tpop') && document.querySelectorAll('.mpin').length === 0")

    b.press(".tcomments")
    assert b.until("!!document.querySelector('.toverlay')")
    assert not b.js("document.querySelector('.toverlay').textContent.includes('Make this clearer')")
    b.press(".ttoggle .tr")
    assert b.until("document.querySelector('.toverlay').textContent.includes('Make this clearer')")
    b.press(".toverlay .trow")
    assert b.until("!!document.querySelector('.tpop')")
    b.press(".tpop .tp-resolve")
    assert b.until("document.querySelectorAll('.mpin').length === 1")
    b.press(".tp-close")
    assert b.until("!document.querySelector('.tpop')")

    b.cdp.call("Page.navigate", {"url": b.js("location.href")}, session=b.sid)
    assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))", timeout=60)
    assert b.until("document.querySelectorAll('.mpin').length === 1", timeout=30)
    assert b.until("document.querySelector('.tcomments .badge').textContent === '1'", timeout=30)
    b.press(".mpin")
    assert b.until("!!document.querySelector('.tpop')")
    assert b.js("document.querySelector('.tpop').textContent.includes('A reply from the popover')")
    assert len(server_threads(server, rid)["threads"][0]["comments"]) == 2


def test_a_pin_keeps_its_marker_after_hermes_answers_and_resolves(thread_review, server, chrome):
    """hide_resolved_pins defaults to off: a thread Hermes answers and resolves keeps its dot, live and on reload."""
    rid = thread_review
    b = open_board(chrome, server, rid)
    on(b)
    b.click(*point(b, "#copy"))
    save_pin(b, "@Hermes make this clearer")
    tid = server_threads(server, rid)["threads"][0]["id"]

    owner = users.owner_user()
    threads.reply(rid, tid, f"user:{owner['id']}", "Hermes", "owner", "Done, the copy is shorter now.")
    threads.set_status(rid, tid, "resolved", f"user:{owner['id']}")

    b.press(".mpin[data-thread]")
    assert b.until("!!document.querySelector('.tpop .tp-resolve') && "
                   "document.querySelector('.tpop .tp-resolve').getAttribute('aria-label') === 'Reopen' && "
                   "document.querySelector('.tpop').textContent.includes('the copy is shorter')", timeout=15), \
        "the reply and the resolve never reached the board"
    assert b.js("document.querySelectorAll('.mpin[data-thread]').length") == 1, "the resolved pin lost its marker"
    b.press(".tp-close")
    assert b.until("!document.querySelector('.tpop')")
    assert b.js("document.querySelectorAll('.mpin[data-thread]').length") == 1, "the resolved pin lost its marker"

    b.cdp.call("Page.navigate", {"url": b.js("location.href")}, session=b.sid)
    assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && "
                   "document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))", timeout=60)
    if b.js("document.querySelector('#comment-toggle').getAttribute('aria-pressed')") != "true":
        on(b)   # thread markers from the server draw in comment mode
    assert b.until("document.querySelectorAll('.mpin[data-thread]').length === 1", timeout=30), \
        "the resolved pin lost its marker after a reload"


def test_a_late_reply_frame_cannot_undo_a_newer_resolve(hiding_review, server, chrome, monkeypatch):
    """A reply frame that lands after a newer resolve must not downgrade the thread on screen (2026-11-17 flake).

    The capture holds the reply's own SSE frame, so the replay is deterministic: the reply HTTP response, then the
    resolve HTTP response and its live frame are all applied before the held reply snapshot is delivered.
    """
    rid = hiding_review
    held = []
    real_publish = live.publish

    def hold_reply(review_id, kind, thread_id, actor, data):
        if review_id == rid and kind == "thread.replied" and not held:
            held.append((review_id, kind, thread_id, actor, data))
            return 0
        return real_publish(review_id, kind, thread_id, actor, data)

    def resolved_ui():
        return {
            "pins": b.js("document.querySelectorAll('.mpin').length"),
            "label": b.js("document.querySelector('.tpop .tp-resolve').getAttribute('aria-label')"),
            "pressed": b.js("document.querySelector('.tpop .tp-resolve').getAttribute('aria-pressed')"),
            "on": b.js("document.querySelector('.tpop .tp-resolve').classList.contains('on')"),
        }

    monkeypatch.setattr(live, "publish", hold_reply)
    b = open_board(chrome, server, rid)
    reload_with_a_counted_event_stream(b, f"{server}/r/{rid}/")
    on(b)
    b.click(*point(b, "#copy"))
    save_pin(b, "Make this clearer")

    b.press(".mpin")
    assert b.until("!!document.querySelector('.tpop textarea')")
    b.js("(function(t){t.value='A reply that arrives late';t.dispatchEvent(new Event('input'));return 1})"
         "(document.querySelector('.tpop textarea'))")
    b.press(".tpop .tp-send")
    assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2")
    assert len(held) == 1, "the reply's live frame was not held back"

    b.press(".tpop .tp-resolve")
    assert b.until("!document.querySelector('.tpop') && document.querySelectorAll('.mpin').length === 0")

    b.press(".tcomments")
    assert b.until("!!document.querySelector('.toverlay')")
    b.press(".ttoggle .tr")
    assert b.until("document.querySelector('.toverlay').textContent.includes('Resolved')")
    b.press(".toverlay .trow")
    assert b.until("!!document.querySelector('.tpop')")
    assert resolved_ui() == {"pins": 0, "label": "Reopen", "pressed": "true", "on": True}, \
        "the reopened thread must show its resolved state before the late frame"

    real_publish(*held[0])
    assert b.until("window.__replyFrames === 1", timeout=10), "the late reply frame never reached the board"
    assert resolved_ui() == {"pins": 0, "label": "Reopen", "pressed": "true", "on": True}, \
        "the late reply frame downgraded the newer resolve on screen"


def test_reply_enter_shift_enter_and_the_limit(thread_review, server, chrome):
    rid = thread_review
    owner = users.owner_user()
    threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned",
                   anchor={"block": "m1", "option": None, "w": "desktop", "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    on(b)
    b.press(".mpin")
    assert b.until("!!document.querySelector('.tpop textarea')")
    ta = "document.querySelector('.tpop textarea')"
    assert b.js(ta + ".maxLength") == 2000
    b.js(ta + ".focus()")
    b.cdp.call("Input.insertText", {"text": "line one"}, session=b.sid)
    b.key("Enter", "Enter", 13)
    assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2")
    time.sleep(0.6)  # the reply's live echo repaints once; the next keys must land in the one textarea it leaves

    b.js(ta + ".focus()")
    b.cdp.call("Input.insertText", {"text": "x"}, session=b.sid)
    b.cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter",
                                          "windowsVirtualKeyCode": 13, "modifiers": 8, "text": "\r"}, session=b.sid)
    b.cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                                          "windowsVirtualKeyCode": 13, "modifiers": 8}, session=b.sid)
    assert b.until(ta + ".value.includes('\\n')"), "Shift+Enter must insert a newline"
    assert b.js("document.querySelectorAll('.tpop .tcomment').length") == 2

    b.js(ta + ".value='';" + ta + ".focus()")
    b.cdp.call("Input.insertText", {"text": "y" * 2500}, session=b.sid)
    assert b.js(ta + ".value.length") <= 2000


def test_typing_posts_a_live_typing_frame(thread_review, server, chrome):
    """A second browser identity is not needed: the board's POST and the SSE fan-out are what is under test."""
    rid = thread_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned",
                       anchor={"block": "m1", "option": None, "w": "desktop", "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    on(b)
    b.press(".mpin")
    assert b.until("!!document.querySelector('.tpop textarea')")
    live = Stream(server, f"/r/{rid}/events")
    try:
        assert live.until("hello") is not None
        b.js("document.querySelector('.tpop textarea').focus()")
        b.cdp.call("Input.insertText", {"text": "h"}, session=b.sid)
        frame = live.until("typing", timeout=4)
        assert frame is not None
        data = json.loads(frame["data"])
        assert data["thread"] == t["id"] and data["actor"] == f"user:{owner['id']}" and data["name"] == owner["name"]
    finally:
        live.close()


def test_activity_shows_a_reply_and_is_unread_until_opened(thread_review, server, chrome):
    rid = thread_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned",
                       anchor={"block": "m1", "option": None, "w": "desktop", "x": 0.3, "y": 0.4})
    member = account(review_id=rid, role="commenter", name="Maya")
    code, _, out = http("POST", f"{server}/r/{rid}/api/threads/{t['id']}/reply", {"text": "Maya was here"},
                        signed_in(member))
    assert code == 200, out

    b = open_board(chrome, server, rid)
    reload_with_a_delayed_event_stream(b, f"{server}/r/{rid}/")
    assert b.js("document.querySelector('.tactivity .badge').textContent") == "1", \
        "a replayed reply must not be counted again on top of the fetched snapshot"
    assert b.until("document.querySelector('.tactivity .badge').textContent === '1'")
    assert b.js("document.querySelector('.tactivity').classList.contains('has-unread')")
    b.press(".tactivity")
    assert b.until("!!document.querySelector('.toverlay')")
    assert b.until("document.querySelector('.toverlay').textContent.includes('Maya replied to thread 1')")
    assert b.until("document.querySelector('.tactivity .badge').textContent === '0'")


def as_account(b, server, rid, member):
    b.cdp.call("Network.enable", session=b.sid)
    for key, value in [("mr_session", member["session"]), ("mr_csrf", member["csrf"])]:
        assert b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                          session=b.sid)["success"]
    b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
    assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && "
                   "document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))", timeout=60)
    time.sleep(0.6)


def test_a_commenter_can_open_comment_mode_and_post_a_first_comment(thread_review, server, chrome, monkeypatch):
    rid = thread_review
    member = account(review_id=rid, role="commenter", name="Maya")
    b = open_board(chrome, server, rid)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    as_account(b, server, rid, member)
    assert b.until("!!document.querySelector('#comment-toggle')")
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    b.click(*point(b, "#copy"))
    assert b.until("!!document.querySelector('.popover textarea')")
    b.js("(function(t){t.value='From Maya';t.dispatchEvent(new Event('input'));return 1})"
         "(document.querySelector('.popover textarea'))")
    b.press(".popover .btn-primary")
    assert b.until("!!document.querySelector('.mpin[data-thread]')")
    listed = server_threads(server, rid, member)["threads"]
    assert len(listed) == 1 and listed[0]["comments"][0]["text"] == "From Maya"


def test_a_viewer_has_no_comment_toggle_and_no_composer(thread_review, server, chrome, monkeypatch):
    rid = thread_review
    member = account(review_id=rid, role="viewer", name="Vic")
    b = open_board(chrome, server, rid)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    as_account(b, server, rid, member)
    assert b.js("document.querySelector('#comment-toggle') === null")
    b.key("c", "KeyC", 67, text="c")
    assert b.js("document.querySelector('#comment-toggle') === null")
    b.click(*point(b, "#copy"))
    assert not b.js("!!document.querySelector('.popover textarea')")


def test_a_commenter_keeps_the_toggle_on_a_submitted_round(thread_review, server, chrome, monkeypatch):
    rid = thread_review
    data = store.load(rid)
    service.submit(rid, 1, {"guidelines": "Submitted for round 1"}, data["token"], do_notify=False)
    member = account(review_id=rid, role="commenter", name="Maya")
    b = open_board(chrome, server, rid)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    as_account(b, server, rid, member)
    assert b.until("!!document.querySelector('#comment-toggle')")
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    b.click(*point(b, "#copy"))
    assert b.until("!!document.querySelector('.popover textarea')")


def test_the_first_comment_is_2000_chars_and_keeps_newlines(thread_review, server, chrome):
    rid = thread_review
    b = open_board(chrome, server, rid)
    on(b)
    b.click(*point(b, "#copy"))
    assert b.until("!!document.querySelector('.popover textarea')")
    assert b.js("document.querySelector('.popover textarea').maxLength") == 2000
    b.js("(function(t){t.value='line one\\nline two';t.dispatchEvent(new Event('input'));return 1})"
         "(document.querySelector('.popover textarea'))")
    b.press(".popover .btn-primary")
    assert b.until("!!document.querySelector('.mpin[data-thread]')")
    listed = server_threads(server, rid)["threads"]
    assert listed[0]["comments"][0]["text"] == "line one\nline two"


def test_a_remote_reply_keeps_the_unsent_reply_and_focus(thread_review, server, chrome):
    rid = thread_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned",
                       anchor={"block": "m1", "option": None, "w": "desktop", "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    on(b)
    b.press(".mpin")
    assert b.until("!!document.querySelector('.tpop textarea')")
    ta = "document.querySelector('.tpop textarea')"
    b.js(ta + ".focus()")
    b.js("(function(t){t.value='UNSENT DRAFT';t.dispatchEvent(new Event('input'));return 1})(" + ta + ")")
    assert b.js("document.activeElement === " + ta)
    member = account(review_id=rid, role="commenter", name="Maya")
    code, _, data = http("POST", f"{server}/r/{rid}/api/threads/{t['id']}/reply",
                         {"text": "Concurrent reply"}, signed_in(member))
    assert code == 200, data
    assert b.until("document.querySelector('.tpop').textContent.includes('Concurrent reply')")
    assert b.js(ta + ".value") == "UNSENT DRAFT"
    assert b.until("document.activeElement === " + ta)


def test_a_remote_reply_keeps_an_unsaved_comment_edit(thread_review, server, chrome):
    """A live repaint must not throw away the comment editor and the text in it (9a3b940 rebuilt the popover and lost both)."""
    rid = thread_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Owner comment",
                       anchor={"block": "m1", "option": None, "w": "desktop", "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    on(b)
    b.press(".mpin")
    assert b.until("document.querySelectorAll('.tpop .tcomment').length === 1")
    b.press(".tpop .cmore")
    b.press(".tp-menu .menu-item")
    assert b.until("document.querySelectorAll('.tpop textarea').length === 2")   # reply box + the comment editor
    editor = "document.querySelector('.tpop .tcomment textarea')"
    b.js(editor + ".focus()")
    b.js("(function(t){t.value='UNSAVED EDIT';t.dispatchEvent(new Event('input'));return 1})(" + editor + ")")
    assert b.js("document.activeElement === " + editor)

    member = account(review_id=rid, role="commenter", name="Maya")
    code, _, data = http("POST", f"{server}/r/{rid}/api/threads/{t['id']}/reply",
                         {"text": "Concurrent reply"}, signed_in(member))
    assert code == 200, data
    assert b.until("document.querySelector('.tpop').textContent.includes('Concurrent reply')")
    assert b.until("!!document.querySelector('.tpop .tcomment textarea')"), "the editor must survive the repaint"
    assert b.js(editor + ".value") == "UNSAVED EDIT"
    assert b.until("document.activeElement === " + editor)
    b.press(".tpop .tcomment .btn-primary")                     # Save still reaches the right comment
    assert b.until("document.querySelector('.tpop').textContent.includes('UNSAVED EDIT')")
    listed = server_threads(server, rid)["threads"][0]
    assert listed["comments"][0]["text"] == "UNSAVED EDIT"
    assert listed["comments"][1]["text"] == "Concurrent reply"


def test_accepting_a_mention_with_enter_does_not_send(thread_review, server, chrome):
    rid = thread_review
    owner = users.owner_user()
    threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned",
                   anchor={"block": "m1", "option": None, "w": "desktop", "x": 0.3, "y": 0.4})
    account(review_id=rid, role="commenter", name="Maya")
    b = open_board(chrome, server, rid)
    on(b)
    b.press(".mpin")
    assert b.until("!!document.querySelector('.tpop textarea')")
    ta = "document.querySelector('.tpop textarea')"
    b.js(ta + ".focus()")
    b.cdp.call("Input.insertText", {"text": "@Maya"}, session=b.sid)
    assert b.until("!!document.querySelector('.ml')")
    b.key("Enter", "Enter", 13)
    assert b.until(ta + ".value === '@Maya '")
    assert b.js("document.querySelectorAll('.tpop .tcomment').length") == 1   # accepting a mention is not a send
    b.key("Enter", "Enter", 13)
    assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2")
    listed = server_threads(server, rid)["threads"][0]
    assert listed["comments"][-1]["text"] == "@Maya"


def test_accepting_a_mention_in_the_new_thread_composer_does_not_create_it(thread_review, server, chrome):
    rid = thread_review
    member = account(review_id=rid, role="commenter", name="Maya")
    b = open_board(chrome, server, rid)
    on(b)
    b.click(*point(b, "#copy"))
    assert b.until("!!document.querySelector('.popover textarea')")
    ta = "document.querySelector('.popover textarea')"
    b.js(ta + ".focus()")
    b.cdp.call("Input.insertText", {"text": "@Maya"}, session=b.sid)
    assert b.until("!!document.querySelector('.ml')")
    b.key("Enter", "Enter", 13)
    assert b.until(ta + ".value === '@Maya '")
    assert b.js("!!document.querySelector('.popover textarea')")
    assert server_threads(server, rid, member)["threads"] == []


def test_a_remote_member_comment_has_no_edit_or_delete_for_the_owner(thread_review, server, chrome):
    rid = thread_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Owner comment",
                       anchor={"block": "m1", "option": None, "w": "desktop", "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    on(b)
    b.press(".mpin")
    assert b.until("document.querySelectorAll('.tpop .cmore').length === 1")
    member = account(review_id=rid, role="commenter", name="Maya")
    code, _, data = http("POST", f"{server}/r/{rid}/api/threads/{t['id']}/reply",
                         {"text": "Maya's reply"}, signed_in(member))
    assert code == 200, data
    assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2")
    assert b.js("!!document.querySelectorAll('.tpop .tcomment')[0].querySelector('.cmore')")
    assert b.js("!document.querySelectorAll('.tpop .tcomment')[1].querySelector('.cmore')")


def test_a_moved_server_only_mock_thread_detaches_at_its_last_page_spot(thread_review, server, chrome):
    """Detachment uses where the pin last resolved on this page, not the spot it was created at (9a3b940 jumped back)."""
    rid = thread_review
    b = open_board(chrome, server, rid)
    on(b)
    b.click(*point(b, "#copy"))
    save_pin(b)
    b.js("localStorage.clear()")
    b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
    assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && "
                   "document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))", timeout=60)
    time.sleep(0.5)
    if b.js("document.querySelector('#comment-toggle').getAttribute('aria-pressed')") != "true":
        on(b)
    assert b.until("!!document.querySelector('.mpin[data-thread]')")

    def xy():
        return json.loads(b.js(
            "JSON.stringify((()=>{let r=document.querySelector('.mpin[data-thread]').getBoundingClientRect();"
            "return [r.left+r.width/2+scrollX,r.top+r.height/2+scrollY]})())"))

    original = xy()
    b.js("(()=>{let root=document.querySelector('.mock-host').shadowRoot,s=document.createElement('div');"
         "s.id='spacer';s.style.height='120px';root.querySelector('#copy').before(s);return 1})()")
    assert b.until("!!document.querySelector('.mpin[data-thread]')")
    time.sleep(0.6)                                     # the mock surface repaints and re-resolves the moved target
    moved = xy()
    assert moved[1] - original[1] > 40, (original, moved)   # the pin followed its element down the mock

    b.js("window.__removed=document.querySelector('.mock-host').shadowRoot.querySelector('#copy');"
         "window.__removed.remove()")
    assert b.until("!!document.querySelector('.mpin.det')")
    time.sleep(0.6)
    after = xy()
    assert abs(after[1] - moved[1]) < 3, (original, moved, after)
    b.js("(()=>{let root=document.querySelector('.mock-host').shadowRoot;"
         "root.querySelector('#spacer').before(window.__removed)})()")
    assert b.until("!!document.querySelector('.mpin:not(.det)')")


def test_a_server_only_mock_thread_keeps_its_document_spot_when_the_target_is_removed(thread_review, server, chrome):
    rid = thread_review
    b = open_board(chrome, server, rid)
    on(b)
    b.click(*point(b, "#copy"))
    save_pin(b)
    b.js("localStorage.clear()")
    b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
    assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && "
                   "document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))", timeout=60)
    time.sleep(0.5)
    if b.js("document.querySelector('#comment-toggle').getAttribute('aria-pressed')") != "true":
        on(b)
    assert b.until("!!document.querySelector('.mpin[data-thread]')")

    def xy():
        return json.loads(b.js(
            "JSON.stringify((()=>{let r=document.querySelector('.mpin[data-thread]').getBoundingClientRect();"
            "return [r.left+r.width/2+scrollX,r.top+r.height/2+scrollY]})())"))

    before = xy()
    b.js("window.__removed=document.querySelector('.mock-host').shadowRoot.querySelector('#copy');"
         "window.__removed.remove()")
    assert b.until("!!document.querySelector('.mpin.det')")
    time.sleep(0.5)
    after = xy()
    assert abs(after[1] - before[1]) < 3, (before, after)
    b.js("(()=>{let root=document.querySelector('.mock-host').shadowRoot;root.querySelector('#other').before(window.__removed)})()")
    assert b.until("!!document.querySelector('.mpin:not(.det)')")
