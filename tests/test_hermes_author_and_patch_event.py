"""Two board contracts: a patched block is announced live, and Hermes answers a thread as Hermes.

`patch_block` always publishes `block.patched` (the board swaps that block in place), and the plugin's reply and
resolve tools store their writes under the fixed agent actor, never the owner account that held the pen.
"""

import json

from conftest import http
from mockreview import api, db, live, service, store, threads, users
from test_mention_wake import board, call_tool, plugin_tools, post, pin_requests, tick, bodies


def kinds(rid):
    return [(r["kind"], json.loads(r["data"])) for r in live.replay(rid, 0)[1]]


def of_kind(rid, kind):
    return [d for k, d in kinds(rid) if k == kind]


# ---------------------------------------------------------------- block.patched

def test_patch_block_without_request_ids_publishes_block_patched(env, tmp_path):
    rid = board(tmp_path)["id"]
    before = len(kinds(rid))
    out = api.patch_block(rid, "m1", "<p>new</p>", round_n=1, if_rev=0)
    assert out["rev"] == 1 and out["done"] == []
    new = kinds(rid)[before:]
    assert [k for k, _ in new] == [live.BLOCK_PATCHED]
    assert new[0][1] == {"round": 1, "block": "m1", "option": None, "rev": 1}


def test_patch_block_with_a_closed_request_publishes_both_events(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    code, _, out = post(server, rid, "@Hermes make this clearer")
    assert code == 201, out
    tick()
    req = next(iter(pin_requests(rid)))
    before = len(kinds(rid))
    out = api.patch_block(rid, "m1", "<p>clearer</p>", round_n=1, if_rev=0, request_ids=[req])
    assert out["done"] == [req]
    new = [k for k, _ in kinds(rid)[before:]]
    assert new == [live.BLOCK_PATCHED, live.PIN_REQUESTS]


def test_a_refused_patch_publishes_nothing(env, tmp_path):
    rid = board(tmp_path)["id"]
    before = len(kinds(rid))
    try:
        api.patch_block(rid, "m1", "<p>x</p>", round_n=1, if_rev=7)
    except store.ReviewError:
        pass
    else:
        raise AssertionError("a stale revision was accepted")
    assert len(kinds(rid)) == before


def test_block_patched_stays_out_of_the_activity_feed_and_the_unread_count(env, tmp_path):
    rid = board(tmp_path)["id"]
    api.patch_block(rid, "m1", "<p>new</p>", round_n=1, if_rev=0)
    owner = users.owner_user()
    actor = f"user:{owner['id']}"
    assert [e["kind"] for e in threads.activity(rid, actor)] == []
    assert threads.activity_unread(rid, actor) == 0


# ---------------------------------------------------------------- Hermes's replies

def _thread(server, rid):
    code, _, out = post(server, rid, "Is this subtotal right?", mentions=())
    assert code == 201, out
    return json.loads(out)["thread"]["id"]


def _owner_actor():
    return f"user:{users.owner_user()['id']}"


def test_a_tool_reply_is_stored_as_hermes_not_the_owner(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _thread(server, rid)
    out = call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "Yes, it matches."})
    assert "error" not in out, out
    c = out["comment"]
    assert c["author"] == threads.AGENT_ACTOR == "agent:hermes"
    assert c["author_name"] == "Hermes" and c["author_kind"] == "hermes"
    assert c["mine"] is False
    assert out["as"]["name"] == "Hermes"
    # the owner's own read of the thread: not theirs to edit, and still unread for them
    thread = threads.list_threads(rid, principal=_owner_actor())[0]
    hermes = [x for x in thread["comments"] if x["author"] == "agent:hermes"]
    assert len(hermes) == 1 and hermes[0]["mine"] is False and hermes[0]["author_kind"] == "hermes"
    assert thread["unread"] is True


def test_the_owner_cannot_edit_hermes_reply_as_their_own(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _thread(server, rid)
    out = call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "Done."})
    cid = out["comment"]["id"]
    try:
        threads.edit_comment(rid, cid, _owner_actor(), "rewritten", principal=_owner_actor())
    except store.ReviewError as e:
        assert e.status == 403
    else:
        raise AssertionError("the owner edited Hermes's comment as their own")


def test_the_reply_event_and_the_rest_read_say_hermes(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _thread(server, rid)
    call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "Checked."})
    replied = [d for d in of_kind(rid, "thread.replied")][-1]
    assert replied["actor"] == "agent:hermes" and replied["actor_kind"] == "hermes" and replied["name"] == "Hermes"
    code, _, raw = http("GET", f"{server}/r/{rid}/api/threads")
    assert code == 200, raw
    comments = json.loads(raw)["threads"][0]["comments"]
    assert [c["author_kind"] for c in comments] == ["member", "hermes"]
    assert comments[-1]["author_name"] == "Hermes" and comments[-1]["mine"] is False
    feed = threads.activity(rid, _owner_actor())
    assert feed[0]["kind"] == "thread.replied" and feed[0]["actor_kind"] == "hermes"


def test_a_hermes_reply_never_wakes(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _thread(server, rid)
    out = call_tool(plugin_tools(), "mock_review_reply",
                    {"thread_id": tid, "text": "@Hermes loop?", "mentions": ["hermes"]})
    assert "error" not in out, out
    tick()
    assert pin_requests(rid) == {} and bodies(env) == []
    assert out["thread"]["activated"] is False


def test_the_audit_row_names_the_real_account(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _thread(server, rid)
    call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "Checked."})
    row = next(r for r in users.audit_rows() if r["action"] == "plugin.reply")
    assert row["actor"] == _owner_actor() and row["detail"]["thread_id"] == tid


def test_resolve_is_attributed_to_hermes(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _thread(server, rid)
    out = call_tool(plugin_tools(), "mock_review_resolve", {"thread_id": tid})
    assert out["status"] == "resolved" and out["as"]["name"] == "Hermes"
    status = [d for d in of_kind(rid, "thread.status")][-1]
    assert status["actor"] == "agent:hermes" and status["actor_kind"] == "hermes" and status["name"] == "Hermes"
    row = next(r for r in users.audit_rows() if r["action"] == "plugin.resolve")
    assert row["actor"] == _owner_actor()


def test_older_owner_comments_are_left_alone(env, server, tmp_path):
    rid = board(tmp_path)["id"]
    tid = _thread(server, rid)
    thread = threads.list_threads(rid, principal=_owner_actor())[0]
    assert thread["id"] == tid
    first = thread["comments"][0]
    assert first["author"] == _owner_actor() and first["author_kind"] == "member" and first["mine"] is True
