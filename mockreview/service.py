"""Submission handling shared by the local board POST and the here.now poller."""

from __future__ import annotations

import calendar
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional

from . import config, feedback, notify, owner, settings, store
from .store import ReviewError

log = logging.getLogger("mockreview.service")


class AlreadySubmitted(ReviewError):
    pass


def _write_feedback_files(data: Dict[str, Any], round_: Dict[str, Any]) -> str:
    d = store.review_dir(data["id"]) / "rounds" / str(round_["n"])
    md = feedback.render_markdown(data, round_)
    store.atomic_write(d / "feedback.md", md)
    store.atomic_write(
        d / "feedback.json",
        json.dumps({"review_id": data["id"], "round": round_["n"], "submission": round_["submission"],
                    "feedback": round_["feedback"]}, indent=2, ensure_ascii=False),
    )
    return str(d / "feedback.md")


def _human(iso: Optional[str]) -> str:
    return (iso or "?").replace("T", " ")[:16] + " UTC" if iso else "earlier"


def _parse_iso(iso: Optional[str]) -> Optional[float]:
    if not isinstance(iso, str):
        return None
    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def notice_text(data: Dict[str, Any], round_: Dict[str, Any]) -> str:
    """The instant ack sent to the origin chat on an accepted submit. Counts and title only, never reviewer text."""
    fb = round_.get("feedback") or {}
    n = round_["n"]
    detail = ""
    if fb.get("kind") == "decisions":
        from .store import round_index
        c = feedback.decision_counts(fb, round_index(round_))
        detail = f" (approved {c['approve']}, change {c['change']}, skip {c['skip']})"
    return (f"📨 Got your feedback on {data['title']}, round {n}{detail}. "
            f"Hermes is working on round {n + 1}; the link lands here when it's ready.")


def stalled_text(data: Dict[str, Any], round_: Dict[str, Any], minutes: float) -> str:
    n = round_["n"]
    return (f"⏳ No round {n + 1} yet for {data['title']}, {minutes:g} min after you sent it. "
            f"The agent may have stalled; ask in this chat or resend from the board.")


def feedback_path(review_id: str, n: int) -> str:
    return str(store.review_dir(review_id) / "rounds" / str(n) / "feedback.md")


def submit(review_id: str, round_n: Any, raw: Any, token: Any, amend: bool = False,
           source: str = "local", record_id: Optional[str] = None, do_notify: bool = True) -> Dict[str, Any]:
    """Store feedback for the current round (idempotent per round) and wake Hermes.

    Raises ReviewError (403 bad token, 409 already submitted / closed / old round).
    """
    with store.locked(review_id):
        data = store.load(review_id)
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), data["token"].encode()):
            raise ReviewError("This link's submit token is not valid for this review.", 403)
        if data["status"] != "open":
            raise ReviewError("This review is closed — feedback can no longer be sent.", 409)
        current = data["rounds"][-1]
        if not isinstance(round_n, int) or isinstance(round_n, bool) or round_n != current["n"]:
            raise ReviewError(
                f"Round {round_n} is read-only — only the current round ({current['n']}) takes feedback.", 409)
        prev = current.get("submission")
        if current.get("feedback") and not amend:
            raise AlreadySubmitted(
                f"Feedback for round {current['n']} was already sent ({_human(prev.get('submitted_at'))}). "
                "Use “Amend feedback” to replace it.", 409)
        fb = feedback.sanitize_for_round(raw, current, data.get("settings"))
        seq = (prev or {}).get("seq", 0) + 1
        no_wake = data.get("wake") is False
        current["feedback"] = fb
        current["submission"] = {
            "seq": seq,
            "source": source,
            "record_id": record_id,
            "submitted_at": store.now_iso(),
            "status": "stored" if no_wake else "pending_notify",
            "attempts": 0,
            "last_error": None,
            "next_attempt": 0,
            "notified_at": None,
            "delivery_id": f"mockreview-{review_id}-r{current['n']}-s{seq}",
            "notice": {
                "status": "stored" if no_wake else "pending_notify",
                "attempts": 0,
                "last_error": None,
                "next_attempt": 0,
                "notified_at": None,
                "delivery_id": f"mockreview-{review_id}-r{current['n']}-s{seq}-notice",
            },
        }
        store.save(review_id, data)
        _write_feedback_files(data, current)
    result = {"ok": True, "review_id": review_id, "round": round_n, "amended": seq > 1,
              "status": "pending_notify", "notified": False}
    if no_wake:
        result.update(status="stored", detail="this board has wake off: feedback is stored, nobody is notified")
    elif do_notify:
        (ok, msg), (notice_ok, notice_msg) = deliver_submission(review_id, round_n)
        result.update(notified=ok, status="notified" if ok else "pending_notify", detail=msg,
                      notice={"notified": notice_ok, "detail": notice_msg})
    return result


def _wake_note(data: Dict[str, Any], round_: Dict[str, Any]) -> str:
    """Text a recovered (adopted) delivery carries so the woken session knows it is not a new request. Applies to
    the submission it was written for only: a later amend is a real new submission and is not marked."""
    wn = data.get("wake_note")
    sub = round_.get("submission") or {}
    if isinstance(wn, dict) and wn.get("round") == round_["n"] and wn.get("seq") == sub.get("seq"):
        return str(wn.get("text") or "")
    return ""


def build_payload(data: Dict[str, Any], round_: Dict[str, Any]) -> Dict[str, Any]:
    note = _wake_note(data, round_)
    payload = _build_payload(data, round_)
    if note:
        payload["summary"] = note + "\n\n" + payload["summary"]
        payload["recovered"] = True
    return payload


def _build_payload(data: Dict[str, Any], round_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_type": "review.submitted",
        "review_id": data["id"],
        "round": round_["n"],
        **store.normalize_origin(data.get("origin")),
        "title": data["title"],
        "feedback_path": feedback_path(data["id"], round_["n"]),
        "summary": feedback.short_summary(data, round_),
        "review_url": store.review_url(data),
        "mode": data["mode"],
        "amended": (round_.get("submission") or {}).get("seq", 1) > 1,
    }


SEND_LEASE_SECONDS = 30.0
STALLED_SCAN_SECONDS = 30.0   # minimum interval between watchdog directory scans in the worker tick


def notify_round(review_id: str, round_n: int, url: Optional[str] = None) -> "tuple[bool, str]":
    """POST the wake-up for a round's current submission and record the outcome.

    Exactly once per submission: a notified submission is not sent again, and a send in flight (another thread or
    process holds the lease) is not duplicated. The gateway's own delivery-id filter (1 h) is the second line.
    Only the owning server delivers: a call bound to the server's store from another process never sends.
    """
    if owner.is_bound():
        return False, "not the owner service: only the board server delivers wake-ups"
    with store.locked(review_id):
        data = store.load(review_id)
        r = store.get_round(data, round_n)
        sub = r.get("submission")
        if data.get("wake") is False:
            return False, "this board has wake off"
        if not sub or not r.get("feedback"):
            return False, "no submission"
        if sub.get("status") == "notified":
            return True, "already notified"
        if sub.get("sending_until", 0) > time.time():
            return False, "a send for this submission is already in flight"
        seq = sub["seq"]
        try:
            payload = build_payload(data, r)
        except ReviewError as e:
            sub.update(last_error=e.message, next_attempt=time.time() + config.retry_seconds())
            store.save(review_id, data)
            return False, e.message
        delivery_id = sub["delivery_id"]
        sub["sending_until"] = time.time() + SEND_LEASE_SECONDS
        store.save(review_id, data)
    try:
        ok, msg = notify.send(payload, delivery_id, url=url)
    except BaseException:
        with store.locked(review_id):
            data = store.load(review_id)
            cur = (store.get_round(data, round_n).get("submission") or {})
            if cur.get("seq") == seq:
                cur.pop("sending_until", None)
                store.save(review_id, data)
        raise
    with store.locked(review_id):
        data = store.load(review_id)
        r = store.get_round(data, round_n)
        sub = r.get("submission")
        if not sub or sub["seq"] != seq:  # amended while we were sending; the newer one gets its own send
            return ok, msg
        sub.pop("sending_until", None)
        sub["attempts"] = sub.get("attempts", 0) + 1
        if ok:
            sub.update(status="notified", notified_at=store.now_iso(), last_error=None, gateway_response=msg)
            log.info("notified Hermes: %s round %s (%s)", review_id, round_n, msg)
        else:
            sub.update(status="pending_notify", last_error=msg,
                       next_attempt=time.time() + config.retry_seconds())
            log.error("notify failed for %s round %s, will retry: %s", review_id, round_n, msg)
        store.save(review_id, data)
    return ok, msg


TERMINAL_NOTICE = ("notified", "dropped", "no_route")


def _notice_payload(data: Dict[str, Any], round_: Dict[str, Any], key: str, text: str) -> Dict[str, Any]:
    """Origin like the wake payload (the route's `--deliver-chat-id '{chat_id}'` renders from it), the event, the
    round and the message. The message carries counts and the title only: it is mirrored into the chat's session."""
    return {**store.normalize_origin(data.get("origin")),
            "event_type": "review.stalled" if key == "stalled_notice" else "review.notice",
            "review_id": data["id"], "round": round_["n"], "message": text}


def _deliver_notice(review_id: str, round_n: int, key: str, url: Optional[str] = None,
                    now: Optional[float] = None) -> "tuple[bool, str]":
    """POST one notice (key "notice" = the ack, "stalled_notice" = the ping) and persist its state at
    submission[key]. Only the owning server delivers; a failure is logged and retried, never raised. A retry
    after a gateway answer (502, ...) uses a new delivery id, because Hermes keeps the first one as seen even though it
    was not delivered; a retry after no answer keeps the id. Terminal states: notified; dropped (moot: newer round or closed review); no_route (the gateway has no
    such route, 404: re-run `hermes mockreview setup`). The ack also waits for the wake to be accepted, because
    "Hermes is working on it" is only true then."""
    if owner.is_bound():
        return False, "not the owner service: only the board server delivers notices"
    now = time.time() if now is None else now
    with store.locked(review_id):
        data = store.load(review_id)
        r = store.get_round(data, round_n)
        sub = r.get("submission")
        if data.get("wake") is False:
            return False, "this board has wake off"
        if not sub or not r.get("feedback"):
            return False, "no submission"
        state = sub.get(key) or {}
        if state.get("status") == "notified":
            return True, "already notified"
        if state.get("status") in TERMINAL_NOTICE:
            return False, f"notice {state['status']}"
        if data["status"] != "open" or data["rounds"][-1]["n"] != round_n:
            state.update(status="dropped", last_error=None)
            sub[key] = state
            store.save(review_id, data)
            return False, "dropped: a newer round exists or the review is closed"
        minutes = settings.get("stale_after_minutes")
        if key == "stalled_notice":
            ts = _parse_iso(sub.get("submitted_at"))
            if not minutes:
                return False, "stale-after pings are off"
            if ts is None or now - ts < minutes * 60.0:
                return False, "not stale yet"
        elif sub.get("status") != "notified":
            return False, "the wake-up is not accepted yet; the ack follows it"
        if state.get("sending_until", 0) > now:
            return False, "a notice for this submission is already in flight"
        seq = sub["seq"]
        try:
            text = stalled_text(data, r, minutes) if key == "stalled_notice" else notice_text(data, r)
            payload = _notice_payload(data, r, key, text)
        except ReviewError as e:
            state.update(status="pending_notify", last_error=e.message, next_attempt=now + config.retry_seconds())
            sub[key] = state
            store.save(review_id, data)
            return False, e.message
        suffix = "stalled" if key == "stalled_notice" else "notice"
        base_id = f"mockreview-{review_id}-r{round_n}-s{seq}-{suffix}"
        state.setdefault("delivery_id", base_id)
        delivery_id = state["delivery_id"]
        platform = payload["platform"]
        state["sending_until"] = now + SEND_LEASE_SECONDS
        sub[key] = state
        store.save(review_id, data)
    try:
        ok, msg = notify.send_notice(payload, delivery_id, platform, url=url)
    except Exception as e:
        log.exception("notice send raised for %s round %s (%s)", review_id, round_n, key)
        ok, msg = False, f"notice send raised: {e}"
    with store.locked(review_id):
        data = store.load(review_id)
        r = store.get_round(data, round_n)
        sub = r.get("submission")
        if not sub or sub["seq"] != seq:   # amended while we were sending; the newer submission has its own notices
            return ok, msg
        state = sub.get(key) or {}
        state.pop("sending_until", None)
        state["attempts"] = state.get("attempts", 0) + 1
        if ok:
            state.update(status="notified", notified_at=store.now_iso(), last_error=None, gateway_response=msg)
            log.info("notice delivered for %s round %s (%s): %s", review_id, round_n, key, msg)
        elif notify.is_no_route(msg):
            state.update(status="no_route", last_error=msg)
            log.warning("the gateway has no notice route for %s: run `hermes mockreview setup` to create %s "
                        "(the wake-up is unaffected; this notice will not be retried)",
                        platform, config.notice_route_name(platform))
        else:
            state.update(status="pending_notify", last_error=msg, next_attempt=time.time() + config.retry_seconds())
            if msg.startswith("HTTP "):
                # The gateway answered, so it recorded this delivery id before it tried to deliver: the same id would
                # come back "200 duplicate" for an hour and nothing would reach the chat. The next attempt gets a new
                # id. With no answer at all (unreachable, timeout) the id stays: that POST may have been delivered.
                state["delivery_id"] = f"{base_id}-a{state['attempts'] + 1}"
            log.error("notice failed for %s round %s (%s), will retry: %s", review_id, round_n, key, msg)
        sub[key] = state
        store.save(review_id, data)
    return ok, msg


def notify_notice(review_id: str, round_n: int, url: Optional[str] = None) -> "tuple[bool, str]":
    """POST the instant ack for a round's current submission, once its wake-up was accepted."""
    return _deliver_notice(review_id, round_n, "notice", url=url)


def notify_stalled(review_id: str, round_n: int, url: Optional[str] = None,
                   now: Optional[float] = None) -> "tuple[bool, str]":
    """POST the stalled ping for a round whose submission is past stale_after_minutes with no newer round."""
    return _deliver_notice(review_id, round_n, "stalled_notice", url=url, now=now)


def deliver_submission(review_id: str, round_n: int) -> "tuple[tuple[bool, str], tuple[bool, str]]":
    """Deliver one submission: the wake-up first, then the ack straight after it. The local server's submit, the
    here.now poller and retry_pending all go through here, so they order and gate the two the same way. A failed
    wake leaves the ack pending (it is never sent for an un-woken submission); an ack failure never affects the
    wake. Returns ((wake_ok, detail), (ack_ok, detail))."""
    wake = notify_round(review_id, round_n)
    return wake, notify_notice(review_id, round_n)


def check_stalled(now: Optional[float] = None) -> int:
    """Watchdog: POST exactly one stalled ping per submission whose round is the newest, still open, and past
    stale_after_minutes with no newer round. Only the owner service runs it (never a bound CLI call). Skips wake-off
    and closed reviews before doing any work. Returns how many pings were delivered."""
    if not settings.get("stale_after_minutes") or owner.is_bound():
        return 0
    now = time.time() if now is None else now
    sent = 0
    for rid in store.list_ids():
        try:
            data = store.load(rid)
        except (ReviewError, ValueError, OSError):
            continue
        if data.get("status") != "open" or data.get("wake") is False:
            continue
        r = data["rounds"][-1]
        sub = r.get("submission")
        if not sub or not r.get("feedback"):
            continue
        state = sub.get("stalled_notice") or {}
        if state.get("status") in TERMINAL_NOTICE:
            continue
        if state.get("status") == "pending_notify" and state.get("next_attempt", 0) > now:
            continue
        ok, _ = notify_stalled(rid, r["n"], now=now)
        if ok:
            sent += 1
            if data["mode"] == "herenow":   # the hosted board only learns about the ping from a republish
                from . import herenow
                herenow.republish_quietly(rid)
    return sent


def retry_pending(now: Optional[float] = None) -> int:
    """Retry every pending_notify submission that is due (wake first, ack right after it), and every pending ack
    whose wake already landed. Returns how many wakes became notified."""
    now = time.time() if now is None else now
    done = 0
    for rid in store.list_ids():
        try:
            data = store.load(rid)
        except (ReviewError, ValueError, OSError):
            continue
        for r in data["rounds"]:
            sub = r.get("submission") or {}
            if sub.get("status") == "pending_notify" and sub.get("next_attempt", 0) <= now:
                (ok, _), _ack = deliver_submission(rid, r["n"])
                if ok:
                    done += 1
                    if data["mode"] == "herenow" and data["status"] == "open":
                        from . import herenow
                        herenow.republish_quietly(rid)
            elif sub.get("status") == "notified":
                notice = sub.get("notice") or {}
                if notice.get("status") == "pending_notify" and notice.get("next_attempt", 0) <= now:
                    notify_notice(rid, r["n"])
    return done
