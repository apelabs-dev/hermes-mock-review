"""A mention wake still queued when Hermes answers its thread (or the thread is resolved) is dropped, never sent.

Delivery is asserted on what the gateway receiver got (`env["receiver"].wakes`), not on a status flag alone.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from authkit import account, signed_in
from conftest import http
from mockreview import notify, service, store, threads
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


def _deliver_only_first(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes first", "c1")
    code, _, out = post(server, rid, "@Hermes second", idem="c2", thread_id=tid)
    assert code == 200, out
    second = json.loads(out)["comment"]["id"]
    assert _statuses(rid) == ["pending_notify", "pending_notify"]

    # Due wakes normally coalesce. Put the second on a retry delay so only the first is delivered.
    with store.locked(rid):
        data = store.load(rid)
        data["thread_wakes"][second]["next_attempt"] = 1e12
        store.save(rid, data)
    tick()
    first, = bodies(env)
    assert [m["text"] for m in first["mentions"]] == ["@Hermes first"]
    with store.locked(rid):
        data = store.load(rid)
        data["thread_wakes"][second]["next_attempt"] = 0.0
        store.save(rid, data)
    return rid, tid, first, second


def test_a_named_reply_drops_a_queued_wake_for_a_comment_present_at_delivery(env, server, tmp_path):
    rid, tid, first, second = _deliver_only_first(env, server, tmp_path)

    replied = call_tool(plugin_tools(), "mock_review_reply",
                        {"thread_id": tid, "text": "Both answered.", "request_id": first["request_id"]})
    assert "error" not in replied, replied
    tick()
    tick()
    assert [b["text"] for b in bodies(env)] == ["@Hermes first"]
    requests = pin_requests(rid)
    assert requests[first["request_id"]]["status"] == "done"
    dropped = next(q for q in requests.values() if q["comment_id"] == second)
    assert dropped["status"] == "done" and dropped["detail"] == "answered before delivery"


def test_a_named_reply_leaves_the_queue_without_evidence_of_visible_comments(env, server, tmp_path):
    rid, tid, first, _ = _deliver_only_first(env, server, tmp_path)
    with store.locked(rid):
        data = store.load(rid)
        wake = data["thread_wakes"][first["comment_id"]]
        # Legacy wakes may have an acknowledgement time, but no record of what the agent could see.
        wake.pop("comments_before_send", None)
        wake.pop("delivered_at", None)
        store.save(rid, data)

    replied = call_tool(plugin_tools(), "mock_review_reply",
                        {"thread_id": tid, "text": "First answered.", "request_id": first["request_id"]})
    assert "error" not in replied, replied
    assert pin_requests(rid)[first["request_id"]]["status"] == "done"
    tick()
    assert [b["text"] for b in bodies(env)] == ["@Hermes first", "@Hermes second"]


def test_a_named_reply_leaves_a_comment_posted_later_in_the_same_second(env, server, tmp_path, monkeypatch):
    stamp = store.now_iso()
    monkeypatch.setattr(store, "now_iso", lambda: stamp)
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes first", "c1")
    tick()
    first, = bodies(env)
    code, _, out = post(server, rid, "@Hermes second", idem="c2", thread_id=tid)
    assert code == 200, out

    replied = call_tool(plugin_tools(), "mock_review_reply",
                        {"thread_id": tid, "text": "First answered.", "request_id": first["request_id"]})
    assert "error" not in replied, replied
    tick()
    assert [b["text"] for b in bodies(env)] == ["@Hermes first", "@Hermes second"]


def _thread_texts(rid, tid):
    listed = call_tool(plugin_tools(), "mock_review_list_comments", {"review_id": rid})
    thread = next(t for t in listed["threads"] if t["id"] == tid)
    return [c["text"] for c in thread["comments"]]


def test_a_named_reply_keeps_a_wake_for_a_comment_not_yet_committed(env, server, tmp_path, monkeypatch):
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes first", "c1")
    stamped, resume = Event(), Event()
    now = threads.now

    def pause_before_write():
        stamp = now()
        stamped.set()
        assert resume.wait(5)
        return stamp

    with ThreadPoolExecutor(max_workers=1) as pool, monkeypatch.context() as patch:
        patch.setattr(threads, "now", pause_before_write)
        second = pool.submit(post, server, rid, "@Hermes second", idem="c2", thread_id=tid)
        try:
            assert stamped.wait(5)
            tick()
            first, = bodies(env)
            assert _thread_texts(rid, tid) == ["@Hermes first"]
        finally:
            resume.set()
        code, _, out = second.result(timeout=5)
        assert code == 200, out

    replied = call_tool(plugin_tools(), "mock_review_reply",
                        {"thread_id": tid, "text": "First answered.", "request_id": first["request_id"]})
    assert "error" not in replied, replied
    tick()
    tick()
    assert [b["text"] for b in bodies(env)] == ["@Hermes first", "@Hermes second"]


@pytest.mark.parametrize("lose_ack", [False, True], ids=["delayed-ack", "duplicate-ack"])
def test_a_named_reply_keeps_a_wake_for_a_comment_posted_after_receipt(env, server, tmp_path, monkeypatch, lose_ack):
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes first", "c1")
    request = notify._request
    first_attempts = []

    def intercept_ack(url, payload, secret, delivery_id, timeout):
        code, text = request(url, payload, secret, delivery_id, timeout)
        if payload["text"] == "@Hermes first":
            first_attempts.append(delivery_id)
            if len(first_attempts) == 1:
                assert code == 202
                assert _thread_texts(rid, tid) == ["@Hermes first"]
                result, _, out = post(server, rid, "@Hermes second", idem="c2", thread_id=tid)
                assert result == 200, out
                if lose_ack:
                    raise TimeoutError("lost the acknowledgement")
            else:
                assert lose_ack and first_attempts == [delivery_id, delivery_id]
                return 200, '{"status": "duplicate"}'
        return code, text

    monkeypatch.setattr(notify, "_request", intercept_ack)
    tick()
    first, = bodies(env)
    if lose_ack:
        # Retry only the first delivery with the same id, as a deduplicating gateway would see it.
        assert service._deliver_thread_wakes(rid, [first["comment_id"]]) == (True, "HTTP 200 duplicate")
    replied = call_tool(plugin_tools(), "mock_review_reply",
                        {"thread_id": tid, "text": "First answered.", "request_id": first["request_id"]})
    assert "error" not in replied, replied
    tick()
    tick()
    expected = ["@Hermes first"] * (2 if lose_ack else 1) + ["@Hermes second"]
    assert [b["text"] for b in bodies(env)] == expected


def test_a_reviewers_resolve_leaves_the_owners_queued_wake_alone(env, server, tmp_path):
    """A resolve by a reviewer is theirs: it does not cancel a wake the owner queued for that thread. The owner's own
    resolve still covers it (test above)."""
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes is this right?", "c1")
    reviewer = account(review_id=rid, role="reviewer", name="Rae")

    code, _, out = http("POST", f"{server}/r/{rid}/api/threads/{tid}/status", {"status": "resolved"}, signed_in(reviewer))

    assert code == 200, out
    assert _statuses(rid) == ["pending_notify"], "a reviewer's resolve cancelled the owner's wake"
    tick()
    assert len(env["receiver"].wakes) == 1


def test_the_owners_resolve_over_http_drops_the_queued_wake(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _open(server, rid, "@Hermes is this right?", "c1")

    code, _, out = http("POST", f"{server}/r/{rid}/api/threads/{tid}/status", {"status": "resolved"})

    assert code == 200, out
    assert _statuses(rid) == ["answered"]
    tick()
    assert env["receiver"].wakes == []


def test_a_round_pinned_viewers_activity_carries_no_global_thread_number(env, server, tmp_path):
    """The live stream strips `n` for a round-pinned viewer; the persisted activity must project the same way."""
    from mockreview import users
    rid = board(tmp_path)["id"]
    _open(server, rid, "@Hermes hello", "c1")
    actor = f"user:{users.owner_user()['id']}"
    pinned = threads.Scope(viewed=1, rounds=frozenset({1}), pinned=1)

    full = [e for e in threads.activity(rid, actor) if isinstance(e["data"].get("thread"), dict)]
    narrow = [e for e in threads.activity(rid, actor, scope=pinned) if isinstance(e["data"].get("thread"), dict)]

    assert full and "n" in full[0]["data"]["thread"], "the unrestricted feed lost its thread number"
    assert narrow and all("n" not in e["data"]["thread"] for e in narrow), "a pinned viewer's feed carries n"
