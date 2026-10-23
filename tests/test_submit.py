import json

import pytest

from conftest import FEEDBACK, http
from mockreview import config, feedback, service, store
from mockreview.store import ReviewError


def submit_url(server, rid):
    return f"{server}/r/{rid}/api/submit"


def test_submit_notifies_and_is_idempotent(server, review, env):
    rid = review["id"]
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _, out = http("POST", submit_url(server, rid), body)
    res = json.loads(out)
    assert code == 200 and res["notified"] is True and res["status"] == "notified"
    rec = env["receiver"].requests
    # the wake first, then the deliver-only ack
    assert len(rec) == 2 and rec[1]["path"] == "/webhooks/mock-review-discord-notice"
    assert json.loads(rec[1]["body"])["message"].startswith("📨")
    payload = json.loads(rec[0]["body"])
    assert rec[0]["ok"] and rec[0]["path"] == "/webhooks/mock-review-discord"
    assert payload["event_type"] == "review.submitted" and payload["review_id"] == rid
    assert payload["round"] == 1 and payload["thread_id"] == "test-topic" and payload["title"] == "Test review"
    assert payload["feedback_path"] == str(config.data_root() / rid / "rounds" / "1" / "feedback.md")
    assert "Variant B" in payload["summary"]
    assert (config.data_root() / rid / "rounds/1/feedback.md").is_file()

    # second submit for the same round: rejected, Hermes not woken again
    code, _, out = http("POST", submit_url(server, rid), body)
    assert code == 409 and "already sent" in json.loads(out)["error"] and "Amend" in json.loads(out)["error"]
    assert len(rec) == 2

    # amend replaces it and re-notifies with new delivery ids (wake + ack)
    amended = json.loads(json.dumps(FEEDBACK))
    amended["guidelines"] = "Actually: drop the table."
    code, _, out = http("POST", submit_url(server, rid), {**body, "feedback": amended, "amend": True})
    assert code == 200 and json.loads(out)["amended"] is True
    assert len(rec) == 4 and json.loads(rec[2]["body"])["amended"] is True
    wake_ids = [{k.lower(): v for k, v in rec[i]["headers"].items()}["x-request-id"] for i in (0, 2)]
    assert wake_ids[0] != wake_ids[1]
    data = store.load(rid)
    assert data["rounds"][0]["feedback"]["guidelines"] == "Actually: drop the table."
    assert data["rounds"][0]["submission"]["seq"] == 2


def test_wrong_token_rejected(server, review):
    code, _, out = http("POST", submit_url(server, review["id"]), {"round": 1, "token": "nope", "feedback": FEEDBACK})
    assert code == 403
    assert store.load(review["id"])["rounds"][0]["feedback"] is None


def test_old_round_and_closed_review_rejected(review, variants):
    rid = review["id"]
    store.add_round(rid, [store.parse_variant_spec(variants[0])])
    with pytest.raises(ReviewError, match="read-only"):
        service.submit(rid, 1, FEEDBACK, review["token"], do_notify=False)
    with store.locked(rid):
        d = store.load(rid)
        d["status"] = "closed"
        store.save(rid, d)
    with pytest.raises(ReviewError, match="closed"):
        service.submit(rid, 2, FEEDBACK, review["token"], do_notify=False)


def test_empty_feedback_rejected(review):
    with pytest.raises(ReviewError, match="Nothing to send"):
        service.submit(review["id"], 1, {"variants": {}}, review["token"], do_notify=False)


def test_sanitize_drops_unknown_caps_and_cleans():
    raw = {
        "choice": "z", "evil": "x", "guidelines": "g" * 9000 + "\x00",
        "variants": {"a": {"note": "n\x07ote", "pins": [
            {"frame": "desktop", "x": 3, "y": -1, "text": "  a\nb  ", "extra": 1},
            {"frame": "tablet", "x": 0.5, "y": 0.5, "text": "bad frame"},
            {"frame": "phone", "x": "0.5", "y": 0.5, "text": "bad x"},
            {"frame": "phone", "x": 0.5, "y": 0.5, "text": ""},
            {"frame": "phone", "x": float("nan"), "y": 0.5, "text": "nan"},
        ]}, "zz": {"note": "ignored"}},
    }
    fb = feedback.sanitize(raw, ["a", "b"])
    assert fb["choice"] is None and "evil" not in fb and set(fb["variants"]) == {"a", "b"}
    assert len(fb["guidelines"]) == feedback.MAX_GUIDELINES and "\x00" not in fb["guidelines"]
    assert fb["variants"]["a"]["note"] == "note"
    assert fb["variants"]["a"]["pins"] == [{"frame": "desktop", "x": 1.0, "y": 0.0, "page_h": 800, "text": "a b"}]


def test_pin_count_capped():
    pins = [{"frame": "phone", "x": 0.1, "y": 0.1, "text": f"p{i}"} for i in range(200)]
    fb = feedback.sanitize({"variants": {"a": {"pins": pins}}}, ["a"])
    assert len(fb["variants"]["a"]["pins"]) == feedback.MAX_PINS


def test_pending_notify_when_secret_missing_then_retried(review, env, secret_values):
    secret_values.clear()
    res = service.submit(review["id"], 1, FEEDBACK, review["token"])
    assert res["notified"] is False and res["status"] == "pending_notify"
    assert "MOCKREVIEW_WEBHOOK_SECRET is not set" in res["detail"]
    sub = store.load(review["id"])["rounds"][0]["submission"]
    assert sub["status"] == "pending_notify" and "MOCKREVIEW_WEBHOOK_SECRET" in sub["last_error"]
    assert env["receiver"].requests == []
    # The scoped secret becomes available; the next retry pass delivers the wake, then the ack.
    from conftest import SECRET
    secret_values["MOCKREVIEW_WEBHOOK_SECRET"] = SECRET
    assert service.retry_pending() == 1
    rec = env["receiver"].requests
    assert len(rec) == 2 and rec[0]["ok"] and not rec[0]["path"].endswith("-notice") and rec[1]["path"].endswith("-notice")
    assert store.load(review["id"])["rounds"][0]["submission"]["status"] == "notified"
    assert store.load(review["id"])["rounds"][0]["submission"]["notice"]["status"] == "notified"
    assert service.retry_pending() == 0


def test_pending_when_hermes_returns_error(review, env):
    env["receiver"].status = 500
    res = service.submit(review["id"], 1, FEEDBACK, review["token"])
    assert res["status"] == "pending_notify" and "HTTP 500" in res["detail"]
    env["receiver"].status = 202
    assert service.retry_pending() == 1
