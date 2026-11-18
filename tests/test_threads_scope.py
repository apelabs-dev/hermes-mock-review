"""One read scope per request: a pinned viewer guest reads exactly its round on every thread surface."""

import json
import time

import pytest

from authkit import account, peer, signed_in  # noqa: F401
from conftest import http
from mockreview import db, live, threads, users
from test_board_threads import thread_review  # noqa: F401  (fixture)
from test_live import Stream
from test_live_round_tool import Board, chrome  # noqa: F401  (fixture)
from test_share_dialog import add_rounds, join, owner_post

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def clean():
    live.reset()
    threads.reset_limits()
    yield
    live.reset()
    threads.reset_limits()


def actor(user):
    return f"user:{user['id']}"


def make_thread(rid, owner, text, round_n=None, block="a"):
    return threads.create(rid, actor(owner), owner["name"], "owner", text,
                          anchor={"block": block, "x": 0.2, "y": 0.3}, round_n=round_n)


def get_json(server, path, who):
    code, _, body = http("GET", server + path, headers=signed_in(who, False))
    assert code == 200, body
    return json.loads(body)


def stored_events(rid):
    """(event id, payload round) for every persisted event, in id order."""
    out = []
    for r in db.connect().execute("SELECT id, data FROM events WHERE review_id = ? ORDER BY id", (rid,)).fetchall():
        payload = json.loads(r["data"] or "{}")
        thread = payload.get("thread")
        n = thread.get("round") if isinstance(thread, dict) else None
        out.append((int(r["id"]), int(n) if n is not None else None))
    return out


def next_persisted_or_eof(s, timeout=4):
    """Read raw SSE lines: distinguish a genuine socket EOF from silence."""
    deadline = time.monotonic() + timeout
    fields, data = {}, []
    while time.monotonic() < deadline:
        raw = s._readline(deadline)
        assert raw is not None, "expected a control event or genuine EOF, not silence"
        if raw == b"":
            return {"eof": True}
        line = raw.decode().rstrip("\r\n")
        if not line:
            if fields.get("id"):
                fields["data"] = "\n".join(data)
                return fields
            fields, data = {}, []
            continue
        k, sep, val = line.partition(":")
        if sep:
            if k == "data":
                data.append(val.lstrip())
            else:
                fields[k] = val.lstrip()
    raise AssertionError("stream neither closed nor delivered the allowed frame")


def test_view_scope_is_one_round_for_a_pinned_viewer_guest(review, tmp_path):
    """Catches a scope that lets a pinned link widen through `?round=`, or treats owner/member/latest as pinned."""
    rid = review["id"]
    add_rounds(review, tmp_path, 2)                     # rounds 1, 2, 3

    owner = threads.view_scope(rid, guest=False, role="owner")
    member = threads.view_scope(rid, guest=False, role="commenter")
    users.share("test", rid, "create", "viewer")        # the link stays on `latest`
    commenter = threads.view_scope(rid, guest=True, role="commenter")
    latest = threads.view_scope(rid, guest=True, role="viewer")
    for scope in (owner, member, commenter, latest):
        assert scope.restricted is False and scope.viewed == 3 and scope.pinned is None

    users.share("test", rid, "update", round="2")
    pinned = threads.view_scope(rid, guest=True, role="viewer")
    assert pinned.restricted is True and pinned.rounds == {2} and pinned.pinned == 2 and pinned.viewed == 2
    asked = threads.view_scope(rid, guest=True, role="viewer", round_n=2)
    assert asked.rounds == {2} and asked.viewed == 2
    narrow = threads.view_scope(rid, guest=True, role="viewer", round_n=1)
    assert narrow.rounds == frozenset() and narrow.allows_round(1) is False
    assert narrow.allows_round(None) is False          # a restricted scope never widens to unknown rounds
    wide = threads.view_scope(rid, guest=True, role="viewer", round_n=3)
    assert wide.rounds == frozenset() and wide.allows_round(3) is False   # intersect, never replace

    assert pinned.allows_round(1) is False and pinned.allows_round(2) is True
    assert pinned.allows_round(None) is False and owner.allows_round(None) is True

    users.share("test", rid, "update", round="9")       # no such round
    gone = threads.view_scope(rid, guest=True, role="viewer")
    assert gone.restricted is False and gone.viewed == 3 and gone.pinned is None


def test_a_pinned_viewer_guest_reads_only_its_round(env, server, review, tmp_path, peer):
    """Catches a round-3 comment leaking through the default listing, `?round=`, counts, activity or state.json."""
    rid = review["id"]
    owner = users.owner_user()
    visible = make_thread(rid, owner, "VISIBLE ROUND ONE")
    add_rounds(review, tmp_path, 2)
    make_thread(rid, owner, "SECRET ROUND THREE", round_n=3)
    link = users.share("test", rid, "create", "viewer")
    users.share("test", rid, "update", round="1")
    guest = join(server, link["path"])

    listing = None
    for query in ("", "?round=1"):
        listing = get_json(server, f"/r/{rid}/api/threads{query}", guest)
        assert [t["id"] for t in listing["threads"]] == [visible["id"]], query
        assert listing["counts"]["open"] == 1, query
        assert "SECRET ROUND THREE" not in json.dumps(listing), query

    hidden = get_json(server, f"/r/{rid}/api/threads?round=3", guest)
    assert hidden["threads"] == [] and hidden["counts"]["open"] == 0
    assert hidden["counts"]["activity_unread"] == 0
    assert "SECRET ROUND THREE" not in json.dumps(hidden)

    acts = listing["activity"]
    assert not any("SECRET ROUND THREE" in json.dumps(e) for e in acts)
    unread = [e for e in acts if e["unread"]]
    assert unread and listing["counts"]["activity_unread"] == len(unread)
    assert all(e["data"]["thread"]["round"] == 1 for e in acts if isinstance(e["data"].get("thread"), dict))

    events = stored_events(rid)
    visible_ids = [i for i, n in events if n == 1]
    hidden_ids = [i for i, n in events if n == 3]
    assert hidden_ids and max(hidden_ids) > max(visible_ids)
    state = get_json(server, f"/r/{rid}/state.json", guest)
    assert state["threads"] == {"open": 1, "rev": max(visible_ids)}
    assert [r["n"] for r in state["rounds"]] == [1] and state["pinned_round"] == 1
    assert "SECRET ROUND THREE" not in json.dumps(state)


def test_a_query_intersects_the_pinned_allowlist_both_ways(env, server, review, tmp_path, peer):
    """Catches a `?round=` that replaces the pinned exact allowlist instead of intersecting it, in either direction."""
    rid = review["id"]
    owner = users.owner_user()
    private = make_thread(rid, owner, "PRIVATE ROUND ONE", block="a")
    add_rounds(review, tmp_path, 2)                     # rounds 2 and 3
    visible = make_thread(rid, owner, "VISIBLE ROUND THREE", round_n=3, block="b")
    link = users.share("test", rid, "create", "viewer")
    users.share("test", rid, "update", round="3")
    guest = join(server, link["path"])

    for query in ("", "?round=3"):
        listing = get_json(server, f"/r/{rid}/api/threads{query}", guest)
        assert [t["id"] for t in listing["threads"]] == [visible["id"]], query
        assert listing["counts"]["open"] == 1, query

    hidden = get_json(server, f"/r/{rid}/api/threads?round=1", guest)
    assert hidden["threads"] == [] and hidden["activity"] == []
    assert hidden["counts"] == {"open": 0, "total": 0, "unread": 0, "activity_unread": 0}
    assert "PRIVATE ROUND ONE" not in json.dumps(hidden) and "VISIBLE ROUND THREE" not in json.dumps(hidden)

    events = stored_events(rid)
    state = get_json(server, f"/r/{rid}/state.json", guest)
    assert state["threads"] == {"open": 1, "rev": max(i for i, n in events if n == 3)}

    users.share("test", rid, "update", round="1")       # re-pin: the allowlist is now {1}
    listing = get_json(server, f"/r/{rid}/api/threads", guest)
    assert [t["id"] for t in listing["threads"]] == [private["id"]]
    hidden = get_json(server, f"/r/{rid}/api/threads?round=3", guest)
    assert hidden["threads"] == [] and hidden["activity"] == []
    assert hidden["counts"] == {"open": 0, "total": 0, "unread": 0, "activity_unread": 0}
    assert "VISIBLE ROUND THREE" not in json.dumps(hidden)
    state = get_json(server, f"/r/{rid}/state.json", guest)
    assert state["threads"] == {"open": 1, "rev": max(i for i, n in events if n == 1)}

    member = account(review_id=rid, role="commenter", name="Maya")
    listing = get_json(server, f"/r/{rid}/api/threads?round=3", member)
    assert {t["id"] for t in listing["threads"]} == {private["id"], visible["id"]}
    assert any("VISIBLE ROUND THREE" in json.dumps(e) for e in listing["activity"])
    state = get_json(server, f"/r/{rid}/state.json", member)
    assert state["threads"]["open"] == 2


def test_a_member_and_a_latest_link_still_see_every_round(env, server, review, tmp_path, peer):
    """Catches over-filtering: people who may see both rounds must still get the round-3 thread everywhere."""
    rid = review["id"]
    owner = users.owner_user()
    visible = make_thread(rid, owner, "VISIBLE ROUND ONE")
    add_rounds(review, tmp_path, 2)
    secret = make_thread(rid, owner, "SECRET ROUND THREE", round_n=3)
    member = account(review_id=rid, role="commenter", name="Maya")
    link = users.share("test", rid, "create", "viewer")     # a link left on `latest`
    guest = join(server, link["path"])
    both = {visible["id"], secret["id"]}

    for who in (member, guest):
        for query in ("", "?round=3"):
            listing = get_json(server, f"/r/{rid}/api/threads{query}", who)
            assert {t["id"] for t in listing["threads"]} == both
            assert listing["counts"]["open"] == 2
            assert any("SECRET ROUND THREE" in json.dumps(e) for e in listing["activity"])
        state = get_json(server, f"/r/{rid}/state.json", who)
        assert state["threads"]["open"] == 2


def test_a_pinned_viewer_stream_drops_other_rounds_and_keeps_its_cursor(env, server, review, tmp_path, peer):
    """Catches a hidden event replayed or delivered live, and a reconnect that stalls on a skipped event's id.

    The Stream helper cannot read again after a per-read timeout (its buffered reader reports EOF), so every wait here
    is for a frame that must arrive; "nothing hidden was delivered" is asserted from the order of what did arrive.
    """
    rid = review["id"]
    owner = users.owner_user()
    visible = make_thread(rid, owner, "VISIBLE ROUND ONE")
    add_rounds(review, tmp_path, 2)
    secret = make_thread(rid, owner, "SECRET ROUND THREE", round_n=3)
    link = users.share("test", rid, "create", "viewer")
    users.share("test", rid, "update", round="1")
    guest = join(server, link["path"])
    member = account(review_id=rid, role="commenter", name="Maya")

    g = Stream(server, f"/r/{rid}/events", signed_in(guest, False))
    m = Stream(server, f"/r/{rid}/events", signed_in(member, False))
    g2 = None
    try:
        assert g.until("hello") is not None
        assert m.until("hello") is not None

        # Replay: the guest's round-1 thread, then the presence frame. A replayed round-3 thread would land in between.
        created = g.until("thread.created", timeout=3)
        assert created is not None and json.loads(created["data"])["thread"]["id"] == visible["id"]
        after = g.frame(timeout=3)
        assert after is not None and after.get("event") == "presence", after
        cursor = int(created["id"])

        replayed_ids = []
        while len(replayed_ids) < 2:
            f = m.until("thread.created", timeout=3)
            assert f is not None, replayed_ids
            replayed_ids.append(json.loads(f["data"])["thread"]["id"])
        assert set(replayed_ids) == {visible["id"], secret["id"]}, "the member replays both rounds"

        # Live: the hidden reply then the visible one. The guest's first thread.replied must be the visible one, which
        # is only true if the hidden round's frame was dropped.
        code, _, out = owner_post(server, f"/r/{rid}/api/threads/{secret['id']}/reply", {"text": "HIDDEN REPLY"}, peer)
        assert code == 200, out
        code, _, out = owner_post(server, f"/r/{rid}/api/threads/{visible['id']}/reply", {"text": "VISIBLE REPLY"}, peer)
        assert code == 200, out
        member_texts = []
        for _ in range(2):
            f = m.until("thread.replied", timeout=4)
            assert f is not None, member_texts
            member_texts.append(json.loads(f["data"])["thread"]["comments"][-1]["text"])
        assert member_texts == ["HIDDEN REPLY", "VISIBLE REPLY"], member_texts
        guest_frame = g.until("thread.replied", timeout=4)
        assert guest_frame is not None, "the guest stream must still be alive"
        assert "VISIBLE REPLY" in guest_frame["data"] and "HIDDEN REPLY" not in guest_frame["data"], guest_frame

        # Reconnect from the cursor before the hidden reply: the next replayed frame is the visible one, not the hidden.
        headers = dict(signed_in(guest, False))
        headers["Last-Event-ID"] = str(cursor)
        g2 = Stream(server, f"/r/{rid}/events", headers)
        assert g2.until("hello") is not None
        replayed = g2.until("thread.replied", timeout=4)
        assert replayed is not None and "VISIBLE REPLY" in replayed["data"], replayed
        code, _, out = owner_post(server, f"/r/{rid}/api/threads/{visible['id']}/reply", {"text": "AFTER RECONNECT"},
                                  peer)
        assert code == 200, out
        after_frame = g2.until("thread.replied", timeout=4)
        assert after_frame is not None and int(after_frame["id"]) > int(replayed["id"])
        assert "AFTER RECONNECT" in after_frame["data"]
    finally:
        g.close()
        m.close()
        if g2 is not None:
            g2.close()


def test_view_scope_is_computed_once_per_read_request(env, server, review, monkeypatch):
    """Catches a surface that recomputes the scope per query instead of once per request."""
    rid = review["id"]
    calls = []
    actual = threads.view_scope

    def counting(*args, **kwargs):
        calls.append(1)
        return actual(*args, **kwargs)

    monkeypatch.setattr(threads, "view_scope", counting)
    member = account(review_id=rid, role="commenter", name="Maya")

    get_json(server, f"/r/{rid}/state.json", member)
    assert len(calls) == 1
    get_json(server, f"/r/{rid}/api/threads", member)
    assert len(calls) == 2
    get_json(server, f"/r/{rid}/api/threads?round=3", member)
    assert len(calls) == 3

    s = Stream(server, f"/r/{rid}/events", signed_in(member, False))
    try:
        assert s.until("hello") is not None
        assert len(calls) == 4
    finally:
        s.close()


@pytest.mark.parametrize("initial", ["latest", "3"])
def test_repinning_a_link_closes_an_already_open_guest_stream(env, server, review, tmp_path, peer, initial):
    """Catches a re-pinned link leaving an open guest SSE socket streaming under its old scope."""
    rid = review["id"]
    owner = users.owner_user()
    one = make_thread(rid, owner, "ROUND ONE THREAD", block="a")
    add_rounds(review, tmp_path, 2)                     # rounds 2 and 3
    three = make_thread(rid, owner, "ROUND THREE THREAD", round_n=3, block="b")
    link = users.share("test", rid, "create", "viewer")
    users.share("test", rid, "update", round=initial)
    guest = join(server, link["path"])
    member = account(review_id=rid, role="commenter", name="Maya")
    cursor = live.latest(rid)

    headers = dict(signed_in(guest, False))
    headers["Last-Event-ID"] = str(cursor)
    g = Stream(server, f"/r/{rid}/events", headers)
    member_headers = dict(signed_in(member, False))
    member_headers["Last-Event-ID"] = str(cursor)
    m = Stream(server, f"/r/{rid}/events", member_headers)
    g2 = None
    try:
        assert g.until("hello") is not None
        assert m.until("hello") is not None

        code, _, out = owner_post(server, f"/r/{rid}/api/share", {"action": "update", "round": "1"}, peer)
        assert code == 200, out
        fresh = get_json(server, f"/r/{rid}/state.json", guest)
        assert [r["n"] for r in fresh["rounds"]] == [1] and fresh["pinned_round"] == 1

        code, _, out = owner_post(server, f"/r/{rid}/api/threads/{three['id']}/reply",
                                  {"text": "SECRET AFTER REPIN"}, peer)
        assert code == 200, out
        code, _, out = owner_post(server, f"/r/{rid}/api/threads/{one['id']}/reply",
                                  {"text": "VISIBLE CONTROL AFTER REPIN"}, peer)
        assert code == 200, out

        frame = next_persisted_or_eof(g)
        if not frame.get("eof"):
            assert "VISIBLE CONTROL AFTER REPIN" in frame.get("data", "")
            assert "SECRET AFTER REPIN" not in frame.get("data", "")
        while not frame.get("eof"):
            frame = next_persisted_or_eof(g)
            assert "SECRET AFTER REPIN" not in frame.get("data", "")

        member_texts = []
        for _ in range(2):
            f = m.until("thread.replied", timeout=4)
            assert f is not None, member_texts
            member_texts.append(json.loads(f["data"])["thread"]["comments"][-1]["text"])
        assert member_texts == ["SECRET AFTER REPIN", "VISIBLE CONTROL AFTER REPIN"], member_texts

        # Reconnect from the pre-repin cursor: the hidden reply is replayed and dropped, the visible one arrives.
        headers = dict(signed_in(guest, False))
        headers["Last-Event-ID"] = str(cursor)
        g2 = Stream(server, f"/r/{rid}/events", headers)
        assert g2.until("hello") is not None
        replayed = next_persisted_or_eof(g2)
        assert not replayed.get("eof") and "VISIBLE CONTROL AFTER REPIN" in replayed.get("data", "")
        assert "SECRET AFTER REPIN" not in replayed.get("data", "")

        code, _, out = owner_post(server, f"/r/{rid}/api/threads/{one['id']}/reply",
                                  {"text": "VISIBLE AFTER RECONNECT"}, peer)
        assert code == 200, out
        after = next_persisted_or_eof(g2)
        assert not after.get("eof") and "VISIBLE AFTER RECONNECT" in after.get("data", "")
        assert "SECRET AFTER REPIN" not in after.get("data", "")
    finally:
        g.close()
        m.close()
        if g2 is not None:
            g2.close()


def test_a_latest_link_guest_stream_still_receives_a_new_round(env, server, review, tmp_path, peer):
    """Control: a link left on `latest` is unrestricted, so a round added after subscribing still reaches the board."""
    rid = review["id"]
    owner = users.owner_user()
    link = users.share("test", rid, "create", "viewer")     # a link left on `latest`
    guest = join(server, link["path"])
    member = account(review_id=rid, role="commenter", name="Maya")

    g = Stream(server, f"/r/{rid}/events", signed_in(guest, False))
    m = Stream(server, f"/r/{rid}/events", signed_in(member, False))
    try:
        assert g.until("hello") is not None
        assert m.until("hello") is not None

        add_rounds(review, tmp_path, 1)                     # round 2
        made = make_thread(rid, owner, "NEW ROUND TWO", round_n=2, block="b")

        for s in (g, m):
            f = s.until("thread.created", timeout=4)
            assert f is not None
            created = json.loads(f["data"])
            assert created["thread"]["id"] == made["id"] and created["thread"]["round"] == 2

        for who in (guest, member):
            state = get_json(server, f"/r/{rid}/state.json", who)
            assert state["current_round"] == 2
    finally:
        g.close()
        m.close()


def test_carried_threads_still_carry_for_unrestricted_readers(env, server, review, tmp_path):
    """Catches a scope that turns carry-over off for the people who may see both rounds."""
    rid = review["id"]
    owner = users.owner_user()
    t = make_thread(rid, owner, "carried from round one")
    add_rounds(review, tmp_path, 1)                     # round 2
    rows = threads.list_threads(rid, actor(owner), round_n=2)
    assert [r["id"] for r in rows] == [t["id"]] and rows[0]["carried"] is True

    member = account(review_id=rid, role="commenter", name="Maya")
    listing = get_json(server, f"/r/{rid}/api/threads?round=2", member)
    assert [r["id"] for r in listing["threads"]] == [t["id"]]
    assert listing["threads"][0]["carried"] is True


def test_a_pinned_guest_board_never_shows_a_newer_round_comment(env, server, thread_review, tmp_path, chrome, peer):
    """The Chrome repro from review round 3: a pinned guest's Comments overlay must not gain a round-3 thread."""
    rid = thread_review
    owner = users.owner_user()
    visible = threads.create(rid, actor(owner), owner["name"], "owner", "VISIBLE ROUND ONE",
                             anchor={"block": "m1", "x": 0.3, "y": 0.4})
    add_rounds({"id": rid}, tmp_path, 2)
    link = users.share("test", rid, "create", "viewer")
    users.share("test", rid, "update", round="1")

    b = Board(chrome, server + link["path"], wait="!!document.querySelector('.tcomments')")
    b.press(".tcomments")
    assert b.until("!!document.querySelector('.toverlay')")
    assert b.until("document.querySelector('.toverlay').textContent.includes('VISIBLE ROUND ONE')")

    code, _, out = owner_post(server, f"/r/{rid}/api/threads",
                              {"anchor": {"block": "m1", "x": 0.1, "y": 0.1},
                               "text": "NEW SECRET FOR ROUND THREE", "round": 3}, peer)
    assert code == 201, out
    time.sleep(1.5)
    assert not b.js("document.querySelector('.toverlay').textContent.includes('NEW SECRET FOR ROUND THREE')")
    assert b.until("Array.from(document.querySelectorAll('.banner'))"
                   ".some(function (e) { return e.textContent.includes('This link shows round 1.'); })")

    code, _, out = owner_post(server, f"/r/{rid}/api/threads/{visible['id']}/reply", {"text": "A VISIBLE REPLY"}, peer)
    assert code == 200, out
    assert b.until("document.querySelector('.toverlay').textContent.includes('2 comments')", timeout=10), \
        "the visible reply must still reach the open overlay"
    b.press(".toverlay .trow")
    assert b.until("!!document.querySelector('.tpop')")
    assert b.until("document.querySelector('.tpop').textContent.includes('A VISIBLE REPLY')", timeout=10)
