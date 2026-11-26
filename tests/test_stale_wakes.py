"""A mention wake still queued when Hermes answers its thread (or the thread is resolved) is dropped, never sent.

Delivery is asserted on what the gateway receiver got (`env["receiver"].wakes`), not on a status flag alone.
"""

import json

from mockreview import service, threads
from test_mention_wake import (bodies, board, call_tool, pin_requests, plugin_tools, post, tick,  # noqa: F401
                               wake_records)


def _open(server, rid, text, idem):
    code, _, out = post(server, rid, text, idem=idem)
    assert code == 201, out
    return json.loads(out)["thread"]["id"]


def _statuses(rid):
    return sorted(w["status"] for w in wake_records(rid).values())


def test_a_hermes_reply_drops_the_queued_wake_and_nothing_is_sent(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes is this right?", "c1")
    assert env["receiver"].wakes == []
    assert _statuses(rid) == ["pending_notify"]

    replied = call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "Yes, it is."})
    assert "error" not in replied, replied

    assert _statuses(rid) == ["answered"]
    req = next(iter(pin_requests(rid).values()))
    assert req["status"] == "done" and req["detail"] == "answered before delivery"
    tick()
    tick()
    assert env["receiver"].wakes == []


def test_resolving_the_thread_drops_the_queued_wake_and_nothing_is_sent(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes is this right?", "c1")

    threads.set_status(rid, tid, "resolved", "user:someone")

    assert _statuses(rid) == ["answered"]
    req = next(iter(pin_requests(rid).values()))
    assert req["status"] == "done" and req["detail"] == "answered before delivery"
    tick()
    assert env["receiver"].wakes == []


def test_hermes_resolving_drops_the_queued_wake(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes is this right?", "c1")

    resolved = call_tool(plugin_tools(), "mock_review_resolve", {"thread_id": tid})
    assert "error" not in resolved, resolved

    assert _statuses(rid) == ["answered"]
    tick()
    assert env["receiver"].wakes == []


def test_a_delivered_wake_is_untouched_and_only_the_queued_one_is_dropped(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes first", "c1")
    tick()
    assert len(bodies(env)) == 1
    code, _, out = post(server, rid, "@Hermes second", idem="c2", thread_id=tid)
    assert code == 200, out
    ids = {q["text"]: q["comment_id"] for q in pin_requests(rid).values()}
    first, second = ids["@Hermes first"], ids["@Hermes second"]
    assert wake_records(rid)[first]["status"] == "notified"
    assert wake_records(rid)[second]["status"] == "pending_notify"
    delivered = dict(wake_records(rid)[first])

    threads.reply_comment(rid, tid, threads.AGENT_ACTOR, threads.AGENT_NAME, "agent", "Done.", can_activate=False)

    assert wake_records(rid)[first] == delivered
    assert wake_records(rid)[second]["status"] == "answered"
    statuses = {q["comment_id"]: q["status"] for q in pin_requests(rid).values()}
    assert statuses == {first: "woken", second: "done"}
    tick()
    assert [b["text"] for b in bodies(env)] == ["@Hermes first"]


def test_a_queued_wake_on_another_thread_survives(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    answered = _open(server, rid, "@Hermes about this", "c1")
    other = _open(server, rid, "@Hermes about that", "c2")

    threads.reply_comment(rid, answered, threads.AGENT_ACTOR, threads.AGENT_NAME, "agent", "Done.", can_activate=False)
    threads.set_status(rid, answered, "resolved", "user:someone")

    by_thread = {w["thread_id"]: w["status"] for w in wake_records(rid).values()}
    assert by_thread == {answered: "answered", other: "pending_notify"}
    tick()
    sent = bodies(env)
    assert len(sent) == 1 and sent[0]["review_thread_id"] == other and sent[0]["text"] == "@Hermes about that"


def test_a_human_reply_drops_nothing(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes is this right?", "c1")

    code, _, out = post(server, rid, "also, thanks", mentions=(), idem="r1", thread_id=tid)
    assert code == 200, out
    threads.reply_comment(rid, tid, "user:someone", "Someone", "owner", "one more thing", can_activate=False)
    threads.set_status(rid, tid, "open", "user:someone")   # no change, not a resolve

    assert _statuses(rid) == ["pending_notify"]
    tick()
    assert len(bodies(env)) == 1


def test_an_answer_naming_its_request_leaves_a_later_queued_wake(env, server, tmp_path):
    """The tool's `request_id` says which request the answer settles; the others stay queued."""
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes first", "c1")
    tick()
    code, _, out = post(server, rid, "@Hermes second", idem="c2", thread_id=tid)
    assert code == 200, out
    first = next(q["id"] for q in pin_requests(rid).values() if q["status"] == "woken")

    call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "First answered.", "request_id": first})

    assert sorted(w["status"] for w in wake_records(rid).values()) == ["notified", "pending_notify"]
    assert service.retry_thread_wakes(rid, now=1e12) == 1
    assert [b["text"] for b in bodies(env)] == ["@Hermes first", "@Hermes second"]
