"""GUEST-2: a share-link guest's Send and pin fix are stored exactly as before and never wake Hermes.

The store/wake seam runs against the scratch server (the wake hook is conftest's Receiver, so `.requests`/`.wakes`
must stay empty); the copy half drives real headless Chrome at 1440 and 390 because the Send control is UI.
"""

import json
import re

import pytest

from authkit import account, signed_in, token_of
from conftest import FEEDBACK, http
from mockreview import api, config, service, store, users
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
SAVED_GUEST = "Saved for the owner. A guest link never pings Hermes — the owner starts it."
STORED_GUEST_COPY = "Saved by a share-link guest. Hermes was not pinged: tell it in chat."
WAKE_TEXT = re.compile(r"Hermes is on it|notified|Send to Hermes")

DECISIONS = {"blocks": [
    {"type": "decision", "id": "d1", "title": "Layout", "layout": "side", "options": [
        {"id": "a", "label": "Today", "html": "<div class='a'>A</div>"},
        {"id": "b", "label": "All", "recommended": True, "html": "<div class='b'>B</div>"}]},
    {"type": "mock", "id": "m1", "title": "Today", "html": "<p>today</p>"},
]}


def join_guest(rid, role="reviewer"):
    made = users.share("test", rid, "create", role)
    return made, users.join_as_guest(users.resolve_share_token(token_of(made["path"])))


@pytest.fixture
def pin_board(env, tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    return store.create_review("Guest pin board", "brief", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(path)), look={"pin_fix": True})


# ---------------------------------------------------------------- A. Send


def test_a_reviewer_guests_send_is_stored_and_never_wakes(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/submit", body, signed_in(guest))
    assert code == 200, out
    res = json.loads(out)
    assert res["ok"] is True and res["notified"] is False and res["status"] == "stored", res
    assert res["detail"] == service.GUEST_NO_WAKE, res

    data = store.load(rid)
    rnd = data["rounds"][0]
    assert rnd["feedback"] and rnd["submission"]["status"] == "stored", rnd["submission"]
    assert (config.data_root() / rid / "rounds" / "1" / "feedback.md").is_file()
    assert env["receiver"].requests == []

    # The 409 "already sent" answer is the same for a guest, and the stored submission is never retried into a wake.
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/submit", body, signed_in(guest))
    assert code == 409 and "already sent" in json.loads(out)["error"]
    assert service.retry_pending() == 0
    service.flush_pin_fixes(rid)
    assert env["receiver"].requests == []


# ---------------------------------------------------------------- B. pin fix


def test_a_reviewer_guests_pin_fix_is_stored_and_never_wakes(env, server, pin_board, monkeypatch):
    rid = pin_board["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": pin_board["token"], "pin": {"block": "m1", "text": "make it bigger"}}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/pin-fix", body, signed_in(guest))
    assert code == 200, out
    res = json.loads(out)
    assert res["ok"] is True and res["notified"] is False, res

    requests = store.load(rid)["rounds"][-1]["pin_requests"]
    assert len(requests) == 1
    req = next(iter(requests.values()))
    assert req["status"] == "queued" and req["text"] == "make it bigger"
    assert env["receiver"].wakes == []

    # The §3c regression: a queued request must not ride the worker tick's coalesce into a wake.
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    service.retry_pending()
    service.flush_pin_fixes(rid)
    assert env["receiver"].wakes == []
    assert res["detail"] == service.GUEST_NO_WAKE, res
    assert store.load(rid)["rounds"][-1]["pin_requests"][req["id"]].get("quiet") is True


# ---------------------------------------------------------------- C. the controls


def test_a_signed_in_reviewers_send_still_wakes(env, server, review):
    rid = review["id"]
    reviewer = account(review_id=rid, role="reviewer")
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/submit", body, signed_in(reviewer))
    assert code == 200, out
    res = json.loads(out)
    assert res["notified"] is True and res["status"] == "notified", res
    assert len(env["receiver"].wakes) == 1
    assert len(env["receiver"].notices) == 1
    assert store.load(rid)["rounds"][0]["submission"]["status"] == "notified"


def test_a_signed_in_reviewers_pin_fix_still_wakes(env, server, pin_board, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_board["id"]
    reviewer = account(review_id=rid, role="reviewer")
    body = {"round": 1, "token": pin_board["token"], "pin": {"block": "m1", "text": "gap"}}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/pin-fix", body, signed_in(reviewer))
    assert code == 200, out
    res = json.loads(out)
    assert res["notified"] is True, res
    assert len(env["receiver"].wakes) == 1
    assert next(iter(store.load(rid)["rounds"][-1]["pin_requests"].values()))["status"] == "woken"


# ---------------------------------------------------------------- D. the board copy


@pytest.mark.browser
@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)])
def test_a_guest_gets_the_saved_copy_and_no_wake(env, review, server, chrome, width, height, mobile):
    rid = review["id"]
    made = users.share("test", rid, "create", "reviewer")
    guest = users.join_as_guest(users.resolve_share_token(token_of(made["path"])))
    b = Board(chrome, "about:blank", wait="true", width=width, height=height, mobile=mobile)
    try:
        b.cdp.call("Network.enable", session=b.sid)
        for key, value in [("mr_session", guest["session"]), ("mr_csrf", guest["csrf"])]:
            assert b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                              session=b.sid)["success"]
        b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
        assert b.until("!!document.querySelector('.btn-send')", timeout=60)
        assert b.js("document.querySelector('.btn-send').textContent.trim()") == "Send"
        assert not WAKE_TEXT.search(b.js("document.body.innerText") or ""), \
            "the board promised a wake before the guest sent anything"

        b.press(".btn-pick")   # the control is enabled for a guest as soon as there is something to send
        assert b.until("(function(){var e=document.querySelector('.btn-send');"
                       "return !!e && !e.disabled && e.getAttribute('aria-disabled') !== 'true'})()")
        b.js("window.__toasts=[];new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach("
             "function(n){if(n.classList&&n.classList.contains('toast'))window.__toasts.push("
             "{text:n.textContent,cls:n.className})})})}).observe(document.body,{childList:true});")
        b.press(".btn-send")
        assert b.until("(window.__toasts||[]).length > 0", timeout=15), "no toast after Send"
        toast = json.loads(b.js("JSON.stringify(window.__toasts[0])"))
        assert toast["text"] == SAVED_GUEST, toast
        assert "toast-ok" in toast["cls"], toast
        assert b.until("!!document.querySelector('.sb-sub')", timeout=15)
        assert b.js("document.querySelector('.sb-sub').textContent") == SAVED_GUEST
        assert not WAKE_TEXT.search(b.js("document.body.innerText") or ""), \
            "the board still promised a wake to a guest"

        assert env["receiver"].requests == []
        assert store.load(rid)["rounds"][0]["submission"]["status"] == "stored"
    finally:
        b.js("localStorage.clear()")


# ---------------------------------------------------------------- E. downstream paths


@pytest.mark.browser
@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)])
def test_the_owner_sees_a_guests_send_was_saved_and_never_pinged(env, review, server, chrome, width, height, mobile):
    rid = review["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/submit", body, signed_in(guest))
    assert code == 200, out
    assert json.loads(out)["status"] == "stored"

    b = Board(chrome, "about:blank", wait="true", width=width, height=height, mobile=mobile)
    try:
        b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
        assert b.until("!!document.querySelector('.banner')", timeout=60)
        assert b.until("!!document.querySelector('.sb-sub')", timeout=60)
        text = b.js("document.body.innerText") or ""
        assert not WAKE_TEXT.search(text), text
        assert b.js("document.querySelector('.banner').textContent") == STORED_GUEST_COPY
        assert b.js("document.querySelector('.sb-sub').textContent") == STORED_GUEST_COPY
        assert b.js("document.querySelector('.sb-title').textContent") == "Saved"
        assert env["receiver"].requests == []
    finally:
        b.js("localStorage.clear()")


def test_a_guests_stored_send_never_triggers_the_stalled_ping(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/submit", body, signed_in(guest))
    assert code == 200, out
    assert json.loads(out)["status"] == "stored"
    ts = service._parse_iso(store.load(rid)["rounds"][0]["submission"]["submitted_at"])
    assert service.check_stalled(now=ts + 60 * 60) == 0
    assert env["receiver"].notices == []
    assert "stalled_notice" not in store.load(rid)["rounds"][0]["submission"]


def test_a_guests_pin_fix_survives_the_timeout_pass_and_stays_visible(env, server, pin_board):
    rid = pin_board["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": pin_board["token"], "pin": {"block": "m1", "text": "make it bigger"}}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/pin-fix", body, signed_in(guest))
    assert code == 200, out
    before = store.load(rid)["rounds"][-1]["pin_requests"]
    assert len(before) == 1
    req_id = next(iter(before))
    ts = service._parse_iso(before[req_id]["requested_at"])
    assert service.check_stalled(now=ts + 20 * 60) == 0
    after = store.load(rid)["rounds"][-1]["pin_requests"][req_id]
    assert after["status"] == "queued" and after["quiet"] is True, after
    assert req_id in api.get_feedback(rid)["next_step"]
    assert env["receiver"].requests == []
