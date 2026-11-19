"""Real-Chrome tests for server threads in the prototype and artifact frames: the frame receives the thread (with its
id) in comment mode, and a marker click opens that thread instead of a new draft composer."""

import json
import re
import time
from pathlib import Path

import pytest

from mockreview import store, threads, users
from test_live_round_tool import Board, chrome, live_review  # noqa: F401

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
ARTIFACT = {"blocks": [{"type": "artifact", "id": "film", "title": "Film", "markable": False,
                        "html": "<!doctype html><html><body><p id=\"copy\">Artifact page</p></body></html>"}]}


@pytest.fixture
def artifact_review(env, tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(ARTIFACT), encoding="utf-8")
    data = store.create_review("Artifact threads", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(path)), wake=False)
    threads.reset_limits()
    return data["id"]


def frame_session(b, rid):
    target = b.frame_target(lambda url: rid in url)
    sid = b.cdp.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["sessionId"]
    b.cdp.call("Runtime.enable", session=sid)
    return sid


def frame_eval(b, sid, expr):
    return b.cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session=sid)["result"].get("value")


def wait_frame(b, sid, expr, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if frame_eval(b, sid, expr):
            return True
        b.cdp.pump(0.1)
    return False


def observe(b, sid):
    assert frame_eval(b, sid, "window.__reviewStates=[];"
                              "addEventListener('message',e=>{if(e.data&&e.data.cmd==='state')"
                              "window.__reviewStates.push(e.data)});true")


def last_state(b, sid):
    return json.loads(frame_eval(b, sid, "JSON.stringify(window.__reviewStates["
                                         "window.__reviewStates.length-1]||null)"))


def test_a_server_thread_reaches_the_prototype_frame_and_opens(server, live_review, chrome):
    rid = live_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "From another browser",
                       anchor={"block": "proto", "w": "desktop", "x": 0.2, "y": 0.2,
                               "target": {"path": "#copy", "sig": {"tag": "p", "h": "abcd"}, "label": "copy",
                                          "doc": {"x": 80, "y": 160}}})
    b = Board(chrome, f"{server}/r/{rid}/")
    sid = frame_session(b, rid)
    observe(b, sid)
    assert b.until("document.querySelector('.tcomments .badge').textContent === '1'")
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    assert wait_frame(b, sid, "(()=>{let s=window.__reviewStates;return s.length&&"
                              f"s[s.length-1].pins.some(p=>p.thread==={json.dumps(t['id'])});}})()"), last_state(b, sid)
    state = last_state(b, sid)
    pin = next(p for p in state["pins"] if p.get("thread") == t["id"])
    assert state["comment"] is True and pin["i"] >= 60
    frame_eval(b, sid, "parent.postMessage({__mockreview:1,type:'pin',i:%d,thread:%s},'*');true"
                       % (pin["i"], json.dumps(t["id"])))
    assert b.until("!!document.querySelector('.tpop') && "
                   "document.querySelector('.tpop').textContent.includes('From another browser')")


def test_a_server_thread_reaches_the_artifact_frame_and_opens(server, artifact_review, chrome):
    rid = artifact_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "On the artifact",
                       anchor={"block": "film", "option": None, "w": "desktop", "x": 0.3, "y": 0.3})
    b = Board(chrome, f"{server}/r/{rid}/", wait="!!document.querySelector('.art-frame')")
    sid = frame_session(b, rid)
    observe(b, sid)
    assert b.until("document.querySelector('.tcomments .badge').textContent === '1'")
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    assert wait_frame(b, sid, "(()=>{let s=window.__reviewStates;return s.length&&"
                              f"s[s.length-1].pins.some(p=>p.thread==={json.dumps(t['id'])});}})()"), last_state(b, sid)
    state = last_state(b, sid)
    pin = next(p for p in state["pins"] if p.get("thread") == t["id"])
    assert state["comment"] is True and pin["i"] >= 60
    frame_eval(b, sid, "parent.postMessage({__mockreview:1,type:'pin',i:%d,thread:%s},'*');true"
                       % (pin["i"], json.dumps(t["id"])))
    assert b.until("!!document.querySelector('.tpop') && "
                   "document.querySelector('.tpop').textContent.includes('On the artifact')")


def test_a_frame_opens_a_widthless_artifact_thread_from_its_own_marker(server, artifact_review, chrome):
    rid = artifact_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "On the artifact",
                       anchor={"block": "film", "option": None, "x": 0.3, "y": 0.3})
    b = Board(chrome, f"{server}/r/{rid}/", wait="!!document.querySelector('.art-frame')")
    sid = frame_session(b, rid)
    observe(b, sid)
    assert b.until("document.querySelector('.tcomments .badge').textContent === '1'")
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    assert wait_frame(b, sid, "(()=>{let s=window.__reviewStates;return s.length&&"
                              f"s[s.length-1].pins.some(p=>p.thread==={json.dumps(t['id'])});}})()"), last_state(b, sid)
    state = last_state(b, sid)
    pin = next(p for p in state["pins"] if p.get("thread") == t["id"])
    assert state["comment"] is True and pin["i"] >= 60
    frame_eval(b, sid, "parent.postMessage({__mockreview:1,type:'pin',i:%d,thread:%s},'*');true"
                       % (pin["i"], json.dumps(t["id"])))
    assert b.until("!!document.querySelector('.tpop') && "
                   "document.querySelector('.tpop').textContent.includes('On the artifact')")


# ---------------------------------------------------------------- a frame's thread opens
TWO_BLOCKS = {"blocks": [
    {"type": "artifact", "id": "film", "title": "Film", "markable": False,
     "html": "<!doctype html><html><body><p id=\"copy\">Artifact page</p></body></html>"},
    {"type": "artifact", "id": "poster", "title": "Poster", "markable": False,
     "html": "<!doctype html><html><body><p>Poster</p></body></html>"}]}


@pytest.fixture
def two_block_review(env, tmp_path):
    path = tmp_path / "two.json"
    path.write_text(json.dumps(TWO_BLOCKS), encoding="utf-8")
    data = store.create_review("Frame opens", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(path)), wake=False)
    threads.reset_limits()
    return data["id"]


def test_the_board_js_never_marks_read_from_a_frame_message():
    """SECAUDIT cand_0159254039278b811d070458, static half: every thread open that a frame can trigger goes through
    openFrameThread (own block, no read), and only board-owned DOM calls T.open without it."""
    code = (Path(__file__).resolve().parent.parent / "mockreview" / "board" / "board.js").read_text(encoding="utf-8")
    for frame_open in ("T.open(d.thread", "T.open(t)", "T.open(st"):
        assert frame_open not in code      # a frame message never opens a thread directly
    assert "openFrameThread(f, b.id, d.thread, d.detached)" in code
    assert "openFrameThread(f, 'proto', d.thread, d.detached)" in code
    assert code.count("read: false") >= 1
    threads_js = (Path(__file__).resolve().parent.parent / "mockreview" / "board" / "threads.js").read_text(
        encoding="utf-8")
    assert "if (!extra || extra.read !== false) markRead(t);" in threads_js


@pytest.mark.browser
def test_a_frame_opens_only_its_own_blocks_thread_and_never_marks_it_read(server, two_block_review, chrome):
    rid = two_block_review
    owner = users.owner_user()
    # a thread someone else wrote is unread for the owner until the owner reads it
    own = threads.create(rid, "user:424242", "Someone Else", "reviewer", "On the film",
                         anchor={"block": "film", "option": None, "w": "desktop", "x": 0.3, "y": 0.3})
    other = threads.create(rid, "user:424242", "Someone Else", "reviewer", "On the poster",
                           anchor={"block": "poster", "option": None, "w": "desktop", "x": 0.4, "y": 0.4})
    b = Board(chrome, f"{server}/r/{rid}/", wait="!!document.querySelector('.art-frame')")
    sid = frame_session(b, rid)
    observe(b, sid)
    assert b.until("document.querySelector('.tcomments .badge').textContent === '2'")
    b.press("#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
    assert wait_frame(b, sid, "(()=>{let s=window.__reviewStates;return s.length&&"
                              f"s[s.length-1].pins.some(p=>p.thread==={json.dumps(own['id'])});}})()"), \
        last_state(b, sid)
    state = last_state(b, sid)
    pin = next(p for p in state["pins"] if p.get("thread") == own["id"])

    # another block's thread: the frame names an id the board never showed it, so nothing opens
    frame_eval(b, sid, "parent.postMessage({__mockreview:1,type:'pin',i:%d,thread:%s},'*');true"
                       % (pin["i"], json.dumps(other["id"])))
    b.cdp.pump(0.5)
    assert not b.js("!!document.querySelector('.tpop')")
    assert b.js("document.querySelector('.tcomments .badge').textContent") == "2"

    # its own thread opens, but the frame's word is not a read: the unread badge stays
    frame_eval(b, sid, "parent.postMessage({__mockreview:1,type:'pin',i:%d,thread:%s},'*');true"
                       % (pin["i"], json.dumps(own["id"])))
    assert b.until("!!document.querySelector('.tpop') && "
                   "document.querySelector('.tpop').textContent.includes('On the film')")
    assert b.js("document.querySelector('.tcomments .badge').textContent") == "2"

    principal = f"user:{owner['id']}"
    opened = {t["id"]: t["unread"] for t in threads.listing(rid, principal=principal)["threads"]}
    assert opened[own["id"]] is True and opened[other["id"]] is True    # nothing the frame sent was a read

    # a tap on the board's own thread row is the trusted read
    assert b.js("(function(){var c=document.querySelector('button.tcomments');if(!c)return false;c.click();return true;})()")
    assert b.until("!!document.querySelector('.trow')")
    assert b.js("(function(){var r=document.querySelectorAll('.trow');"
                "for(var i=0;i<r.length;i++){if(r[i].textContent.indexOf('On the film')>=0){r[i].click();return true;}}"
                "return false;})()")
    read = {}
    for _ in range(50):                       # the read POST is async
        read = {t["id"]: t["unread"] for t in threads.listing(rid, principal=principal)["threads"]}
        if read[own["id"]] is False:
            break
        time.sleep(0.1)
    assert read[own["id"]] is False and read[other["id"]] is True
