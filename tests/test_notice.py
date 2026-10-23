"""The instant ack notice and the 15-minute stalled ping (rel/notice)."""

import json
import os
import sys
import time as _time
from pathlib import Path

import pytest

from conftest import FEEDBACK, http
from mockreview import config, notify, service, store
from test_store import _decisions

ORIGIN = {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}
DEC_FB = {"marks": {"recap": {"status": "approve"}, "d1": {"status": "change", "note": "smaller"}},
          "pins": [], "guidelines": "Please simplify the layout."}


def _notice(env):
    return [r for r in env["receiver"].requests if r["path"].endswith("-notice")]


def _wake(env):
    return [r for r in env["receiver"].requests if not r["path"].endswith("-notice")]


def _rid(r):
    return {k.lower(): v for k, v in r["headers"].items()}["x-request-id"]


def _decisions_file(tmp_path):
    f = tmp_path / "dec" / "decisions.json"
    if not f.is_file():
        _decisions(tmp_path)
    return f


def _decisions_review(tmp_path):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    return store.create_review("Decide", "", ORIGIN, "tailscale", spec)


def _age_submission(rid, seconds):
    with store.locked(rid):
        d = store.load(rid)
        old = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - seconds))
        d["rounds"][0]["submission"]["submitted_at"] = old
        store.save(rid, d)


# ---------------------------------------------------------------- ack on Send

def test_submit_sends_wake_then_ack_with_own_delivery_ids(review, env):
    rid = review["id"]
    res = service.submit(rid, 1, FEEDBACK, review["token"])
    assert res["notified"] is True and res["status"] == "notified"
    reqs = env["receiver"].requests
    assert [r["path"].endswith("-notice") for r in reqs] == [False, True]   # wake first, then the ack
    wake, notice = reqs[0], reqs[1]
    assert notice["path"] == "/webhooks/mock-review-discord-notice" and notice["ok"]
    body = json.loads(notice["body"])
    assert body["message"] == ("📨 Got your feedback on Test review, round 1. "
                               "Hermes is working on round 2; the link lands here when it's ready.")
    assert res["notice"]["notified"] is True
    assert json.loads(wake["body"])["event_type"] == "review.submitted"
    assert _rid(wake) == f"mockreview-{rid}-r1-s1"
    assert _rid(notice) == f"mockreview-{rid}-r1-s1-notice"


def test_notice_payload_carries_the_origin_and_no_free_text(review, env):
    rid = review["id"]
    service.submit(rid, 1, FEEDBACK, review["token"])
    body = json.loads(_notice(env)[0]["body"])
    assert body == {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic",
                    "event_type": "review.notice", "review_id": rid, "round": 1, "message": body["message"]}
    blob = json.dumps(body)
    for leak in ("Keep the table.", "Clearest", "make this button smaller", "Nice type"):
        assert leak not in blob


def test_notice_payload_without_thread_has_no_thread_key(env, tmp_path):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    data = store.create_review("Decide", "", {"platform": "telegram", "chat_id": "c1"}, "tailscale", spec)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    body = json.loads(_notice(env)[0]["body"])
    assert "thread_id" not in body and body["chat_id"] == "c1" and body["platform"] == "telegram"
    assert _notice(env)[0]["path"] == "/webhooks/mock-review-telegram-notice"


def test_decisions_notice_has_counts_and_no_free_text(env, tmp_path):
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    body = json.loads(_notice(env)[0]["body"])["message"]
    assert body.startswith("📨 Got your feedback on Decide, round 1 (approved 1, change 1, skip 0). ")
    assert "smaller" not in body and "Please simplify" not in body          # free text stays out


def test_amend_gets_its_own_notice(review, env):
    service.submit(review["id"], 1, FEEDBACK, review["token"])
    amended = json.loads(json.dumps(FEEDBACK))
    amended["guidelines"] = "drop the table"
    service.submit(review["id"], 1, amended, review["token"], amend=True)
    assert [_rid(n) for n in _notice(env)] == [f"mockreview-{review['id']}-r1-s1-notice",
                                               f"mockreview-{review['id']}-r1-s2-notice"]


def test_ack_failure_does_not_fail_the_submit_and_stays_pending(review, env):
    env["receiver"].notice_status = 502
    res = service.submit(review["id"], 1, FEEDBACK, review["token"])
    assert res["notified"] is True and res["status"] == "notified"          # the wake still went
    assert res["notice"]["notified"] is False
    assert len(_wake(env)) == 1 and len(_notice(env)) == 1                  # exactly one ack POST in the submit call
    sub = store.load(review["id"])["rounds"][0]["submission"]
    assert sub["notice"]["status"] == "pending_notify" and "502" in sub["notice"]["last_error"]


def test_ack_after_a_gateway_failure_is_retried_with_a_fresh_delivery_id(review, env):
    # Hermes records a delivery id before it delivers, so the id of a 502 is burned: a retry with the same id is
    # answered "200 duplicate" and nothing reaches the chat. After a gateway answer the retry needs a new id.
    env["receiver"].notice_status = 502
    service.submit(review["id"], 1, FEEDBACK, review["token"])
    env["receiver"].notice_status = 200
    assert service.retry_pending() == 0                                     # wake already notified; the ack retried
    sub = store.load(review["id"])["rounds"][0]["submission"]
    assert sub["notice"]["status"] == "notified" and sub["notice"]["notified_at"]
    assert sub["notice"]["gateway_response"] == "HTTP 200 delivered"
    ids = [_rid(r) for r in _notice(env)]
    base = f"mockreview-{review['id']}-r1-s1-notice"
    assert ids == [base, base + "-a2"]
    assert len(env["receiver"].delivered_notices) == 1                       # really delivered, not a duplicate
    assert service.retry_pending() == 0 and len(_notice(env)) == 2         # notified: never again


def test_ack_that_keeps_failing_stays_pending_and_never_reads_a_duplicate_as_sent(review, env):
    env["receiver"].notice_status = 502
    service.submit(review["id"], 1, FEEDBACK, review["token"])
    for _ in range(3):
        service.retry_pending()
    sub = store.load(review["id"])["rounds"][0]["submission"]
    assert sub["notice"]["status"] == "pending_notify" and "502" in sub["notice"]["last_error"]
    assert sub["notice"]["attempts"] == 4
    ids = [_rid(r) for r in _notice(env)]
    assert len(ids) == 4 and len(set(ids)) == 4                              # every attempt a fresh id
    assert env["receiver"].delivered_notices == []


def test_ack_after_no_answer_keeps_the_same_delivery_id(review, env, monkeypatch):
    # No HTTP answer (unreachable, timeout): the first POST may still have been delivered, so the retry keeps the id
    # and a "200 duplicate" then really means it got through. That avoids a second ack in the chat.
    real = notify.post_notice
    calls = []

    def flaky(url, payload, secret, delivery_id, timeout=10.0):
        calls.append(delivery_id)
        if len(calls) == 1:
            return False, f"cannot reach {url}: timed out"
        return real(url, payload, secret, delivery_id, timeout)
    monkeypatch.setattr(notify, "post_notice", flaky)
    service.submit(review["id"], 1, FEEDBACK, review["token"])
    assert store.load(review["id"])["rounds"][0]["submission"]["notice"]["status"] == "pending_notify"
    service.retry_pending()
    assert calls == [f"mockreview-{review['id']}-r1-s1-notice"] * 2
    assert store.load(review["id"])["rounds"][0]["submission"]["notice"]["status"] == "notified"


def test_no_ack_while_the_wake_fails_then_it_follows_the_wake(review, env):
    env["receiver"].status = 500
    res = service.submit(review["id"], 1, FEEDBACK, review["token"])
    assert res["notified"] is False and res["status"] == "pending_notify"
    assert _notice(env) == []                                                # never an ack for an un-woken submit
    assert service.retry_pending() == 0 and _notice(env) == []
    env["receiver"].status = 202
    assert service.retry_pending() == 1                                      # the wake lands...
    assert [r["path"].endswith("-notice") for r in env["receiver"].requests][-2:] == [False, True]  # ...ack right after
    sub = store.load(review["id"])["rounds"][0]["submission"]
    assert sub["status"] == "notified" and sub["notice"]["status"] == "notified"


def test_404_is_terminal_no_route_with_one_warning(review, env, caplog):
    env["receiver"].notice_status = 404
    with caplog.at_level("WARNING", logger="mockreview.service"):
        res = service.submit(review["id"], 1, FEEDBACK, review["token"])
    assert res["notified"] is True and res["notice"]["notified"] is False
    sub = store.load(review["id"])["rounds"][0]["submission"]
    assert sub["notice"]["status"] == "no_route"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "hermes mockreview setup" in warnings[0] and "mock-review-discord-notice" in warnings[0]
    for _ in range(3):
        service.retry_pending()
    assert len(_notice(env)) == 1                                            # never retried
    assert len(_wake(env)) == 1                                              # the wake is unaffected


def test_pending_ack_is_dropped_after_a_newer_round(env, tmp_path):
    env["receiver"].notice_status = 502
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    assert store.load(data["id"])["rounds"][0]["submission"]["notice"]["status"] == "pending_notify"
    store.add_round(data["id"], store.parse_round_spec(decisions=str(_decisions_file(tmp_path))))
    env["receiver"].notice_status = 200
    n_before = len(_notice(env))
    service.retry_pending()
    assert len(_notice(env)) == n_before                                     # no POST
    assert store.load(data["id"])["rounds"][0]["submission"]["notice"]["status"] == "dropped"


def test_pending_ack_is_dropped_when_the_review_closes(review, env):
    env["receiver"].notice_status = 502
    service.submit(review["id"], 1, FEEDBACK, review["token"])
    with store.locked(review["id"]):
        d = store.load(review["id"])
        d["status"] = "closed"
        store.save(review["id"], d)
    env["receiver"].notice_status = 200
    service.retry_pending()
    assert len(_notice(env)) == 1
    assert store.load(review["id"])["rounds"][0]["submission"]["notice"]["status"] == "dropped"


def test_bound_call_never_sends_a_notice(review, env, monkeypatch):
    from mockreview import owner
    monkeypatch.setattr(owner, "is_bound", lambda: True)
    assert service.notify_notice(review["id"], 1)[0] is False
    assert env["receiver"].requests == []


def test_notice_classifier_and_wake_classifier():
    assert notify.classify_delivered(200, '{"status":"delivered"}')[0] is True
    assert notify.classify_delivered(200, '{"status":"duplicate"}')[0] is True
    assert notify.classify_delivered(502, '{"status":"error","error":"Delivery failed"}')[0] is False
    assert notify.classify_delivered(202, '{"status":"accepted"}')[0] is False   # deliver_only never answers 202
    assert notify.classify(202, '{"status":"accepted"}')[0] is True             # the wake still treats 202 as success
    assert notify.is_no_route(notify.classify_delivered(404, '{"error":"Unknown route"}')[1])
    assert not notify.is_no_route(notify.classify_delivered(502, "x")[1])


def test_notice_webhook_url_builds_the_notice_route(env):
    assert config.notice_webhook_url("telegram").endswith("/webhooks/mock-review-telegram-notice")
    with pytest.raises(ValueError):
        config.notice_webhook_url("")                                        # no silent fallback to the wake URL


# ---------------------------------------------------------------- Hermes-faithful render

def test_hermes_renders_the_notice_route_against_the_real_payload(review, env):
    """Render the route exactly as `hermes webhook subscribe` stores it, with Hermes's own adapter code."""
    src = os.environ.get("HERMES_AGENT_SRC")
    if src:
        sys.path.insert(0, str(Path(src).expanduser()))
    try:
        from gateway.platforms.webhook import WebhookAdapter
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Hermes webhook adapter unavailable ({type(e).__name__}); set HERMES_AGENT_SRC")
    service.submit(review["id"], 1, FEEDBACK, review["token"])
    payload = json.loads(_notice(env)[0]["body"])
    adapter = WebhookAdapter.__new__(WebhookAdapter)       # the render helpers only read their arguments
    assert adapter._render_delivery_extra({"chat_id": "{chat_id}"}, payload) == {"chat_id": "test-chat"}
    rendered = adapter._render_prompt("{message}", payload, "review.notice", "mock-review-discord-notice")
    assert rendered == payload["message"] and rendered.startswith("📨 Got your feedback")
    # control: a payload without the origin leaves the placeholder literal, which is what broke delivery
    assert adapter._render_delivery_extra({"chat_id": "{chat_id}"}, {"message": "x"}) == {"chat_id": "{chat_id}"}


# ---------------------------------------------------------------- watchdog

def _stalled(env):
    return [r for r in _notice(env) if json.loads(r["body"])["event_type"] == "review.stalled"]


def _submitted_ts(rid, n=0):
    return service._parse_iso(store.load(rid)["rounds"][n]["submission"]["submitted_at"])


def test_watchdog_threshold_boundary_and_exact_text(env, tmp_path):
    data = _decisions_review(tmp_path)
    rid = data["id"]
    service.submit(rid, 1, DEC_FB, data["token"])
    ts = _submitted_ts(rid)
    assert service.check_stalled(now=ts + 15 * 60 - 5) == 0                  # a few seconds early: nothing
    assert _stalled(env) == []
    assert service.check_stalled(now=ts + 15 * 60) == 1                      # at the threshold: one ping
    assert service.check_stalled(now=ts + 16 * 60) == 0                      # never twice
    ping = _stalled(env)
    assert len(ping) == 1
    body = json.loads(ping[0]["body"])
    assert body == {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic",
                    "event_type": "review.stalled", "review_id": rid, "round": 1, "message": body["message"]}
    assert body["message"] == ("⏳ No round 2 yet for Decide, 15 min after you sent it. "
                               "The agent may have stalled; ask in this chat or resend from the board.")
    assert _rid(ping[0]) == f"mockreview-{rid}-r1-s1-stalled"
    assert "smaller" not in body["message"]


def test_watchdog_uses_configured_minutes_in_text(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_MINUTES", "12")
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    assert service.check_stalled(now=_submitted_ts(data["id"]) + 12 * 60 - 5) == 0
    assert service.check_stalled(now=_submitted_ts(data["id"]) + 12 * 60) == 1
    assert "12 min after you sent it" in json.loads(_stalled(env)[0]["body"])["message"]


def test_watchdog_survives_a_restart(env, tmp_path):
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    _age_submission(data["id"], 20 * 60)
    assert service.check_stalled() == 1
    reloaded = store.load(data["id"])                                        # a fresh process reads the same file
    assert reloaded["rounds"][0]["submission"]["stalled_notice"]["status"] == "notified"
    assert service.check_stalled() == 0 and len(_stalled(env)) == 1


def test_amend_gets_its_own_ping(env, tmp_path):
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    _age_submission(data["id"], 20 * 60)
    assert service.check_stalled() == 1
    service.submit(data["id"], 1, DEC_FB, data["token"], amend=True)
    assert "stalled_notice" not in store.load(data["id"])["rounds"][0]["submission"]
    assert service.check_stalled() == 0                                      # the amend is fresh
    _age_submission(data["id"], 20 * 60)
    assert service.check_stalled() == 1
    assert [_rid(r).split("-r1-")[1] for r in _stalled(env)] == ["s1-stalled", "s2-stalled"]


def test_watchdog_skips_a_newer_round(env, tmp_path):
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    _age_submission(data["id"], 20 * 60)
    store.add_round(data["id"], store.parse_round_spec(decisions=str(_decisions_file(tmp_path))))
    assert service.check_stalled() == 0 and _stalled(env) == []


def test_watchdog_skips_closed_and_wake_off_reviews(env, tmp_path):
    closed = _decisions_review(tmp_path)
    service.submit(closed["id"], 1, DEC_FB, closed["token"])
    _age_submission(closed["id"], 20 * 60)
    with store.locked(closed["id"]):
        d = store.load(closed["id"])
        d["status"] = "closed"
        store.save(closed["id"], d)
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    quiet = store.create_review("Quiet", "", ORIGIN, "tailscale", spec, wake=False)
    service.submit(quiet["id"], 1, DEC_FB, quiet["token"])
    _age_submission(quiet["id"], 20 * 60)
    before = len(env["receiver"].requests)
    assert service.check_stalled() == 0
    assert len(env["receiver"].requests) == before                           # nothing was sent for either


def test_watchdog_off_when_setting_zero(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_MINUTES", "0")
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    _age_submission(data["id"], 24 * 3600)
    assert service.check_stalled() == 0 and _stalled(env) == []


def test_watchdog_never_runs_from_a_bound_call(env, tmp_path, monkeypatch):
    from mockreview import owner
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    _age_submission(data["id"], 20 * 60)
    monkeypatch.setattr(owner, "is_bound", lambda: True)
    assert service.check_stalled() == 0 and _stalled(env) == []


def test_stalled_ping_failure_retries_only_while_still_stale(env, tmp_path):
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    _age_submission(data["id"], 20 * 60)
    env["receiver"].notice_status = 502
    assert service.check_stalled() == 0
    st = store.load(data["id"])["rounds"][0]["submission"]["stalled_notice"]
    assert st["status"] == "pending_notify"
    env["receiver"].notice_status = 200
    assert service.check_stalled() == 1                                      # retried (backoff is 0 in tests)
    ids = [_rid(r) for r in _stalled(env)]
    assert len(ids) == 2 and ids[1] == ids[0] + "-a2"                       # the 502 burned the first id
    assert len(env["receiver"].delivered_notices) == 2                       # the ack, then the ping, both really sent


def test_stalled_ping_404_is_terminal(env, tmp_path):
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    _age_submission(data["id"], 20 * 60)
    env["receiver"].notice_status = 404
    assert service.check_stalled() == 0
    assert store.load(data["id"])["rounds"][0]["submission"]["stalled_notice"]["status"] == "no_route"
    n = len(env["receiver"].requests)
    assert service.check_stalled() == 0 and len(env["receiver"].requests) == n


def test_stalled_ping_republishes_a_herenow_board(env, tmp_path, monkeypatch):
    from mockreview import herenow
    data = _decisions_review(tmp_path)
    with store.locked(data["id"]):
        d = store.load(data["id"])
        d["mode"] = "herenow"
        store.save(data["id"], d)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    _age_submission(data["id"], 20 * 60)
    seen = []
    monkeypatch.setattr(herenow, "republish_quietly", lambda rid: seen.append(rid))
    assert service.check_stalled() == 1
    assert seen == [data["id"]]


def test_worker_tick_runs_the_watchdog(env, monkeypatch):
    from mockreview import server
    calls = []
    monkeypatch.setattr(service, "check_stalled", lambda *a, **k: calls.append(1) or 0)
    w = server.Worker(tick=0.02)
    w.start()
    try:
        end = _time.time() + 5
        while not calls and _time.time() < end:
            _time.sleep(0.02)
    finally:
        w.stop_event.set()
        w.join(5)
    assert calls
    assert not w.is_alive()


def test_worker_tick_skips_the_watchdog_when_off(env, monkeypatch):
    from mockreview import server
    monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_MINUTES", "0")
    calls = []
    monkeypatch.setattr(service, "check_stalled", lambda *a, **k: calls.append(1) or 0)
    w = server.Worker(tick=0.02)
    w.start()
    try:
        _time.sleep(0.3)
    finally:
        w.stop_event.set()
        w.join(5)
    assert not calls
