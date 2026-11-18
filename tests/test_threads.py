"""The thread model: create/reply/edit/delete, caps, mentions, unread, carry-over and migrations."""

import json
import sqlite3
import uuid

import pytest

from authkit import account
from mockreview import db, live, store, threads, users
from mockreview.store import ReviewError

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}


@pytest.fixture(autouse=True)
def clean_limits():
    threads.reset_limits()
    live.reset()
    yield
    live.reset()
    threads.reset_limits()


@pytest.fixture
def owner():
    return users.owner_user()


@pytest.fixture
def rid(review):
    return review["id"]


def actor(user):
    return f"user:{user.get('id', user.get('user_id'))}"


def create(rid, owner, text="First", role="owner", **kw):
    return threads.create(rid, actor(owner), owner["name"], role, text,
                          anchor=kw.pop("anchor", {"block": "a", "x": 0.2, "y": 0.3}), **kw)


def test_thread_round_trip_and_shape(rid, owner):
    t = create(rid, owner)
    assert set(t) >= {"id", "round", "status", "activated", "carried", "anchor", "created_at", "updated_at",
                      "comments", "unread", "detached", "n"}
    assert t["status"] == "open" and t["round"] == 1 and t["n"] == 1
    assert t["anchor"] == {"block": "a", "x": 0.2, "y": 0.3}
    c = t["comments"][0]
    assert set(c) == {"id", "author", "author_name", "author_kind", "role", "text", "created_at", "edited_at",
                      "deleted_at", "mentions", "mine", "sent_to_hermes"}
    assert c["text"] == "First" and c["mine"] is True and c["author_name"] == owner["name"]
    assert c["author_kind"] == "member"

    t = threads.reply(rid, t["id"], actor(owner), owner["name"], "owner", "A reply")
    assert [x["text"] for x in t["comments"]] == ["First", "A reply"]
    cid = t["comments"][1]["id"]
    t = threads.edit_comment(rid, cid, actor(owner), "Edited reply")
    assert t["comments"][1]["text"] == "Edited reply" and t["comments"][1]["edited_at"] is not None
    t = threads.set_status(rid, t["id"], "resolved", actor(owner))
    assert t["status"] == "resolved"
    with pytest.raises(ReviewError) as e:
        threads.reply(rid, t["id"], actor(owner), owner["name"], "owner", "nope")
    assert e.value.status == 400 and "Reopen" in e.value.message
    t = threads.set_status(rid, t["id"], "open", actor(owner))
    assert [x["text"] for x in threads.reply(rid, t["id"], actor(owner), owner["name"], "owner", "Again")["comments"]][-1] == "Again"


def test_last_comment_deletion_removes_the_thread(rid, owner):
    t = create(rid, owner, "One")
    t = threads.reply(rid, t["id"], actor(owner), owner["name"], "owner", "Two")
    first, second = t["comments"]
    kept = threads.delete_comment(rid, first["id"], actor(owner))
    assert kept is not None and kept["id"] == t["id"] and len(kept["comments"]) == 2
    assert kept["comments"][0]["deleted_at"] is not None and kept["comments"][0]["text"] == ""
    assert kept["comments"][0]["author_name"] == owner["name"]
    assert threads.delete_comment(rid, second["id"], actor(owner)) is None
    assert threads.list_threads(rid, actor(owner)) == []
    kinds = [r["kind"] for r in db.connect().execute("SELECT kind FROM events WHERE review_id = ? ORDER BY id", (rid,))]
    assert "thread.deleted" in kinds and "comment.deleted" in kinds


def test_text_caps_and_controls(rid, owner):
    with pytest.raises(ReviewError):
        create(rid, owner, "   \n\t ")
    with pytest.raises(ReviewError) as e:
        create(rid, owner, "x" * 2001)
    assert e.value.status == 400 and "2000" in e.value.message
    t = create(rid, owner, "  a\x01b  \r\n  c\t  ")
    assert t["comments"][0]["text"] == "ab\n  c"
    big = {"block": "a", "x": 0.2, "y": 0.3, "target": {"path": "#x", "sig": {"tag": "p", "h": "abcd"}, "label": "x" * 120}}
    assert len(json.dumps(threads.clean_anchor(big))) < threads.MAX_ANCHOR_BYTES
    with pytest.raises(ReviewError):
        threads.clean_anchor({"block": "a" * 65})
    old = threads.MAX_ANCHOR_BYTES
    threads.MAX_ANCHOR_BYTES = 100
    try:
        with pytest.raises(ReviewError) as e:
            threads.clean_anchor(big)
        assert e.value.status == 400 and "large" in e.value.message
    finally:
        threads.MAX_ANCHOR_BYTES = old
    with pytest.raises(ReviewError):
        threads.clean_anchor({"block": "a", "target": {"path": "#x", "sig": {"tag": "p", "h": "abcd"}, "evil": 1}})


def test_open_thread_cap(rid, owner):
    now = 1.0
    with db.tx() as conn:
        for i in range(threads.MAX_OPEN_THREADS):
            tid, cid = uuid.uuid4().hex, uuid.uuid4().hex
            conn.execute("INSERT INTO threads (id, review_id, round, anchor, status, activated, carried, created_by, "
                         "created_at, updated_at) VALUES (?, ?, 1, '{}', 'open', 0, 0, ?, ?, ?)",
                         (tid, rid, actor(owner), now + i, now + i))
            conn.execute("INSERT INTO comments (id, thread_id, author, author_name, text, idem, created_at) "
                         "VALUES (?, ?, ?, ?, 'x', '', ?)", (cid, tid, actor(owner), owner["name"], now + i))
    with pytest.raises(ReviewError) as e:
        create(rid, owner, "one too many")
    assert e.value.status == 400 and "open threads" in e.value.message


def test_comments_per_thread_and_rate_limit(rid, owner, monkeypatch):
    monkeypatch.setattr(threads, "RATE_WRITES", 1000)
    t = create(rid, owner)
    for i in range(threads.MAX_COMMENTS - 1):
        t = threads.reply(rid, t["id"], actor(owner), owner["name"], "owner", f"reply {i}")
    assert len([c for c in t["comments"] if c["deleted_at"] is None]) == threads.MAX_COMMENTS
    with pytest.raises(ReviewError) as e:
        threads.reply(rid, t["id"], actor(owner), owner["name"], "owner", "full")
    assert e.value.status == 400 and "full" in e.value.message

    threads.reset_limits()
    monkeypatch.setattr(threads, "RATE_WRITES", 20)
    second = create(rid, owner, "rate")
    for i in range(19):
        threads.reply(rid, second["id"], actor(owner), owner["name"], "owner", f"r{i}")
    with pytest.raises(ReviewError) as e:
        threads.reply(rid, second["id"], actor(owner), owner["name"], "owner", "21st")
    assert e.value.status == 429 and e.value.message == "slow down"


def test_mentions_and_activation(rid, owner):
    commenter = account(review_id=rid, role="commenter", name="Maya")
    reviewer = account(review_id=rid, role="reviewer", name="Ravi")
    outsider = account(name="Outside")

    with pytest.raises(ReviewError) as e:
        create(rid, owner, "hi", mentions=["user:%s" % outsider["user_id"]])
    assert e.value.status == 400

    t = create(rid, owner, "for you", mentions=["user:%s" % commenter["user_id"], "hermes"])
    assert t["activated"] is True and len(t["comments"][0]["mentions"]) == 2
    assert t["comments"][0]["sent_to_hermes"] is True

    plain = create(rid, owner, "not active", mentions=["hermes"], role="commenter", can_activate=False)
    assert plain["activated"] is False and plain["comments"][0]["sent_to_hermes"] is False
    assert [m["kind"] for m in plain["comments"][0]["mentions"]] == ["hermes"]

    rev = create(rid, owner, "reviewer activates", mentions=["hermes"], role="reviewer", can_activate=True)
    assert rev["activated"] is True


def test_unread_and_activity_cursors_are_independent(rid, owner):
    other = account(review_id=rid, role="commenter", name="Maya")
    t = create(rid, owner, "first")
    assert threads.list_threads(rid, actor(owner))[0]["unread"] is False
    t = threads.reply(rid, t["id"], actor(other), "Maya", "commenter", "from Maya")
    assert threads.list_threads(rid, actor(owner))[0]["unread"] is True
    threads.mark_read(rid, actor(owner), t["id"])
    assert threads.list_threads(rid, actor(owner))[0]["unread"] is False

    threads.mark_read(rid, actor(owner), "")
    assert threads.activity_unread(rid, actor(owner)) == 0
    threads.reply(rid, t["id"], actor(other), "Maya", "commenter", "another")
    assert threads.activity_unread(rid, actor(owner)) >= 1
    threads.mark_read(rid, actor(owner), t["id"])       # a thread read does not move the activity cursor
    assert threads.activity_unread(rid, actor(owner)) >= 1
    threads.mark_read(rid, actor(owner), "")
    assert threads.activity_unread(rid, actor(owner)) == 0
    assert threads.activity(rid, actor(owner))[0]["unread"] is False


def _decisions(options=("a", "b"), block="d1"):
    return {"blocks": [{"type": "decision", "id": block, "title": "Pick", "options": [
        {"id": o, "label": o.upper(), "html": "<p>%s</p>" % o} for o in options]}]}


def _round(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return store.parse_round_spec(decisions=str(p))


def test_carry_over_and_detached(env, tmp_path, owner):
    data = store.create_review("Carry", "", ORIGIN, "local", _round(tmp_path, "r1.json", _decisions()))
    rid = data["id"]
    t = threads.create(rid, actor(owner), owner["name"], "owner", "pinned",
                       anchor={"block": "d1", "option": "a", "x": 0.4, "y": 0.6})
    store.add_round(rid, _round(tmp_path, "r2.json", _decisions(options=("a", "c"))))
    rows = threads.list_threads(rid, actor(owner), round_n=2)
    assert len(rows) == 1 and rows[0]["carried"] is True and rows[0]["detached"] is False
    assert rows[0]["anchor"] == t["anchor"]
    store.add_round(rid, _round(tmp_path, "r3.json", _decisions(block="d9")))
    rows = threads.list_threads(rid, actor(owner), round_n=3)
    assert rows[0]["carried"] is True and rows[0]["detached"] is True
    assert rows[0]["anchor"] == t["anchor"]


def test_a_mock_or_generated_ref_is_not_detached(env, tmp_path, owner):
    """A pin on a context mock, or on a block the board gave a generated ref, still resolves: only a ref the round no
    longer renders is `detached` (the amber "Element removed" pin)."""
    dec = {"blocks": [{"type": "mock", "id": "m1", "html": "<p>total</p>"},
                      {"type": "text", "text": "a paragraph with no id"}]}
    data = store.create_review("Mocked", "", ORIGIN, "local", _round(tmp_path, "m.json", dec))
    rid = data["id"]
    on_mock = threads.create(rid, actor(owner), owner["name"], "owner", "on the mock",
                             anchor={"block": "m1", "x": 0.2, "y": 0.3})
    on_text = threads.create(rid, actor(owner), owner["name"], "owner", "on the paragraph",
                             anchor={"block": "s1", "x": 0.5, "y": 0.5})
    gone = threads.create(rid, actor(owner), owner["name"], "owner", "on a block r2 dropped",
                          anchor={"block": "m9", "x": 0.1, "y": 0.1})
    rows = {t["id"]: t for t in threads.list_threads(rid, actor(owner))}
    assert rows[on_mock["id"]]["detached"] is False
    assert rows[on_text["id"]]["detached"] is False
    assert rows[gone["id"]]["detached"] is True


def test_a_version_one_database_migrates_without_data_loss(env, review):
    conn = sqlite3.connect(str(db.path()))
    try:
        db.migrate(conn, db.MIGRATIONS[:1])
        conn.execute("INSERT INTO users (name, email, role, source, created_at) VALUES ('Old', '', 'member', 'invite', 1.0)")
        conn.commit()
    finally:
        conn.close()
    db.forget_migrations()
    db.close_all()
    conn = db.connect()
    assert db.version(conn) == db.MIGRATIONS[-1][0] >= 3
    assert conn.execute("SELECT name FROM users").fetchone()[0] == "Old"
    assert store.load(review["id"])["id"] == review["id"]
    assert threads.list_threads(review["id"]) == []
    assert threads.summary(review["id"]) == {"open": 0, "rev": 0}


def test_a_deleted_user_reads_as_former_member(rid, owner):
    member = account(review_id=rid, role="commenter", name="Maya")
    t = threads.create(rid, actor(member), "Maya", "commenter", "gone soon")
    users.delete_user(actor(owner), member["user_id"])
    row = threads.list_threads(rid, actor(owner))[0]
    assert row["comments"][0]["author_name"] == "Former member"


def test_targetless_board_pin_keeps_its_page_spot():
    """Canvas pins x live threads: a board pin with no target posts its last page spot as a top-level `doc` (px)."""
    from mockreview import threads
    out = threads.clean_anchor({"block": "s1", "on": "board", "w": "desktop", "x": 0.5, "y": 0.25,
                                "doc": {"x": 640.4, "y": 2200.126}})
    assert out["doc"] == {"x": 640.4, "y": 2200.13}
    assert "target" not in out
    # a pin with a target keeps the spot inside target.doc, never a second top-level copy
    out = threads.clean_anchor({"block": "s1", "on": "board", "target": {"path": "#a", "sig": {"tag": "p", "h": "abcd"}, "x": 0.1,
                                "y": 0.2, "doc": {"x": 1, "y": 2}}, "doc": {"x": 9, "y": 9}})
    assert "doc" not in out and out["target"]["doc"] == {"x": 1.0, "y": 2.0}


def test_shared_events_drop_mine_and_unread_but_the_read_model_keeps_them(rid, owner):
    t = create(rid, owner)
    threads.reply(rid, t["id"], actor(owner), owner["name"], "owner", "A reply")
    rows = db.connect().execute("SELECT kind, data FROM events WHERE review_id = ? ORDER BY id", (rid,)).fetchall()
    created = [json.loads(r["data"]) for r in rows if r["kind"] == "thread.created"][0]
    replied = [json.loads(r["data"]) for r in rows if r["kind"] == "thread.replied"][0]
    for event in (created, replied):
        assert "unread" not in event["thread"]
        assert all("mine" not in c for c in event["thread"]["comments"])
    member = account(review_id=rid, role="commenter", name="Maya")
    mine = threads.listing(rid, principal=actor(member))["threads"][0]["comments"]
    assert [c["mine"] for c in mine] == [False, False]
    own = threads.listing(rid, principal=actor(owner))["threads"][0]["comments"]
    assert [c["mine"] for c in own] == [True, True]
