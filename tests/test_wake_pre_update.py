"""Every store or path that can carry state created before this update into a wake or a send: one test per source.

The rule this file keeps: a record with no explicit post-update owner confirmation is never delivered. The older
routes had no per-request ingress proof, so a record queued before the request gate existed may have arrived through a
public path. Only a fresh owner action (Send from private ingress, or re-confirming Reviewer in Share) authorizes it.

Most tests here are red on the pre-fix head and guard the fix afterwards; a few are controls (a source that is safe
already) and stay green on both, named as such in `.dispatch/pr68-r2/class-table.md`.
"""

import json
import sqlite3
import time

import pytest

from authkit import account, signed_in, token_of
from conftest import http
from mockreview import config, db, service, store, threads, users, wakeguard
from test_mention_wake import board as mention_board, tick, wake_records
from test_owner_wake_gate import (_legacy_mention, _legacy_pin_fix, _legacy_send, funnel,  # noqa: F401
                                  join_guest, mention, pin_board, pin_requests)

FUNNEL_URL = "https://node.tailnet.example.test"


# ---------------------------------------------------------------- invites


def _schema5_invite(review_role="reviewer", all_reviews=False, review_id="old-review"):
    """A real invite row written at schema 5, the shape an install had before this update, and its token."""
    db.path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db.path()), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        assert db.migrate(conn, db.MIGRATIONS[:5]) == 5
        nonce = "pre-update-invite-nonce"
        iid = conn.execute(
            "INSERT INTO invites (nonce_hash, role, review_id, all_reviews, review_role, created_by, created_at, "
            "expires_at) VALUES (?, 'member', ?, ?, ?, 'test', ?, ?)",
            (users.digest(nonce), review_id, 1 if all_reviews else 0, review_role, users.now(),
             users.now() + 86400)).lastrowid
        return users._timed(conn, "invite").dumps({"i": iid, "n": nonce})
    finally:
        conn.close()


def test_a_pre_update_reviewer_invite_does_not_wake_after_redemption(env):
    token = _schema5_invite()
    out = users.redeem("invite", token, name="Invited reviewer")
    grant = db.connect().execute("SELECT role, wake_ok FROM grants WHERE user_id = ? AND review_id = ?",
                                 (out["user_id"], "old-review")).fetchone()
    assert dict(grant) == {"role": "reviewer", "wake_ok": 0}, dict(grant)
    assert users.wake_ok_for("old-review", f"user:{out['user_id']}") is False


def test_a_pre_update_all_reviews_reviewer_invite_does_not_wake_after_redemption(env):
    token = _schema5_invite(all_reviews=True, review_id=None)
    out = users.redeem("invite", token, name="Invited all reviews")
    grant = db.connect().execute("SELECT role, all_reviews, wake_ok FROM grants WHERE user_id = ?",
                                 (out["user_id"],)).fetchone()
    assert dict(grant) == {"role": "reviewer", "all_reviews": 1, "wake_ok": 0}, dict(grant)
    assert users.wake_ok_for("any-review", f"user:{out['user_id']}") is False


def test_a_fresh_reviewer_invite_still_wakes_after_redemption(env):
    inv = users.create_invite("test", role="member", review_id="fresh-review", review_role="reviewer")
    out = users.redeem("invite", token_of(inv["path"]), name="Fresh reviewer")
    assert users.wake_ok_for("fresh-review", f"user:{out['user_id']}") is True


def test_a_redeemed_pre_update_invite_keeps_its_board_role(env):
    token = _schema5_invite()
    out = users.redeem("invite", token, name="Still a reviewer")
    assert users.grant_role(out["user_id"], "old-review") == "reviewer"


# ---------------------------------------------------------------- share links and guest rows


def _queued_guest_mention(server, tmp_path):
    rid = mention_board(tmp_path)["id"]
    guest = join_guest(rid)
    code, res = mention(server, rid, signed_in(guest))
    assert code == 201 and res["wake"]["status"] == "pending_notify", res
    return rid, guest


def _failed_reason(rid):
    rec = next(iter(wake_records(rid).values()))
    chip = pin_requests(rid)[rec["request_id"]]
    return rec, chip


def test_a_queued_guest_mention_after_the_link_expires_is_not_sent(env, server, tmp_path):
    rid, _guest = _queued_guest_mention(server, tmp_path)
    with db.tx() as conn:
        conn.execute("UPDATE share_links SET expires_at = ? WHERE review_id = ?", (time.time() - 1, rid))
    tick()
    assert env["receiver"].wakes == []
    rec, chip = _failed_reason(rid)
    assert rec["status"] == "failed" and rec["last_error"] == wakeguard.REVIEWER_WAKE_ENDED, rec
    assert chip["status"] == "failed" and chip["detail"] == wakeguard.REVIEWER_WAKE_ENDED, chip


def test_a_queued_guest_mention_after_the_link_is_stopped_says_the_link_ended(env, server, tmp_path):
    rid, _guest = _queued_guest_mention(server, tmp_path)
    users.share("test", rid, "stop")
    tick()
    assert env["receiver"].wakes == []
    rec, chip = _failed_reason(rid)
    assert rec["status"] == "failed" and rec["last_error"] == wakeguard.REVIEWER_WAKE_ENDED, rec
    assert chip["detail"] == wakeguard.REVIEWER_WAKE_ENDED, chip


def test_a_queued_guest_mention_after_regeneration_says_the_link_ended(env, server, tmp_path):
    rid, _guest = _queued_guest_mention(server, tmp_path)
    users.share("test", rid, "regenerate")
    tick()
    assert env["receiver"].wakes == []
    rec, _chip = _failed_reason(rid)
    assert rec["status"] == "failed" and rec["last_error"] == wakeguard.REVIEWER_WAKE_ENDED, rec


def test_a_queued_guest_mention_from_an_older_link_generation_is_not_sent(env, server, tmp_path):
    rid, _guest = _queued_guest_mention(server, tmp_path)
    with db.tx() as conn:   # the guest row survived a generation bump, as a pre-update row could
        conn.execute("UPDATE share_links SET generation = generation + 1 WHERE review_id = ?", (rid,))
    tick()
    assert env["receiver"].wakes == []
    rec, _chip = _failed_reason(rid)
    assert rec["status"] == "failed" and rec["last_error"] == wakeguard.REVIEWER_WAKE_ENDED, rec


def test_a_queued_guest_mention_on_a_members_only_link_is_not_sent(env, server, tmp_path):
    rid, _guest = _queued_guest_mention(server, tmp_path)
    with db.tx() as conn:   # a guest row that outlived the audience change
        conn.execute("UPDATE share_links SET audience = 'members' WHERE review_id = ?", (rid,))
    tick()
    assert env["receiver"].wakes == []
    rec, _chip = _failed_reason(rid)
    assert rec["status"] == "failed" and rec["last_error"] == wakeguard.REVIEWER_WAKE_ENDED, rec


def test_a_queued_guest_mention_on_a_live_confirmed_link_is_sent_once(env, server, tmp_path):
    rid, _guest = _queued_guest_mention(server, tmp_path)
    tick()
    assert len(env["receiver"].wakes) == 1
    rec = next(iter(wake_records(rid).values()))
    assert rec["status"] == "notified", rec


def test_a_queued_guest_mention_survives_the_guest_session_expiring_while_the_link_is_live(env, server, tmp_path):
    """Delivery follows the link, not the cookie: the record was admitted while the link was live and the owner's
    confirmation is still there. It ends with the link, never with one browser session."""
    rid, guest = _queued_guest_mention(server, tmp_path)
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE guest_id = (SELECT id FROM guests WHERE review_id = ?)", (rid,))
    tick()
    assert len(env["receiver"].wakes) == 1
    assert users.wake_ok_for(rid, f"guest:{guest['guest_id']}") is True


# ---------------------------------------------------------------- grants


def test_a_pre_update_reviewer_grant_needs_reconfirmation(env, tmp_path):
    rid = mention_board(tmp_path)["id"]
    member = account(review_id=rid, role="reviewer")
    with db.tx() as conn:
        conn.execute("UPDATE grants SET wake_ok = 0 WHERE user_id = ? AND review_id = ?", (member["user_id"], rid))
    assert users.wake_ok_for(rid, f"user:{member['user_id']}") is False
    users.grant("test", member["user_id"], "reviewer", review_id=rid)
    assert users.wake_ok_for(rid, f"user:{member['user_id']}") is True


# ---------------------------------------------------------------- submissions


def _sent(monkeypatch):
    sent = []
    monkeypatch.setattr(service.notify, "send",
                        lambda payload, delivery_id, url=None: (sent.append(delivery_id) or (True, "ok")))
    return sent


@pytest.mark.parametrize("funnel_mapped", [True, False], ids=["funnel-held", "no-funnel-delivered"])
def test_a_legacy_admin_send_follows_the_funnel_hold(env, review, monkeypatch, funnel_mapped):
    if funnel_mapped:
        monkeypatch.setattr(config, "funnel_url", lambda: FUNNEL_URL)
    owner = users.owner_user()
    rid, token = review["id"], review["token"]
    _legacy_send(rid, token, f"user:{owner['id']}", monkeypatch)
    sent = _sent(monkeypatch)

    wake, _ack = service.deliver_submission(rid, 1)
    sub = store.load(rid)["rounds"][0]["submission"]
    if funnel_mapped:
        assert sent == [] and wake == (False, service.LEGACY_FUNNEL_NO_WAKE), wake
        assert sub["status"] == "stored" and sub["stored_detail"] == service.LEGACY_FUNNEL_NO_WAKE, sub
    else:
        assert sent == [f"mockreview-{rid}-r1-s1"] and wake == (True, "ok"), wake
        assert sub["status"] == "notified", sub


@pytest.mark.parametrize("funnel_mapped", [True, False], ids=["funnel-held", "no-funnel-delivered"])
def test_a_member_promoted_after_queueing_follows_the_funnel_hold(env, review, monkeypatch, funnel_mapped):
    """A current admin role is not historical ingress proof; a Funnel mapping still holds the record."""
    if funnel_mapped:
        monkeypatch.setattr(config, "funnel_url", lambda: FUNNEL_URL)
    member = account(instance="member", name="Member")
    rid, token = review["id"], review["token"]
    _legacy_send(rid, token, f"user:{member['user_id']}", monkeypatch)
    users.set_role("test", member["user_id"], "admin")
    sent = _sent(monkeypatch)

    wake, _ack = service.deliver_submission(rid, 1)
    if funnel_mapped:
        assert sent == [] and wake == (False, service.LEGACY_FUNNEL_NO_WAKE), wake
    else:
        assert sent == [f"mockreview-{rid}-r1-s1"] and wake == (True, "ok"), wake


# ---------------------------------------------------------------- pin fixes


@pytest.mark.parametrize("funnel_mapped", [True, False], ids=["funnel-held", "no-funnel-delivered"])
def test_a_legacy_admin_pin_fix_follows_the_funnel_hold(env, tmp_path, funnel_mapped, monkeypatch):
    if funnel_mapped:
        monkeypatch.setattr(config, "funnel_url", lambda: FUNNEL_URL)
    data = pin_board(tmp_path)
    pid = _legacy_pin_fix(data["id"], f"user:{users.owner_user()['id']}")

    ok, detail = service.flush_pin_fixes(data["id"])
    q = pin_requests(data["id"])[pid]
    if funnel_mapped:
        assert ok is False and detail == service.LEGACY_FUNNEL_NO_WAKE, (ok, detail)
        assert env["receiver"].wakes == []
        assert q["quiet"] is True and q["detail"] == service.LEGACY_FUNNEL_NO_WAKE, q
    else:
        assert ok is True and len(env["receiver"].wakes) == 1
        assert q["status"] == "woken", q


def _pending_wake(rid, ids):
    """A pin-fix wake batch that was already `pending_notify` before the upgrade."""
    with store.locked(rid):
        data = store.load(rid)
        r = data["rounds"][-1]
        r["pin_fix"] = {"seq": 1, "wake": {"status": "pending_notify", "requests": list(ids),
                                           "delivery_id": f"mockreview-{rid}-r{r['n']}-p1", "attempts": 1,
                                           "next_attempt": 0.0, "sending_until": 0.0, "last_error": "gateway down",
                                           "notified_at": None}}
        for i in ids:
            r["pin_requests"][i]["wake_seq"] = 1
        store.save(rid, data)


def test_a_legacy_pending_pin_wake_on_a_funnel_board_is_held(env, tmp_path, funnel, monkeypatch):
    data = pin_board(tmp_path)
    pid = _legacy_pin_fix(data["id"], f"user:{users.owner_user()['id']}")
    _pending_wake(data["id"], [pid])
    ok, detail = service.flush_pin_fixes(data["id"])
    wake = store.load(data["id"])["rounds"][-1]["pin_fix"]["wake"]
    assert env["receiver"].wakes == []
    assert ok is False and detail == service.LEGACY_FUNNEL_NO_WAKE, (ok, detail)
    assert wake["status"] == "failed" and wake["last_error"] == service.LEGACY_FUNNEL_NO_WAKE, wake
    assert pin_requests(data["id"])[pid]["detail"] == service.LEGACY_FUNNEL_NO_WAKE


def test_a_legacy_pending_pin_wake_by_a_member_is_not_sent(env, tmp_path):
    data = pin_board(tmp_path)
    member = account(instance="member", name="Member")
    pid = _legacy_pin_fix(data["id"], f"user:{member['user_id']}")
    _pending_wake(data["id"], [pid])
    service.flush_pin_fixes(data["id"])
    assert env["receiver"].wakes == []
    wake = store.load(data["id"])["rounds"][-1]["pin_fix"]["wake"]
    assert wake["status"] == "failed" and wake["last_error"] == service.LEGACY_QUEUED_NO_WAKE, wake


def test_a_legacy_pending_pin_wake_without_a_funnel_sends_only_the_admin(env, tmp_path):
    data = pin_board(tmp_path)
    member = account(instance="member", name="Member")
    mine = _legacy_pin_fix(data["id"], f"user:{users.owner_user()['id']}")
    theirs = _legacy_pin_fix(data["id"], f"user:{member['user_id']}")
    _pending_wake(data["id"], [mine, theirs])
    ok, _detail = service.flush_pin_fixes(data["id"])
    assert ok is True
    assert [json.loads(w["body"])["requests"] for w in env["receiver"].wakes] == [[mine]]
    assert theirs not in (store.load(data["id"])["rounds"][-1]["pin_fix"]["wake"]["requests"] or [])


# ---------------------------------------------------------------- mention wakes


@pytest.mark.parametrize("funnel_mapped", [True, False], ids=["funnel-held", "no-funnel-delivered"])
def test_a_legacy_admin_mention_follows_the_funnel_hold(env, server, tmp_path, funnel_mapped, monkeypatch):
    if funnel_mapped:
        monkeypatch.setattr(config, "funnel_url", lambda: FUNNEL_URL)
    rid = mention_board(tmp_path)["id"]
    comment_id = _legacy_mention(server, rid, None, "legacy-owner")

    tick()
    rec = wake_records(rid)[comment_id]
    chip = pin_requests(rid)[rec["request_id"]]
    if funnel_mapped:
        assert env["receiver"].wakes == []
        assert rec["status"] == "failed" and rec["last_error"] == service.LEGACY_FUNNEL_NO_WAKE, rec
        assert chip["status"] == "failed" and chip["detail"] == service.LEGACY_FUNNEL_NO_WAKE, chip
    else:
        assert len(env["receiver"].wakes) == 1
        assert rec["status"] == "notified", rec


# ---------------------------------------------------------------- notices


def test_a_notice_only_posts_a_chat_message_and_never_wakes(env, review, monkeypatch, funnel):
    """A notice goes to the deliver-only `<route>-<platform>-notice` route, which renders a chat message; it never
    starts an agent turn, and the ack waits for an accepted wake that a held legacy record never got."""
    owner = users.owner_user()
    rid, token = review["id"], review["token"]
    _legacy_send(rid, token, f"user:{owner['id']}", monkeypatch)
    service.check_stalled(now=time.time() + 16 * 60)
    assert env["receiver"].wakes == []
    sub = store.load(rid)["rounds"][0]["submission"]
    assert sub.get("notice") is None or sub["notice"].get("status") != "notified", sub
    assert any(r["path"].endswith("-notice") for r in env["receiver"].requests)


# ---------------------------------------------------------------- access tokens


def test_an_agent_token_reply_never_queues_a_wake(env, server, tmp_path):
    """A bearer is an explicit agent credential: it writes a comment, never an @Hermes wake (the server's token
    half of the mention gate). No `by_owner` record is created, so nothing can be delivered later."""
    rid = mention_board(tmp_path)["id"]
    owner = users.owner_user()
    thread = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned note",
                            anchor={"block": "m1", "x": 0.3, "y": 0.4})
    token = users.create_token(owner["id"], "agent")["token"]
    code, _, out = http("POST", f"{server}/r/{rid}/api/threads/{thread['id']}/reply",
                        {"text": "@Hermes from an agent", "mentions": ["hermes"]},
                        headers={"Authorization": f"Bearer {token}"}, csrf=False)
    assert code == 200 and "wake" not in json.loads(out), out
    tick()
    assert env["receiver"].wakes == [] and wake_records(rid) == {}


# ---------------------------------------------------------------- database failures


def test_a_db_error_while_reading_the_confirmation_fails_closed(monkeypatch):
    """A database error in the mention gate answers "saved, not woken" with a reason, never a 500 and never a
    silently pending batch (security round 2, Low)."""

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "connect", broken)
    assert users.wake_reason("r", "user:1") == wakeguard.REVIEWER_WAKE_UNCONFIRMED
    assert users.wake_ok_for("r", "guest:g1") is False
