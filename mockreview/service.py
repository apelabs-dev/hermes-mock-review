"""Submission handling shared by the board POST and the server's delivery passes."""

from __future__ import annotations

import calendar
import hmac
import json
import logging
import sqlite3
import time
from typing import Any, Dict, Optional

from . import (config, feedback, notify, owner, platforms, sessionwake, settings, store, targets, threads, users,
               wakeguard)
from .store import ReviewError

log = logging.getLogger("mockreview.service")

# The detail a guest submission is stored with when its caller gives no reason. The board server never sends one any
# more (api/submit is the owner's alone, wakeguard.request_gate); `submit(guest=True)` keeps the guard.
GUEST_NO_WAKE = "a share-link guest never wakes Hermes: what you sent is saved for the owner"
GUEST_NEVER_DELIVERED = "a share-link guest's feedback is never delivered to Hermes"
# A record queued before the `by_owner` marker existed has no proof it came from the owner, Funnel or not: it stays
# stored until the owner sends it again from private ingress (or re-confirms the Reviewer role).
LEGACY_QUEUED_NO_WAKE = ("queued before this update without a by_owner marker: not sent, because the record has no "
                         "proof it came from the owner; send it again from private ingress to wake Hermes")


class AlreadySubmitted(ReviewError):
    pass


def _write_feedback_files(data: Dict[str, Any], round_: Dict[str, Any]) -> str:
    d = store.review_dir(data["id"]) / "rounds" / str(round_["n"])
    md = feedback.render_markdown(data, round_)
    store.atomic_write(d / "feedback.md", md)
    store.atomic_write(
        d / "feedback.json",
        json.dumps({"review_id": data["id"], "round": round_["n"], "submission": round_["submission"],
                    "feedback": None if store.guest_submission(round_) else round_["feedback"]},
                   indent=2, ensure_ascii=False),
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
    if is_build(round_):
        return (f"📨 {data['title']}: Got it — building round {n}'s plan now. "
                f"The result lands as round {n + 1} on the same link.")
    detail = ""
    if fb.get("kind") == "decisions":
        from .store import round_index
        c = feedback.decision_counts(fb, round_index(round_))
        detail = f" (approved {c['approve']}, change {c['change']}, skip {c['skip']})"
    return (f"📨 Got your feedback on {data['title']}, round {n}{detail}. "
            f"Hermes is working on round {n + 1}; the link lands here when it's ready.")


def stalled_text(data: Dict[str, Any], round_: Dict[str, Any], minutes: float,
                 read_minutes: Optional[float] = None) -> str:
    """The stalled ping. read_minutes set: the session read the feedback that long ago and still posted no round."""
    n = round_["n"]
    if read_minutes is not None:
        return (f"⏳ No round {n + 1} yet for {data['title']}: Hermes read your feedback {read_minutes:g} min ago "
                f"and has not posted the next round. Ask in this chat or resend from the board.")
    return (f"⏳ No round {n + 1} yet for {data['title']}, {minutes:g} min after you sent it. "
            f"The agent may have stalled; ask in this chat or resend from the board.")


def is_build(round_: Dict[str, Any]) -> bool:
    """True when the round's submission pressed the gate ("Build"): the agent builds and posts round N+1."""
    return (round_.get("feedback") or {}).get("action") == "build"


def nothing_to_build(round_: Dict[str, Any]) -> bool:
    """True when a submitted decisions round asks for no further work: every decision is Approved or Skipped (a note
    on an approve or skip is fine), every question took the recommendation, with no pins and no general notes, and the
    gate was not pressed. The agent is only
    expected to close then, so no round N+1 will come and a stalled ping would be wrong. A build is the opposite: the
    whole round is approved precisely so that round N+1 (the result) comes."""
    from .store import round_index
    fb = round_.get("feedback") or {}
    if fb.get("kind") != "decisions" or fb.get("action") == "build" or fb.get("pins") \
            or (fb.get("guidelines") or "").strip():
        return False
    index = round_index(round_)
    c = feedback.decision_counts(fb, index)
    # a custom answer overrides the recommendation, so the plan has to be revised: a round N+1 is due
    custom = any(((fb.get("marks") or {}).get(d["id"]) or {}).get("choice") == "custom" for d in index)
    return bool(index) and c["change"] == 0 and c["open"] == 0 and not custom


def feedback_path(review_id: str, n: int) -> str:
    return str(store.review_dir(review_id) / "rounds" / str(n) / "feedback.md")


def submit(review_id: str, round_n: Any, raw: Any, token: Any, amend: bool = False,
           source: str = "local", record_id: Optional[str] = None, do_notify: bool = True,
           no_wake_reason: str = "", author: str = "", guest: bool = False) -> Dict[str, Any]:
    """Store feedback for the current round (idempotent per round) and wake Hermes.

    A non-empty `no_wake_reason` stores the feedback as `stored` and never delivers it (a share-link guest);
    a wake-off board keeps its own message, which wins when both apply. `guest` marks the caller as a share-link
    guest: its submission is always `stored` and never delivered, and it can only replace its own record.
    Raises ReviewError (403 bad token, 409 already submitted / closed / old round).
    """
    is_guest = bool(guest)
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
        if is_guest:
            if prev and (not prev.get("guest") or prev.get("author") != author):
                raise AlreadySubmitted(
                    f"Feedback for round {current['n']} was already sent by someone else. "
                    "A share-link guest can only change its own.", 409)
            if prev and not amend:
                raise AlreadySubmitted(
                    f"Feedback for round {current['n']} was already sent ({_human(prev.get('submitted_at'))}). "
                    "Use “Amend feedback” to replace it.", 409)
        elif prev and prev.get("guest"):
            # A member or the owner sends over a guest record: the guest copy is kept in the review record (not shown
            # on the board), and this submission takes the canonical slot (seq continues so delivery ids stay unique).
            current.setdefault("guest_submissions", {})[prev.get("author") or "guest"] = {
                "feedback": current.get("feedback"), "submission": prev}
        elif current.get("feedback") and not amend:
            raise AlreadySubmitted(
                f"Feedback for round {current['n']} was already sent ({_human(prev.get('submitted_at'))}). "
                "Use “Amend feedback” to replace it.", 409)
        fb = feedback.sanitize_for_round(raw, current, data.get("settings"))
        seq = (prev or {}).get("seq", 0) + 1
        wake_off = data.get("wake") is False
        no_wake = wake_off or bool(no_wake_reason) or is_guest
        status = "stored" if no_wake else "pending_notify"
        current["feedback"] = fb
        current["submission"] = {
            "seq": seq,
            "author": author,
            "guest": is_guest,
            "by_owner": not no_wake,
            "source": source,
            "record_id": record_id,
            "submitted_at": store.now_iso(),
            "status": status,
            # Why the request gate saved it without a wake (the owner from Funnel, say): the board shows it.
            "stored_detail": no_wake_reason if no_wake_reason and not wake_off and not is_guest else None,
            "attempts": 0,
            "last_error": None,
            "next_attempt": 0,
            "notified_at": None,
            "delivery_id": f"mockreview-{review_id}-r{current['n']}-s{seq}",
            "notice": {
                "status": status,
                "attempts": 0,
                "last_error": None,
                "next_attempt": 0,
                "notified_at": None,
                "delivery_id": f"mockreview-{review_id}-r{current['n']}-s{seq}-notice",
            },
        }
        _write_feedback_files(data, current)
        store.save(review_id, data)
    result = {"ok": True, "review_id": review_id, "round": round_n, "amended": seq > 1,
              "status": "pending_notify", "notified": False}
    if wake_off:
        result.update(status="stored", detail="this board has wake off: feedback is stored, nobody is notified")
    elif is_guest:
        result.update(status="stored", detail=no_wake_reason or GUEST_NO_WAKE)
    elif no_wake_reason:
        result.update(status="stored", detail=no_wake_reason)
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
    if store.guest_submission(round_):
        raise ReviewError(GUEST_NEVER_DELIVERED, 409)
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
        "action": (round_.get("feedback") or {}).get("action", "send"),
    }


SEND_LEASE_SECONDS = 30.0
STALLED_SCAN_SECONDS = 30.0   # minimum interval between watchdog directory scans in the worker tick
PIN_TERMINAL = ("done", "failed", "timeout")
MENTION_EVENT = "review.thread_mention"
# The gateway rejected the payload itself, or the origin is a chat nothing can wake: the same wake fails again.
MENTION_PERMANENT = ("HTTP 400", "HTTP 413", "HTTP 422", notify.CANNOT_WAKE)


def _review_setting(data: Dict[str, Any], name: str) -> Any:
    """A pin_fix* setting as stored in this review: its own value wins over every later config edit, and only a review
    that lacks the key falls back to the current default (`settings.get`'s chain: env, config, default)."""
    return settings.get(name, (data.get("settings") or {}).get(name))


def _pin_html_refs(review_id: str, n: int) -> "tuple[Dict[str, Any], Dict[str, Any]]":
    """The round's stored decisions.json and {ref: block} for every block, including generated `sN` refs."""
    path = store.safe_variant_file(review_id, str(n), "a", "decisions.json")
    try:
        dec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ReviewError(f"round {n} decisions.json cannot be read: {e}", 409)
    sections = store.section_index(dec)
    return dec, {s["ref"]: dec["blocks"][i] for i, s in enumerate(sections) if i < len(dec["blocks"])}


def _pin_editable(block: Dict[str, Any]) -> bool:
    """True when patch_block can replace this block's HTML (the wording of the refusal names the same kinds)."""
    t = block.get("type")
    if t == "decision":
        return bool(block.get("options"))
    if t in ("mock", "piece"):
        return isinstance(block.get("html"), str)
    return t == "artifact" and bool(block.get("file"))


def request_pin_fix(review_id: str, round_n: Any, pin: Any, token: Any, idem: Any = None,
                    do_notify: bool = True, requester: Optional[str] = None,
                    no_wake_reason: str = "") -> Dict[str, Any]:
    """Store one reviewer pin fix for the newest round and, from the board server, wake Hermes once.

    A non-empty `no_wake_reason` stores the request marked `quiet` and never delivers it (a share-link guest);
    a wake-off board keeps its own message, which wins when both apply.
    Raises ReviewError (403 bad token, 409 closed/old round/pin_fix off/a block without HTML, 429 a cap).
    The request is deduped by a non-empty `idem` from the same `requester`; nothing here sends a chat ack.
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
        if not _review_setting(data, "pin_fix"):
            raise ReviewError("this review does not take pin fixes (pin_fix is off)", 409)
        if not isinstance(pin, dict):
            raise ReviewError("a pin fix needs a pin with a block and text", 409)
        ref = pin.get("block")
        text = feedback.clean_line(pin.get("text"), 2000)
        try:
            _dec, by_ref = _pin_html_refs(review_id, current["n"])
        except ReviewError:
            by_ref = {}
        block = by_ref.get(ref) if isinstance(ref, str) else None
        if ref in store.RESERVED_REFS or block is None or not _pin_editable(block):
            raise ReviewError(
                "a pin fix edits a block that has HTML (a mock, a decision option, a piece or an artifact); "
                f"this pin is on {ref}", 409)
        if not text:
            raise ReviewError("a pin fix needs text (what to change)", 409)
        option = None
        if block.get("type") == "decision":
            oids = [o.get("id") for o in (block.get("options") or [])]
            option = pin.get("option") if pin.get("option") in oids else None
        requests = current.get("pin_requests")
        if not isinstance(requests, dict):
            requests = current["pin_requests"] = {}
        idem_key = idem.strip() if isinstance(idem, str) else ""
        if idem_key:
            for q in requests.values():
                if q.get("idem") == idem_key and q.get("idem_owner") == requester:
                    return {"ok": True, "review_id": review_id, "round": current["n"], "request": q,
                            "status": q.get("status"), "notified": False, "duplicate": True,
                            "detail": "duplicate request: this pin fix is already stored"}
        per_round = int(_review_setting(data, "pin_fix_max_per_round"))
        if len(requests) >= per_round:
            raise ReviewError(f"this round takes at most {per_round} pin fix requests (pin_fix_max_per_round)", 429)
        now = time.time()
        per_hour = int(_review_setting(data, "pin_fix_max_per_hour"))
        recent = 0
        for round_ in data["rounds"]:
            for q in (round_.get("pin_requests") or {}).values():
                if not _counts_toward_wake_budget(q):
                    continue
                ts = _parse_iso(q.get("requested_at"))
                if ts is not None and now - ts < 3600.0:
                    recent += 1
        if recent >= per_hour:
            raise ReviewError(f"this review takes at most {per_hour} pin fix requests an hour (pin_fix_max_per_hour)",
                              429)
        used = set(requests)
        pin_id = pin.get("id") if isinstance(pin.get("id"), str) and store.PIN_ID_RE.fullmatch(pin["id"]) else None
        pid = pin_id
        if not pid or pid in used:
            pid = store.new_pin_id()
            while pid in used:
                pid = store.new_pin_id()
        req = {"id": pid, "block": ref, "option": option, "text": text,
               "target": targets.clean_target(pin.get("target")),
               "rev": int((current.get("blocks_rev") or {}).get(ref, 0) or 0),
               "status": "queued", "idem": idem_key, "idem_owner": requester,
               "by_owner": not (data.get("wake") is False or bool(no_wake_reason)),
               "requested_at": store.now_iso(), "wake_seq": 0, "done_at": None, "detail": ""}
        if pin_id:
            req["pin_id"] = pin_id
        try:
            x, y = feedback._num(pin.get("x")), feedback._num(pin.get("y"))
        except OverflowError:
            x = y = None
        if x is not None and y is not None:
            req["position"] = {"x": min(max(x, 0.0), 1.0), "y": min(max(y, 0.0), 1.0)}
        if data.get("wake") is False or no_wake_reason:
            req["quiet"] = True   # stored and visible, but the coalesce must never carry it into a wake
        requests[pid] = req
        store.save(review_id, data, pin_round=current["n"])
    notified, detail = False, ""
    if data.get("wake") is False:
        detail = "this board has wake off: the request is stored, nobody is notified"
    elif no_wake_reason:
        detail = no_wake_reason
    elif do_notify:
        notified, detail = flush_pin_fixes(review_id)
        try:
            stored = store.load(review_id)
            fresh = (stored["rounds"][-1].get("pin_requests") or {}).get(pid)
            if fresh:
                req = fresh
        except (ReviewError, ValueError, OSError):
            pass
    return {"ok": True, "review_id": review_id, "round": round_n, "request": req,
            "status": req.get("status"), "notified": notified, "detail": detail}


def _last_wake_at(data: Dict[str, Any], state: Dict[str, Any]) -> float:
    """The per-review wake clock, falling back to the round-local one for reviews written before it moved."""
    review_state = data.get("pin_fix")
    if isinstance(review_state, dict) and review_state.get("last_wake_at") is not None:
        return float(review_state.get("last_wake_at") or 0)
    return float(state.get("last_wake_at") or 0)


def _stamp_wake(data: Dict[str, Any], state: Dict[str, Any], now: float) -> None:
    """One wake attempt stamps the clock per review and per round: a new round must not reset the spacing."""
    state["last_wake_at"] = now
    review_state = data.get("pin_fix")
    if not isinstance(review_state, dict):
        review_state = data["pin_fix"] = {}
    review_state["last_wake_at"] = now


def flush_pin_fixes(review_id: str, now: Optional[float] = None) -> "tuple[bool, str]":
    """Coalesce queued pin fixes into one wake and deliver it; retry a pending one when due.

    Only the owning board server delivers (`owner.is_bound()` means a CLI/tool call: store, do not send). A burst
    inside `pin_fix_window_seconds` becomes one wake carrying every queued request id; `pin_fix_min_seconds` spaces
    two wakes apart. Returns (delivered, detail).
    """
    if owner.is_bound():
        return False, "not the owner service: only the board server delivers wake-ups"
    now = time.time() if now is None else now
    with store.locked(review_id):
        data = store.load(review_id)
        if data.get("status") != "open":
            return False, "review is closed"
        r = data["rounds"][-1]
        if data.get("wake") is False:
            return False, "this board has wake off"
        requests = r.get("pin_requests") or {}
        held_detail = ""
        for q in requests.values():
            if q.get("status") == "queued" and _counts_toward_wake_budget(q) and "by_owner" not in q:
                q.update(quiet=True, detail=LEGACY_QUEUED_NO_WAKE)
                held_detail = LEGACY_QUEUED_NO_WAKE
        state = r.get("pin_fix") or {}
        wake = state.get("wake") or {}
        if wake.get("status") == "pending_notify":
            ids = list(wake.get("requests") or [])
            allowed = [i for i in ids if _pin_wake_allowed(requests.get(i))]
            if allowed != ids:
                held = [i for i in ids if i not in allowed]
                for i in held:
                    q = requests.get(i)
                    if q:
                        q["detail"] = _pin_refusal(q) or LEGACY_QUEUED_NO_WAKE
                wake["requests"] = allowed
                if not allowed:
                    wake.update(status="failed", sending_until=0.0, last_error=LEGACY_QUEUED_NO_WAKE,
                                next_attempt=0.0)
                state["wake"] = wake
                r["pin_fix"] = state
                held_detail = LEGACY_QUEUED_NO_WAKE
        if held_detail:
            log.warning("mock-review: legacy pin fix queue for %s held: %s", review_id, held_detail)
            store.save(review_id, data, pin_round=r["n"])
        queued = sorted((q for q in requests.values()
                         if q.get("status") == "queued" and _counts_toward_wake_budget(q)),
                        key=lambda q: q.get("requested_at") or "")
        in_flight = float(wake.get("sending_until") or 0) > now
        min_seconds = float(_review_setting(data, "pin_fix_min_seconds"))
        window = float(_review_setting(data, "pin_fix_window_seconds"))
        newest = _parse_iso(max((q.get("requested_at") or "" for q in queued), default=""))
        ready = (bool(queued) and not in_flight and now - _last_wake_at(data, state) >= min_seconds
                 and newest is not None and now - newest >= window)
        pending_due = (wake.get("status") == "pending_notify"
                       and float(wake.get("next_attempt") or 0) <= now)
        if pending_due and not in_flight:
            ids = list(wake.get("requests") or [])
            wake["sending_until"] = now + SEND_LEASE_SECONDS
            _stamp_wake(data, state, now)
            r["pin_fix"] = state
            store.save(review_id, data)
        elif ready and wake.get("status") != "pending_notify":
            seq = int(state.get("seq") or 0) + 1
            ids = [q["id"] for q in queued]
            for q in queued:
                q["wake_seq"] = seq
            wake = {"status": "pending_notify", "requests": ids,
                    "delivery_id": f"mockreview-{review_id}-r{r['n']}-p{seq}", "attempts": 0,
                    "next_attempt": 0.0, "sending_until": now + SEND_LEASE_SECONDS,
                    "last_error": None, "notified_at": None}
            state.update(seq=seq, wake=wake)
            _stamp_wake(data, state, now)
            r["pin_fix"] = state
            store.save(review_id, data)
        elif pending_due and in_flight:
            return False, "a send for this pin fix wake is already in flight"
        else:
            return False, held_detail or "nothing to wake"
        delivery_id = wake["delivery_id"]
        ids = list(wake.get("requests") or [])
        try:
            payload = {"event_type": "review.pin_fix", "review_id": review_id, "round": r["n"],
                       **store.normalize_origin(data.get("origin")), "title": data["title"],
                       "review_url": store.review_url(data), "mode": data["mode"], "action": "pin_fix",
                       "requests": ids,
                       "summary": (f"{len(ids)} pin fix request(s) on {data['title']} round {r['n']} — "
                                   f"{', '.join((requests.get(i) or {}).get('block') or '?' for i in ids[:2])}: "
                                   f"{feedback.clean_line((requests.get(ids[0]) or {}).get('text'), 160)}")}
        except ReviewError as e:
            for i in ids:
                q = requests.get(i)
                if q and q.get("status") not in PIN_TERMINAL:
                    q.update(status="failed", detail=e.message)
            wake.update(status="failed", sending_until=0.0, last_error=e.message, next_attempt=0.0)
            _stamp_wake(data, state, now)
            r["pin_fix"] = state
            store.save(review_id, data, pin_round=r["n"])
            return False, e.message
        held = wakeguard.review_exposure(data)
        if held:   # create/add-round refuse this; the setting or host may have changed since
            detail = f"held, not woken: {held}"
            for i in ids:
                q = requests.get(i)
                if q and q.get("status") not in PIN_TERMINAL:
                    q.update(status="failed", detail=detail)
            wake.update(status="failed", sending_until=0.0, last_error=detail, next_attempt=0.0)
            _stamp_wake(data, state, now)
            r["pin_fix"] = state
            store.save(review_id, data, pin_round=r["n"])
            log.warning("mock-review: pin fix wake for %s round %s held: %s", review_id, r["n"], held)
            return False, detail
    try:
        ok, msg = notify.send(payload, delivery_id)
    except Exception as e:
        log.exception("pin fix wake send raised for %s", review_id)
        ok, msg = False, f"pin fix send raised: {e}"
    with store.locked(review_id):
        data = store.load(review_id)
        r = data["rounds"][-1]
        requests = r.get("pin_requests") or {}
        state = r.get("pin_fix") or {}
        wake = state.get("wake") or {}
        if wake.get("delivery_id") != delivery_id or wake.get("status") != "pending_notify":
            return ok, msg    # superseded while we were sending (a newer round or a newer wake owns the outcome)
        wake["sending_until"] = 0.0
        wake["attempts"] = int(wake.get("attempts") or 0) + 1
        changed = False
        if ok:
            for i in ids:
                q = requests.get(i)
                if q and q.get("status") == "queued":
                    q.update(status="woken", detail="")
                    changed = True
            wake.update(status="notified", notified_at=store.now_iso(), last_error=None, next_attempt=0.0)
            log.info("pin fix wake delivered for %s round %s (%s)", review_id, r["n"], msg)
        elif msg.startswith(MENTION_PERMANENT):
            for i in ids:
                q = requests.get(i)
                if q and q.get("status") == "queued":
                    q.update(status="failed", detail=msg)
                    changed = True
            wake.update(status="failed", last_error=msg, next_attempt=0.0)
        else:
            wake.update(status="pending_notify", last_error=msg, next_attempt=now + config.retry_seconds())
            log.error("pin fix wake failed for %s round %s, will retry: %s", review_id, r["n"], msg)
        _stamp_wake(data, state, now)
        r["pin_fix"] = state
        store.save(review_id, data, pin_round=r["n"] if changed else None)
    return ok, msg


def pin_fix_status(data: Dict[str, Any], request: Dict[str, Any]) -> Dict[str, Any]:
    """The single status seam the thread chip consumes: request status -> chip state + its detail.

    Pure: no IO, no locks. `data` is accepted so the caller can pass the review it already holds.
    """
    state = {"queued": "queued", "woken": "working", "done": "done",
             "failed": "failed", "timeout": "failed"}.get((request or {}).get("status"), "queued")
    return {"state": state, "detail": (request or {}).get("detail") or ""}


def pin_timeout_minutes() -> float:
    # Turning off the chat's stalled pings still leaves pin requests with a finite lifetime.
    return settings.get("stale_after_minutes") or 15.0


def _mention_attempt_at(wake: Dict[str, Any]) -> Optional[float]:
    first = wake.get("first_attempt_at")
    if isinstance(first, (int, float)):
        return first
    # Older records only have the attempt count, outcome and send lease. Keep their previous clock if the
    # first attempt is unknown, using the lease to recover an attempt that was interrupted before recording an outcome.
    if wake.get("attempts"):
        return _parse_iso(wake.get("at"))
    lease = wake.get("sending_until")
    return lease - SEND_LEASE_SECONDS if isinstance(lease, (int, float)) and lease > 0 else None


def pin_request_deadline(data: Dict[str, Any], request: Dict[str, Any], minutes: float) -> Optional[float]:
    if request.get("source") == "mention":
        wake = (data.get("thread_wakes") or {}).get(request.get("comment_id")) or {}
        started = _mention_attempt_at(wake)
        if started is None and request.get("status") == "woken":
            started = _parse_iso(request.get("requested_at"))
    else:
        started = _parse_iso(request.get("requested_at"))
    return None if started is None else started + minutes * 60.0


def _mention_wakes(data: Dict[str, Any]) -> Dict[str, Any]:
    wakes = data.get("thread_wakes")
    if not isinstance(wakes, dict):
        wakes = data["thread_wakes"] = {}
    return wakes


def _legacy_hold() -> str:
    """Why a record without `by_owner` must not be delivered now.

    A record queued before this update has no `by_owner` marker, so it has no proof it came from the owner, Funnel or
    not: it stays saved until the owner sends it again from private ingress.
    """
    return LEGACY_QUEUED_NO_WAKE


def _pin_refusal(request: Dict[str, Any]) -> str:
    """Why this pin fix may not ride a wake, or "" when it may: only a request that carries `by_owner` may."""
    if not request:
        return LEGACY_QUEUED_NO_WAKE
    if "by_owner" in request:
        return "" if request.get("by_owner") is True else LEGACY_QUEUED_NO_WAKE
    return _legacy_hold()


def _pin_wake_allowed(request: Dict[str, Any]) -> bool:
    """Whether a pin fix may ride a wake: only a request that carries the owner's `by_owner` marker."""
    return not _pin_refusal(request)


def _counts_toward_wake_budget(request: Dict[str, Any]) -> bool:
    """One rule for both caps. A quiet request never wakes. A mention's wake is already counted by its `thread_wakes`
    record, so the chip request that goes with it is bookkeeping and must not spend the budget a second time."""
    return not request.get("quiet") and request.get("source") != "mention"


def _wake_budget_used(data: Dict[str, Any], round_n: int, now: float) -> "tuple[int, int]":
    """(last hour, this round) asks for a wake already taken: mentions and pin fixes share one budget.

    A capped mention is recorded so the same comment never wakes later, but it asked for nothing: it does not spend
    the budget it was refused by. A quiet pin fix (a guest's) and a mention's chip request spend nothing either.
    """
    hour = on_round = 0
    for rec in (data.get("thread_wakes") or {}).values():
        if not isinstance(rec, dict) or rec.get("status") == "capped":
            continue
        ts = _parse_iso(rec.get("at"))
        if ts is not None and now - ts < 3600.0:
            hour += 1
        if int(rec.get("round") or 0) == round_n:
            on_round += 1
    for r in data.get("rounds") or []:
        for q in (r.get("pin_requests") or {}).values():
            if not _counts_toward_wake_budget(q):
                continue
            ts = _parse_iso(q.get("requested_at"))
            if ts is not None and now - ts < 3600.0:
                hour += 1
            if int(r.get("n") or 0) == round_n:
                on_round += 1
    return hour, on_round


MENTION_TERMINAL = ("notified", "failed", "capped")
MENTION_WAKE_HOUR_SECONDS = 3600.0


def _mark_wake_seen(data: Dict[str, Any], comment_id: str) -> None:
    """Remember every comment that ever had a wake record, so pruning can never make an idem replay wake again.

    `_bounded_thread_wakes` drops an id only once its comment row no longer exists, so the map stays bounded by the
    review's surviving comment rows (deleting a thread removes them).
    """
    seen = data.get("thread_wake_seen")
    if not isinstance(seen, dict):
        seen = data["thread_wake_seen"] = {}
    if comment_id not in seen:
        seen[comment_id] = {"at": store.now_iso()}


def _prune_wake_seen(data: Dict[str, Any]) -> bool:
    """Drop `thread_wake_seen` ids whose comment row is gone. Returns True when the map changed.

    Only a deleted comment can free an id: while its row exists, a dropped id would let an idem replay wake again. A
    read that fails (a fake or incomplete data file, a missing database) prunes nothing and is retried next tick.
    """
    seen = data.get("thread_wake_seen")
    if not isinstance(seen, dict) or not seen:
        return False
    try:
        thread_ids = [r["id"] for r in threads._threads_rows(str(data["id"]))]
        alive = {c["id"] for group in threads._comments_for(thread_ids).values() for c in group}
    except (OSError, sqlite3.Error, ReviewError, KeyError, TypeError):
        return False
    gone = [comment_id for comment_id in seen if comment_id not in alive]
    for comment_id in gone:
        seen.pop(comment_id, None)
    return bool(gone)


def _wake_prunable(rec: Dict[str, Any], newest_round: int, now: float) -> bool:
    """A terminal record may go only once it cannot count: over an hour old and not on the newest round.

    Every record's `round` is the newest round at queue time (see `wake_thread_mention`), so a record over an hour old
    that is off the newest round can never be counted again: the hour cap sees only records younger than an hour and
    the round cap only records on the newest round. A record without a round (a capped one written before this rule)
    cannot be proven off the newest round, so it stays.
    """
    if rec.get("status") not in MENTION_TERMINAL:
        return False
    ts = _parse_iso(rec.get("at"))
    if ts is None or now - ts <= MENTION_WAKE_HOUR_SECONDS:
        return False
    rec_round = rec.get("round")
    if rec_round is None:
        return False
    return int(rec_round or 0) != newest_round


def _bounded_thread_wakes(data: Dict[str, Any], now: Optional[float] = None) -> bool:
    """Keep the mention-wake state bounded: a terminal record loses its text and anchor (only a pending retry needs
    them), a terminal record is pruned only when `_wake_prunable` says it cannot count, and a `thread_wake_seen` id is
    dropped once its comment row is gone. Returns True when the stored file changed."""
    wakes = data.get("thread_wakes")
    rounds = data.get("rounds") or []
    newest_round = int(rounds[-1].get("n") or 0) if rounds else 0
    now = time.time() if now is None else now
    changed = _prune_wake_seen(data)
    if not isinstance(wakes, dict):
        return changed
    for comment_id, rec in list(wakes.items()):
        if not isinstance(rec, dict) or rec.get("status") not in MENTION_TERMINAL:
            continue
        for key in ("text", "anchor"):
            if key in rec:
                rec.pop(key, None)
                changed = True
        if _wake_prunable(rec, newest_round, now):
            wakes.pop(comment_id, None)
            changed = True
    return changed


def _prune_thread_wakes(review_id: str, now: Optional[float] = None) -> bool:
    """Run the bound on the worker tick even when nothing is due, so old terminal records do not pile up."""
    with store.locked(review_id):
        data = store.load(review_id)
        if not _bounded_thread_wakes(data, now):
            return False
        store.save(review_id, data)
        return True


def _mention_request(data: Dict[str, Any], review_id: str, wake: Dict[str, Any]) -> Optional[str]:
    """Open one activity request per accepted mention on an editable block of the newest round."""
    rounds = data.get("rounds") or []
    if not rounds:
        return None
    current = rounds[-1]
    if int(wake.get("round") or 0) != int(current.get("n") or 0):
        return None
    block = str(wake.get("block") or "")
    if not block or block in store.RESERVED_REFS:
        return None
    try:
        _dec, by_ref = _pin_html_refs(review_id, int(current["n"]))
    except (ReviewError, OSError, ValueError, KeyError, TypeError):
        return None
    target = by_ref.get(block)
    if not isinstance(target, dict) or not _pin_editable(target):
        return None
    requests = current.get("pin_requests")
    if not isinstance(requests, dict):
        requests = current["pin_requests"] = {}
    idem = f"mention:{wake.get('comment_id')}"
    for q in requests.values():
        if q.get("idem") == idem and q.get("idem_owner") == "mention":
            return q.get("id")
    anchor = wake.get("anchor") if isinstance(wake.get("anchor"), dict) else {}
    option = anchor.get("option") if target.get("type") == "decision" else None
    if target.get("type") == "decision":
        oids = [o.get("id") for o in (target.get("options") or [])]
        option = option if option in oids else None
    pid = store.new_pin_id()
    while pid in requests:
        pid = store.new_pin_id()
    requests[pid] = {
        "id": pid, "block": block, "option": option,
        "rev": int((current.get("blocks_rev") or {}).get(block, 0) or 0),
        "text": feedback.clean_line(wake.get("text"), 2000),
        "status": "queued", "requested_at": wake.get("at") or store.now_iso(), "wake_seq": 0,
        "done_at": None, "detail": "",
        "source": "mention", "thread_id": wake.get("thread_id"), "comment_id": wake.get("comment_id"),
        "idem": idem, "idem_owner": "mention",
    }
    return pid


def _set_mention_request(data: Dict[str, Any], request_id: Optional[str], status: str, detail: str = "") -> bool:
    """Mirror a wake outcome onto its pin request and report whether the status changed."""
    if not request_id:
        return False
    for r in data.get("rounds") or []:
        q = (r.get("pin_requests") or {}).get(request_id)
        if not isinstance(q, dict):
            continue
        if q.get("status") in PIN_TERMINAL:
            return False
        q.update(status=status, detail=detail)
        if status == "done":
            q["done_at"] = store.now_iso()
        return True
    return False


def close_mention_requests(review_id: str, thread_id: Optional[str],
                           request_id: Optional[str] = None) -> int:
    """Close the mention chip requests a reply or a resolve answered.

    Thread matches in the newest round close once delivered. An explicit `request_id` can also close a queued request
    or a request from another thread. Returns how many closed.
    """
    with store.locked(review_id):
        data = store.load(review_id)
        if data.get("status") != "open":
            return 0
        current = data["rounds"][-1]
        now = store.now_iso()
        closed = 0
        for q in (current.get("pin_requests") or {}).values():
            if not isinstance(q, dict) or q.get("source") != "mention" or q.get("status") in PIN_TERMINAL:
                continue
            exact = request_id and q.get("id") == request_id
            thread_match = thread_id and q.get("thread_id") == thread_id and q.get("status") != "queued"
            if exact or thread_match:
                q.update(status="done", done_at=now, detail="answered in the thread")
                closed += 1
        if closed:
            store.save(review_id, data, pin_round=current["n"])
        return closed


def _mention_payload(data: Dict[str, Any], wakes: "list[Dict[str, Any]]") -> Dict[str, Any]:
    """One wake for every due mention: the first entry stays top-level for older receivers, `mentions` carries all."""
    first = wakes[0]
    block = first.get("block") or "the board"
    summary = (f"{first.get('author_name') or 'Someone'} mentioned @Hermes in thread "
               f"{first.get('thread_id')} on {block}: {feedback.clean_line(first.get('text'), 160)}")
    return {
        "event_type": MENTION_EVENT,
        "review_id": data["id"],
        "round": first["round"],
        **store.normalize_origin(data.get("origin")),
        "title": data["title"],
        "review_url": store.review_url(data),
        "mode": data["mode"],
        "review_thread_id": first["thread_id"],
        "comment_id": first["comment_id"],
        "anchor": first.get("anchor") or {},
        "block": first.get("block") or "",
        "text": first.get("text") or "",
        "request_id": first.get("request_id"),
        "summary": summary,
        "mentions": [{"thread_id": w.get("thread_id"), "comment_id": w.get("comment_id"),
                      "text": w.get("text") or "", "block": w.get("block") or "",
                      "request_id": w.get("request_id")} for w in wakes],
    }


def _capped_wake(review_id: str, data: Dict[str, Any], wakes: Dict[str, Any], comment_id: str,
                 round_n: int, detail: str) -> Dict[str, Any]:
    """Record a mention the cap refused: the comment stays, and no later retry of it can wake Hermes."""
    wakes[comment_id] = {"status": "capped", "detail": detail, "round": round_n, "at": store.now_iso()}
    _mark_wake_seen(data, comment_id)
    _bounded_thread_wakes(data)
    store.save(review_id, data)
    return {"status": "capped", "notified": False, "detail": detail}


def _mention_by_owner(rec: Dict[str, Any]) -> bool:
    """Whether the owner queued this mention wake: only a record that carries `by_owner` does. A record queued before
    the marker existed has no proof it came from the owner, so it fails closed."""
    return rec.get("by_owner") is True


def wake_thread_mention(review_id: str, thread: Dict[str, Any], comment_id: str,
                        by_owner: bool = False) -> Dict[str, Any]:
    """Queue one activating `@Hermes` comment for the worker tick.

    Called by the board server right after it stored the comment; an idem replay names the same comment id and is a
    duplicate, never a second wake. The POST answer is not blocked by the gateway call: the 5 s tick coalesces and
    delivers what is due (`retry_thread_wakes`). The pin_fix_max_per_hour / pin_fix_max_per_round budget is shared
    with pin fixes, because both wake the same agent, and it is counted against the newest round at queue time even
    when the thread was opened in an older round: the record's `round` is always that newest round. Only the owning
    board server delivers: a CLI or MCP call bound to its store stores the comment (a no-op here) and sends nothing.
    The board server calls this only for a comment the request gate let through (wakeguard.request_gate): a token
    reply, a wake-off board, a commenter and a reviewer the gate held never reach here. `by_owner` is recorded, and
    `_deliver_thread_wakes` checks a non-owner record against the gate again when it sends. The checks below are the
    second half of that guard.
    """
    if owner.is_bound():
        return {"status": "deferred", "notified": False,
                "detail": "not the owner service: only the board server delivers wake-ups"}
    comment = next((c for c in thread.get("comments") or [] if c.get("id") == comment_id), None)
    if comment is None or not comment.get("sent_to_hermes"):
        return {"status": "ignored", "notified": False, "detail": "this comment does not mention Hermes"}
    author = str(comment.get("author") or "")
    if not author.startswith(("user:", "guest:")):
        return {"status": "ignored", "notified": False, "detail": "only a person on the board wakes Hermes"}
    if author.startswith("guest:") and by_owner:
        return {"status": "ignored", "notified": False, "detail": "a share-link guest is never the owner"}
    anchor = thread.get("anchor") if isinstance(thread.get("anchor"), dict) else {}
    block = str(anchor.get("block") or anchor.get("option") or "")
    now = time.time()
    with store.locked(review_id):
        data = store.load(review_id)
        if data.get("status") != "open":
            return {"status": "closed", "notified": False, "detail": "review is closed"}
        if data.get("wake") is False:
            return {"status": "off", "notified": False,
                    "detail": "this board has wake off: the comment is stored, nobody is notified"}
        wakes = _mention_wakes(data)
        seen = wakes.get(comment_id)
        if isinstance(seen, dict):
            return {"status": seen.get("status"), "notified": seen.get("status") == "notified",
                    "duplicate": True, "delivery_id": seen.get("delivery_id"),
                    "detail": "duplicate request: this comment is already recorded"}
        seen_map = data.get("thread_wake_seen")
        if isinstance(seen_map, dict) and comment_id in seen_map:
            # The wake record itself may have been pruned; the seen map keeps the id while its comment row exists, so
            # an idem replay stays a duplicate and never wakes Hermes twice.
            return {"status": "duplicate", "notified": False, "duplicate": True,
                    "detail": "duplicate request: this comment is already recorded"}
        round_n = int(data["rounds"][-1]["n"])
        hour, on_round = _wake_budget_used(data, round_n, now)
        per_round = int(_review_setting(data, "pin_fix_max_per_round"))
        if on_round >= per_round:
            return _capped_wake(review_id, data, wakes, comment_id, round_n,
                                f"this round takes at most {per_round} wake requests (pin_fix_max_per_round)")
        per_hour = int(_review_setting(data, "pin_fix_max_per_hour"))
        if hour >= per_hour:
            return _capped_wake(review_id, data, wakes, comment_id, round_n,
                                f"this review takes at most {per_hour} wake requests an hour (pin_fix_max_per_hour)")
        wake = {"thread_id": thread["id"], "comment_id": comment_id, "round": round_n,
                "text": comment.get("text") or "", "anchor": anchor, "block": block,
                "author_name": comment.get("author_name") or "", "at": store.now_iso(),
                "status": "pending_notify", "attempts": 0, "next_attempt": 0.0, "sending_until": 0.0,
                "last_error": None, "request_id": None, "by_owner": bool(by_owner), "author": author,
                "delivery_id": f"mockreview-{review_id}-m-{comment_id}"}
        wakes[comment_id] = wake
        wake["request_id"] = _mention_request(data, review_id, wake)
        _mark_wake_seen(data, comment_id)
        store.save(review_id, data, pin_round=round_n if wake["request_id"] else None)
    return {"status": "pending_notify", "notified": False,
            "detail": "queued: the worker tick delivers it", "delivery_id": wake["delivery_id"]}


def _deliver_thread_wakes(review_id: str, comment_ids: "list[str]",
                          now: Optional[float] = None) -> "tuple[bool, str]":
    """POST one mention wake covering every pending comment in `comment_ids` and record the outcome.

    A wake queued by an older version may still need its pin request opened here. One send means one delivery id
    and one stamp.
    A permanent failure, a held board or a closed review fails the covered records and their requests; a transient
    failure leaves them pending for the retry. A record the owner did not queue is failed with
    REVIEWER_WAKE_UNCONFIRMED, and not sent, when its author's link or grant no longer carries the owner's Reviewer
    confirmation (the owner may have changed the role after the mention was queued), or with REVIEWER_WAKE_ENDED when
    its share link stopped, was regenerated or expired since. It is failed with TOOLS_NO_WAKE, and not sent, when the
    board now wakes a Desktop chat or wake_toolsets is set (the request gate's reviewer rule, checked again because the
    settings may have changed since the mention was queued). A record queued before this update carries no `by_owner`
    marker, so it has no proof it came from the owner, Funnel or not: it is failed with LEGACY_QUEUED_NO_WAKE and not
    sent.
    """
    if owner.is_bound():
        return False, "not the owner service: only the board server delivers wake-ups"
    with store.locked(review_id):
        data = store.load(review_id)
        wakes = data.get("thread_wakes") or {}
        picked = [wakes.get(cid) for cid in comment_ids]
        picked = [w for w in picked if isinstance(w, dict) and w.get("status") == "pending_notify"]
        if not picked:
            return False, "no such mention wake"
        if data.get("status") != "open":
            for w in picked:
                w.update(status="failed", sending_until=0.0, last_error="review is closed", next_attempt=0.0)
                _set_mention_request(data, w.get("request_id"), "failed", "review is closed")
            _bounded_thread_wakes(data)
            store.save(review_id, data)
            return False, "review is closed"
        if data.get("wake") is False:
            for w in picked:
                w.update(status="failed", sending_until=0.0, last_error="this board has wake off", next_attempt=0.0)
                _set_mention_request(data, w.get("request_id"), "failed", "this board has wake off")
            _bounded_thread_wakes(data)
            store.save(review_id, data)
            return False, "this board has wake off"
        checked_at = time.time() if now is None else now
        if any(float(w.get("sending_until") or 0) > checked_at for w in picked):
            return False, "a send for this mention wake is already in flight"
        pin_changed = False
        newest = data["rounds"][-1]
        round_n = newest["n"]
        legacy_refused = [w for w in picked if "by_owner" not in w]
        for w in legacy_refused:
            w.update(status="failed", sending_until=0.0, last_error=LEGACY_QUEUED_NO_WAKE, next_attempt=0.0)
            pin_changed = (_set_mention_request(data, w.get("request_id"), "failed", LEGACY_QUEUED_NO_WAKE)
                           or pin_changed)
        if legacy_refused:
            log.warning("mock-review: legacy mention wake for %s comments %s not sent: %s", review_id,
                        ", ".join(str(w.get("comment_id")) for w in legacy_refused), LEGACY_QUEUED_NO_WAKE)
        held_legacy = {id(w) for w in legacy_refused}
        picked = [w for w in picked if id(w) not in held_legacy]
        if not picked:
            _bounded_thread_wakes(data)
            store.save(review_id, data, pin_round=round_n if pin_changed else None)
            return False, LEGACY_QUEUED_NO_WAKE
        unconfirmed = []
        for w in picked:
            if _mention_by_owner(w):
                continue
            reason = users.wake_reason(review_id, str(w.get("author") or ""))
            if reason:
                unconfirmed.append((w, reason))
        for w, reason in unconfirmed:
            w.update(status="failed", sending_until=0.0, last_error=reason, next_attempt=0.0)
            pin_changed = _set_mention_request(data, w.get("request_id"), "failed", reason) or pin_changed
        if unconfirmed:
            log.warning("mock-review: mention wake for %s comments %s not sent: %s", review_id,
                        ", ".join(str(w.get("comment_id")) for w, _ in unconfirmed),
                        "; ".join(sorted({reason for _, reason in unconfirmed})))
        refused_ids = {id(w) for w, _ in unconfirmed}
        picked = [w for w in picked if id(w) not in refused_ids]
        if not picked:
            _bounded_thread_wakes(data)
            store.save(review_id, data, pin_round=round_n if pin_changed else None)
            return False, unconfirmed[0][1]
        for w in picked:
            if not w.get("request_id"):
                w["request_id"] = _mention_request(data, review_id, w)
                pin_changed = pin_changed or bool(w["request_id"])
        reviewer_held = wakeguard.reviewer_mention_reason(data.get("origin"))
        if reviewer_held:
            refused = [w for w in picked if not _mention_by_owner(w)]
            for w in refused:
                w.update(status="failed", sending_until=0.0, last_error=reviewer_held, next_attempt=0.0)
                pin_changed = _set_mention_request(data, w.get("request_id"), "failed", reviewer_held) or pin_changed
            if refused:
                log.warning("mock-review: mention wake for %s comments %s not sent: %s", review_id,
                            ", ".join(str(w.get("comment_id")) for w in refused), reviewer_held)
            picked = [w for w in picked if w not in refused]
            if not picked:
                _bounded_thread_wakes(data)
                store.save(review_id, data, pin_round=round_n if pin_changed else None)
                return False, reviewer_held
        held = wakeguard.review_exposure(data)
        if held:   # create/add-round refuse this; the setting or host may have changed since
            detail = f"held, not woken: {held}"
            for w in picked:
                w.update(status="failed", sending_until=0.0, last_error=detail, next_attempt=0.0)
                pin_changed = _set_mention_request(data, w.get("request_id"), "failed", detail) or pin_changed
            _bounded_thread_wakes(data)
            store.save(review_id, data, pin_round=round_n if pin_changed else None)
            log.warning("mock-review: mention wake for %s comments %s held: %s",
                        review_id, ", ".join(comment_ids), held)
            return False, detail
        try:
            payload = _mention_payload(data, picked)
        except ReviewError as e:
            for w in picked:
                w.update(status="failed", sending_until=0.0, last_error=e.message, next_attempt=0.0)
                pin_changed = _set_mention_request(data, w.get("request_id"), "failed", e.message) or pin_changed
            _bounded_thread_wakes(data)
            store.save(review_id, data, pin_round=round_n if pin_changed else None)
            return False, e.message
        delivery_id = f"mockreview-{review_id}-m-{picked[0]['comment_id']}"
        requests = newest.get("pin_requests") or {}
        now = time.time() if now is None else now
        for w in picked:
            if "first_attempt_at" not in w:
                previous = _mention_attempt_at(w)
                w["first_attempt_at"] = now if previous is None else previous
                q = requests.get(w.get("request_id")) or {}
                pin_changed = pin_changed or (previous is None and q.get("status") == "queued")
            w["sending_until"] = now + SEND_LEASE_SECONDS
        state = newest.get("pin_fix")
        if not isinstance(state, dict):
            state = newest["pin_fix"] = {}
        _stamp_wake(data, state, now)
        store.save(review_id, data, pin_round=round_n if pin_changed else None)
    try:
        ok, msg = notify.send(payload, delivery_id)
    except Exception as e:
        log.exception("mention wake send raised for %s", review_id)
        ok, msg = False, f"mention wake send raised: {e}"
    with store.locked(review_id):
        data = store.load(review_id)
        wakes = data.get("thread_wakes") or {}
        current = [wakes.get(cid) for cid in comment_ids]
        current = [w for w in current if isinstance(w, dict) and w.get("status") == "pending_notify"]
        if not current:
            return ok, msg    # superseded while we were sending
        for w in current:
            w["sending_until"] = 0.0
            w["attempts"] = int(w.get("attempts") or 0) + 1
            w["delivery_id"] = delivery_id
        changed = False
        if ok:
            for w in current:
                w.update(status="notified", notified_at=store.now_iso(), last_error=None, next_attempt=0.0)
                changed = _set_mention_request(data, w.get("request_id"), "woken") or changed
            log.info("mention wake delivered for %s comments %s (%s)",
                     review_id, ", ".join(comment_ids), msg)
        elif msg.startswith(MENTION_PERMANENT):
            for w in current:
                w.update(status="failed", last_error=msg, next_attempt=0.0)
                changed = _set_mention_request(data, w.get("request_id"), "failed", msg) or changed
        else:
            for w in current:
                w.update(status="pending_notify", last_error=msg, next_attempt=now + config.retry_seconds())
            log.error("mention wake failed for %s comments %s, will retry: %s",
                      review_id, ", ".join(comment_ids), msg)
        _bounded_thread_wakes(data)
        store.save(review_id, data, pin_round=round_n if changed else None)
    return ok, msg


def _due_mention_wakes(data: Dict[str, Any], now: float) -> "list[str]":
    return [cid for cid, wake in (data.get("thread_wakes") or {}).items()
            if isinstance(wake, dict) and wake.get("status") == "pending_notify"
            and float(wake.get("next_attempt") or 0) <= now]


def retry_thread_wakes(review_id: str, now: Optional[float] = None) -> int:
    """Deliver the due mention wakes for one review, one coalesced wake per tick, spaced by `pin_fix_min_seconds`.

    Inside the spacing window each due record is pushed to `last + min_seconds` and nothing is sent (the gate). A
    burst of due mentions becomes ONE wake carrying every entry. Returns 1 when a wake was delivered.
    """
    if owner.is_bound():
        return 0
    due_at = time.time() if now is None else now
    try:
        data = store.load(review_id)
    except (ReviewError, ValueError, OSError):
        return 0
    if data.get("status") != "open" or data.get("wake") is False:
        return 0
    try:
        _prune_thread_wakes(review_id, due_at)
    except (ReviewError, ValueError, OSError):
        log.warning("mention wake prune failed for %s", review_id)
    due = _due_mention_wakes(data, due_at)
    if not due:
        return 0
    state = data["rounds"][-1].get("pin_fix")
    state = state if isinstance(state, dict) else {}
    last = _last_wake_at(data, state)
    min_seconds = float(_review_setting(data, "pin_fix_min_seconds"))
    if due_at - last < min_seconds:
        with store.locked(review_id):
            data = store.load(review_id)
            wakes = data.get("thread_wakes") or {}
            changed = False
            for comment_id in due:
                wake = wakes.get(comment_id)
                if isinstance(wake, dict) and wake.get("status") == "pending_notify":
                    wake["next_attempt"] = last + min_seconds
                    changed = True
            if changed:
                store.save(review_id, data)
        return 0
    ok, _detail = _deliver_thread_wakes(review_id, due, now=now)
    return 1 if ok else 0


def _timeout_pin_requests(review_id: str, now: float, minutes: float) -> int:
    """Expire the newest round's activity and stop retries for expired asks."""
    with store.locked(review_id):
        data = store.load(review_id)
        if data.get("status") != "open":
            return 0
        r = data["rounds"][-1]
        expired = set()
        for pid, q in (r.get("pin_requests") or {}).items():
            if q.get("status") in PIN_TERMINAL:
                continue
            if q.get("quiet"):   # never sent (a guest's pin fix): it waits for the owner to wake Hermes
                continue
            deadline = pin_request_deadline(data, q, minutes)
            if deadline is not None and now >= deadline:
                q.update(status="timeout", detail=f"no patch within {minutes:g} min")
                expired.add(q.get("id") or pid)
        if expired:
            for w in (data.get("thread_wakes") or {}).values():
                if w.get("status") == "pending_notify" and w.get("request_id") in expired:
                    w.update(status="failed", last_error="pin request timed out", next_attempt=0.0, sending_until=0.0)
            wake = (r.get("pin_fix") or {}).get("wake") or {}
            requests = r["pin_requests"]
            if wake.get("status") == "pending_notify" and not any(
                    (requests.get(pid) or {}).get("status") == "queued" for pid in (wake.get("requests") or [])):
                wake.update(status="failed", last_error="pin request timed out", next_attempt=0.0, sending_until=0.0)
            _bounded_thread_wakes(data, now)
            store.save(review_id, data, pin_round=r["n"])
        return len(expired)


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
        if store.guest_submission(r):
            return False, GUEST_NEVER_DELIVERED
        if sub.get("status") == "notified":
            return True, "already notified"
        if sub.get("status") != "pending_notify":   # stored by the request gate (or wake off): never woken
            return False, sub.get("stored_detail") or "nothing pending to send"
        legacy_reason = _legacy_hold() if "by_owner" not in sub else ""
        if legacy_reason:
            sub.update(status="stored", stored_detail=legacy_reason, next_attempt=0)
            notice = sub.get("notice")
            if isinstance(notice, dict):
                notice.update(status="stored", next_attempt=0)
            store.save(review_id, data)
            log.warning("mock-review: legacy wake for %s round %s not sent: %s", review_id, round_n, legacy_reason)
            return False, legacy_reason
        held = wakeguard.review_exposure(data)
        if held:   # create/add-round refuse this; the setting or host may have changed since
            sub.update(last_error=f"held, not woken: {held}", next_attempt=time.time() + config.retry_seconds())
            store.save(review_id, data)
            log.warning("mock-review: wake for %s round %s held: %s", review_id, round_n, held)
            return False, f"held, not woken: {held}"
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


TERMINAL_NOTICE = ("notified", "dropped", "no_route", "skipped")


def _no_notice_route(origin: Any) -> Optional[str]:
    """Why no notice can reach this origin's chat, or None when its notice route can. A Desktop chat has no route
    (sessionwake.NO_NOTICE: the wake turn itself is what it shows); a chat nothing can wake has none either."""
    if not isinstance(origin, dict) or not origin.get("platform"):
        return None   # a legacy origin: _notice_payload drops it with its own reason
    kind, why = platforms.classify(origin["platform"])
    if kind == platforms.SESSION:
        return sessionwake.NO_NOTICE
    if kind == platforms.UNSUPPORTED:
        return platforms.unsupported_message(origin["platform"], why)
    return None


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
    was not delivered; a retry after no answer keeps the id. Terminal states: notified; dropped (moot: newer round or closed review; or
    permanent: the payload cannot be built, or the gateway rejected it with 400/413/422); no_route (the gateway has no
    such route, 404: re-run `hermes mockreview setup`); skipped (the stalled ping: nothing to build, or the
    session acknowledged the submission; either notice: the origin chat has no notice route, a Desktop chat). The
    ack also waits for the wake to be accepted, because "Hermes is working on it" is only true then. After Build the stalled ping waits build_stale_factor times
    longer."""
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
        if store.guest_submission(r):
            return False, GUEST_NEVER_DELIVERED
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
        no_notice = _no_notice_route(data.get("origin"))
        if no_notice:
            state.update(status="skipped", reason=no_notice)
            sub[key] = state
            store.save(review_id, data)
            return False, f"skipped: {no_notice}"
        minutes = settings.get("stale_after_minutes")
        read_minutes = None
        if key == "stalled_notice":
            if not minutes:
                return False, "stale-after pings are off"
            if sub.get("status") == "stored":
                return False, "stored, not delivered: nothing can have stalled"
            # The ping is for a likely stall only: not for a round that asks for no more work, not once the session
            # acknowledged it, and after a read only once the longer read window passed.
            skip = ("nothing to build: every decision approved or skipped" if nothing_to_build(r)
                    else "acknowledged" if sub.get("acked_at") else None)
            if skip:
                state.update(status="skipped", reason=skip)
                sub[key] = state
                store.save(review_id, data)
                return False, f"skipped: {skip}"
            # A build takes longer than a feedback round: both waits stretch by build_stale_factor, and the ping
            # names the wait that applied.
            factor = settings.get("build_stale_factor") if is_build(r) else 1
            minutes *= factor
            read = _parse_iso(sub.get("read_at"))
            if read is not None:
                read_minutes = settings.get("stale_after_read_minutes")
                if not read_minutes:
                    return False, "the feedback was read; read pings are off"
                read_minutes *= factor
                if now - read < read_minutes * 60.0:
                    return False, "read, not stale yet"
            else:
                ts = _parse_iso(sub.get("submitted_at"))
                if ts is None or now - ts < minutes * 60.0:
                    return False, "not stale yet"
        elif sub.get("status") != "notified":
            return False, "the wake-up is not accepted yet; the ack follows it"
        if state.get("sending_until", 0) > now:
            return False, "a notice for this submission is already in flight"
        seq = sub["seq"]
        try:
            text = (stalled_text(data, r, minutes, read_minutes) if key == "stalled_notice"
                    else notice_text(data, r))
            payload = _notice_payload(data, r, key, text)
        except ReviewError as e:
            # A payload that cannot be built (a legacy origin without chat_id) never can be: retrying it only
            # rewrote review.json every retry_seconds. One line, then terminal.
            state.update(status="dropped", last_error=e.message)
            sub[key] = state
            store.save(review_id, data)
            log.warning("%s for %s round %s cannot be built and is not retried: %s",
                        key, review_id, round_n, e.message)
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
        elif notify.is_permanent(msg):
            state.update(status="dropped", last_error=msg)
            log.warning("the gateway rejected %s for %s round %s; not retried: %s", key, review_id, round_n, msg)
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
    """Deliver one submission: the wake-up first, then the ack straight after it. The local server's submit and
    retry_pending both go through here, so they order and gate the two the same way. A failed wake leaves the ack
    pending (it is never sent for an un-woken submission); an ack failure never affects the wake.
    Returns ((wake_ok, detail), (ack_ok, detail))."""
    with store.locked(review_id):
        data = store.load(review_id)
        if store.guest_submission(store.get_round(data, round_n)):
            return (False, GUEST_NEVER_DELIVERED), (False, GUEST_NEVER_DELIVERED)
    wake = notify_round(review_id, round_n)
    return wake, notify_notice(review_id, round_n)


def check_stalled(now: Optional[float] = None) -> int:
    """Watchdog: POST exactly one stalled ping per submission whose round is the newest, still open, and past
    stale_after_minutes with no newer round. Only the owner service runs it (never a bound CLI call). Skips wake-off
    and closed reviews before doing any work. Returns how many pings were delivered."""
    if owner.is_bound():
        return 0
    now = time.time() if now is None else now
    minutes = settings.get("stale_after_minutes")
    pin_minutes = minutes or pin_timeout_minutes()
    sent = 0
    for rid in store.list_ids():
        try:
            data = store.load(rid)
        except (ReviewError, ValueError, OSError):
            continue
        if data.get("status") != "open" or data.get("wake") is False:
            continue
        try:
            _timeout_pin_requests(rid, now, pin_minutes)
        except (ReviewError, ValueError, OSError):
            log.exception("pin fix timeout pass failed for %s", rid)
        if not minutes:
            continue
        r = data["rounds"][-1]
        sub = r.get("submission")
        if not sub or not r.get("feedback"):
            continue
        if sub.get("status") == "stored":   # never delivered (a guest's Send): nothing can have stalled
            continue
        state = sub.get("stalled_notice") or {}
        if state.get("status") in TERMINAL_NOTICE:
            continue
        if state.get("status") == "pending_notify" and state.get("next_attempt", 0) > now:
            continue
        ok, _ = notify_stalled(rid, r["n"], now=now)
        if ok:
            sent += 1
    return sent


def expire_pin_requests(now: Optional[float] = None) -> int:
    """The pin-request half of `check_stalled`, for when stalled pings are off: time out working pin requests on
    open wake-on reviews after `pin_timeout_minutes()`, so a request never shows working forever. Sends nothing.
    Only the owner service runs it. Returns how many requests timed out."""
    if owner.is_bound():
        return 0
    now = time.time() if now is None else now
    minutes = pin_timeout_minutes()
    expired = 0
    for rid in store.list_ids():
        try:
            data = store.load(rid)
        except (ReviewError, ValueError, OSError):
            continue
        if data.get("status") != "open" or data.get("wake") is False:
            continue
        try:
            expired += _timeout_pin_requests(rid, now, minutes)
        except (ReviewError, ValueError, OSError):
            log.exception("pin fix timeout pass failed for %s", rid)
    return expired


def retry_pending(now: Optional[float] = None) -> int:
    """Retry every pending_notify submission that is due (wake first, ack right after it), and every pending ack
    whose wake already landed. Returns how many wakes became notified."""
    scan_at = time.time() if now is None else now
    done = 0
    for rid in store.list_ids():
        try:
            data = store.load(rid)
        except (ReviewError, ValueError, OSError):
            continue
        for r in data["rounds"]:
            sub = r.get("submission") or {}
            if sub.get("guest"):   # a share-link guest's stored record is never delivered
                continue
            if sub.get("status") == "pending_notify" and sub.get("next_attempt", 0) <= scan_at:
                (ok, _), _ack = deliver_submission(rid, r["n"])
                if ok:
                    done += 1
            elif sub.get("status") == "notified":
                notice = sub.get("notice") or {}
                if notice.get("status") == "pending_notify" and notice.get("next_attempt", 0) <= scan_at:
                    notify_notice(rid, r["n"])
        try:
            flush_pin_fixes(rid, scan_at)   # a due pin fix wake retries on the worker tick (every 5 s)
        except Exception:
            log.exception("pin fix flush failed for %s", rid)
        try:
            retry_thread_wakes(rid, now)   # a due mention wake retries on the same tick
        except Exception:
            log.exception("mention wake retry failed for %s", rid)
    return done
