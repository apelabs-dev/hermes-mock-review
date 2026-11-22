"""FIX-AMEND: a share-link guest can never replace a member's feedback or reach Hermes.

Send is the owner's alone: the board server refuses a guest's or a member reviewer's Send, amend and Build with 403
before service.submit runs (wakeguard.SUBMIT_OWNER_ONLY). service.submit keeps its own guest rules behind that, so a
guest record that reaches it some other way (an older board, a future route) is still stored, owner-only and never
delivered. The service-level tests below build that record by calling service.submit the way the route used to.

Every submission records its author; a guest's canonical record is owner-only data, and every agent-facing reader
(api.get_feedback, feedback.md/feedback.json) is guest-blind. The deep copy of FEEDBACK carries a unique string so a
leak into a wake body, an ack, a file or a reader result is easy to see.
"""

import json

import pytest

from authkit import account, signed_in, token_of
from conftest import FEEDBACK, SECRET, http
from mockreview import api, config, service, store, users, wakeguard
from test_plan_format import PLAN, _all_resolved

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
GUEST_A = "GUEST-AMEND-A-4f71"
GUEST_B = "GUEST-AMEND-B-9c02"
OWNER_TEXT = "Keep the table."


def join_guest(rid, role="reviewer"):
    made = users.share("test", rid, "create", role)
    return made, users.join_as_guest(users.resolve_share_token(token_of(made["path"])))


def submit_url(server, rid):
    return f"{server}/r/{rid}/api/submit"


def guest_feedback(text):
    fb = json.loads(json.dumps(FEEDBACK))
    fb["guidelines"] = text
    return fb


def guest_submit(rid, guest, token, feedback, amend=False):
    """A guest's Send as service.submit sees it: what the route passed before Send became the owner's alone."""
    return service.submit(rid, 1, feedback, token, amend=amend, source="local", no_wake_reason=service.GUEST_NO_WAKE,
                          author=f"guest:{guest['guest_id']}", guest=True)


def stored_submission(rid):
    return store.load(rid)["rounds"][0]["submission"]


def dump(sub):
    return json.dumps(sub, sort_keys=True)


def assert_no_guest_text(requests, text):
    for r in requests:
        assert text not in r["body"].decode("utf-8"), r["path"]


def assert_route_refuses(server, rid, guest, body):
    code, _headers, out = http("POST", submit_url(server, rid), body, signed_in(guest))
    assert code == 403 and json.loads(out)["error"] == wakeguard.SUBMIT_OWNER_ONLY, out


# ---------------------------------------------------------------- negative


def test_a_guest_cannot_amend_a_members_pending_submission(env, server, review, secret_values):
    rid = review["id"]
    _made, guest = join_guest(rid)
    secret_values.clear()
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _headers, out = http("POST", submit_url(server, rid), body)
    assert code == 200 and json.loads(out)["status"] == "pending_notify", out
    before = dump(stored_submission(rid))

    assert_route_refuses(server, rid, guest, {**body, "feedback": guest_feedback(GUEST_A), "amend": True})
    with pytest.raises(store.ReviewError, match="someone else") as e:
        guest_submit(rid, guest, review["token"], guest_feedback(GUEST_A), amend=True)
    assert e.value.status == 409
    assert dump(stored_submission(rid)) == before
    assert stored_submission(rid)["status"] == "pending_notify"

    secret_values["MOCKREVIEW_WEBHOOK_SECRET"] = SECRET
    assert service.retry_pending() == 1
    assert len(env["receiver"].wakes) == 1
    wake = env["receiver"].wakes[0]["body"].decode("utf-8")
    assert OWNER_TEXT in wake
    assert_no_guest_text(env["receiver"].requests, GUEST_A)
    got = api.get_feedback(rid)
    assert got["feedback"]["guidelines"] == OWNER_TEXT
    assert GUEST_A not in json.dumps(got)


def test_a_guest_cannot_change_a_notified_members_submission(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _headers, out = http("POST", submit_url(server, rid), body)
    assert code == 200 and json.loads(out)["status"] == "notified", out
    before = dump(stored_submission(rid))

    assert_route_refuses(server, rid, guest, {**body, "feedback": guest_feedback(GUEST_A), "amend": True})
    with pytest.raises(store.ReviewError, match="someone else"):
        guest_submit(rid, guest, review["token"], guest_feedback(GUEST_A), amend=True)
    after = stored_submission(rid)
    assert dump(after) == before
    assert after["status"] == "notified" and after["seq"] == 1
    assert len(env["receiver"].requests) == 2
    assert env["receiver"].requests[1]["path"].endswith("-notice")
    assert_no_guest_text(env["receiver"].requests, GUEST_A)


def test_a_guests_send_is_refused_at_the_route_and_a_stored_one_never_reaches_the_agent_readers(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": review["token"], "feedback": guest_feedback(GUEST_A)}
    assert_route_refuses(server, rid, guest, body)
    assert store.load(rid)["rounds"][0].get("submission") is None

    res = guest_submit(rid, guest, review["token"], guest_feedback(GUEST_A))
    assert res["status"] == "stored" and res["notified"] is False, res
    assert res["detail"] == service.GUEST_NO_WAKE
    assert env["receiver"].requests == []

    got = api.get_feedback(rid)
    assert got["feedback"] is None and got["feedback_path"] is None, got
    assert GUEST_A not in got["markdown"]
    assert "next_step" not in got and "next_round" not in got, got

    rounds = config.data_root() / rid / "rounds" / "1"
    md = (rounds / "feedback.md").read_text(encoding="utf-8")
    saved = json.loads((rounds / "feedback.json").read_text(encoding="utf-8"))
    assert GUEST_A not in md
    assert saved["feedback"] is None
    assert GUEST_A not in json.dumps(saved)
    assert service.retry_pending() == 0
    assert env["receiver"].requests == []


# ---------------------------------------------------------------- positive


def test_a_guests_stored_record_is_amended_only_by_that_guest_and_never_over_http(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    assert guest_submit(rid, guest, review["token"], guest_feedback(GUEST_A))["status"] == "stored"

    assert_route_refuses(server, rid, guest, {"round": 1, "token": review["token"],
                                              "feedback": guest_feedback(GUEST_B), "amend": True})
    assert store.load(rid)["rounds"][0]["feedback"]["guidelines"] == GUEST_A

    res = guest_submit(rid, guest, review["token"], guest_feedback(GUEST_B), amend=True)
    assert res["amended"] is True and res["status"] == "stored", res
    assert res["notified"] is False and res["detail"] == service.GUEST_NO_WAKE
    rnd = store.load(rid)["rounds"][0]
    assert rnd["feedback"]["guidelines"] == GUEST_B
    assert rnd["submission"]["seq"] == 2 and rnd["submission"]["guest"] is True
    assert env["receiver"].requests == []

    got = api.get_feedback(rid)
    assert got["feedback"] is None and GUEST_B not in json.dumps(got)


def test_a_member_reviewer_cannot_send_and_the_owner_amends_its_own_submission(env, server, review):
    rid = review["id"]
    reviewer = account(review_id=rid, role="reviewer")
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    for amend in (False, True):
        code, _headers, out = http("POST", submit_url(server, rid), {**body, "amend": amend}, signed_in(reviewer))
        assert code == 403 and json.loads(out)["error"] == wakeguard.SUBMIT_OWNER_ONLY, out
    assert store.load(rid)["rounds"][0].get("submission") is None

    code, _headers, out = http("POST", submit_url(server, rid), body)
    assert code == 200 and json.loads(out)["status"] == "notified", out

    amended = guest_feedback("Actually: drop the table.")
    code, _headers, out = http("POST", submit_url(server, rid), {**body, "feedback": amended, "amend": True})
    res = json.loads(out)
    assert code == 200 and res["amended"] is True and res["status"] == "notified", out
    assert stored_submission(rid)["seq"] == 2
    rec = env["receiver"].requests
    assert len(rec) == 4
    wake = json.loads(rec[2]["body"])
    assert wake["amended"] is True
    assert "Actually: drop the table." in rec[2]["body"].decode("utf-8")


def test_the_owner_can_supersede_a_guests_submission(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    assert guest_submit(rid, guest, review["token"], guest_feedback(GUEST_A))["status"] == "stored"

    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK, "amend": True}
    code, _headers, out = http("POST", submit_url(server, rid), body)
    res = json.loads(out)
    assert code == 200 and res["status"] == "notified", out
    rnd = store.load(rid)["rounds"][0]
    assert rnd["submission"]["guest"] is False
    assert rnd["submission"]["author"] == f"user:{users.owner_user()['id']}"
    kept = rnd["guest_submissions"]
    assert len(kept) == 1
    saved = next(iter(kept.values()))
    assert saved["feedback"]["guidelines"] == GUEST_A
    assert saved["submission"]["guest"] is True
    assert_no_guest_text(env["receiver"].requests, GUEST_A)
    got = api.get_feedback(rid)
    assert got["feedback"]["guidelines"] == OWNER_TEXT
    assert GUEST_A not in json.dumps(got)

    before = dump(stored_submission(rid))
    assert_route_refuses(server, rid, guest, {"round": 1, "token": review["token"],
                                              "feedback": guest_feedback(GUEST_B), "amend": True})
    with pytest.raises(store.ReviewError, match="someone else"):
        guest_submit(rid, guest, review["token"], guest_feedback(GUEST_B), amend=True)
    assert dump(stored_submission(rid)) == before


# ---------------------------------------------------------------- the payload seam


def test_build_payload_refuses_a_guest_round(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    assert guest_submit(rid, guest, review["token"], guest_feedback(GUEST_A))["status"] == "stored"

    data = store.load(rid)
    with pytest.raises(store.ReviewError, match="never delivered to Hermes"):
        service.build_payload(data, data["rounds"][0])


def test_notify_round_refuses_a_guest_round(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    assert guest_submit(rid, guest, review["token"], guest_feedback(GUEST_A))["status"] == "stored"

    assert service.notify_round(rid, 1) == (False, service.GUEST_NEVER_DELIVERED)
    assert env["receiver"].requests == []


# ---------------------------------------------------------------- the re-ask guard

def _plan(tmp_path):
    """The shared plan example on disk, as a decisions review: add_round can re-send the exact file."""
    f = tmp_path / "plan" / "decisions.json"
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps(PLAN), encoding="utf-8")
    data = store.create_review("Plan", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(f)))
    return data, str(f)


def test_a_guests_marks_do_not_settle_the_round_for_the_agent(env, server, tmp_path):
    data, path = _plan(tmp_path)
    rid = data["id"]
    _made, guest = join_guest(rid)
    feedback = {"marks": _all_resolved(), "action": "send"}
    assert_route_refuses(server, rid, guest, {"round": 1, "token": data["token"], "feedback": feedback})
    assert guest_submit(rid, guest, data["token"], feedback)["status"] == "stored"

    first = store.load(rid)["rounds"][0]
    assert store.guest_submission(first) is True
    assert first["feedback"]["marks"]["d1"]["status"] == "approve"       # only a guest marked it
    # Nobody the agent reports to settled round 1, so the same ids may come back: no "already settled" refusal.
    assert api.reasked(first, store.parse_round_spec(decisions=path)) == []
    assert api.add_round(rid, decisions=path)["round"] == 2
    assert len(store.load(rid)["rounds"]) == 2
