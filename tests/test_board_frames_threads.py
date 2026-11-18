"""Real-Chrome tests for server threads in the prototype and artifact frames: the frame receives the thread (with its
id) in comment mode, and a marker click opens that thread instead of a new draft composer."""

import json
import time

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
    targets = b.cdp.call("Target.getTargets")["targetInfos"]
    target = next(x for x in targets if x["type"] == "iframe" and rid in x["url"])
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
