"""Server-sent events: hello, replay, reset, heartbeat, seats, slow consumers and the stream closer."""

from http import client as httpclient
import json
import socket
import threading
import time
import urllib.parse
import uuid

import pytest

from authkit import account, loopback_owner_off, peer, signed_in, token_of  # noqa: F401
from conftest import http
from mockreview import db, live, threads, users


@pytest.fixture(autouse=True)
def clean_live():
    live.reset()
    threads.reset_limits()
    yield
    live.reset()
    threads.reset_limits()


@pytest.fixture
def owner():
    return users.owner_user()


def actor(user):
    return f"user:{user['id']}"


def make_thread(rid, owner, text="hello"):
    return threads.create(rid, actor(owner), owner["name"], "owner", text,
                          anchor={"block": "a", "x": 0.2, "y": 0.3})


class Stream:
    """A real streaming GET; `frame()` returns one parsed SSE frame or None on timeout/EOF."""

    def __init__(self, server, path, headers=None, expect=200):
        u = urllib.parse.urlsplit(server)
        self.conn = httpclient.HTTPConnection(u.hostname, u.port, timeout=8)
        self.conn.request("GET", path, headers=headers or {})
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        self.sock = self.conn.sock or getattr(getattr(self.resp.fp, "raw", None), "_sock", None)
        assert self.status == expect, (self.status, self.resp.read()[:300])

    def _readline(self, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        if self.sock is not None:
            self.sock.settimeout(left)
        try:
            return self.resp.fp.readline()
        except (socket.timeout, TimeoutError, OSError):
            return None

    def frame(self, timeout=4.0):
        deadline = time.monotonic() + timeout
        fields, data = {}, []
        while True:
            raw = self._readline(deadline)
            if raw is None:
                return None
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if fields or data:
                    if data:
                        fields["data"] = "\n".join(data)
                    return fields
                continue
            if line.startswith(":"):
                return {"comment": line[1:].strip()}
            key, sep, value = line.partition(":")
            if not sep:
                continue
            value = value[1:] if value.startswith(" ") else value
            if key == "data":
                data.append(value)
            else:
                fields[key] = value

    def until(self, event, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            f = self.frame(max(0.1, deadline - time.monotonic()))
            if f and f.get("event") == event:
                return f
        return None

    def until_comment(self, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            f = self.frame(max(0.1, deadline - time.monotonic()))
            if f and "comment" in f:
                return f
        return None

    def eof(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            if self.sock is not None:
                self.sock.settimeout(left)
            try:
                chunk = self.resp.fp.read(1)
            except (socket.timeout, TimeoutError, OSError):
                return False
            if not chunk:
                return True
        return False

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def payload(frame):
    return json.loads(frame["data"])


def test_hello_then_a_new_thread_arrives(server, review, owner):
    rid = review["id"]
    s = Stream(server, f"/r/{rid}/events")
    hello = s.until("hello")
    assert hello is not None
    h = payload(hello)
    assert h["actor"] == actor(owner) and h["rev"] == 0 and h["presence"]["count"] >= 1
    assert h["me"]["review_role"] == "owner" and "comment" in h["me"]["caps"]
    code, _, out = http("POST", f"{server}/r/{rid}/api/threads",
                        {"anchor": {"block": "a", "x": 0.1, "y": 0.2}, "text": "live one"})
    assert code == 201, out
    f = s.until("thread.created", timeout=3)
    assert f is not None and payload(f)["thread"]["comments"][0]["text"] == "live one"
    assert int(f["id"]) >= 1
    s.close()


def test_replay_from_last_event_id(server, review, owner):
    rid = review["id"]
    for i in range(3):
        make_thread(rid, owner, "replay %d" % i)
    rows = db.connect().execute("SELECT id FROM events WHERE review_id = ? ORDER BY id", (rid,)).fetchall()
    assert len(rows) == 3
    first = rows[0]["id"]
    s = Stream(server, f"/r/{rid}/events", {"Last-Event-ID": str(first)})
    assert s.until("hello") is not None
    seen = []
    deadline = time.monotonic() + 4
    while len(seen) < 2 and time.monotonic() < deadline:
        f = s.frame(max(0.1, deadline - time.monotonic()))
        if f and f.get("event") == "thread.created":
            seen.append(int(f["id"]))
    assert seen == [rows[1]["id"], rows[2]["id"]]
    s.close()


def test_reset_when_the_cursor_is_older_than_the_retained_window(server, review, owner):
    rid = review["id"]
    make_thread(rid, owner)
    with db.tx() as conn:
        base = int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE review_id = ?", (rid,)).fetchone()[0])
        for i in range(live.KEEP_EVENTS + 5):
            conn.execute("INSERT INTO events (review_id, seq, kind, thread_id, actor, data, at) "
                         "VALUES (?, ?, 'thread.status', '', 'system', '{}', ?)", (rid, base + i + 1, time.time()))
    live.publish(rid, "thread.status", "", "system", {"prune": True})
    latest = live.latest(rid)
    s = Stream(server, f"/r/{rid}/events", {"Last-Event-ID": "1"})
    assert s.until("hello") is not None
    reset = s.until("reset", timeout=3)
    assert reset is not None and payload(reset)["rev"] == latest
    s.close()


def test_heartbeat_arrives_on_a_quiet_stream(server, review, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_SSE_HEARTBEAT_SECONDS", "1")
    s = Stream(server, f"/r/{review['id']}/events")
    assert s.until("hello") is not None
    assert s.until_comment(timeout=3) is not None
    s.close()


def test_seat_cap_reserves_one_for_the_owner(server, review):
    rid = review["id"]
    member = account(review_id=rid, role="reviewer", name="Maya")
    several = []
    try:
        for _ in range(live.MAX_STREAMS - 1):
            s = Stream(server, f"/r/{rid}/events", signed_in(member, False))
            assert s.until("hello") is not None
            several.append(s)
        refused = Stream(server, f"/r/{rid}/events", signed_in(member, False), expect=429)
        body = json.loads(refused.resp.read())
        assert body == {"ok": False, "error": "too many live connections"}
        refused.close()
        owner_stream = Stream(server, f"/r/{rid}/events")
        assert owner_stream.until("hello") is not None
        several.append(owner_stream)
        assert live.REGISTRY.count(rid, owner=True) == 1
        assert live.REGISTRY.count(rid, owner=False) == live.MAX_STREAMS - 1
    finally:
        for s in several:
            s.close()


def test_a_slow_consumer_is_closed_without_blocking_the_publisher(env, review):
    rid = review["id"]
    sub = live.REGISTRY.subscribe(rid, "user:1", "Owner", "user", owner=True)
    try:
        while not sub.queue.full():
            sub.queue.put_nowait((0, "presence", {}))
        start = time.monotonic()
        live.publish(rid, "thread.status", "", "system", {"ok": True})
        assert time.monotonic() - start < 1.0
        assert sub.closed and sub.slow
    finally:
        live.REGISTRY.unsubscribe(sub)


def test_sign_out_others_ends_the_stream(server, review):
    rid = review["id"]
    dana = account(review_id=rid, role="reviewer", name="Dana")
    other = users.redeem("login", token_of(users.create_login_link("test", dana["user_id"])["path"]))
    other_hash = users.session_row(other["session"])["id_hash"]
    s = Stream(server, f"/r/{rid}/events", signed_in(dana, False))
    assert s.until("hello") is not None
    users.sign_out_others(dana["user_id"], other_hash)
    assert s.eof(2), "the signed-out stream must end"
    s.close()


def test_revoking_a_grant_ends_the_stream_and_refuses_its_session(server, review, owner):
    rid = review["id"]
    member = account(review_id=rid, role="commenter", name="Rae")
    s = Stream(server, f"/r/{rid}/events", signed_in(member, False))
    try:
        assert s.until("hello") is not None
        users.revoke_grant(actor(owner), member["user_id"], rid)
        make_thread(rid, owner, "after the revoke")
        assert s.eof(2), "a revoked grant must end the open stream"
        assert s.frame(timeout=0.2) is None, "no thread.created frame may arrive after the revoke"
        code, _, body = http("GET", f"{server}/r/{rid}/state.json", headers=signed_in(member, False))
        assert code in (403, 404), (code, body)
    finally:
        s.close()


def test_a_revoke_ends_a_stream_signed_in_by_a_proxy_identity(server, review, owner, peer, monkeypatch):  # noqa: F811
    """A stream signed in by a linked proxy identity has no session hash, so only its actor can close it."""
    rid = review["id"]
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    peer["addr"] = "127.0.0.1"
    mo = account(name="Mo", identity={"source": "forwarded", "subject": "mo@example.test", "name": "Mo",
                                      "email": "mo@example.test"})
    users.grant(actor(owner), mo["user_id"], "commenter", review_id=rid)
    s = Stream(server, f"/r/{rid}/events",
               headers={"X-Forwarded-User": "mo", "X-Forwarded-Email": "mo@example.test"})
    try:
        assert s.until("hello") is not None
        users.revoke_grant(actor(owner), mo["user_id"], rid)
        make_thread(rid, owner, "after the identity revoke")
        assert s.eof(2), "a revoked proxy-identity stream must end"
        assert s.frame(timeout=0.2) is None, "no thread.created frame may arrive after the revoke"
    finally:
        s.close()


def test_demoting_an_admin_ends_the_stream(server, review, owner):
    rid = review["id"]
    admin = account(review_id=rid, role="reviewer", instance="admin", name="Ari")
    s = Stream(server, f"/r/{rid}/events", signed_in(admin, False))
    try:
        assert s.until("hello") is not None
        users.set_role(actor(owner), admin["user_id"], "member")
        assert s.eof(2), "a role change must end the open stream"
    finally:
        s.close()


@pytest.mark.slow
def test_an_expired_grant_ends_the_stream_on_the_next_recheck(server, review, owner):
    rid = review["id"]
    member = account(name="Eli")
    users.grant(actor(owner), member["user_id"], "commenter", review_id=rid, expires="1m")
    s = Stream(server, f"/r/{rid}/events", signed_in(member, False))
    try:
        assert s.until("hello") is not None
        with db.tx() as conn:
            conn.execute("UPDATE grants SET expires_at = ? WHERE user_id = ? AND review_id = ?",
                         (time.time() + 0.5, member["user_id"], rid))
        assert s.eof(8), "the 5-second recheck must end a stream whose grant expired"
    finally:
        s.close()


def test_regenerating_a_share_link_ends_the_guest_stream(server, review):
    rid = review["id"]
    made = users.share("test", rid, "create", "commenter")
    guest = users.join_as_guest(users.resolve_share_token(token_of(made["path"])))
    s = Stream(server, f"/r/{rid}/events", signed_in(guest, False))
    assert s.until("hello") is not None
    users.share("test", rid, "regenerate")
    assert s.eof(2), "the old generation's stream must end"
    s.close()


def test_anonymous_and_no_access_answers_are_json(server, review, peer, loopback_owner_off):  # noqa: F811
    rid = review["id"]
    peer["addr"] = "192.0.2.10"
    code, headers, body = http("GET", f"{server}/r/{rid}/events")
    assert code == 401 and json.loads(body) == {"ok": False, "error": "sign in first", "signin": "/signin"}
    assert headers["Content-Type"].startswith("application/json")
    outsider = account(name="Outside")
    code, _, body = http("GET", f"{server}/r/{rid}/events", headers=signed_in(outsider, False))
    assert code == 404
    code2, _, dead = http("GET", f"{server}/r/{rid}/state.json", headers=signed_in(outsider, False))
    assert code2 == 404 and body == dead


def test_replay_live_handoff_delivers_a_persisted_event_once(server, review, monkeypatch):
    """A publish that lands between subscribe and replay is in the replay rows *and* the queue: exactly one copy."""
    rid = review["id"]
    actual_replay = live.replay
    published = []

    def publish_in_the_gap(review_id, last):
        published.append(live.publish(review_id, "thread.status", "", "system", {"handoff": "once"}))
        return actual_replay(review_id, last)

    monkeypatch.setattr(live, "replay", publish_in_the_gap)
    s = Stream(server, f"/r/{rid}/events")
    try:
        assert s.until("hello") is not None
        first = s.until("thread.status", timeout=4)
        assert first is not None and int(first["id"]) == 1, first
        assert published == [1]
        later = live.publish(rid, "thread.status", "", "system", {"handoff": "later"})
        assert later == 2
        nxt = s.until("thread.status", timeout=4)
        assert nxt is not None and int(nxt["id"]) == 2, nxt   # the queued copy of id 1 would arrive here first
    finally:
        s.close()


def test_a_reconnect_mid_burst_is_duplicate_free_and_ordered(server, review, monkeypatch):
    """Events published while the stream hands over arrive once each, in id order, with no gap after the cursor."""
    rid = review["id"]
    actual_replay = live.replay

    def replay_during_the_burst(review_id, last):
        for i in range(4):
            live.publish(review_id, "thread.status", "", "system", {"burst": i})
        time.sleep(0.05)
        return actual_replay(review_id, last)

    monkeypatch.setattr(live, "replay", replay_during_the_burst)
    s = Stream(server, f"/r/{rid}/events", headers={"Last-Event-ID": "0"})
    try:
        assert s.until("hello") is not None
        live.publish(rid, "thread.status", "", "system", {"burst": "live"})
        ids, deadline = [], time.monotonic() + 6
        while len(ids) < 5 and time.monotonic() < deadline:
            f = s.until("thread.status", timeout=max(0.5, deadline - time.monotonic()))
            assert f is not None, ids
            ids.append(int(f["id"]))
        assert ids == [1, 2, 3, 4, 5], ids
    finally:
        s.close()


def test_the_seat_cap_is_25_in_total_with_one_kept_for_the_owner():
    r = live.Registry()
    try:
        for i in range(24):
            r.subscribe("r", f"user:{i + 2}", "Member", "user")
        accepted, refused = 0, 0
        for _ in range(5):
            try:
                r.subscribe("r", "user:1", "Owner", "user", owner=True)
                accepted += 1
            except live.TooManyStreams:
                refused += 1
        assert (accepted, refused) == (1, 4)
        assert r.count("r") == 25 and r.count("r", owner=True) == 1
        with pytest.raises(live.TooManyStreams):
            r.subscribe("r", "user:99", "Late member", "user")
    finally:
        r.reset()


def persisted_ids(rid):
    return [int(r["id"]) for r in db.connect().execute(
        "SELECT id FROM events WHERE review_id = ? ORDER BY id", (rid,)).fetchall()]


def test_a_slow_fanout_cannot_let_a_later_event_overtake_an_earlier_one(server, review, monkeypatch):
    """Writing 1 and 2 concurrently: the stream must see [1, 2], because the cursor only moves forward.

    The commit and its fan-out are one step (the publisher holds the ordering lock across both), so a writer that is
    slow between its insert and its enqueue cannot have its event skipped by the floor the next event sets. On
    9a3b940 the slow writer's event never reached the wire at all.
    """
    rid = review["id"]
    s = Stream(server, f"/r/{rid}/events")
    actual_fanout = live.Registry._fanout
    try:
        assert s.until("hello") is not None

        def slow_first_fanout(self, review_id, item):
            if item[0] == 1:
                time.sleep(0.5)          # writer 2 must queue behind this on a correct tree
            actual_fanout(self, review_id, item)

        monkeypatch.setattr(live.Registry, "_fanout", slow_first_fanout)
        committed = threading.Event()

        def writer_one():
            committed.set()
            live.publish(rid, "thread.status", "", "system", {"writer": 1})

        def writer_two():
            committed.wait(5)
            time.sleep(0.05)             # 1 has committed and is still inside its fan-out
            live.publish(rid, "thread.status", "", "system", {"writer": 2})

        first, second = threading.Thread(target=writer_one), threading.Thread(target=writer_two)
        first.start(), second.start()
        first.join(10), second.join(10)
        assert not first.is_alive() and not second.is_alive()

        ids, deadline = [], time.monotonic() + 5
        while len(ids) < 2 and time.monotonic() < deadline:
            f = s.until("thread.status", timeout=max(0.2, deadline - time.monotonic()))
            if f is None:
                break
            ids.append(int(f["id"]))
        assert ids == persisted_ids(rid) == [1, 2], (ids, persisted_ids(rid))
    finally:
        s.close()


def test_concurrent_publishers_reach_the_wire_as_exactly_the_persisted_ids(server, review):
    """Four writers, three events each: no event is dropped or reordered on the wire."""
    rid = review["id"]
    s = Stream(server, f"/r/{rid}/events")
    try:
        assert s.until("hello") is not None

        def writer(n):
            for i in range(3):
                live.publish(rid, "thread.status", "", "system", {"writer": n, "n": i})

        writers = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for w in writers:
            w.start()
        for w in writers:
            w.join(15)
        assert not any(w.is_alive() for w in writers)
        want = persisted_ids(rid)
        assert len(want) == 12, want

        ids, deadline = [], time.monotonic() + 10
        while len(ids) < len(want) and time.monotonic() < deadline:
            f = s.until("thread.status", timeout=max(0.2, deadline - time.monotonic()))
            if f is None:
                break
            ids.append(int(f["id"]))
        assert ids == want, (ids, want)
    finally:
        s.close()
