"""Build end to end: the board's build submit wakes the origin session with a build intent, the ack says the
build started, mock_review_feedback carries action + next_step, the stalled ping waits build_stale_factor times longer,
and the next round (the result) clears it. A plain Send on the same plan board keeps today's behaviour."""

import copy
import json

import pytest

from conftest import SECRET, http, verify_v2_copy
from mockreview import api, cli, mcp, service, settings, setup, store
from test_notice import _rid, _stalled, _submitted_ts
from test_plan_format import PLAN, _all_resolved

ORIGIN = {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}
DAY = 24 * 3600
BUILD_ACK = "📨 Plan: Got it — building round 1's plan now. The result lands as round 2 on the same link."
NEXT_STEP = ("The reviewer approved the whole round and pressed {label}. Build it now in this session. When done, call "
             "mock_review_add_round with the result (what you built, with notes_answered for every note) so the "
             "reviewer sees it on the same link. Do not ask for confirmation again.")
SEND_ACK = ("📨 Got your feedback on Plan, round 1 (approved 4, change 0, skip 1). "
            "Hermes is working on round 2; the link lands here when it's ready.")


def _plan_file(tmp_path, dec=None, name="plan"):
    f = tmp_path / name / "decisions.json"
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps(dec or PLAN), encoding="utf-8")
    return f


def _review(tmp_path, dec=None, name="plan"):
    spec = store.parse_round_spec(decisions=str(_plan_file(tmp_path, dec, name)))
    return store.create_review("Plan", "", ORIGIN, "tailscale", spec)


def _build(data, **over):
    return service.submit(data["id"], 1, {"marks": _all_resolved(**over), "action": "build"}, data["token"])


def _round(rid, n=0):
    return store.load(rid)["rounds"][n]


def _tool_feedback(rid):
    res = mcp.call_tool("mock_review_feedback", {"review_id": rid}, mcp.Options())
    assert res["isError"] is False
    return json.loads(res["content"][0]["text"])


# ---------------------------------------------------------------- the build submit, the wake and the ack

def test_build_submit_from_the_board_wakes_with_the_build_intent_and_acks_the_build(server, env, tmp_path, capsys):
    data = _review(tmp_path)
    rid = data["id"]
    body = {"round": 1, "token": data["token"], "feedback": {"marks": _all_resolved(), "action": "build"}}
    code, _, out = http("POST", f"{server}/r/{rid}/api/submit", body)
    res = json.loads(out)
    assert code == 200 and res["notified"] is True and res["notice"]["notified"] is True

    rec = env["receiver"]
    assert [r["path"] for r in rec.requests] == ["/webhooks/mock-review-discord", "/webhooks/mock-review-discord-notice"]
    wake = rec.wakes[0]
    assert wake["ok"] and verify_v2_copy(wake["headers"], wake["body"], SECRET)       # signed like every wake
    assert not verify_v2_copy(wake["headers"], wake["body"].replace(b'"build"', b'"send"'), SECRET)
    payload = json.loads(wake["body"])
    assert payload["action"] == "build" and payload["event_type"] == "review.submitted"
    assert payload["review_id"] == rid and payload["round"] == 1 and payload["title"] == "Plan"
    assert (payload["platform"], payload["chat_id"], payload["thread_id"]) == ("discord", "test-chat", "test-topic")
    assert payload["summary"].splitlines()[0] == "- **Build pressed**"
    assert payload["amended"] is False and "recovered" not in payload
    assert _rid(wake) == f"mockreview-{rid}-r1-s1"

    notice = rec.notices[0]
    assert notice["ok"] and _rid(notice) == f"mockreview-{rid}-r1-s1-notice"
    ack = json.loads(notice["body"])
    assert ack == {**ORIGIN, "event_type": "review.notice", "review_id": rid, "round": 1, "message": BUILD_ACK}

    # mock_review_feedback, through the api, the MCP tool and the CLI: the same action and next_step
    fb = api.get_feedback(rid)
    assert fb["action"] == "build" and fb["next_step"] == NEXT_STEP.format(label="Build")
    tool = _tool_feedback(rid)
    assert (tool["action"], tool["next_step"]) == (fb["action"], fb["next_step"])
    assert cli.main(["feedback", rid, "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["action"], printed["next_step"]) == (fb["action"], fb["next_step"])


def test_next_step_names_the_rounds_own_gate_label(env, tmp_path):
    dec = copy.deepcopy(PLAN)
    dec["gate"] = {**dec["gate"], "label": "Ship it"}
    data = _review(tmp_path, dec)
    _build(data)
    assert api.get_feedback(data["id"])["next_step"] == NEXT_STEP.format(label="Ship it")
    assert json.loads(env["receiver"].notices[0]["body"])["message"] == BUILD_ACK


def test_the_build_ack_follows_the_wake_and_never_precedes_it(env, tmp_path):
    env["receiver"].status = 500
    data = _review(tmp_path)
    res = _build(data)
    assert res["notified"] is False and env["receiver"].notices == []            # no ack for an un-woken build
    env["receiver"].status = 202
    assert service.retry_pending() == 1
    assert [r["path"].endswith("-notice") for r in env["receiver"].requests][-2:] == [False, True]
    assert json.loads(env["receiver"].notices[0]["body"])["message"] == BUILD_ACK
    assert json.loads(env["receiver"].wakes[-1]["body"])["action"] == "build"


def test_the_build_ack_keeps_no_route_terminal_and_wake_off_silent(env, tmp_path):
    env["receiver"].notice_status = 404
    data = _review(tmp_path)
    assert _build(data)["notified"] is True
    assert _round(data["id"])["submission"]["notice"]["status"] == "no_route"
    service.retry_pending()
    assert len(env["receiver"].notices) == 1
    quiet = store.create_review("Quiet", "", ORIGIN, "tailscale",
                                store.parse_round_spec(decisions=str(_plan_file(tmp_path, name="quiet"))), wake=False)
    n = len(env["receiver"].requests)
    assert _build(quiet)["status"] == "stored"
    assert len(env["receiver"].requests) == n                                     # wake off: no wake, no ack
    assert service.notify_notice(quiet["id"], 1) == (False, "this board has wake off")


# ---------------------------------------------------------------- nothing_to_build

def test_a_build_is_never_nothing_to_build_but_the_same_marks_sent_are(env, tmp_path):
    built = _review(tmp_path)
    _build(built)
    assert service.nothing_to_build(_round(built["id"])) is False
    sent = _review(tmp_path, name="sent")
    service.submit(sent["id"], 1, {"marks": _all_resolved(q2={"choice": "recommended"})}, sent["token"])
    assert service.nothing_to_build(_round(sent["id"])) is True


def test_a_custom_answer_sent_without_build_still_expects_a_revised_round(env, tmp_path):
    # the reviewer overrode a recommendation: the plan must be revised, so the stalled ping stays on
    sent = _review(tmp_path, name="custom")
    service.submit(sent["id"], 1, {"marks": _all_resolved()}, sent["token"])   # q2 is a custom answer
    assert service.nothing_to_build(_round(sent["id"])) is False


# ---------------------------------------------------------------- the stalled ping waits longer for a build

def test_unread_build_pings_after_stale_after_minutes_times_the_factor(env, tmp_path):
    data = _review(tmp_path)
    _build(data)
    rid, ts = data["id"], _submitted_ts(data["id"])
    assert service.check_stalled(now=ts + 15 * 60) == 0                          # a Send would ping here
    assert service.check_stalled(now=ts + 3.9 * 15 * 60) == 0
    assert _stalled(env) == []
    assert service.check_stalled(now=ts + 4 * 15 * 60) == 1
    assert service.check_stalled(now=ts + DAY) == 0                               # once per submission
    ping = _stalled(env)
    assert len(ping) == 1 and _rid(ping[0]) == f"mockreview-{rid}-r1-s1-stalled"
    assert json.loads(ping[0]["body"])["message"] == (
        "⏳ No round 2 yet for Plan, 60 min after you sent it. "
        "The agent may have stalled; ask in this chat or resend from the board.")


def test_read_build_pings_after_stale_after_read_minutes_times_the_factor(env, tmp_path):
    data = _review(tmp_path)
    _build(data)
    api.get_feedback(data["id"])
    read = service._parse_iso(_round(data["id"])["submission"]["read_at"])
    assert service.check_stalled(now=read + 60 * 60) == 0
    assert service.check_stalled(now=read + 3.9 * 60 * 60) == 0
    assert service.check_stalled(now=read + 4 * 60 * 60) == 1
    assert json.loads(_stalled(env)[0]["body"])["message"] == (
        "⏳ No round 2 yet for Plan: Hermes read your feedback 240 min ago and has not posted the next round. "
        "Ask in this chat or resend from the board.")


def test_a_custom_build_stale_factor_is_honoured(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_BUILD_STALE_FACTOR", "2.5")
    monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_MINUTES", "10")
    data = _review(tmp_path)
    _build(data)
    ts = _submitted_ts(data["id"])
    assert service.check_stalled(now=ts + 25 * 60 - 5) == 0
    assert service.check_stalled(now=ts + 25 * 60) == 1
    assert "25 min after you sent it" in json.loads(_stalled(env)[0]["body"])["message"]


def test_factor_one_is_the_send_wait(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_BUILD_STALE_FACTOR", "1")
    data = _review(tmp_path)
    _build(data)
    assert service.check_stalled(now=_submitted_ts(data["id"]) + 15 * 60) == 1


@pytest.mark.parametrize("value", ["0", "0.5", "24.5", "-4"])
def test_build_stale_factor_is_at_least_one_and_at_most_24(monkeypatch, caplog, value):
    monkeypatch.setenv("MOCKREVIEW_BUILD_STALE_FACTOR", value)
    with caplog.at_level("WARNING", logger="mockreview.settings"):
        assert settings.get("build_stale_factor") == 4.0                         # ignored: the default applies
    assert "must be between 1 and 24" in caplog.text
    with pytest.raises(settings.SettingsError, match="between 1 and 24"):
        settings.get("build_stale_factor", float(value))                          # an explicit value is refused


def test_off_waits_stay_off_for_a_build(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_MINUTES", "0")
    data = _review(tmp_path)
    _build(data)
    assert service.check_stalled(now=_submitted_ts(data["id"]) + 30 * DAY) == 0
    assert service.notify_stalled(data["id"], 1, now=_submitted_ts(data["id"]) + 30 * DAY) == (
        False, "stale-after pings are off")
    monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_MINUTES", "15")
    monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_READ_MINUTES", "0")
    api.get_feedback(data["id"])
    assert service.check_stalled(now=_submitted_ts(data["id"]) + 30 * DAY) == 0
    assert _stalled(env) == []


def test_the_next_round_clears_the_build_stall_and_the_instruction(env, tmp_path):
    data = _review(tmp_path)
    _build(data)
    rid = data["id"]
    api.get_feedback(rid)
    api.add_round(rid, decisions=str(_plan_file(tmp_path, name="result")))       # the build result, round 2
    assert service.check_stalled(now=_submitted_ts(rid) + 30 * DAY) == 0 and _stalled(env) == []
    old = api.get_feedback(rid, 1)
    assert old["action"] == "build" and "next_step" not in old                    # not owed any more
    assert api.get_feedback(rid)["action"] == "send"                              # round 2: nothing sent yet


def test_ack_on_a_build_silences_the_ping_like_any_round_with_work(env, tmp_path):
    data = _review(tmp_path)
    _build(data)
    assert api.ack(data["id"])["stalled_notice"] == "skipped"
    assert service.check_stalled(now=_submitted_ts(data["id"]) + DAY) == 0 and _stalled(env) == []


# ---------------------------------------------------------------- a plain Send on the same plan board

def test_a_plain_send_on_a_plan_board_keeps_send_behaviour(env, tmp_path):
    data = _review(tmp_path)
    rid = data["id"]
    service.submit(rid, 1, {"marks": _all_resolved(p1={"status": "change", "note": "split it"})}, data["token"])
    payload = json.loads(env["receiver"].wakes[0]["body"])
    assert payload["action"] == "send" and "Build pressed" not in payload["summary"]
    assert json.loads(env["receiver"].notices[0]["body"])["message"] == (
        "📨 Got your feedback on Plan, round 1 (approved 3, change 1, skip 1). "
        "Hermes is working on round 2; the link lands here when it's ready.")
    ts = _submitted_ts(rid)
    assert service.check_stalled(now=ts + 15 * 60 - 5) == 0
    assert service.check_stalled(now=ts + 15 * 60) == 1                           # the unmultiplied wait
    assert "15 min after you sent it" in json.loads(_stalled(env)[0]["body"])["message"]
    fb = _tool_feedback(rid)
    assert fb["action"] == "send" and "next_step" not in fb


def test_a_plain_send_read_window_is_unmultiplied(env, tmp_path):
    data = _review(tmp_path)
    service.submit(data["id"], 1, {"marks": _all_resolved(p1={"status": "change", "note": "split it"})}, data["token"])
    api.get_feedback(data["id"])
    read = service._parse_iso(_round(data["id"])["submission"]["read_at"])
    assert service.check_stalled(now=read + 60 * 60 - 5) == 0
    assert service.check_stalled(now=read + 60 * 60) == 1
    assert "read your feedback 60 min ago" in json.loads(_stalled(env)[0]["body"])["message"]


def test_an_all_approved_send_on_a_plan_board_still_expects_no_round(env, tmp_path):
    data = _review(tmp_path)
    service.submit(data["id"], 1, {"marks": _all_resolved(q2={"choice": "recommended"})}, data["token"])
    assert json.loads(env["receiver"].wakes[0]["body"])["action"] == "send"
    assert json.loads(env["receiver"].notices[0]["body"])["message"] == SEND_ACK
    assert service.check_stalled(now=_submitted_ts(data["id"]) + DAY) == 0
    assert _round(data["id"])["submission"]["stalled_notice"]["reason"].startswith("nothing to build")


def test_a_variants_round_payload_and_feedback_say_send(review, env):
    from conftest import FEEDBACK
    service.submit(review["id"], 1, FEEDBACK, review["token"])
    assert json.loads(env["receiver"].wakes[0]["body"])["action"] == "send"
    fb = api.get_feedback(review["id"])
    assert fb["action"] == "send" and "next_step" not in fb


def test_mcp_tool_descriptions_teach_action_next_step_and_the_build_result():
    listed = {t["name"]: t["description"] for t in mcp.tools()}
    assert "action" in listed["mock_review_feedback"] and "next_step" in listed["mock_review_feedback"]
    assert "Build" in listed["mock_review_add_round"]
    assert "Build" in listed["mock_review_ack"]


def test_the_skill_says_what_to_do_when_start_building_wakes_you():
    from conftest import ROOT
    text = (ROOT / "skills" / "usage" / "SKILL.md").read_text(encoding="utf-8")
    section = text.split("## When Build wakes you", 1)[1].split("\n## ", 1)[0]
    for phrase in ("Build the approved plan in this session", "approved default, settled rule and question answer",
                   "`mock_review_add_round`", "`notes_answered`", "`revised`", "fresh `gate`",
                   "Never re-ask", "Do not call `mock_review_ack` instead of building"):
        assert phrase in section, phrase
    assert '"preset": "plan"' in text and "design preset" in text


def test_wake_prompt_mentions_the_build_and_keeps_the_hand_off():
    from mockreview import setup
    assert "act on the notes, or build if Build was pressed." in setup.PROMPT_TEMPLATE
    assert setup.HANDOFF_LINE in setup.PROMPT_TEMPLATE


# ---------------------------------------------------------------- a next round does not re-ask what was settled

def _sent(tmp_path, **over):
    data = _review(tmp_path)
    service.submit(data["id"], 1, {"marks": _all_resolved(**over), "action": "send"}, data["token"])
    return data["id"]


def _same_plan(tmp_path):
    """Round 1's file sent again: every block back, none marked as revised."""
    dec = copy.deepcopy(PLAN)
    for b in dec["blocks"]:
        b.pop("revised", None)
    return str(_plan_file(tmp_path, dec, "again"))


def _next_plan(tmp_path, keep, revised=()):
    """PLAN with only the markable blocks in `keep` (context blocks stay), `revised` set on the ones named."""
    dec = copy.deepcopy(PLAN)
    for b in dec["blocks"]:
        b.pop("revised", None)
    markable = {"decision", "question", "piece", "defaults"}
    dec["blocks"] = [b for b in dec["blocks"] if b["type"] not in markable or b.get("id", "defaults") in keep]
    for b in dec["blocks"]:
        if b.get("id") in revised:
            b["revised"] = "Revised"
    dec["notes_answered"] = [n for n in dec.get("notes_answered") or [] if n["ref"] in keep]
    return str(_plan_file(tmp_path, dec, "next"))


def test_add_round_refuses_a_round_that_re_asks_approved_blocks_and_answered_questions(env, tmp_path):
    rid = _sent(tmp_path, p1={"status": "change", "note": "split it"})
    with pytest.raises(store.ReviewError) as e:
        api.add_round(rid, decisions=_same_plan(tmp_path))          # the copied round 1 file
    msg = str(e.value)
    assert e.value.status == 409 and "round 1 already settled" in msg and "allow_reask" in msg
    for settled in ("defaults", "q1", "q2", "p2", "d1"):
        assert settled in msg
    assert "p1" not in msg and "p3" not in msg                                         # on Change / skipped: may return
    assert len(store.load(rid)["rounds"]) == 1                                         # nothing was added

    res = api.add_round(rid, decisions=_next_plan(tmp_path, {"p1", "p3"}, revised={"p1"}))
    assert res["round"] == 2


def test_a_revised_block_and_allow_reask_pass_the_re_ask_guard(env, tmp_path):
    rid = _sent(tmp_path, p1={"status": "change", "note": "split it"})
    res = api.add_round(rid, decisions=_next_plan(tmp_path, {"p1", "p2"}, revised={"p1", "p2"}))   # p2 approved, but changed
    assert res["round"] == 2

    rid = _sent(tmp_path, p1={"status": "change", "note": "split it"})
    assert api.add_round(rid, decisions=_same_plan(tmp_path), allow_reask=True)["round"] == 2
    rid = _sent(tmp_path, p1={"status": "change", "note": "split it"})
    tool = mcp.call_tool("mock_review_add_round", {"review_id": rid, "decisions": _same_plan(tmp_path)},
                         mcp.Options())
    assert tool["isError"] is True and "already settled" in tool["content"][0]["text"]
    assert cli.main(["add-round", rid, "--decisions", _same_plan(tmp_path), "--allow-reask"]) == 0


def test_a_round_with_no_feedback_yet_is_not_guarded(env, tmp_path):
    data = _review(tmp_path)
    assert api.add_round(data["id"], decisions=_same_plan(tmp_path))["round"] == 2


def test_send_feedback_and_the_wake_prompt_say_what_the_next_round_may_ask(env, tmp_path):
    rid = _sent(tmp_path, p1={"status": "change", "note": "split it"})
    fb = api.get_feedback(rid)
    assert fb["action"] == "send" and fb["next_round"] == api.SEND_NEXT_ROUND and "settled" in fb["next_round"]
    assert _tool_feedback(rid)["next_round"] == fb["next_round"]
    assert "go in a `settled` block" in setup.PROMPT_TEMPLATE
