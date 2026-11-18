"""The MCP comment tools: per-user tokens, role scopes, the live event path, resolve and the @Hermes guard.

These drive ``mcp.call_tool`` exactly as a coding agent would over stdio: JSON arguments in, one JSON text block out.
A real subprocess proves the token can come from ``MOCKREVIEW_TOKEN``; a real SSE connection proves a reply reaches a
person's board through the board's live event path.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from authkit import account, loopback_owner_off, signed_in  # noqa: F401 - the fixture is used by name
from conftest import ROOT, http, python_child_env
from mockreview import auth, config, db, live, mcp, runtime, store, threads, users, wakeguard
from test_live import Stream

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
DECISIONS = {"blocks": [{"type": "mock", "id": "m1", "html": "<p>Review this total</p>"}]}


@pytest.fixture(autouse=True)
def clean_live():
    live.reset()
    threads.reset_limits()
    yield
    live.reset()
    threads.reset_limits()


# ---------------------------------------------------------------- helpers

def call(name, arguments, token=None):
    return mcp.call_tool(name, arguments, mcp.Options(token=token))


def payload(result):
    text = result["content"][0]["text"]
    assert result["content"][0]["type"] == "text"
    return json.loads(text)


def error(result):
    assert result["isError"] is True, result
    return payload(result)["error"]


def make_review(env, tmp_path, title="Comments"):
    path = tmp_path / f"{title}.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review(title, "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)), wake=False)
    return data["id"]


def make_thread(rid, owner, text="Please tighten this"):
    return threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", text,
                          anchor={"block": "m1", "x": 0.2, "y": 0.3})


def token_for(user_id, name="agent"):
    created = users.create_token(user_id, name)
    return {"id": created["id"], "token": created["token"]}


def counts():
    conn = db.connect()
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("threads", "comments", "mentions", "reads", "events", "audit")}


def thread_status(tid):
    return db.connect().execute("SELECT status FROM threads WHERE id = ?", (tid,)).fetchone()["status"]


def child_env(tmp_path, token):
    """The environment of a real stdio server: the same hermetic homes as the test, sharing its store."""
    env = python_child_env(tmp_path)
    env.update(HERMES_HOME=os.environ["HERMES_HOME"], MOCKREVIEW_HOME=os.environ["MOCKREVIEW_HOME"],
               MOCKREVIEW_OWNER="self", MOCKREVIEW_TOKEN=token, PYTHONDONTWRITEBYTECODE="1")
    return env


def child_call(env, name, arguments):
    """One tools/call line into a real `python -m mockreview.mcp` child; returns the MCP tool result."""
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    proc = subprocess.run([sys.executable, "-m", "mockreview.mcp"], cwd=str(ROOT), env=env,
                          input=json.dumps(req) + "\n", capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=90)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "", proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert len(responses) == 1, responses
    return responses[0]["result"]


def dead_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture
def board(env, tmp_path):
    """One review with an open thread and one token per role: owner, reviewer, commenter, viewer, and a member with
    no grant on the review at all."""
    rid = make_review(env, tmp_path)
    owner = users.owner_user()
    reviewer = account(rid, "reviewer", name="Rita Reviewer")
    commenter = account(rid, "commenter", name="Cam Commenter")
    viewer = account(rid, "viewer", name="Vic Viewer")
    stranger = account(name="Sam Stranger")
    thread = make_thread(rid, owner)
    return {
        "review_id": rid, "thread_id": thread["id"],
        "owner": {**owner, "token": token_for(owner["id"])["token"]},
        "reviewer": {**reviewer, "token": token_for(reviewer["user_id"])["token"]},
        "commenter": {**commenter, "token": token_for(commenter["user_id"])["token"]},
        "viewer": {**viewer, "token": token_for(viewer["user_id"])["token"]},
        "stranger": {**stranger, "token": token_for(stranger["user_id"])["token"]},
    }


# ---------------------------------------------------------------- the token gate

def test_comment_tools_refuse_a_missing_token(board):
    rid, tid = board["review_id"], board["thread_id"]
    for name, arguments in (("list_comments", {"review_id": rid}),
                            ("reply", {"thread_id": tid, "text": "hello"}),
                            ("resolve", {"thread_id": tid})):
        message = error(call(name, arguments))
        assert "access token" in message and "MOCKREVIEW_TOKEN" in message


def test_comment_tools_refuse_an_unknown_or_revoked_token(board):
    rid, tid = board["review_id"], board["thread_id"]
    message = error(call("list_comments", {"review_id": rid}, token="mrp_not-a-real-token"))
    assert "not valid" in message
    revoked = token_for(board["reviewer"]["user_id"], "retired")
    users.revoke_token(board["reviewer"]["user_id"], revoked["id"])
    assert "not valid" in error(call("list_comments", {"review_id": rid}, token=revoked["token"]))
    assert "not valid" in error(call("reply", {"thread_id": tid, "text": "hello"}, token=revoked["token"]))


def test_a_token_without_a_grant_on_the_review_is_refused(board):
    rid, tid = board["review_id"], board["thread_id"]
    message = error(call("list_comments", {"review_id": rid}, token=board["stranger"]["token"]))
    assert "no access" in message
    message = error(call("reply", {"thread_id": tid, "text": "hello"}, token=board["stranger"]["token"]))
    assert "no access" in message
    assert "no access" in error(call("resolve", {"thread_id": tid}, token=board["stranger"]["token"]))


def test_a_missing_review_and_a_bad_status_are_reported(board):
    other = "0" * 32
    assert "no access" in error(call("list_comments", {"review_id": other}, token=board["stranger"]["token"]))
    assert "review not found" in error(call("list_comments", {"review_id": other}, token=board["owner"]["token"]))
    assert "status must be" in error(call("list_comments", {"review_id": board["review_id"], "status": "closed"},
                                          token=board["reviewer"]["token"]))


# ---------------------------------------------------------------- roles

def test_viewer_token_cannot_reply_or_resolve(board):
    rid, tid = board["review_id"], board["thread_id"]
    message = error(call("reply", {"thread_id": tid, "text": "looking good"}, token=board["viewer"]["token"]))
    assert "viewer" in message and "reviewer or commenter" in message
    message = error(call("resolve", {"thread_id": tid}, token=board["viewer"]["token"]))
    assert "viewer" in message and "only a reviewer" in message
    listed = payload(call("list_comments", {"review_id": rid}, token=board["viewer"]["token"]))
    assert [len(t["comments"]) for t in listed["threads"]] == [1]      # nothing was written


def test_commenter_may_reply_but_not_resolve(board):
    tid = board["thread_id"]
    body = payload(call("reply", {"thread_id": tid, "text": "On it"}, token=board["commenter"]["token"]))
    assert body["as"]["role"] == "commenter"
    message = error(call("resolve", {"thread_id": tid}, token=board["commenter"]["token"]))
    assert "commenter" in message and "only a reviewer" in message


def test_owner_token_may_reply_and_resolve(board):
    tid = board["thread_id"]
    body = payload(call("reply", {"thread_id": tid, "text": "Fixed"}, token=board["owner"]["token"]))
    assert body["as"]["role"] == "owner"
    body = payload(call("resolve", {"thread_id": tid}, token=board["owner"]["token"]))
    assert body["status"] == "resolved"


# ---------------------------------------------------------------- reply: author, live event, audit

def test_reply_shows_in_the_thread_with_its_author_and_fires_a_live_event(board, server):
    rid, tid = board["review_id"], board["thread_id"]
    s = Stream(server, f"/r/{rid}/events", signed_in(board["reviewer"], csrf=False))
    try:
        result = call("reply", {"thread_id": tid, "text": "Tightened in round 2"}, token=board["reviewer"]["token"])
        assert result["isError"] is False
        body = payload(result)
        comment = body["comment"]
        assert comment["author"] == f"user:{board['reviewer']['user_id']}"
        assert comment["author_name"] == "Rita Reviewer"
        assert comment["role"] == "reviewer" and comment["mine"] is True
        assert comment["text"] == "Tightened in round 2"

        frame = s.until("thread.replied")
        assert frame is not None, "the reply never reached the live stream"
        data = json.loads(frame["data"])
        assert data["thread"]["id"] == tid
        assert data["comment"] == comment["id"]
        assert data["actor"] == f"user:{board['reviewer']['user_id']}"
        assert data["name"] == "Rita Reviewer"
        assert any(c["id"] == comment["id"] for c in data["thread"]["comments"])
    finally:
        s.conn.close()

    listed = payload(call("list_comments", {"review_id": rid}, token=board["reviewer"]["token"]))
    assert listed["counts"]["open"] == 1
    shown = listed["threads"][0]
    assert shown["id"] == tid
    assert [(c["author_name"], c["text"]) for c in shown["comments"]][-1] == ("Rita Reviewer",
                                                                              "Tightened in round 2")


def test_reply_is_audited_under_the_token_user(board):
    tid = board["thread_id"]
    body = payload(call("reply", {"thread_id": tid, "text": "Done"}, token=board["reviewer"]["token"]))
    call("resolve", {"thread_id": tid}, token=board["reviewer"]["token"])
    rows = users.audit_rows(50)
    actor = f"user:{board['reviewer']['user_id']}"
    replies = [r for r in rows if r["action"] == "mcp.reply"]
    assert replies and replies[0]["actor"] == actor
    assert replies[0]["target"] == f"review:{board['review_id']}"
    assert replies[0]["detail"]["thread_id"] == tid
    assert replies[0]["detail"]["comment"] == body["comment"]["id"]
    resolves = [r for r in rows if r["action"] == "mcp.resolve"]
    assert resolves and resolves[0]["actor"] == actor and resolves[0]["detail"]["thread_id"] == tid


def test_reply_is_plain_text_with_the_ui_limits(board):
    tid = board["thread_id"]
    message = error(call("reply", {"thread_id": tid, "text": "x" * 2001}, token=board["reviewer"]["token"]))
    assert "2000" in message
    message = error(call("reply", {"thread_id": tid, "text": "   \n\t "}, token=board["reviewer"]["token"]))
    assert "write something first" in message
    body = payload(call("reply", {"thread_id": tid, "text": "Line one\r\nLine two\x00"},
                        token=board["reviewer"]["token"]))
    assert body["comment"]["text"] == "Line one\nLine two"


def test_missing_arguments_are_tool_errors(board):
    assert "required" in error(call("list_comments", {}, token=board["reviewer"]["token"]))
    assert "required" in error(call("reply", {"thread_id": board["thread_id"]}, token=board["reviewer"]["token"]))
    assert "required" in error(call("reply", {"text": "x"}, token=board["reviewer"]["token"]))
    assert "required" in error(call("resolve", {}, token=board["reviewer"]["token"]))
    assert "thread not found" in error(call("reply", {"thread_id": "f" * 32, "text": "x"},
                                           token=board["reviewer"]["token"]))
    assert "thread not found" in error(call("resolve", {"thread_id": "nope"}, token=board["reviewer"]["token"]))


# ---------------------------------------------------------------- resolve

def test_resolve_hides_the_thread_until_it_is_reopened(board):
    rid, tid = board["review_id"], board["thread_id"]
    token = board["reviewer"]["token"]
    body = payload(call("resolve", {"thread_id": tid}, token=token))
    assert body["status"] == "resolved" and body["thread"]["status"] == "resolved"

    open_only = payload(call("list_comments", {"review_id": rid}, token=token))
    assert open_only["threads"] == [] and open_only["counts"] == {"open": 0, "resolved": 1}
    hidden = payload(call("list_comments", {"review_id": rid, "status": "open"}, token=token))
    assert hidden["threads"] == []
    resolved = payload(call("list_comments", {"review_id": rid, "status": "resolved"}, token=token))
    assert [t["id"] for t in resolved["threads"]] == [tid]
    every = payload(call("list_comments", {"review_id": rid, "status": "all"}, token=token))
    assert [t["status"] for t in every["threads"]] == ["resolved"]

    message = error(call("reply", {"thread_id": tid, "text": "late reply"}, token=token))
    assert "Reopen" in message

    body = payload(call("resolve", {"thread_id": tid, "reopen": True}, token=token))
    assert body["status"] == "open" and body["thread"]["status"] == "open"
    assert [t["id"] for t in payload(call("list_comments", {"review_id": rid}, token=token))["threads"]] == [tid]


def test_resolve_fires_the_live_status_event(board, server):
    rid, tid = board["review_id"], board["thread_id"]
    s = Stream(server, f"/r/{rid}/events", signed_in(board["viewer"], csrf=False))
    try:
        call("resolve", {"thread_id": tid}, token=board["reviewer"]["token"])
        frame = s.until("thread.status")
        assert frame is not None
        assert json.loads(frame["data"])["status"] == "resolved"
    finally:
        s.conn.close()


def test_reopen_argument_must_be_boolean(board):
    message = error(call("resolve", {"thread_id": board["thread_id"], "reopen": "yes"},
                         token=board["reviewer"]["token"]))
    assert "true or false" in message


# ---------------------------------------------------------------- the @Hermes loop guard

def test_a_hermes_mention_in_an_mcp_reply_never_activates_the_thread(board):
    rid, tid = board["review_id"], board["thread_id"]
    result = call("reply", {"thread_id": tid, "text": "@Hermes please fix this too",
                            "mentions": ["hermes", "@Hermes"]}, token=board["reviewer"]["token"])
    assert result["isError"] is False
    body = payload(result)
    assert body["thread"]["activated"] is False
    assert body["comment"]["sent_to_hermes"] is False
    assert body["comment"]["mentions"] == []
    kinds = [r["kind"] for r in db.connect().execute("SELECT kind FROM mentions WHERE comment_id = ?",
                                                     (body["comment"]["id"],)).fetchall()]
    assert kinds == []
    events = [r["kind"] for r in db.connect().execute("SELECT kind FROM events WHERE review_id = ?",
                                                      (rid,)).fetchall()]
    assert "mention" not in events
    stored = db.connect().execute("SELECT activated FROM threads WHERE id = ?", (tid,)).fetchone()
    assert stored["activated"] == 0


def test_mentions_still_reach_people_who_can_see_the_review(board):
    rid, tid = board["review_id"], board["thread_id"]
    target = f"user:{board['commenter']['user_id']}"
    body = payload(call("reply", {"thread_id": tid, "text": "ping", "mentions": [target]},
                        token=board["reviewer"]["token"]))
    assert body["comment"]["mentions"] == [{"kind": "user", "target": target}]
    assert body["thread"]["activated"] is False


# ---------------------------------------------------------------- the environment

def test_the_token_can_come_from_the_environment(board, tmp_path):
    """The same server started with MOCKREVIEW_TOKEN (no --token) acts as that user: this is how an MCP client's
    configuration supplies a token."""
    rid = board["review_id"]
    env = python_child_env(tmp_path)
    env.update(PYTHONPATH=str(ROOT), MOCKREVIEW_HOME=str(tmp_path / "data"), MOCKREVIEW_OWNER="self",
               MOCKREVIEW_TOKEN=board["reviewer"]["token"], PYTHONIOENCODING="utf-8")
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "list_comments", "arguments": {"review_id": rid}}},
    ]
    proc = subprocess.run([sys.executable, "-m", "mockreview.mcp"],
                          input="".join(json.dumps(line) + "\n" for line in lines),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=str(ROOT), env=env, timeout=90)
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in responses] == [1, 2]
    result = responses[1]["result"]
    assert result["isError"] is False, result
    body = payload(result)
    assert body["as"]["role"] == "reviewer"
    assert [t["id"] for t in body["threads"]] == [board["thread_id"]]
    assert board["reviewer"]["token"] not in proc.stdout
    assert board["reviewer"]["token"] not in proc.stderr


# ---------------------------------------------------------------- a separate stdio process reaches an open board

@pytest.mark.parametrize("action", ["reply", "resolve"])
def test_a_separate_stdio_process_reply_reaches_an_open_board_stream(board, server, tmp_path, action):
    rid, tid = board["review_id"], board["thread_id"]
    reviewer = board["reviewer"]
    s = Stream(server, f"/r/{rid}/events", signed_in(board["viewer"], csrf=False))
    try:
        assert s.until("hello") is not None
        assert s.until("presence") is not None          # the replay window is drained before any write
        code, _, raw = http("POST", f"{server}/r/{rid}/api/threads/{tid}/reply",
                            {"text": "same-process HTTP positive control"}, signed_in(reviewer))
        assert code == 200, raw
        control = s.until("thread.replied")
        assert control is not None and json.loads(control["data"])["thread"]["id"] == tid
        cursor = int(control["id"])

        arguments = {"thread_id": tid}
        if action == "reply":
            arguments["text"] = "from a separate stdio process"
        result = child_call(child_env(tmp_path, reviewer["token"]), action, arguments)
        assert result["isError"] is False, result
        body = payload(result)
        assert body["as"]["name"] == "Rita Reviewer"

        expected = "thread.replied" if action == "reply" else "thread.status"
        stored = threads.list_threads(rid)[0]
        if action == "reply":
            assert stored["comments"][-1]["text"] == "from a separate stdio process"
            assert stored["comments"][-1]["author"] == f"user:{reviewer['user_id']}"
        else:
            assert stored["status"] == "resolved"
        rows = db.connect().execute("SELECT kind FROM events WHERE review_id = ? AND id > ? ORDER BY id",
                                    (rid, cursor)).fetchall()
        assert expected in [r["kind"] for r in rows]

        replay = Stream(server, f"/r/{rid}/events",
                        {**signed_in(board["viewer"], csrf=False), "Last-Event-ID": str(cursor)})
        try:
            assert replay.until(expected) is not None, "the persisted event was not replayable"
        finally:
            replay.close()
        frame = s.until(expected, timeout=3)
        assert frame is not None, f"{action} was stored and replayed, but the already-open stream never received it"
    finally:
        s.close()


# ---------------------------------------------------------------- exact comment under concurrency

def test_the_tool_returns_and_audits_its_exact_comment_under_concurrency(board, monkeypatch):
    rid, tid = board["review_id"], board["thread_id"]
    original = threads._thread_dict
    ready, finished, errors = threading.Event(), threading.Event(), []
    main = threading.get_ident()
    commenter = board["commenter"]

    def peer():
        try:
            assert ready.wait(5)
            threads.reply(rid, tid, f"user:{commenter['user_id']}", "Cam Commenter", "commenter",
                          "concurrent human reply")
        except BaseException as e:      # noqa: BLE001 - reported below, never silent
            errors.append(repr(e))
        finally:
            db.close_all()
            finished.set()

    worker = threading.Thread(target=peer)

    def held_read(*args, **kwargs):
        if threading.get_ident() == main:
            ready.set()
            assert finished.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(threads, "_thread_dict", held_read)
    worker.start()
    try:
        result = payload(call("reply", {"thread_id": tid, "text": "agent own reply"}, board["reviewer"]["token"]))
    finally:
        worker.join(6)
    assert not worker.is_alive() and not errors, errors

    own = db.connect().execute("SELECT id FROM comments WHERE thread_id = ? AND text = ?",
                               (tid, "agent own reply")).fetchone()["id"]
    peer_row = db.connect().execute("SELECT id FROM comments WHERE thread_id = ? AND text = ?",
                                    (tid, "concurrent human reply")).fetchone()["id"]
    assert result["comment"]["id"] == own, "the tool reported another author's comment as its reply"
    audit = next(r for r in users.audit_rows() if r["action"] == "mcp.reply")
    assert audit["detail"]["comment"] == own
    assert own != peer_row


def test_the_tool_keeps_its_own_comment_when_the_same_author_replies_concurrently(board, monkeypatch):
    rid, tid = board["review_id"], board["thread_id"]
    reviewer = board["reviewer"]
    original = threads._thread_dict
    ready, finished, errors = threading.Event(), threading.Event(), []
    main = threading.get_ident()
    peer_reply = {}

    def peer():
        try:
            assert ready.wait(5)
            _, cid = threads.reply_comment(rid, tid, f"user:{reviewer['user_id']}", "Rita Reviewer", "reviewer",
                                           "second reply from the same author")
            peer_reply["id"] = cid
        except BaseException as e:      # noqa: BLE001 - reported below, never silent
            errors.append(repr(e))
        finally:
            db.close_all()
            finished.set()

    worker = threading.Thread(target=peer)

    def held_read(*args, **kwargs):
        if threading.get_ident() == main:
            ready.set()
            assert finished.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(threads, "_thread_dict", held_read)
    worker.start()
    try:
        result = payload(call("reply", {"thread_id": tid, "text": "the tool's own reply"},
                              board["reviewer"]["token"]))
    finally:
        worker.join(6)
    assert not worker.is_alive() and not errors, errors

    own = db.connect().execute("SELECT id FROM comments WHERE thread_id = ? AND text = ?",
                               (tid, "the tool's own reply")).fetchone()["id"]
    assert result["comment"]["id"] == own
    assert result["comment"]["text"] == "the tool's own reply"
    assert peer_reply["id"] != own
    audit = next(r for r in users.audit_rows() if r["action"] == "mcp.reply")
    assert audit["detail"]["comment"] == own


# ---------------------------------------------------------------- no thread-existence oracle

@pytest.mark.parametrize("name", ["reply", "resolve"])
@pytest.mark.parametrize("identity", ["missing-token", "stranger-token"])
def test_existing_and_absent_threads_are_indistinguishable_for_a_missing_or_stranger_token(
        board, name, identity):
    tid = board["thread_id"]
    missing = "f" * 32
    token = None if identity == "missing-token" else board["stranger"]["token"]
    arguments = {"thread_id": tid}
    if name == "reply":
        arguments["text"] = "probe"
    absent = {**arguments, "thread_id": missing}

    before = counts()
    existing_error = error(call(name, arguments, token))
    absent_error = error(call(name, absent, token))
    assert existing_error == absent_error, "the error discloses whether an inaccessible thread exists"
    if identity == "stranger-token":
        assert "thread not found" in existing_error and "no access" in existing_error
    else:
        assert "access token" in existing_error
    assert counts() == before, "a refused call wrote rows"


# ---------------------------------------------------------------- personal access tokens as bearer credentials

def test_a_bearer_token_acts_as_its_user_over_loopback(board, server):
    rid, tid = board["review_id"], board["thread_id"]
    url = f"{server}/r/{rid}/api/threads/{tid}/reply"
    reviewer = board["reviewer"]
    code, _, raw = http("POST", url, {"text": "as the reviewer"},
                        headers={"Authorization": f"Bearer {reviewer['token']}"}, csrf=False)
    assert code == 200, raw
    comment = json.loads(raw)["comment"]
    assert comment["author"] == f"user:{reviewer['user_id']}"
    assert comment["author_name"] == "Rita Reviewer" and comment["role"] == "reviewer"
    assert comment["text"] == "as the reviewer"

    before = counts()
    code, _, raw = http("POST", url, {"text": "viewer may not"},
                        headers={"Authorization": f"Bearer {board['viewer']['token']}"}, csrf=False)
    assert code == 403, raw
    assert counts() == before

    code, _, raw = http("POST", url, {"text": "as the commenter"},
                        headers={"Authorization": f"Bearer {board['commenter']['token']}"}, csrf=False)
    assert code == 200, raw
    assert json.loads(raw)["comment"]["author_name"] == "Cam Commenter"

    # A commenter's bearer may reply, but resolving is the reviewer rule on the server too, not only in the tool.
    status_url = f"{server}/r/{rid}/api/threads/{tid}/status"
    before = counts()
    code, _, raw = http("POST", status_url, {"status": "resolved"},
                        headers={"Authorization": f"Bearer {board['commenter']['token']}"}, csrf=False)
    assert code == 403, raw
    assert json.loads(raw)["ok"] is False
    assert counts() == before, "a refused status write changed rows"
    assert thread_status(tid) == "open"


def test_only_a_reviewer_or_owner_bearer_resolves_and_reopens(board, server):
    rid, tid = board["review_id"], board["thread_id"]
    status_url = f"{server}/r/{rid}/api/threads/{tid}/status"
    for name in ("reviewer", "owner"):
        code, _, raw = http("POST", status_url, {"status": "resolved"},
                            headers={"Authorization": f"Bearer {board[name]['token']}"}, csrf=False)
        assert code == 200, (name, raw)
        assert json.loads(raw)["thread"]["status"] == "resolved"
        assert thread_status(tid) == "resolved"
        code, _, raw = http("POST", status_url, {"status": "open"},
                            headers={"Authorization": f"Bearer {board[name]['token']}"}, csrf=False)
        assert code == 200, (name, raw)
        assert json.loads(raw)["thread"]["status"] == "open"
        assert thread_status(tid) == "open"
    # A commenter's bearer cannot take either direction, including reopening a thread someone resolved.
    code, _, raw = http("POST", status_url, {"status": "resolved"},
                        headers={"Authorization": f"Bearer {board['reviewer']['token']}"}, csrf=False)
    assert code == 200, raw
    code, _, raw = http("POST", status_url, {"status": "open"},
                        headers={"Authorization": f"Bearer {board['commenter']['token']}"}, csrf=False)
    assert code == 403, raw
    assert thread_status(tid) == "resolved"


def test_a_commenter_session_cookie_still_resolves_under_the_board_rule(board, server):
    rid, tid = board["review_id"], board["thread_id"]
    url = f"{server}/r/{rid}/api/threads/{tid}/status"
    code, _, raw = http("POST", url, {"status": "resolved"}, signed_in(board["commenter"]), csrf=False)
    assert code == 200, raw
    assert json.loads(raw)["thread"]["status"] == "resolved"
    assert thread_status(tid) == "resolved"


def test_a_revoked_or_unknown_bearer_never_becomes_the_loopback_owner(board, server):
    rid, tid = board["review_id"], board["thread_id"]
    url = f"{server}/r/{rid}/api/threads/{tid}/reply"
    retired = token_for(board["reviewer"]["user_id"], "retired")
    users.revoke_token(board["reviewer"]["user_id"], retired["id"])
    before = counts()
    for bad in ("mrp_bogus", retired["token"]):
        code, _, raw = http("POST", url, {"text": "must not exist"},
                            headers={"Authorization": f"Bearer {bad}"}, csrf=False)
        assert code == 401, (code, raw)
        assert json.loads(raw)["ok"] is False
    assert counts() == before, "a dead bearer token created rows as the loopback owner"

    who = auth.identify(auth.Request(peer="100.64.0.9",
                                     headers={"Authorization": f"Bearer {board['reviewer']['token']}"},
                                     local=False))
    assert who.source == "token" and who.principal.kind == "anonymous" and who.user is None


# ---------------------------------------------------------------- the @Hermes loop guard through the server

def test_mcp_reply_through_the_owner_process_still_cannot_activate_hermes(board, server, monkeypatch, tmp_path):
    rid, tid = board["review_id"], board["thread_id"]
    published = []
    original = live.publish

    def spy(*args, **kwargs):
        published.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(live, "publish", spy)
    result = child_call(child_env(tmp_path, board["reviewer"]["token"]), "reply",
                        {"thread_id": tid, "text": "@Hermes please loop", "mentions": ["hermes", "@Hermes"]})
    assert result["isError"] is False, result
    body = payload(result)
    assert body["thread"]["activated"] is False
    assert body["comment"]["sent_to_hermes"] is False
    assert body["comment"]["mentions"] == []
    assert published == ["thread.replied"]
    rows = db.connect().execute("SELECT kind FROM mentions WHERE comment_id = ?",
                                (body["comment"]["id"],)).fetchall()
    assert rows == []
    stored = db.connect().execute("SELECT activated FROM threads WHERE id = ?", (tid,)).fetchone()
    assert stored["activated"] == 0


# ---------------------------------------------------------------- a stale descriptor is ignored

def test_a_stale_server_descriptor_does_not_stop_the_write(board):
    rid, tid = board["review_id"], board["thread_id"]
    root = config.data_root().resolve()
    port = dead_port()
    descriptor = {"api": 1, "service": "mock-review", "pid": os.getpid(), "base": f"http://127.0.0.1:{port}",
                  "host": "127.0.0.1", "port": port, "data_root": str(root), "started_at": time.time()}
    (root / runtime.NAME).write_text(json.dumps(descriptor), encoding="utf-8")
    result = call("reply", {"thread_id": tid, "text": "written without a server"}, board["reviewer"]["token"])
    assert result["isError"] is False, result
    body = payload(result)
    assert body["comment"]["text"] == "written without a server"
    stored = db.connect().execute("SELECT text FROM comments WHERE id = ?", (body["comment"]["id"],)).fetchone()
    assert stored["text"] == "written without a server"
    events = db.connect().execute("SELECT kind FROM events WHERE review_id = ? AND thread_id = ?",
                                  (rid, tid)).fetchall()
    assert "thread.replied" in [e["kind"] for e in events]


# ---------------------------------------------------------------- the token is scoped to the two MCP writes

def test_a_pat_bearer_is_refused_on_every_other_route(board, server):
    """The bearer an agent holds must not mint tokens, delete accounts or read the API: only the two MCP writes."""
    reviewer, owner = board["reviewer"], board["owner"]
    before = [t["name"] for t in users.list_tokens(reviewer["user_id"])]
    code, _, raw = http("POST", f"{server}/api/tokens", {"name": "minted-by-bearer"},
                        headers={"Authorization": f"Bearer {reviewer['token']}"}, csrf=False)
    assert code == 403, raw
    assert [t["name"] for t in users.list_tokens(reviewer["user_id"])] == before

    victim = board["stranger"]["user_id"]
    code, _, raw = http("POST", f"{server}/api/users/{victim}/delete", {},
                        headers={"Authorization": f"Bearer {owner['token']}"}, csrf=False)
    assert code == 403, raw
    assert users.get_user(victim) is not None

    code, _, raw = http("GET", f"{server}/api/owner",
                        headers={"Authorization": f"Bearer {reviewer['token']}"})
    assert code == 403, raw

    # The two routes the stdio server needs still take the token, CSRF-free: the guard is narrow, not a dead switch.
    rid, tid = board["review_id"], board["thread_id"]
    code, _, raw = http("POST", f"{server}/r/{rid}/api/threads/{tid}/reply", {"text": "still allowed"},
                        headers={"Authorization": f"Bearer {reviewer['token']}"}, csrf=False)
    assert code == 200, raw


def test_a_dead_bearer_never_falls_through_to_the_loopback_owner(board, server):
    """A bearer that names no live token is refused even where the loopback owner exists: never a silent downgrade."""
    code, _, raw = http("POST", f"{server}/api/tokens", {"name": "by-dead-bearer"},
                        headers={"Authorization": "Bearer mrp_bogus"}, csrf=False)
    assert code == 403, raw
    assert "by-dead-bearer" not in [t["name"] for t in users.list_tokens(users.owner_user()["id"])]


def test_the_mcp_write_routes_keep_working_with_the_loopback_owner_off(board, server, loopback_owner_off):
    """With the recommended hardening (no ambient owner) the token still authenticates its own two writes only."""
    rid, tid = board["review_id"], board["thread_id"]
    code, _, raw = http("POST", f"{server}/r/{rid}/api/threads/{tid}/reply", {"text": "agent only"},
                        headers={"Authorization": f"Bearer {board['commenter']['token']}"}, csrf=False)
    assert code == 200, raw
    assert json.loads(raw)["comment"]["author_name"] == "Cam Commenter"
    code, _, raw = http("POST", f"{server}/api/tokens", {"name": "no"},
                        headers={"Authorization": f"Bearer {board['owner']['token']}"}, csrf=False)
    assert code == 403, raw


def test_a_bearer_reply_cannot_activate_hermes(board, server):
    """The @Hermes loop guard is not only a client-side rule in the MCP tool: the server refuses activation to a token."""
    rid, tid = board["review_id"], board["thread_id"]
    code, _, raw = http("POST", f"{server}/r/{rid}/api/threads/{tid}/reply",
                        {"text": "@Hermes go", "mentions": ["hermes"]},
                        headers={"Authorization": f"Bearer {board['reviewer']['token']}"}, csrf=False)
    assert code == 200, raw
    body = json.loads(raw)
    assert body["thread"]["activated"] is False
    assert body["comment"]["sent_to_hermes"] is False
    stored = db.connect().execute("SELECT activated FROM threads WHERE id = ?", (tid,)).fetchone()
    assert stored["activated"] == 0, "a bearer reply marked the thread as a wake for Hermes"


# ---------------------------------------------------------------- the descriptor is a rendezvous, not a credential

class RecordingListener:
    """A loopback listener that claims to be the board for a store and records every request it is sent."""

    def __init__(self, root, port=0):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, code, body):
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                seen.append(("GET", self.path, self.headers.get("Authorization")))
                self._answer(200, {"ok": True, "service": "mock-review", "pid": os.getpid(),
                                   "data_root": str(root)})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                seen.append(("POST", self.path, self.headers.get("Authorization")))
                self._answer(500, {"ok": False, "error": "not the board"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def tokens_seen(self, token):
        return [row for row in self.seen if token in (row[2] or "")]


def _descriptor(root, port, secret=None, pid=None):
    body = {"api": 1, "service": "mock-review", "pid": os.getpid() if pid is None else pid,
            "base": f"http://127.0.0.1:{port}", "host": "127.0.0.1", "port": port,
            "data_root": str(root), "started_at": time.time()}
    if secret is not None:
        body["secret"] = secret
    return body


class ClosingListener:
    """A listener that answers the challenge the way a real board does, then closes the connection it answered on."""

    def __init__(self, root, secret, pid, port=0):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _answer(self, code, body, close=False):
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if close:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                seen.append(("GET", self.path, self.headers.get("Authorization")))
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                nonce = (query.get("nonce") or [""])[0]
                self._answer(200, {"ok": True, "service": runtime.SERVICE, "api": runtime.API, "pid": pid,
                                   "data_root": str(root), "nonce": nonce,
                                   "mac": runtime.mac(secret, nonce)}, close=True)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                seen.append(("POST", self.path, self.headers.get("Authorization")))
                self._answer(500, {"ok": False, "error": "not the board"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def tokens_seen(self, token):
        return [row for row in self.seen if token in (row[2] or "")]


def test_an_impostor_with_a_plausible_descriptor_never_receives_the_token(board):
    """A listener on the descriptor's port that echoes its store still cannot answer the secret challenge."""
    root = config.data_root().resolve()
    listener = RecordingListener(root)
    try:
        path = root / runtime.NAME
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_text(json.dumps(_descriptor(root, listener.port, secret=runtime.new_secret())), encoding="utf-8")
        os.chmod(path, 0o600)
        assert runtime.read() is not None, "the descriptor itself is plausible: only the challenge can refuse it"
        result = call("reply", {"thread_id": board["thread_id"], "text": "not for the impostor"},
                      board["reviewer"]["token"])
        assert result["isError"] is False, result
        assert payload(result)["delivery"] == "stored"
        assert listener.tokens_seen(board["reviewer"]["token"]) == []
    finally:
        listener.close()


@pytest.mark.skipif(os.name != "posix", reason="the ownership and mode checks are POSIX-only")
def test_a_descriptor_other_accounts_could_write_or_read_is_never_trusted(board):
    """A symlinked or world-readable descriptor is not this account's rendezvous, so it is ignored before any check."""
    root = config.data_root().resolve()
    listener = RecordingListener(root)
    try:
        link = root / runtime.NAME
        if link.exists() or link.is_symlink():
            link.unlink()
        planted = root.parent / "planted.json"
        planted.write_text(json.dumps(_descriptor(root, listener.port, secret=runtime.new_secret())), encoding="utf-8")
        os.chmod(planted, 0o666)
        link.symlink_to(planted)
        assert runtime.read() is None and runtime.base() is None
        link.unlink()

        link.write_text(json.dumps(_descriptor(root, listener.port, secret=runtime.new_secret())), encoding="utf-8")
        os.chmod(link, 0o644)
        assert runtime.read() is None, "a group- or world-readable descriptor is not trusted"

        result = call("reply", {"thread_id": board["thread_id"], "text": "no token for a planted file"},
                      board["reviewer"]["token"])
        assert result["isError"] is False, result
        assert payload(result)["delivery"] == "stored"
        assert listener.tokens_seen(board["reviewer"]["token"]) == []
    finally:
        listener.close()


def test_a_crashed_server_never_hands_its_token_to_whoever_binds_the_port(board, tmp_path):
    """A real server, SIGKILLed, leaves its descriptor behind; a listener that then binds the port still gets nothing."""
    root = config.data_root().resolve()
    env = child_env(tmp_path, board["reviewer"]["token"])
    port = dead_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "bin" / "mockreview"), "serve", "--host", "127.0.0.1",
                             "--port", str(port), "--no-worker"], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    listener = None
    server_pid = None
    try:
        deadline = time.time() + 20
        info = runtime.read(root)
        while time.time() < deadline and not (info and info.get("port") == port
                                              and runtime.verify(info.get("base"), root)):
            time.sleep(0.1)
            info = runtime.read(root)
        assert info and info.get("port") == port, info
        server_pid = info.get("pid")
        assert isinstance(server_pid, int) and not isinstance(server_pid, bool), info
        assert runtime.verify(info.get("base"), root), info

        os.kill(server_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        proc.wait(10)
        deadline = time.time() + 10
        while wakeguard._alive(server_pid) and time.time() < deadline:
            time.sleep(0.05)
        assert not wakeguard._alive(server_pid), "the killed server is still there: the port would still be its own"
        assert (root / runtime.NAME).exists(), "a crash leaves the descriptor behind: that is the case to survive"

        listener = RecordingListener(root, port=port)
        result = call("reply", {"thread_id": board["thread_id"], "text": "after the crash"},
                      board["reviewer"]["token"])
        assert result["isError"] is False, result
        assert payload(result)["delivery"] == "stored"
        assert listener.tokens_seen(board["reviewer"]["token"]) == []
    finally:
        if server_pid is not None and wakeguard._alive(server_pid):
            os.kill(server_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        if listener is not None:
            listener.close()


def test_a_listener_swapped_in_after_the_proof_never_receives_the_token(board, monkeypatch):
    """A real board answers the challenge, then a recording listener takes its port: the token must not follow. The
    proof and the write share one connection, so a proof that no longer holds leaves the write stored."""
    from mockreview import server as srv

    root = config.data_root().resolve()
    httpd = srv.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]
    info = runtime.read(root)
    real_verify = runtime.verify
    state = {"listener": None, "stopped": False}

    def verify_then_swap(base, root=None, timeout=1.5):
        ok = real_verify(base, root, timeout)
        if ok and state["listener"] is None:
            httpd.shutdown()
            httpd.server_close()
            state["stopped"] = True
            state["listener"] = RecordingListener(config.data_root().resolve(), port=port)
            path = config.data_root().resolve() / runtime.NAME
            path.write_text(json.dumps(_descriptor(config.data_root().resolve(), port,
                                                   secret=info["secret"], pid=info["pid"])),
                            encoding="utf-8")
            os.chmod(path, 0o600)
        return ok

    monkeypatch.setattr(runtime, "verify", verify_then_swap)
    marker = "swapped in after the proof"
    try:
        assert info and info.get("port") == port, info
        result = call("reply", {"thread_id": board["thread_id"], "text": marker}, board["reviewer"]["token"])
        assert result["isError"] is False, result
        assert payload(result)["delivery"] == "stored"
        assert state["listener"].tokens_seen(board["reviewer"]["token"]) == []
        rows = db.connect().execute("SELECT COUNT(*) AS n FROM comments WHERE text = ?", (marker,)).fetchone()["n"]
        assert rows == 1
    finally:
        if state["listener"] is not None:
            state["listener"].close()
        if not state["stopped"]:
            httpd.shutdown()
            httpd.server_close()


def test_a_proven_connection_that_closes_before_the_write_is_stored_not_retried(board):
    """The listener answered the challenge and then closed that connection: the bearer must not follow on a new one."""
    root = config.data_root().resolve()
    secret = runtime.new_secret()
    listener = ClosingListener(root, secret, os.getpid())
    try:
        path = root / runtime.NAME
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_text(json.dumps(_descriptor(root, listener.port, secret=secret, pid=os.getpid())),
                        encoding="utf-8")
        os.chmod(path, 0o600)
        assert runtime.read() is not None
        marker = "the proof connection closed"
        result = call("reply", {"thread_id": board["thread_id"], "text": marker}, board["reviewer"]["token"])
        assert result["isError"] is False, result
        assert payload(result)["delivery"] == "stored"
        assert listener.tokens_seen(board["reviewer"]["token"]) == []
        rows = db.connect().execute("SELECT COUNT(*) AS n FROM comments WHERE text = ?", (marker,)).fetchone()["n"]
        assert rows == 1
    finally:
        listener.close()


class RedirectingListener:
    """A listener that answers the challenge like a real board, keeps the connection open for the write, and answers
    that write with a redirect whose body looks exactly like a successful live write."""

    def __init__(self, root, secret, pid, thread_id, port=0):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _answer(self, code, body, headers=()):
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                seen.append(("GET", self.path, self.headers.get("Authorization")))
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                nonce = (query.get("nonce") or [""])[0]
                self._answer(200, {"ok": True, "service": runtime.SERVICE, "api": runtime.API, "pid": pid,
                                   "data_root": str(root), "nonce": nonce, "mac": runtime.mac(secret, nonce)})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                seen.append(("POST", self.path, self.headers.get("Authorization")))
                self._answer(302, {"ok": True, "thread": {"id": thread_id, "comments": [{"id": "c-redirected"}]},
                                   "comment": {"id": "c-redirected"}}, headers=(("Location", "/elsewhere"),))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def tokens_seen(self, token):
        return [row for row in self.seen if token in (row[2] or "")]


def test_a_redirect_with_a_success_shaped_body_is_not_a_live_write(board):
    """A 302 whose body is shaped like a successful write is still not a write: the tool fails, it never claims live."""
    root = config.data_root().resolve()
    secret = runtime.new_secret()
    tid = board["thread_id"]
    before = counts()
    listener = RedirectingListener(root, secret, os.getpid(), tid)
    try:
        path = root / runtime.NAME
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_text(json.dumps(_descriptor(root, listener.port, secret=secret, pid=os.getpid())),
                        encoding="utf-8")
        os.chmod(path, 0o600)
        assert runtime.read() is not None
        result = call("reply", {"thread_id": tid, "text": "a redirect is not a live write"},
                      board["reviewer"]["token"])
        answer = payload(result)
        assert result["isError"] is True, (
            "the tool reported a live write for a 302 answer: delivery=%r" % (answer.get("delivery"),))
        assert listener.tokens_seen(board["reviewer"]["token"]), (
            "the write was never attempted, so this test would pass for the wrong reason: %r" % (listener.seen,))
        assert counts() == before, "a redirected write must not create a comment or an audit row"
    finally:
        listener.close()


# ---------------------------------------------------------------- a live write and a stored write are not the same success

def test_the_tools_report_whether_the_write_went_live(board, server, tmp_path):
    """`delivery` says which happened: the running board took the write, or it was only saved for the next load."""
    rid, tid = board["review_id"], board["thread_id"]
    assert payload(call("reply", {"thread_id": tid, "text": "into the running board"},
                        board["reviewer"]["token"]))["delivery"] == "live"
    assert payload(call("resolve", {"thread_id": tid}, board["reviewer"]["token"]))["delivery"] == "live"

    (config.data_root().resolve() / runtime.NAME).unlink()
    reopened = call("resolve", {"thread_id": tid, "reopen": True}, board["reviewer"]["token"])
    assert payload(reopened)["delivery"] == "stored"
    stored = child_call(child_env(tmp_path, board["reviewer"]["token"]), "reply",
                        {"thread_id": tid, "text": "with no board listening"})
    assert payload(stored)["delivery"] == "stored"
