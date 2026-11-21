"""@Hermes wakes: one queued wake per activating comment, delivered by the worker tick, and none for a guest, a token
or a wake-off board.

An activating comment is one that sets `activated`: the author's role carries `submit`, they are not a share-link
guest and not a token, and the board was created with wake-up on. The POST only records the wake; the 5 s worker tick
coalesces the due ones into one signed request that carries the review thread, the comments and their blocks. Every
delivered mention also opens a pin request on the anchored block, which is the board's "working" chip.
"""

import json
import sqlite3

from authkit import signed_in, token_of
from conftest import http, load_plugin
from mockreview import api, db, service, store, threads, users
from test_plugin import FakeCtx

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
DECISIONS = {"blocks": [{"type": "mock", "id": "m1", "html": "<p>today</p>"}]}
DEFAULT_LOOK = {"pin_fix": True, "pin_fix_min_seconds": 0.0, "pin_fix_window_seconds": 0.0}


def _decisions(tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    return path


def board(tmp_path, wake=True, look=None):
    merged = dict(DEFAULT_LOOK)
    merged.update(look or {})
    return store.create_review("Mention board", "brief", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(_decisions(tmp_path))), wake=wake, look=merged)


def post(server, rid, text, mentions=("hermes",), anchor="default", idem="c1", thread_id=None):
    body = {"text": text, "mentions": list(mentions), "idem": idem}
    if anchor != "default":
        if anchor is not None:
            body["anchor"] = anchor
    elif thread_id is None:
        body["anchor"] = {"block": "m1", "x": 0.2, "y": 0.3}
    path = "threads" if thread_id is None else f"threads/{thread_id}/reply"
    return http("POST", f"{server}/r/{rid}/api/{path}", body)


def tick():
    """One worker pass: this is what delivers (and coalesces) the queued mention wakes."""
    service.retry_pending()


def bodies(env):
    return [json.loads(w["body"]) for w in env["receiver"].wakes]


def wake_records(rid):
    return store.load(rid).get("thread_wakes") or {}


def pin_requests(rid):
    return store.load(rid)["rounds"][-1].get("pin_requests") or {}


def plugin_tools():
    mod = load_plugin()
    ctx = FakeCtx()
    mod.register(ctx)
    return {name: tool["handler"] for name, tool in ctx.tools.items()}


def call_tool(tools, name, args):
    return json.loads(tools[name](args))


# ---------------------------------------------------------------- one wake per comment


def test_an_owner_mention_queues_one_wake_with_the_thread_comment_and_block(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes make this clearer", idem="c1")
    assert code == 201, out
    result = json.loads(out)
    thread = result["thread"]
    assert result["wake"]["status"] == "pending_notify", result["wake"]
    assert env["receiver"].wakes == []                       # the POST does not call the gateway

    tick()
    wakes = bodies(env)
    assert len(wakes) == 1
    body = wakes[0]
    assert body["event_type"] == "review.thread_mention"
    assert body["review_id"] == rid and body["round"] == 1
    assert body["review_thread_id"] == thread["id"]
    assert body["comment_id"] == thread["comments"][0]["id"]
    assert body["text"] == "@Hermes make this clearer"
    assert body["anchor"]["block"] == "m1"
    assert body["mentions"][0]["comment_id"] == thread["comments"][0]["id"]

    # The same idem is the same comment: replaying the create wakes nobody again.
    code, _, out = post(server, rid, "@Hermes make this clearer", idem="c1")
    assert code == 201, out
    assert json.loads(out)["wake"].get("duplicate") is True
    tick()
    assert len(env["receiver"].wakes) == 1

    # A plain reply to an activated thread is not a new mention.
    code, _, out = post(server, rid, "thanks", mentions=(), idem="r0", thread_id=thread["id"])
    assert code == 200, out
    tick()
    assert len(env["receiver"].wakes) == 1


def test_a_reply_mention_wakes_once_more_and_its_idem_replays(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes first", idem="c1")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    tick()

    code, _, out = post(server, rid, "@Hermes and here too", idem="r1", thread_id=tid)
    assert code == 200, out
    assert json.loads(out)["wake"]["status"] == "pending_notify"
    tick()
    wakes = bodies(env)
    assert len(wakes) == 2
    assert wakes[1]["review_thread_id"] == tid
    assert wakes[1]["comment_id"] == json.loads(out)["comment"]["id"]
    assert wakes[1]["text"] == "@Hermes and here too"

    code, _, out = post(server, rid, "@Hermes and here too", idem="r1", thread_id=tid)
    assert code == 200, out
    assert json.loads(out)["wake"].get("duplicate") is True
    tick()
    assert len(env["receiver"].wakes) == 2


def test_the_hourly_cap_stores_the_comment_but_stops_waking(env, server, tmp_path):
    rid = board(tmp_path, look={"pin_fix_max_per_hour": 1})["id"]
    code, _, out = post(server, rid, "@Hermes one", idem="a")
    assert code == 201, out
    assert json.loads(out)["wake"]["status"] == "pending_notify"
    tick()
    assert len(env["receiver"].wakes) == 1

    code, _, out = post(server, rid, "@Hermes two", idem="b")
    assert code == 201, out
    result = json.loads(out)
    assert result["wake"]["status"] == "capped", result["wake"]
    assert result["thread"]["comments"][0]["text"] == "@Hermes two"
    tick()
    assert len(env["receiver"].wakes) == 1


def test_the_per_round_cap_caps_the_second_mention_and_keeps_its_comment(env, server, tmp_path):
    rid = board(tmp_path, look={"pin_fix_max_per_round": 1})["id"]
    code, _, out = post(server, rid, "@Hermes one", idem="a")
    assert code == 201, out
    tick()
    assert len(env["receiver"].wakes) == 1

    code, _, out = post(server, rid, "@Hermes two", idem="b")
    assert code == 201, out
    result = json.loads(out)
    assert result["wake"]["status"] == "capped", result["wake"]
    assert result["thread"]["comments"][0]["text"] == "@Hermes two"
    records = wake_records(rid)
    assert records[result["thread"]["comments"][0]["id"]]["status"] == "capped"
    tick()
    assert len(env["receiver"].wakes) == 1


# ---------------------------------------------------------------- delivery: retry, coalescing, bounded file


def test_a_transient_failure_stays_pending_and_the_next_tick_delivers_it_once(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes retry me", idem="c1")
    assert code == 201, out
    comment_id = json.loads(out)["thread"]["comments"][0]["id"]

    env["receiver"].status = 502
    tick()
    record = wake_records(rid)[comment_id]
    assert record["status"] == "pending_notify" and record["text"] == "@Hermes retry me"
    assert len(env["receiver"].wakes) == 1                 # the failed attempt reached the gateway

    env["receiver"].status = 202
    tick()
    assert wake_records(rid)[comment_id]["status"] == "notified"
    assert len(env["receiver"].wakes) == 2
    assert bodies(env)[1]["text"] == "@Hermes retry me"    # the retry still carries the text

    tick()
    assert len(env["receiver"].wakes) == 2                 # terminal: never sent twice


def test_a_burst_of_mentions_is_one_wake_carrying_every_entry(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    thread_ids = []
    for i in range(3):
        code, _, out = post(server, rid, f"@Hermes note {i}", idem=f"c{i}")
        assert code == 201, out
        thread_ids.append(json.loads(out)["thread"]["comments"][0]["id"])

    tick()
    wakes = bodies(env)
    assert len(wakes) == 1
    body = wakes[0]
    assert [m["comment_id"] for m in body["mentions"]] == thread_ids
    assert body["comment_id"] == thread_ids[0] and body["text"] == "@Hermes note 0"
    assert len(body["mentions"]) == 3
    records = wake_records(rid)
    assert all(records[cid]["status"] == "notified" for cid in thread_ids)
    assert len({records[cid]["delivery_id"] for cid in thread_ids}) == 1
    tick()
    assert len(env["receiver"].wakes) == 1


def test_terminal_records_lose_their_text_and_stay_until_they_cannot_count(env, tmp_path):
    rid = board(tmp_path, look={"pin_fix": False})["id"]
    owner = users.owner_user()
    actor = f"user:{owner['id']}"
    thread = {"id": "t" + "0" * 31, "round": 1, "anchor": {"block": "m1"}, "comments": []}
    comment_ids = [f"c{i:02d}" for i in range(22)]
    conn = db.connect()
    conn.execute("INSERT INTO threads (id, review_id, round, anchor, status, activated, carried, created_by, "
                 "created_at, updated_at) VALUES (?, ?, ?, ?, 'open', 1, 0, ?, 1.0, 1.0)",
                 (thread["id"], rid, 1, json.dumps(thread["anchor"]), actor))
    for cid in comment_ids:
        conn.execute("INSERT INTO comments (id, thread_id, author, author_name, text, idem, created_at) "
                     "VALUES (?, ?, ?, ?, ?, '', 1.0)", (cid, thread["id"], actor, "Owner", f"@Hermes {cid}"))
        thread["comments"] = [{"id": cid, "text": f"@Hermes {cid}", "author": actor,
                               "author_name": "Owner", "sent_to_hermes": True}]
        result = service.wake_thread_mention(rid, thread, cid)
        assert result["status"] == "pending_notify", result

    assert len(wake_records(rid)) == 22                    # pending records keep their text for the retry
    assert wake_records(rid)[comment_ids[0]]["text"] == "@Hermes c00"
    tick()

    records = wake_records(rid)
    assert len(records) == 22                              # newest-round records stay: the per-round cap counts them
    assert set(records) == set(comment_ids)
    assert all("text" not in rec and "anchor" not in rec for rec in records.values())
    assert all(rec["status"] == "notified" for rec in records.values())
    assert len(env["receiver"].wakes) == 1
    assert len(bodies(env)[0]["mentions"]) == 22

    # Age every record past the hour and move it off the newest round: only then may it be pruned.
    old = "2000-01-01T00:00:00Z"
    data = store.load(rid)
    for cid in comment_ids[:-1]:
        data["thread_wakes"][cid].update(at=old, round=0)
    data["thread_wakes"][comment_ids[-1]]["at"] = old      # old but on the newest round: it stays
    store.save(rid, data)
    tick()
    assert set(wake_records(rid)) == {comment_ids[-1]}
    assert set(store.load(rid)["thread_wake_seen"]) == set(comment_ids)   # idempotency survives pruning

    data = store.load(rid)
    data["thread_wakes"][comment_ids[-1]]["round"] = 0
    store.save(rid, data)
    tick()
    assert wake_records(rid) == {}
    assert len(env["receiver"].wakes) == 1


def test_the_cap_of_twenty_five_stops_at_twenty_five_across_ticks(env, server, tmp_path):
    rid = board(tmp_path, look={"pin_fix_max_per_round": 25, "pin_fix_max_per_hour": 100})["id"]
    for i in range(40):
        if i % 10 == 0:
            threads.reset_limits()      # the per-actor write throttle is not what this test measures
        code, _, out = post(server, rid, f"@Hermes note {i}", idem=f"c{i}")
        assert code == 201, out
        tick()

    statuses = [rec["status"] for rec in wake_records(rid).values()]
    assert statuses.count("notified") == 25
    assert statuses.count("capped") == 15
    assert len(env["receiver"].wakes) == 25


def test_an_idem_replay_never_wakes_after_its_record_was_pruned(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    first_idem, first_text = "c0", "@Hermes note 0"
    first_comment = None
    for i in range(22):
        if i % 10 == 0:
            threads.reset_limits()      # the per-actor write throttle is not what this test measures
        code, _, out = post(server, rid, f"@Hermes note {i}", idem=f"c{i}")
        assert code == 201, out
        if i == 0:
            first_comment = json.loads(out)["thread"]["comments"][0]["id"]
        tick()
    assert len(env["receiver"].wakes) == 22
    before = len(wake_records(rid))

    threads.reset_limits()
    code, _, out = post(server, rid, first_text, idem=first_idem)
    assert code == 201, out
    assert json.loads(out)["wake"].get("duplicate") is True
    tick()
    assert len(env["receiver"].wakes) == 22
    assert len(wake_records(rid)) == before

    # Simulate the prune: the record is gone, and the seen map alone must keep the replay a duplicate.
    data = store.load(rid)
    data["thread_wakes"].pop(first_comment)
    store.save(rid, data)
    assert first_comment not in wake_records(rid)
    code, _, out = post(server, rid, first_text, idem=first_idem)
    assert code == 201, out
    result = json.loads(out)
    assert result["wake"].get("duplicate") is True
    assert result["wake"]["status"] == "duplicate"
    tick()
    assert len(env["receiver"].wakes) == 22
    assert len(wake_records(rid)) == before - 1
    assert not any(rec["status"] == "pending_notify" for rec in wake_records(rid).values())


# ---------------------------------------------------------------- the block's working chip


def test_mention_opens_a_working_chip_and_patch_block_closes_it(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes make this clearer", idem="c1")
    assert code == 201, out
    tick()

    requests = pin_requests(rid)
    assert len(requests) == 1
    req = next(iter(requests.values()))
    assert req["source"] == "mention" and req["status"] == "woken"
    assert req["block"] == "m1" and req["comment_id"] == json.loads(out)["thread"]["comments"][0]["id"]
    state = store.public_state(store.load(rid))["rounds"][0]["pin_requests"][req["id"]]
    assert state["status"] == "working"
    assert bodies(env)[0]["request_id"] == req["id"]

    tools = plugin_tools()
    patched = call_tool(tools, "mock_review_patch_block",
                        {"review_id": rid, "block": "m1", "html": "<p>clearer</p>", "round": 1,
                         "if_rev": 0, "request_ids": [req["id"]]})
    assert "error" not in patched, patched
    assert pin_requests(rid)[req["id"]]["status"] == "done"
    state = store.public_state(store.load(rid))["rounds"][0]["pin_requests"][req["id"]]
    assert state["status"] == "done"


def test_patch_then_reply_leaves_the_chip_done_with_the_patch_detail(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes make this clearer", idem="c1")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    tick()
    req = next(iter(pin_requests(rid).values()))
    assert req["status"] == "woken"

    tools = plugin_tools()
    patched = call_tool(tools, "mock_review_patch_block",
                        {"review_id": rid, "block": "m1", "html": "<p>clearer</p>", "round": 1,
                         "if_rev": 0, "request_ids": [req["id"]]})
    assert "error" not in patched, patched
    assert patched["done"] == [req["id"]]
    done = pin_requests(rid)[req["id"]]
    assert done["status"] == "done"
    assert done["detail"].startswith("patched in round 1")

    replied = call_tool(tools, "mock_review_reply",
                        {"thread_id": tid, "text": "Patched it.", "request_id": req["id"]})
    assert "error" not in replied, replied
    assert replied["closed"] == 0
    final = pin_requests(rid)[req["id"]]
    assert final["status"] == "done" and final["detail"] == done["detail"]


def test_mention_opens_a_working_chip_and_a_plugin_reply_closes_it(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes is this right?", idem="c1")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    tick()
    req = next(iter(pin_requests(rid).values()))
    assert req["status"] == "woken"

    tools = plugin_tools()
    replied = call_tool(tools, "mock_review_reply",
                        {"thread_id": tid, "text": "Yes - I made it clearer."})
    assert "error" not in replied, replied
    assert pin_requests(rid)[req["id"]]["status"] == "done"
    assert pin_requests(rid)[req["id"]]["detail"] == "answered in the thread"


def test_mention_opens_a_working_chip_and_a_plugin_resolve_closes_it(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes is this right?", idem="c1")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    tick()
    req = next(iter(pin_requests(rid).values()))
    assert req["status"] == "woken"

    tools = plugin_tools()
    resolved = call_tool(tools, "mock_review_resolve", {"thread_id": tid, "request_id": req["id"]})
    assert "error" not in resolved, resolved
    assert pin_requests(rid)[req["id"]]["status"] == "done"
    assert store.public_state(store.load(rid))["rounds"][0]["pin_requests"][req["id"]]["status"] == "done"


def test_a_pin_fix_off_board_still_shows_the_working_chip_and_patch_block_closes_it(env, server, tmp_path):
    """The chip is the status of a wake that already happened: `pin_fix` gates a reviewer's pin fixes, not the wake."""
    rid = board(tmp_path, look={"pin_fix": False})["id"]
    code, _, out = post(server, rid, "@Hermes add icons to the hero cards", idem="c1")
    assert code == 201, out
    tick()

    requests = pin_requests(rid)
    assert len(requests) == 1
    req = next(iter(requests.values()))
    assert req["source"] == "mention" and req["status"] == "woken" and req["block"] == "m1"
    state = store.public_state(store.load(rid))["rounds"][0]["pin_requests"]
    assert state[req["id"]]["status"] == "working"
    assert bodies(env)[0]["request_id"] == req["id"]

    tools = plugin_tools()
    patched = call_tool(tools, "mock_review_patch_block",
                        {"review_id": rid, "block": "m1", "html": "<p>icons</p>", "round": 1,
                         "if_rev": 0, "request_ids": [req["id"]]})
    assert "error" not in patched, patched
    assert pin_requests(rid)[req["id"]]["status"] == "done"
    assert store.public_state(store.load(rid))["rounds"][0]["pin_requests"][req["id"]]["status"] == "done"


def test_a_pin_fix_off_board_chip_closes_on_reply_and_on_resolve(env, server, tmp_path):
    tools = plugin_tools()

    replied = board(tmp_path, look={"pin_fix": False})["id"]
    code, _, out = post(server, replied, "@Hermes is this right?", idem="c1")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    tick()
    req = next(iter(pin_requests(replied).values()))
    assert req["status"] == "woken"
    done = call_tool(tools, "mock_review_reply", {"thread_id": tid, "text": "Yes."})
    assert "error" not in done, done
    assert pin_requests(replied)[req["id"]]["status"] == "done"

    resolved = board(tmp_path, look={"pin_fix": False})["id"]
    code, _, out = post(server, resolved, "@Hermes is this right?", idem="c1")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    tick()
    req = next(iter(pin_requests(resolved).values()))
    done = call_tool(tools, "mock_review_resolve", {"thread_id": tid, "request_id": req["id"]})
    assert "error" not in done, done
    assert pin_requests(resolved)[req["id"]]["status"] == "done"


def test_a_pin_fix_off_board_still_caps_mentions_and_the_capped_one_opens_no_chip(env, server, tmp_path):
    rid = board(tmp_path, look={"pin_fix": False, "pin_fix_max_per_round": 1})["id"]
    code, _, out = post(server, rid, "@Hermes one", idem="a")
    assert code == 201, out
    tick()
    assert len(pin_requests(rid)) == 1

    code, _, out = post(server, rid, "@Hermes two", idem="b")
    assert code == 201, out
    assert json.loads(out)["wake"]["status"] == "capped"
    tick()
    assert len(pin_requests(rid)) == 1
    assert len(env["receiver"].wakes) == 1


def test_a_thread_with_no_block_anchor_wakes_but_has_no_chip(env, tmp_path):
    rid = board(tmp_path)["id"]
    owner = users.owner_user()
    thread = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "@Hermes no anchor here",
                            mentions=["hermes"], can_activate=True)
    comment_id = thread["comments"][0]["id"]
    result = service.wake_thread_mention(rid, thread, comment_id)
    assert result["status"] == "pending_notify"
    tick()

    assert len(env["receiver"].wakes) == 1
    body = bodies(env)[0]
    assert body["comment_id"] == comment_id
    assert body["request_id"] is None
    assert pin_requests(rid) == {}


def test_plugin_reply_never_activates_or_wakes(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "plain note", mentions=(), idem="c1")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    assert json.loads(out)["thread"]["activated"] is False

    tools = plugin_tools()
    replied = call_tool(tools, "mock_review_reply",
                        {"thread_id": tid, "text": "@Hermes please look", "mentions": ["hermes"]})
    assert "error" not in replied, replied
    assert replied["thread"]["activated"] is False
    assert pin_requests(rid) == {}
    assert wake_records(rid) == {}
    tick()
    assert env["receiver"].wakes == []


def test_plugin_list_comments_reads_counts_and_filters(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "one", mentions=(), idem="c1")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    code, _, out = post(server, rid, "two", mentions=(), idem="c2")
    assert code == 201, out
    owner = users.owner_user()
    threads.set_status(rid, tid, "resolved", f"user:{owner['id']}", principal=f"user:{owner['id']}")

    tools = plugin_tools()
    opened = call_tool(tools, "mock_review_list_comments", {"review_id": rid})
    assert "error" not in opened, opened
    assert opened["counts"] == {"open": 1, "resolved": 1}
    assert len(opened["threads"]) == 1 and opened["threads"][0]["status"] == "open"
    everything = call_tool(tools, "mock_review_list_comments", {"review_id": rid, "status": "all"})
    assert len(everything["threads"]) == 2
    assert everything["rev"] == opened["rev"]
    assert call_tool(tools, "mock_review_list_comments", {"review_id": "nope-000000"})["error"] == "review not found"


# ---------------------------------------------------------------- who never wakes Hermes


def test_a_guest_mention_never_wakes(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    made = users.share("test", rid, "create", "reviewer")
    guest = users.join_as_guest(users.resolve_share_token(token_of(made["path"])))

    code, _, out = post(server, rid, "@Hermes from the owner", idem="owner")
    assert code == 201, out
    tick()
    assert len(env["receiver"].wakes) == 1
    before = len(env["receiver"].wakes)

    code, _, out = http("POST", f"{server}/r/{rid}/api/threads",
                        {"anchor": {"block": "m1", "x": 0.2, "y": 0.3},
                         "text": "@Hermes from a guest", "mentions": ["hermes"]}, signed_in(guest))
    assert code == 201, out
    thread = json.loads(out)["thread"]
    assert thread["activated"] is False
    tick()
    assert len(env["receiver"].wakes) == before

    code, _, out = http("POST", f"{server}/r/{rid}/api/threads/{thread['id']}/reply",
                        {"text": "@Hermes again", "mentions": ["hermes"]}, signed_in(guest))
    assert code == 200, out
    tick()
    assert len(env["receiver"].wakes) == before


def test_a_token_reply_mention_never_wakes(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    owner = users.owner_user()
    thread = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned note",
                            anchor={"block": "m1", "x": 0.3, "y": 0.4})

    code, _, out = post(server, rid, "@Hermes from a session", idem="session", thread_id=thread["id"])
    assert code == 200, out
    tick()
    assert len(env["receiver"].wakes) == 1
    token = users.create_token(owner["id"], "agent")["token"]

    code, _, out = http("POST", f"{server}/r/{rid}/api/threads/{thread['id']}/reply",
                        {"text": "@Hermes from an agent", "mentions": ["hermes"]},
                        headers={"Authorization": f"Bearer {token}"}, csrf=False)
    assert code == 200, out
    assert "wake" not in json.loads(out)
    tick()
    assert len(env["receiver"].wakes) == 1


def test_a_wake_off_board_mention_never_wakes(env, server, tmp_path):
    live = board(tmp_path)["id"]
    code, _, out = post(server, live, "@Hermes on a live board", idem="live")
    assert code == 201, out
    tick()
    assert len(env["receiver"].wakes) == 1

    quiet = board(tmp_path, wake=False)["id"]
    code, _, out = post(server, quiet, "@Hermes saved only", idem="quiet")
    assert code == 201, out
    assert json.loads(out)["thread"]["activated"] is False
    tick()
    assert len(env["receiver"].wakes) == 1


# ---------------------------------------------------------------- pin fixes follow wake


def test_pin_fix_defaults_follow_wake_and_an_explicit_value_wins(env, tmp_path, monkeypatch):
    decisions = str(_decisions(tmp_path))
    on = api.create("Wake on", ORIGIN, mode="local", decisions=decisions)
    assert on["settings"]["pin_fix"] is True
    assert store.load(on["review_id"])["settings"]["pin_fix"] is True

    off = api.create("Wake off", ORIGIN, mode="local", decisions=decisions, wake=False)
    assert off["settings"]["pin_fix"] is False

    pinned = api.create("No patch", ORIGIN, mode="local", decisions=decisions, pin_fix=False)
    assert pinned["settings"]["pin_fix"] is False

    forced = api.create("Patch with wake off", ORIGIN, mode="local", decisions=decisions,
                        wake=False, pin_fix=True)
    assert forced["settings"]["pin_fix"] is True

    # A config-level true is explicit too: it survives wake=false, and an explicit call value still wins.
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX", "true")
    from_config = api.create("Config patch with wake off", ORIGIN, mode="local", decisions=decisions, wake=False)
    assert from_config["settings"]["pin_fix"] is True
    overridden = api.create("Call wins over config", ORIGIN, mode="local", decisions=decisions,
                            wake=False, pin_fix=False)
    assert overridden["settings"]["pin_fix"] is False


# ---------------------------------------------------------------- a carried thread counts on the newest round


def test_a_carried_threads_mentions_count_against_the_newest_round(env, server, tmp_path):
    """A thread opened in round 1 carries into round 2: its reply wakes spend round 2's budget, not round 1's, and the
    records sit on round 2 so the hour prune cannot free the cap while the round is current."""
    rid = board(tmp_path, look={"pin_fix_max_per_round": 3})["id"]
    code, _, out = post(server, rid, "@Hermes open the thread", idem="open")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    tick()
    assert len(env["receiver"].wakes) == 1

    assert api.add_round(rid, decisions=str(_decisions(tmp_path)), allow_reask=True)["round"] == 2

    reply_ids = []
    for i in range(3):
        code, _, out = post(server, rid, f"@Hermes carried reply {i}", idem=f"r{i}", thread_id=tid)
        assert code == 200, out
        assert json.loads(out)["wake"]["status"] == "pending_notify", json.loads(out)["wake"]
        reply_ids.append(json.loads(out)["comment"]["id"])
        tick()

    records = wake_records(rid)
    for cid in reply_ids:
        assert records[cid]["round"] == 2, records[cid]
        assert records[cid]["status"] == "notified"
    delivered = len(env["receiver"].wakes)
    assert delivered == 4                                  # the opening mention plus three carried replies

    code, _, out = post(server, rid, "@Hermes over the cap", idem="r3", thread_id=tid)
    assert code == 200, out
    assert json.loads(out)["wake"]["status"] == "capped", json.loads(out)["wake"]
    capped_id = json.loads(out)["comment"]["id"]
    tick()
    assert len(env["receiver"].wakes) == delivered

    # An hour later the round-2 records are kept (the round is still newest), so the cap still holds.
    old = "2000-01-01T00:00:00Z"
    data = store.load(rid)
    for cid in list(data["thread_wakes"]):
        data["thread_wakes"][cid]["at"] = old
    store.save(rid, data)
    tick()
    records = wake_records(rid)
    assert records[capped_id]["status"] == "capped"
    assert all(records[cid]["status"] == "notified" for cid in reply_ids)

    code, _, out = post(server, rid, "@Hermes still over the cap", idem="r4", thread_id=tid)
    assert code == 200, out
    assert json.loads(out)["wake"]["status"] == "capped", json.loads(out)["wake"]
    tick()
    assert len(env["receiver"].wakes) == delivered


def test_a_carried_thread_mention_opens_the_working_chip_on_the_newest_round(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes open the thread", idem="open")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    tick()
    round_one = store.load(rid)["rounds"][0].get("pin_requests") or {}
    assert len(round_one) == 1 and next(iter(round_one.values()))["status"] == "woken"

    assert api.add_round(rid, decisions=str(_decisions(tmp_path)), allow_reask=True)["round"] == 2

    code, _, out = post(server, rid, "@Hermes and this carried one", idem="r1", thread_id=tid)
    assert code == 200, out
    comment_id = json.loads(out)["comment"]["id"]
    tick()

    requests = pin_requests(rid)
    assert len(requests) == 1
    req = next(iter(requests.values()))
    assert req["idem"] == f"mention:{comment_id}"
    assert req["idem_owner"] == "mention" and req["status"] == "woken"
    assert req["thread_id"] == tid and req["block"] == "m1"
    round_one = store.load(rid)["rounds"][0].get("pin_requests") or {}
    assert all(q.get("idem") != f"mention:{comment_id}" for q in round_one.values())


def test_a_deleted_comment_frees_its_wake_seen_id(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes first", idem="c1")
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    first = json.loads(out)["thread"]["comments"][0]["id"]
    code, _, out = post(server, rid, "@Hermes second", idem="c2", thread_id=tid)
    assert code == 200, out
    second = json.loads(out)["comment"]["id"]
    tick()
    assert set(store.load(rid)["thread_wake_seen"]) == {first, second}

    conn = sqlite3.connect(str(db.path()))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM comments WHERE id = ?", (first,))
        conn.commit()
    finally:
        conn.close()

    service.retry_pending()
    seen = store.load(rid)["thread_wake_seen"]
    assert first not in seen
    assert set(seen) == {second}


def test_a_sqlite_error_in_the_seen_prune_still_saves_the_notified_record(env, server, tmp_path, monkeypatch):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes keep the save", idem="c1")
    assert code == 201, out
    comment_id = json.loads(out)["thread"]["comments"][0]["id"]

    monkeypatch.setattr(service, "SEND_LEASE_SECONDS", 0.0)
    real = service.threads._threads_rows
    state = {"raised": False}

    def flaky(thread_ids):
        if env["receiver"].wakes and not state["raised"]:
            state["raised"] = True
            raise sqlite3.OperationalError("database is locked")
        return real(thread_ids)

    monkeypatch.setattr(service.threads, "_threads_rows", flaky)
    tick()
    assert wake_records(rid)[comment_id]["status"] == "notified"
    tick()
    assert len(env["receiver"].wakes) == 1
    tick()
    assert len(env["receiver"].wakes) == 1
