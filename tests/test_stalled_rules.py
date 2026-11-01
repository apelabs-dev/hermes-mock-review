"""When the "No round N yet" stalled ping must NOT fire: nothing left to build, the session acknowledged or read the
submission, the review closed, or the notice can never be delivered."""

import json
import os

import pytest

from conftest import load_plugin
from mockreview import api, cli, service, store
from mockreview.store import ReviewError
from test_notice import DEC_FB, _age_submission, _decisions_review, _rid, _stalled, _submitted_ts
from test_plugin import FakeCtx

APPROVED = {"marks": {"recap": {"status": "approve"}, "d1": {"status": "approve", "option": "b", "note": "love it"}},
            "pins": [], "guidelines": ""}
APPROVED_AND_SKIPPED = {"marks": {"recap": {"status": "skip"}, "d1": {"status": "approve", "option": "a"}},
                        "pins": [], "guidelines": ""}
PIN = {"block": "d1", "option": "b", "x": 0.5, "y": 0.5, "text": "tighten this"}
WORK = {
    "change": DEC_FB,
    "pin": {**APPROVED, "pins": [PIN]},
    "guidelines": {**APPROVED, "guidelines": "Use the brand blue everywhere."},
    "open decision": {"marks": {"d1": {"status": "approve", "option": "b"}}, "pins": [], "guidelines": ""},
}
DAY = 24 * 3600


def _sub(rid, n=0):
    return store.load(rid)["rounds"][n]["submission"]


def _submit(tmp_path, fb):
    data = _decisions_review(tmp_path)
    service.submit(data["id"], 1, fb, data["token"])
    return data["id"], data["token"]


# ---------------------------------------------------------------- a. nothing left to build

@pytest.mark.parametrize("fb", [APPROVED, APPROVED_AND_SKIPPED], ids=["all approved", "approved and skipped"])
def test_round_with_nothing_to_build_gets_no_stalled_ping(env, tmp_path, fb):
    rid, _ = _submit(tmp_path, fb)
    assert service.check_stalled(now=_submitted_ts(rid) + DAY) == 0
    assert _stalled(env) == []
    st = _sub(rid)["stalled_notice"]
    assert st["status"] == "skipped" and "nothing to build" in st["reason"]
    assert service.check_stalled(now=_submitted_ts(rid) + 2 * DAY) == 0 and _stalled(env) == []


@pytest.mark.parametrize("kind", sorted(WORK))
def test_round_with_work_left_still_pings_once(env, tmp_path, kind):
    rid, _ = _submit(tmp_path, WORK[kind])
    assert service.check_stalled(now=_submitted_ts(rid) + 15 * 60) == 1
    assert service.check_stalled(now=_submitted_ts(rid) + DAY) == 0
    assert len(_stalled(env)) == 1


# ---------------------------------------------------------------- b. the session acknowledged it

def test_explicit_ack_cancels_the_stalled_ping(env, tmp_path):
    rid, _ = _submit(tmp_path, DEC_FB)
    res = api.ack(rid)
    assert res["review_id"] == rid and res["round"] == 1 and res["acked_at"]
    assert res["stalled_notice"] == "skipped"
    assert service.check_stalled(now=_submitted_ts(rid) + DAY) == 0 and _stalled(env) == []
    st = _sub(rid)["stalled_notice"]
    assert st["status"] == "skipped" and st["reason"] == "acknowledged"


def test_ack_from_the_cli_and_the_tool(env, tmp_path, capsys):
    rid, _ = _submit(tmp_path, DEC_FB)
    assert cli.main(["ack", rid, "--round", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["acked_at"]
    rid2, _ = _submit(tmp_path, DEC_FB)
    ctx = FakeCtx()
    load_plugin().register(ctx)
    res = json.loads(ctx.tools["mock_review_ack"]["handler"]({"review_id": rid2}))
    assert "error" not in res and res["stalled_notice"] == "skipped"
    assert service.check_stalled(now=_submitted_ts(rid2) + DAY) == 0 and _stalled(env) == []


def test_ack_needs_a_submission(env, tmp_path):
    data = _decisions_review(tmp_path)
    with pytest.raises(ReviewError) as e:
        api.ack(data["id"])
    assert e.value.status == 409


def test_ack_after_the_ping_keeps_it_notified(env, tmp_path):
    rid, _ = _submit(tmp_path, DEC_FB)
    assert service.check_stalled(now=_submitted_ts(rid) + 15 * 60) == 1
    assert api.ack(rid)["stalled_notice"] == "notified"
    assert len(_stalled(env)) == 1


def test_amend_after_an_ack_is_a_new_submission_with_its_own_clock(env, tmp_path):
    rid, token = _submit(tmp_path, DEC_FB)
    api.ack(rid)
    service.submit(rid, 1, DEC_FB, token, amend=True)
    assert "acked_at" not in _sub(rid)
    _age_submission(rid, 20 * 60)
    assert service.check_stalled() == 1
    assert [_rid(r).split("-r1-")[1] for r in _stalled(env)] == ["s2-stalled"]


# ---------------------------------------------------------------- b. the session read it

def test_reading_feedback_moves_the_ping_to_the_read_window(env, tmp_path):
    rid, _ = _submit(tmp_path, DEC_FB)
    ts = _submitted_ts(rid)
    api.get_feedback(rid)                                        # the session woke and read it
    read = service._parse_iso(_sub(rid)["read_at"])
    assert read is not None and read >= ts
    assert service.check_stalled(now=ts + 15 * 60) == 0          # the unread ping does not fire
    assert service.check_stalled(now=read + 60 * 60 - 5) == 0
    assert service.check_stalled(now=read + 60 * 60) == 1        # read, and still no round after 60 min
    assert service.check_stalled(now=read + 3 * 3600) == 0       # once per submission
    ping = _stalled(env)
    assert len(ping) == 1
    assert json.loads(ping[0]["body"])["message"] == (
        "⏳ No round 2 yet for Decide: Hermes read your feedback 60 min ago and has not posted the next round. "
        "Ask in this chat or resend from the board.")
    assert _rid(ping[0]) == f"mockreview-{rid}-r1-s1-stalled"


def test_the_feedback_tool_records_the_read(env, tmp_path):
    rid, _ = _submit(tmp_path, DEC_FB)
    ctx = FakeCtx()
    load_plugin().register(ctx)
    json.loads(ctx.tools["mock_review_feedback"]["handler"]({"review_id": rid}))
    assert _sub(rid)["read_at"]
    assert service.check_stalled(now=_submitted_ts(rid) + 15 * 60) == 0 and _stalled(env) == []


def test_read_window_zero_means_no_ping_after_a_read(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_READ_MINUTES", "0")
    rid, _ = _submit(tmp_path, DEC_FB)
    api.get_feedback(rid)
    assert service.check_stalled(now=_submitted_ts(rid) + 30 * DAY) == 0 and _stalled(env) == []


def test_the_first_read_counts_and_a_read_before_send_records_nothing(env, tmp_path):
    data = _decisions_review(tmp_path)
    api.get_feedback(data["id"])
    assert store.load(data["id"])["rounds"][0].get("submission") is None
    service.submit(data["id"], 1, DEC_FB, data["token"])
    api.get_feedback(data["id"])
    first = _sub(data["id"])["read_at"]
    api.get_feedback(data["id"])
    assert _sub(data["id"])["read_at"] == first


# ---------------------------------------------------------------- c. closing cancels

def test_close_cancels_a_pending_stalled_ping(env, tmp_path):
    rid, _ = _submit(tmp_path, DEC_FB)
    _age_submission(rid, 20 * 60)
    env["receiver"].notice_status = 502
    assert service.check_stalled() == 0
    assert _sub(rid)["stalled_notice"]["status"] == "pending_notify"
    api.close(rid)
    st = _sub(rid)["stalled_notice"]
    assert st["status"] == "dropped" and st["reason"] == "review closed"
    env["receiver"].notice_status = 200
    n = len(env["receiver"].requests)
    assert service.check_stalled(now=_submitted_ts(rid) + DAY) == 0
    assert len(env["receiver"].requests) == n                    # a closed review never pings


def test_close_drops_a_stalled_ping_that_was_never_attempted(env, tmp_path):
    rid, _ = _submit(tmp_path, DEC_FB)
    api.close(rid)
    assert _sub(rid)["stalled_notice"] == {"status": "dropped", "reason": "review closed", "last_error": None}


def test_auto_close_cancels_the_stalled_ping(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_AUTO_CLOSE_IDLE_DAYS", "1")
    rid, _ = _submit(tmp_path, DEC_FB)
    assert api.housekeeping(now=_submitted_ts(rid) + 2 * DAY)["closed"] == [rid]
    assert _sub(rid)["stalled_notice"]["status"] == "dropped"


# ---------------------------------------------------------------- d. permanent failures go terminal

def _make_legacy(rid):
    with store.locked(rid):
        d = store.load(rid)
        d["origin"] = {"platform": "discord", "thread_id": "test-topic"}   # a pre-chat_id origin
        store.save(rid, d)


def _file_id(rid):
    st = os.stat(store.review_dir(rid) / "review.json")
    return st.st_ino, st.st_mtime_ns


def test_legacy_origin_stalled_notice_is_dropped_once_not_retried_forever(env, tmp_path, caplog):
    rid, _ = _submit(tmp_path, DEC_FB)
    _make_legacy(rid)
    _age_submission(rid, 20 * 60)
    n = len(env["receiver"].requests)
    with caplog.at_level("WARNING", logger="mockreview.service"):
        assert service.check_stalled() == 0
        st = _sub(rid)["stalled_notice"]
        assert st["status"] == "dropped" and "chat_id is required" in st["last_error"]
        before = _file_id(rid)
        for _ in range(3):
            assert service.check_stalled() == 0
    assert _file_id(rid) == before                               # not rewritten on every scan
    assert len(env["receiver"].requests) == n                    # nothing was ever POSTed
    lines = [r.getMessage() for r in caplog.records if rid in r.getMessage()]
    assert len(lines) == 1 and "not retried" in lines[0]


def test_a_rejected_stalled_notice_400_is_terminal_with_one_line(env, tmp_path, caplog):
    rid, _ = _submit(tmp_path, DEC_FB)
    _age_submission(rid, 20 * 60)
    env["receiver"].notice_status = 400
    with caplog.at_level("WARNING", logger="mockreview.service"):
        assert service.check_stalled() == 0
        n = len(env["receiver"].requests)
        for _ in range(3):
            service.check_stalled()
    assert _sub(rid)["stalled_notice"]["status"] == "dropped"
    assert len(env["receiver"].requests) == n
    assert len([r for r in caplog.records if rid in r.getMessage()]) == 1


def test_a_rejected_ack_notice_400_is_terminal(env, tmp_path):
    env["receiver"].notice_status = 400
    rid, _ = _submit(tmp_path, DEC_FB)
    assert _sub(rid)["notice"]["status"] == "dropped"
    n = len(env["receiver"].requests)
    for _ in range(3):
        service.retry_pending()
    assert len(env["receiver"].requests) == n


def test_a_gateway_error_502_stays_transient(env, tmp_path):
    rid, _ = _submit(tmp_path, DEC_FB)
    _age_submission(rid, 20 * 60)
    env["receiver"].notice_status = 502
    assert service.check_stalled() == 0
    assert _sub(rid)["stalled_notice"]["status"] == "pending_notify"
