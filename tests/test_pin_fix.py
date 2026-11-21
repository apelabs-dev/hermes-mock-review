"""Pin fixes: a reviewer asks for one block of the current round to be patched in place.

The trigger (an @Hermes comment) and the thread chip belong to the thread layer; this module covers the stored
request, the coalesced wake, the per-block revision and the patch itself.
"""

import json
import re
import time

import pytest

from authkit import account, peer, signed_in  # noqa: F401 - fixtures
from conftest import http, load_plugin
from mockreview import api, mcp, owner, service, store, targets
from mockreview.store import ReviewError
from test_plugin import FakeCtx

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
DEC = {"blocks": [
    {"type": "decision", "id": "d1", "title": "Layout", "layout": "side", "options": [
        {"id": "a", "label": "Today", "html": "<div class='a'>A</div>"},
        {"id": "b", "label": "All", "recommended": True, "html": "<div class='b'>B</div>"}]},
    {"type": "mock", "id": "m1", "title": "Today", "html": "<p>today</p>"},
    {"type": "artifact", "id": "art1", "title": "Artifact", "html": "<p>art</p>"},
    {"type": "text", "title": "Why", "text": "context"},
]}


def pin(block="m1", text="make it bigger", **extra):
    return {"block": block, "text": text, **extra}


@pytest.fixture
def review_factory(env, tmp_path):
    count = {"n": 0}

    def make(look=None, mode="local"):
        count["n"] += 1
        f = tmp_path / f"dec-{count['n']}" / "decisions.json"
        f.parent.mkdir()
        f.write_text(json.dumps(DEC), encoding="utf-8")
        return store.create_review("Pin board", "brief", ORIGIN, mode,
                                   store.parse_round_spec(decisions=str(f)), look=look)
    return make


@pytest.fixture
def pin_review(review_factory):
    return review_factory({"pin_fix": True})


@pytest.fixture
def plain_review(review_factory):
    return review_factory()


def _round(rid, n=0):
    return store.load(rid)["rounds"][n]


def test_burst_of_pin_fixes_wakes_once_and_coalesces(pin_review, env):
    rid = pin_review["id"]
    ids = []
    for i in range(10):
        res = service.request_pin_fix(rid, 1, pin("m1", f"fix {i}"), pin_review["token"])
        ids.append(res["request"]["id"])
        assert res["notified"] is False
    assert env["receiver"].wakes == []                      # still inside the coalescing window

    ok, _ = service.flush_pin_fixes(rid, now=time.time() + 21)
    assert ok
    wakes = env["receiver"].wakes
    assert len(wakes) == 1
    body = json.loads(wakes[0]["body"])
    assert body["event_type"] == "review.pin_fix" and body["action"] == "pin_fix"
    assert body["requests"] == ids
    assert {k.lower(): v for k, v in wakes[0]["headers"].items()}["x-request-id"].endswith("-p1")
    assert all(q["status"] == "woken" for q in _round(rid)["pin_requests"].values())


def test_min_interval_respected(pin_review, env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_review["id"]
    first = service.request_pin_fix(rid, 1, pin("m1", "first"), pin_review["token"])
    assert first["notified"] is True and len(env["receiver"].wakes) == 1

    second = service.request_pin_fix(rid, 1, pin("m1", "second"), pin_review["token"])
    assert second["notified"] is False and len(env["receiver"].wakes) == 1

    ok, _ = service.flush_pin_fixes(rid, now=time.time() + 61)
    assert ok and len(env["receiver"].wakes) == 2
    assert json.loads(env["receiver"].wakes[1]["body"])["requests"] == [second["request"]["id"]]


def test_same_idem_is_noop(pin_review, env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_review["id"]
    first = service.request_pin_fix(rid, 1, pin("m1", "fix"), pin_review["token"], idem="abc")
    again = service.request_pin_fix(rid, 1, pin("m1", "fix"), pin_review["token"], idem="abc")
    assert again["duplicate"] is True and again["request"]["id"] == first["request"]["id"]
    assert len(_round(rid)["pin_requests"]) == 1
    assert len(env["receiver"].wakes) == 1


def test_two_requesters_with_the_same_idem_are_independent(server, peer, pin_review):
    rid = pin_review["id"]
    first_user = account(review_id=rid, role="reviewer", name="First")
    second_user = account(review_id=rid, role="reviewer", name="Second")
    body = {"round": 1, "token": pin_review["token"], "idem": "shared-key"}
    code, _, out = http("POST", f"{server}/r/{rid}/api/pin-fix",
                        dict(body, pin=pin("m1", "first text")), signed_in(first_user))
    assert code == 200, out
    first = json.loads(out)
    code, _, out = http("POST", f"{server}/r/{rid}/api/pin-fix",
                        dict(body, pin=pin("m1", "second text")), signed_in(second_user))
    assert code == 200, out
    second = json.loads(out)
    assert first["request"]["id"] != second["request"]["id"]
    stored = _round(rid)["pin_requests"]
    assert len(stored) == 2
    assert stored[first["request"]["id"]]["text"] == "first text"
    assert stored[second["request"]["id"]]["text"] == "second text"


def test_same_requester_retry_is_still_a_noop(server, peer, pin_review):
    rid = pin_review["id"]
    reviewer = account(review_id=rid, role="reviewer")
    body = {"round": 1, "token": pin_review["token"], "pin": pin("m1", "one"), "idem": "shared-key"}
    code, _, out = http("POST", f"{server}/r/{rid}/api/pin-fix", body, signed_in(reviewer))
    assert code == 200, out
    first = json.loads(out)
    code, _, out = http("POST", f"{server}/r/{rid}/api/pin-fix", body, signed_in(reviewer))
    assert code == 200, out
    again = json.loads(out)
    assert again["duplicate"] is True and again["request"]["id"] == first["request"]["id"]
    assert len(_round(rid)["pin_requests"]) == 1


def test_wakeguard_hold_marks_failed(review_factory, env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    data = review_factory({"pin_fix": True}, mode="public")
    res = service.request_pin_fix(data["id"], 1, pin("m1"), data["token"])
    assert res["notified"] is False and env["receiver"].wakes == []
    req = next(iter(_round(data["id"])["pin_requests"].values()))
    assert req["status"] == "failed" and req["detail"].startswith("held, not woken")
    assert _round(data["id"])["pin_fix"]["wake"]["status"] == "failed"


def test_terminal_wake_failure_marks_failed(pin_review, env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    env["receiver"].status = 400
    service.request_pin_fix(pin_review["id"], 1, pin("m1"), pin_review["token"])
    req = next(iter(_round(pin_review["id"])["pin_requests"].values()))
    assert req["status"] == "failed" and req["detail"].startswith("HTTP 400")


def test_retryable_failure_retries_the_same_delivery_id(pin_review, env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_review["id"]
    env["receiver"].status = 500
    res = service.request_pin_fix(rid, 1, pin("m1"), pin_review["token"])
    assert res["status"] == "queued"
    delivery = _round(rid)["pin_fix"]["wake"]["delivery_id"]
    env["receiver"].status = 202
    ok, _ = service.flush_pin_fixes(rid, now=time.time() + 61)
    assert ok
    wakes = env["receiver"].wakes
    assert len(wakes) == 2
    ids = [{k.lower(): v for k, v in w["headers"].items()}["x-request-id"] for w in wakes]
    assert ids[0] == ids[1] == delivery
    assert next(iter(_round(rid)["pin_requests"].values()))["status"] == "woken"


def test_pending_retry_keeps_its_batch_and_delivery_id(pin_review, env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    monkeypatch.setenv("MOCKREVIEW_RETRY_SECONDS", "300")
    rid = pin_review["id"]
    env["receiver"].status = 500
    first = service.request_pin_fix(rid, 1, pin("m1", "first"), pin_review["token"])
    wake = _round(rid)["pin_fix"]["wake"]
    assert wake["status"] == "pending_notify" and wake["delivery_id"].endswith("-p1")
    assert len(env["receiver"].wakes) == 1

    t0 = time.time()
    second = service.request_pin_fix(rid, 1, pin("m1", "second"), pin_review["token"])
    assert second["status"] == "queued"
    ok, _ = service.flush_pin_fixes(rid, now=t0 + 61)
    assert ok is False
    assert len(env["receiver"].wakes) == 1
    wake = _round(rid)["pin_fix"]["wake"]
    assert wake["delivery_id"].endswith("-p1")
    assert wake["requests"] == [first["request"]["id"]]
    assert second["request"]["id"] not in wake["requests"]
    assert _round(rid)["pin_requests"][second["request"]["id"]]["status"] == "queued"

    env["receiver"].status = 202
    ok, _ = service.flush_pin_fixes(rid, now=t0 + 301)
    assert ok is True
    wakes = env["receiver"].wakes
    assert len(wakes) == 2
    ids = [{k.lower(): v for k, v in w["headers"].items()}["x-request-id"] for w in wakes]
    assert ids[0] == ids[1] == wake["delivery_id"]
    assert json.loads(wakes[1]["body"])["requests"] == [first["request"]["id"]]
    assert _round(rid)["pin_requests"][second["request"]["id"]]["status"] == "queued"


def test_timeout_clears_indicator(pin_review, env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_review["id"]
    res = service.request_pin_fix(rid, 1, pin("m1"), pin_review["token"])
    assert res["status"] == "woken"
    service.check_stalled(now=time.time() + (15 + 1) * 60)
    data = store.load(rid)
    req = data["rounds"][0]["pin_requests"][res["request"]["id"]]
    assert req["status"] == "timeout" and "no patch within 15 min" in req["detail"]
    shown = store.public_state(data)["rounds"][0]["pin_requests"][req["id"]]
    assert shown["status"] == "failed"
    assert service.pin_fix_status(data, req) == {"state": "failed", "detail": req["detail"]}


def test_timeout_with_stalled_pings_off(pin_review, env, monkeypatch):
    # The worker's pin-only pass (stalled pings off) times the request out the same way and sends no notice.
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    monkeypatch.setenv("MOCKREVIEW_STALE_AFTER_MINUTES", "0")
    rid = pin_review["id"]
    res = service.request_pin_fix(rid, 1, pin("m1"), pin_review["token"])
    assert res["status"] == "woken"
    wakes = len(env["receiver"].wakes)
    assert service.expire_pin_requests(now=time.time() + 14 * 60) == 0
    assert service.expire_pin_requests(now=time.time() + (15 + 1) * 60) == 1
    req = store.load(rid)["rounds"][0]["pin_requests"][res["request"]["id"]]
    assert req["status"] == "timeout"
    assert len(env["receiver"].wakes) == wakes


def test_patch_block_marks_done(pin_review, env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_review["id"]
    res = service.request_pin_fix(rid, 1, pin("m1", "new copy"), pin_review["token"])
    req_id = res["request"]["id"]
    path = store.review_dir(rid) / "rounds/1/a/decisions.json"
    old_html = next(b for b in json.loads(path.read_text(encoding="utf-8"))["blocks"]
                    if b["id"] == "m1")["html"]
    out = api.patch_block(rid, "m1", "<p class='new'>new</p>", round_n=1, if_rev=0, request_ids=[req_id])
    assert out["rev"] == 1 and out["done"] == [req_id]

    data = store.load(rid)
    stored = json.loads(path.read_text(encoding="utf-8"))
    block = next(b for b in stored["blocks"] if b["id"] == "m1")
    assert block["html"] == targets.stamp_html("<p class='new'>new</p>")
    assert block["html"] != "<p>today</p>"
    assert data["rounds"][0]["blocks_rev"]["m1"] == 1
    req = data["rounds"][0]["pin_requests"][req_id]
    assert req["status"] == "done" and req["done_at"]
    assert req["detail"] == "patched in round 1 (revision 1)"
    history = data["rounds"][0]["history"]
    assert len(history) == 1 and history[0]["action"] == "patch_block" and history[0]["rev"] == 1
    prev = store.review_dir(rid) / history[0]["prev_file"]
    assert prev.is_file() and prev.read_text(encoding="utf-8") == old_html


def test_patch_block_refuses_id_changes(pin_review):
    rid = pin_review["id"]
    path = store.review_dir(rid) / "rounds/1/a/decisions.json"
    before = path.read_bytes()
    with pytest.raises(ReviewError) as e:
        api.patch_block(rid, "nope", "<p>x</p>", round_n=1, if_rev=0)
    assert e.value.status == 404
    with pytest.raises(ReviewError) as e:
        api.patch_block(rid, "d1", "<p>x</p>", option="z", round_n=1, if_rev=0)
    assert e.value.status == 409
    with pytest.raises(ReviewError) as e:
        api.patch_block(rid, "d1", "<p>x</p>", round_n=1, if_rev=0)
    assert e.value.status == 409 and "name the option" in e.value.message
    assert path.read_bytes() == before

    api.patch_block(rid, "m1", '<div id="renamed">x</div>', round_n=1, if_rev=0)
    stored = json.loads(path.read_text(encoding="utf-8"))
    block = next(b for b in stored["blocks"] if b["id"] == "m1")
    assert (block["id"], block["type"], block["title"]) == ("m1", "mock", "Today")
    decision = next(b for b in stored["blocks"] if b["id"] == "d1")
    assert [o["id"] for o in decision["options"]] == ["a", "b"]


def test_patch_block_refuses_a_stale_rev(pin_review):
    rid = pin_review["id"]
    api.patch_block(rid, "m1", "<p>one</p>", round_n=1, if_rev=0)
    with pytest.raises(ReviewError) as e:
        api.patch_block(rid, "m1", "<p>two</p>", round_n=1, if_rev=0)
    assert e.value.status == 409
    assert "revision 0" in e.value.message and "now 1" in e.value.message


def test_patch_requires_round_and_revision(pin_review):
    rid = pin_review["id"]
    path = store.review_dir(rid) / "rounds/1/a/decisions.json"
    before = path.read_bytes()
    with pytest.raises(ReviewError) as e:
        api.patch_block(rid, "m1", "<p>x</p>", if_rev=0)
    assert e.value.status == 400 and "round you read" in e.value.message
    with pytest.raises(ReviewError) as e:
        api.patch_block(rid, "m1", "<p>x</p>", round_n=1)
    assert e.value.status == 400 and "if_rev" in e.value.message
    assert path.read_bytes() == before
    assert store.load(rid)["rounds"][0]["blocks_rev"] == {}
    assert not (store.review_dir(rid) / "rounds/1/history").exists()

    out = api.patch_block(rid, "m1", "<p>x</p>", round_n="1", if_rev="0")
    assert out["rev"] == 1


def test_patch_refuses_a_stale_round(pin_review, tmp_path):
    rid = pin_review["id"]
    nxt = tmp_path / "stale-round" / "decisions.json"
    nxt.parent.mkdir()
    changed = {"blocks": [dict(b) for b in DEC["blocks"]]}
    next(b for b in changed["blocks"] if b["id"] == "m1")["html"] = "<p>round two</p>"
    nxt.write_text(json.dumps(changed), encoding="utf-8")
    api.add_round(rid, decisions=str(nxt))
    path = store.review_dir(rid) / "rounds/2/a/decisions.json"
    before = path.read_bytes()
    with pytest.raises(ReviewError) as e:
        api.patch_block(rid, "m1", "<p>late</p>", round_n=1, if_rev=0)
    assert e.value.status == 409
    assert "not the current round (2)" in e.value.message and "read round 2 again" in e.value.message
    assert path.read_bytes() == before
    assert store.load(rid)["rounds"][1]["blocks_rev"] == {}


def test_patch_uses_the_owner_store(pin_review, env, monkeypatch, tmp_path):
    rid = pin_review["id"]
    real_root = store.config.data_root()
    monkeypatch.setenv("MOCKREVIEW_HOME", str(tmp_path / "private-home"))
    monkeypatch.setattr(owner, "resolve_soft", lambda: owner.Context(real_root, True))
    got = api.get_feedback(rid)
    assert got["review_id"] == rid
    out = api.patch_block(rid, "m1", "<p>owner</p>", round_n=1, if_rev=0)
    assert out["review_id"] == rid and out["rev"] == 1


def test_patch_refuses_request_for_another_block(pin_review, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_review["id"]
    res = service.request_pin_fix(rid, 1, pin("d1", "wider", option="a"), pin_review["token"])
    req_id = res["request"]["id"]
    path = store.review_dir(rid) / "rounds/1/a/decisions.json"
    before = path.read_bytes()
    with pytest.raises(ReviewError) as e:
        api.patch_block(rid, "m1", "<p>wrong</p>", round_n=1, if_rev=0, request_ids=[req_id])
    assert e.value.status == 409
    assert req_id in e.value.message and "this patch changes m1" in e.value.message
    assert path.read_bytes() == before
    assert _round(rid)["blocks_rev"] == {}
    assert _round(rid)["pin_requests"][req_id]["status"] not in service.PIN_TERMINAL
    assert not (store.review_dir(rid) / "rounds/1/history").exists()


def test_patch_refuses_request_for_another_option(pin_review, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_review["id"]
    res = service.request_pin_fix(rid, 1, pin("d1", "note", option="a"), pin_review["token"])
    req_id = res["request"]["id"]
    path = store.review_dir(rid) / "rounds/1/a/decisions.json"
    before = path.read_bytes()
    with pytest.raises(ReviewError) as e:
        api.patch_block(rid, "d1", "<p>other</p>", option="b", round_n=1, if_rev=0, request_ids=[req_id])
    assert e.value.status == 409 and req_id in e.value.message
    assert path.read_bytes() == before
    assert _round(rid)["pin_requests"][req_id]["status"] not in service.PIN_TERMINAL


def test_send_still_accepted_after_pin_fix(server, pin_review):
    rid = pin_review["id"]
    service.request_pin_fix(rid, 1, pin("m1"), pin_review["token"])
    body = {"round": 1, "token": pin_review["token"],
            "feedback": {"marks": {"d1": {"status": "approve", "option": "a"}}}}
    code, _, out = http("POST", f"{server}/r/{rid}/api/submit", body)
    assert code == 200, out


def test_pin_fix_route_checks_host_and_origin(server, pin_review):
    rid = pin_review["id"]
    body = {"round": 1, "token": pin_review["token"], "pin": pin("m1")}
    code, _, _ = http("POST", f"{server}/r/{rid}/api/pin-fix", body, headers={"Host": "attacker.example"})
    assert code == 421
    code, _, _ = http("POST", f"{server}/r/{rid}/api/pin-fix", body,
                      headers={"Origin": "https://attacker.example"})
    assert code == 403
    assert _round(rid)["pin_requests"] == {}


def test_pin_fix_refused_when_pin_fix_off(plain_review):
    rid = plain_review["id"]
    with pytest.raises(ReviewError, match="does not take pin fixes"):
        service.request_pin_fix(rid, 1, pin("m1"), plain_review["token"])
    assert _round(rid)["pin_requests"] == {}


def test_old_review_without_pin_fix_setting_unchanged(plain_review):
    rid = plain_review["id"]
    fb = {"marks": {"d1": {"status": "approve", "option": "a"}}}
    assert service.submit(rid, 1, fb, plain_review["token"], do_notify=False)["ok"]
    got = api.get_feedback(rid)
    assert got["pin_requests"] == []
    assert "mock_review_patch_block" not in (got.get("next_step") or "")
    with pytest.raises(ReviewError, match="does not take pin fixes"):
        service.request_pin_fix(rid, 1, pin("m1"), plain_review["token"])


def test_the_reviews_own_pin_fix_wins_and_only_a_review_without_it_takes_the_default(review_factory, monkeypatch):
    """`_review_setting` reads the review first: a stored value never changes under a later config edit, and only a
    review that lacks the key falls back to the current default chain."""
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX", "true")
    stored_off = review_factory({"pin_fix": False})
    with pytest.raises(ReviewError, match="does not take pin fixes"):
        service.request_pin_fix(stored_off["id"], 1, pin("m1"), stored_off["token"])
    assert _round(stored_off["id"])["pin_requests"] == {}

    old = review_factory()                      # a review from before the setting existed: no settings block at all
    assert store.load(old["id"]).get("settings") is None
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    assert service.request_pin_fix(old["id"], 1, pin("m1"), old["token"])["request"]["id"]


def test_get_feedback_without_requests_unchanged(pin_review):
    rid = pin_review["id"]
    fb = {"marks": {"d1": {"status": "approve", "option": "a"}}}
    service.submit(rid, 1, fb, pin_review["token"], do_notify=False)
    got = api.get_feedback(rid)
    assert got["pin_requests"] == []
    assert "mock_review_patch_block" not in (got.get("next_step") or "")


def test_pins_without_ids_are_accepted(server, pin_review):
    rid = pin_review["id"]
    body = {"round": 1, "token": pin_review["token"],
            "feedback": {"pins": [{"block": "m1", "w": "desktop", "x": 0.2, "y": 0.3, "text": "old style"}]}}
    code, _, out = http("POST", f"{server}/r/{rid}/api/submit", body)
    assert code == 200, out
    stored = _round(rid)["feedback"]["pins"][0]
    assert re.fullmatch(r"[a-z0-9]{1,24}", stored["id"]) and stored["id"].startswith("p")


def test_add_round_clears_requests_as_superseded(pin_review, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_review["id"]
    res = service.request_pin_fix(rid, 1, pin("m1"), pin_review["token"])
    assert res["status"] == "woken"
    nxt = tmp_path / "next" / "decisions.json"
    nxt.parent.mkdir()
    nxt.write_text(json.dumps(DEC), encoding="utf-8")
    api.add_round(rid, decisions=str(nxt))
    req = store.load(rid)["rounds"][0]["pin_requests"][res["request"]["id"]]
    assert req["status"] == "done" and req["detail"] == "superseded by round 2"
    assert store.load(rid)["rounds"][0]["pin_fix"]["wake"]["status"] != "pending_notify"


def test_hour_cap_spans_rounds(review_factory, env, tmp_path):
    data = review_factory({"pin_fix": True, "pin_fix_max_per_hour": 1, "pin_fix_window_seconds": 0})
    rid = data["id"]
    first = service.request_pin_fix(rid, 1, pin("m1", "one"), data["token"])
    assert first["notified"] is True
    nxt = tmp_path / "cap-round" / "decisions.json"
    nxt.parent.mkdir()
    nxt.write_text(json.dumps(DEC), encoding="utf-8")
    api.add_round(rid, decisions=str(nxt))
    with pytest.raises(ReviewError) as e:
        service.request_pin_fix(rid, 2, pin("m1", "two"), data["token"])
    assert e.value.status == 429


def test_min_interval_spans_rounds(review_factory, env, tmp_path):
    data = review_factory({"pin_fix": True, "pin_fix_min_seconds": 3600, "pin_fix_max_per_hour": 60,
                           "pin_fix_window_seconds": 0})
    rid = data["id"]
    first = service.request_pin_fix(rid, 1, pin("m1", "one"), data["token"])
    assert first["notified"] is True and len(env["receiver"].wakes) == 1
    nxt = tmp_path / "interval-round" / "decisions.json"
    nxt.parent.mkdir()
    nxt.write_text(json.dumps(DEC), encoding="utf-8")
    api.add_round(rid, decisions=str(nxt))
    second = service.request_pin_fix(rid, 2, pin("m1", "two"), data["token"])
    assert second["notified"] is False and second["status"] == "queued"
    t0 = time.time()
    ok, _ = service.flush_pin_fixes(rid, now=t0 + 100)
    assert ok is False and len(env["receiver"].wakes) == 1
    ok, _ = service.flush_pin_fixes(rid, now=t0 + 3601)
    assert ok is True and len(env["receiver"].wakes) == 2
    assert json.loads(env["receiver"].wakes[1]["body"])["requests"] == [second["request"]["id"]]


def test_pin_fix_status_seam():
    for status, state in (("queued", "queued"), ("woken", "working"), ("done", "done"),
                          ("failed", "failed"), ("timeout", "failed")):
        assert service.pin_fix_status({}, {"status": status, "detail": "why"}) == {"state": state,
                                                                                   "detail": "why"}
    assert service.pin_fix_status({}, {"status": "done"}) == {"state": "done", "detail": ""}


def test_patch_block_tool_round_trip(pin_review, monkeypatch):
    calls = []
    real = api.patch_block

    def spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(api, "patch_block", spy)
    rid = pin_review["id"]
    result = mcp.call_tool("mock_review_patch_block",
                           {"review_id": rid, "block": "m1", "html": "<p>mcp</p>", "round": 1, "if_rev": 0},
                           mcp.Options())
    assert result["isError"] is False, result
    body = json.loads(result["content"][0]["text"])
    assert body["rev"] == 1

    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    out = json.loads(ctx.tools["mock_review_patch_block"]["handler"](
        {"review_id": rid, "block": "m1", "html": "<p>plugin</p>", "round": 1, "if_rev": 1, "request_ids": []}))
    assert out["rev"] == 2, out
    assert len(calls) == 2
    assert calls[0][0][:3] == (rid, "m1", "<p>mcp</p>")
    assert calls[1][0][:3] == (rid, "m1", "<p>plugin</p>")
