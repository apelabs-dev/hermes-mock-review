"""Comment threads on a review: the model behind the board, the REST API and the MCP tools.

A thread is one anchored conversation: a pin on a block, an option, the bare board or a prototype, with one or more
plain-text comments. Functions here are plain Python and never need an HTTP request, so an agent-facing caller can use
them directly. Every mutation writes its persisted event through `live.publish`, so a board watching the SSE stream
sees the same change whether it came from the API, the CLI or an MCP tool.

Rules kept here, not in the board:
- text is plain: control characters are stripped (newlines and tabs stay), trailing whitespace is trimmed, 2000 chars max;
- a comment belongs to its author: only the author edits or deletes it (`mine`), and deleting the last live comment
  deletes the thread;
- a resolved thread takes no replies until it is reopened;
- unread is per principal (`user:<id>` or `guest:<id>`), in the `reads` table; activity has its own cursor;
- reads are scoped (`Scope`): a viewer guest whose share link picks a round sees exactly that round on every surface,
  everyone else sees every round up to the one they are looking at;
- mentions are structured ids and only @Hermes activates a thread, and only for a role that carries `submit`.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from . import db, live, store, users
from .store import ReviewError

MAX_TEXT = 2000
MAX_OPEN_THREADS = 200
MAX_COMMENTS = 40
# Hermes answering in a thread: one fixed actor that is no account, so the comment is never a person's (`mine` is false
# for everyone) and never wakes anything. Callers audit the real account that held the pen.
AGENT_ACTOR = "agent:hermes"
AGENT_NAME = "Hermes"
MAX_ANCHOR_BYTES = 4096
MAX_IDENT = 64
MAX_IDEM = 100
# int() refuses a digit string over 4300 chars, so every "digits then int()" site caps far below that (the same cap
# server.parse_range_header uses).
MAX_INT_DIGITS = 18
RATE_WRITES = 20
RATE_WINDOW = 60.0
ACTIVATE_ROLES = ("owner", "reviewer")

TRIM = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
SAFE_PATH = re.compile(r"^[^<>{};`\x00-\x1f\x7f]+$")
NTH = re.compile(r":nth-(?:child|of-type)\(\d+\)")
TAG = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
HASH = re.compile(r"^[a-z0-9]{1,40}$")
IDENT = re.compile(r"^[^\x00-\x1f\x7f/]{1,64}$")

EVENT_KINDS = ("thread.created", "thread.replied", "thread.status", "thread.deleted", "comment.edited",
               "comment.deleted", "mention", "review.reset")

_write_lock = threading.Lock()
_writes: Dict[Tuple[str, str], List[float]] = {}


# ---------------------------------------------------------------- parsing

def ascii_int(value: Any, *, signed: bool = False) -> Optional[int]:
    """`value` as an int only when it is ASCII decimal digits (optionally signed) at most MAX_INT_DIGITS long.

    `str.isdigit()` is True for characters `int()` refuses ('²'), and `int()` refuses a digit run over 4300
    chars, so a digit test is never enough on its own: this is the one total function every parse site uses.
    """
    text = str(value or "")
    sign = ""
    if signed and text.startswith("-"):
        sign, text = "-", text[1:]
    if not text or len(text) > MAX_INT_DIGITS or not text.isascii() or not text.isdigit():
        return None
    return int(sign + text)


@dataclass(frozen=True)
class Scope:
    """Which rounds one request may read. `rounds` is an exact allowlist and only a pinned viewer guest gets one;
    everyone else sees every round up to `viewed` (the round their board is showing). A `?round=` query is
    intersected with that allowlist, never replaces it, so another round can yield the empty set."""
    viewed: int
    rounds: Optional[FrozenSet[int]] = None
    pinned: Optional[int] = None          # the share link's round when it produced the allowlist

    @property
    def restricted(self) -> bool:
        return self.rounds is not None

    def allows_round(self, round_n: Any) -> bool:
        """The method form of `sees_round`, kept for callers that already hold a scope."""
        return sees_round(self, round_n)


def sees_round(scope: Scope, round_n: Any) -> bool:
    """The one round gate every read surface shares: state.json, the exports, SSE, the threads API and raw round files.

    A request may read round `round_n` (an int, a path string, or None) only when its scope names it. A pinned viewer
    guest's scope is the share link's exact allowlist, a restricted scope sees no unknown round at all, and an
    unrestricted scope sees every round up to the one it is viewing. The round-file route asks this before it resolves
    a path, so a variant id, an asset, a media Range request or a frame token can never widen the share link's pin.
    """
    if round_n is None:
        return not scope.restricted
    try:
        n = int(round_n)
    except (TypeError, ValueError):
        return not scope.restricted
    return n in scope.rounds if scope.rounds is not None else n <= scope.viewed


_UPDATED_STEP = 1e-3


def now() -> float:
    return time.time()


def _stamp(conn: Any, thread_id: str, t: float) -> float:
    """The `updated_at` to write for a thread inside the open transaction.

    `t` is the wall clock read before the write lock was taken, so a concurrent writer that committed first (or a clock
    that stepped back) can leave the stored value ahead of it. The board drops a snapshot older than the one on screen,
    so a later commit must always carry a larger value: never less than the stored one plus a millisecond.
    """
    row = conn.execute("SELECT updated_at FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return t if row is None else max(t, float(row["updated_at"]) + _UPDATED_STEP)


def reset_limits() -> None:
    """Tests (and a long-lived process after a burst): forget the in-memory write buckets."""
    with _write_lock:
        _writes.clear()


def _rate_check(review_id: str, actor: str) -> None:
    t = time.monotonic()
    key = (review_id, actor)
    with _write_lock:
        bucket = [x for x in _writes.get(key, []) if t - x < RATE_WINDOW]
        if len(bucket) >= RATE_WRITES:
            _writes[key] = bucket
            raise ReviewError("slow down", 429)
        bucket.append(t)
        _writes[key] = bucket


# ---------------------------------------------------------------- text and anchors

def clean_text(value: Any) -> str:
    """A comment as it is stored: newlines kept, controls stripped, trailing whitespace trimmed."""
    if not isinstance(value, str):
        raise ReviewError("write something first", 400)
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = TRIM.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    if not text:
        raise ReviewError("write something first", 400)
    if len(text) > MAX_TEXT:
        raise ReviewError(f"comments are limited to {MAX_TEXT} characters", 400)
    return text


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return TRIM.sub("", str(value))[:limit]


def _ident(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) > MAX_IDENT or not IDENT.match(text):
        raise ReviewError("anchor has an invalid block or option", 400)
    return text


def _frac(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if math.isfinite(n) else 0.0


def _clamp(value: Any) -> float:
    return round(min(1.0, max(0.0, _frac(value))), 4)


def _doc(value: Any) -> float:
    return round(min(1000000.0, max(0.0, _frac(value))), 2)


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 300 or not SAFE_PATH.match(value):
        return ""
    return "" if ":" in NTH.sub("", value) else value


_TARGET_KEYS = {"path", "id", "part", "sig", "label", "x", "y", "pin", "doc", "quote", "position"}
_ANCHOR_KEYS = {"block", "option", "target", "x", "y", "w", "on", "doc"}


def _clean_target(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReviewError("anchor target must be an object", 400)
    extra = set(value) - _TARGET_KEYS
    if extra:
        raise ReviewError("anchor target has an unknown field", 400)
    path = _safe_path(value.get("path"))
    sig = value.get("sig")
    if not path or not isinstance(sig, dict) or set(sig) - {"tag", "h"}:
        raise ReviewError("anchor target is not valid", 400)
    tag, h = str(sig.get("tag") or ""), str(sig.get("h") or "")
    if not TAG.match(tag) or not HASH.match(h):
        raise ReviewError("anchor target is not valid", 400)
    out: Dict[str, Any] = {
        "path": path, "x": _clamp(value.get("x")), "y": _clamp(value.get("y")),
        "pin": {"x": _clamp((value.get("pin") or {}).get("x")), "y": _clamp((value.get("pin") or {}).get("y"))},
        "sig": {"tag": tag, "h": h},
        "label": _text(value.get("label"), 120),
        "doc": {"x": _doc((value.get("doc") or {}).get("x")), "y": _doc((value.get("doc") or {}).get("y"))},
    }
    if _text(value.get("id"), 80):
        out["id"] = _text(value.get("id"), 80)
    if _text(value.get("part"), 40):
        out["part"] = _text(value.get("part"), 40)
    quote = value.get("quote")
    if isinstance(quote, dict):
        exact = quote.get("exact")
        if isinstance(exact, str) and exact and len(exact) <= 160:
            out["quote"] = {"type": "TextQuoteSelector", "exact": exact.replace("\x00", ""),
                            "prefix": _text(quote.get("prefix"), 32), "suffix": _text(quote.get("suffix"), 32)}
            pos = value.get("position")
            if (isinstance(pos, dict) and isinstance(pos.get("start"), int) and isinstance(pos.get("end"), int)
                    and 0 <= pos["start"] < pos["end"] <= 1000000):
                out["position"] = {"type": "TextPositionSelector", "start": pos["start"], "end": pos["end"]}
    return out


def clean_anchor(value: Any) -> Dict[str, Any]:
    """The canvas-pin shape, validated and bounded: block/option ids, a known target, fractions, width and surface."""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ReviewError("anchor must be an object", 400)
    extra = set(value) - _ANCHOR_KEYS
    if extra:
        raise ReviewError("anchor has an unknown field", 400)
    out: Dict[str, Any] = {}
    for name in ("block", "option"):
        v = _ident(value.get(name))
        if v:
            out[name] = v
    if value.get("w") in ("desktop", "tablet", "phone"):
        out["w"] = value["w"]
    if value.get("on") == "board":
        out["on"] = "board"
    for name in ("x", "y"):
        if name in value:
            out[name] = _clamp(value[name])
    target = _clean_target(value.get("target"))
    if target is not None:
        out["target"] = target
    elif isinstance(value.get("doc"), dict):  # A targetless board pin keeps its last page spot in px
        out["doc"] = {"x": _doc(value["doc"].get("x")), "y": _doc(value["doc"].get("y"))}
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_ANCHOR_BYTES:
        raise ReviewError("anchor is too large", 400)
    return out


# ---------------------------------------------------------------- mentions

def _user_has_access(user_id: int, review_id: str) -> bool:
    user = users.get_user(user_id)
    if user is None:
        return False
    return user["role"] == "admin" or users.grant_role(user_id, review_id) is not None


def _guest_of_review(guest_id: str, review_id: str) -> bool:
    g = users.guest(guest_id)
    return g is not None and g["review_id"] == review_id


def clean_mentions(review_id: str, values: Any) -> List[Dict[str, str]]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ReviewError("mentions must be a list", 400)
    out: List[Dict[str, str]] = []
    seen = set()
    for raw in values[:50]:
        target = str(raw or "").strip()
        if target in seen:
            continue
        if target == "hermes":
            seen.add(target)
            out.append({"kind": "hermes", "target": "hermes"})
        elif target.startswith("user:"):
            uid = ascii_int(target[5:])
            if uid is None:
                raise ReviewError("unknown mention", 400)
            if not _user_has_access(uid, review_id):
                raise ReviewError("that person cannot see this review", 400)
            seen.add(target)
            out.append({"kind": "user", "target": f"user:{uid}"})
        elif target.startswith("guest:") and len(target) <= 80 and _guest_of_review(target[6:], review_id):
            seen.add(target)
            out.append({"kind": "guest", "target": target})
        elif target:
            raise ReviewError("unknown mention", 400)
    return out


def _activated(mentions: Sequence[Dict[str, str]], role: Optional[str], can_activate: Optional[bool] = None,
               actor: str = "") -> bool:
    """Whether this comment's @Hermes activates its thread. The board server decides (`can_activate`, from
    wakeguard.request_gate), and only it may let a share-link guest's mention through; a caller that does not decide
    falls back to the role, and a guest never activates there."""
    if can_activate is not None:
        return bool(can_activate and any(m["kind"] == "hermes" for m in mentions))
    if str(actor).startswith("guest:"):
        return False
    allowed = role in ACTIVATE_ROLES
    return bool(allowed and any(m["kind"] == "hermes" for m in mentions))


# ---------------------------------------------------------------- rows and dicts

def _row(r: Any) -> Dict[str, Any]:
    return dict(r) if r is not None else {}


def _check_id(value: str, what: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[a-f0-9]{32}", text):
        raise ReviewError(f"{what} not found", 404)
    return text


def _thread_row(review_id: str, thread_id: str, conn: Any = None) -> Dict[str, Any]:
    c = conn or db.connect()
    r = c.execute("SELECT * FROM threads WHERE id = ? AND review_id = ?",
                  (_check_id(thread_id, "thread"), review_id)).fetchone()
    if r is None:
        raise ReviewError("thread not found", 404)
    return dict(r)


def _comment_row(review_id: str, comment_id: str, conn: Any = None) -> Dict[str, Any]:
    c = conn or db.connect()
    r = c.execute("SELECT c.*, t.review_id, t.status AS thread_status FROM comments c "
                  "JOIN threads t ON t.id = c.thread_id WHERE c.id = ? AND t.review_id = ?",
                  (_check_id(comment_id, "comment"), review_id)).fetchone()
    if r is None:
        raise ReviewError("comment not found", 404)
    return dict(r)


def _name_for(actor: str, fallback: str = "") -> str:
    if actor.startswith("user:") and actor[5:].isdigit():
        user = users.get_user(int(actor[5:]))
        return (user or {}).get("name") or fallback or "Former member"
    if actor.startswith("guest:") and actor[6:]:
        g = users.guest(actor[6:])
        return (g or {}).get("display_name") or fallback or "Guest"
    if actor == AGENT_ACTOR:
        return AGENT_NAME
    return fallback or "Someone"


def role_of(user_id: int, review_id: str) -> Optional[str]:
    """The role a user account holds on one review: owner for an admin, else the live grant (None: no access).
    The one rule behind `mine`/`role` in a comment, the REST API's caps and the MCP tools' own checks."""
    user = users.get_user(user_id)
    if user is None:
        return None
    return "owner" if user["role"] == "admin" else users.grant_role(user_id, review_id)


def _role_for(actor: str, review_id: str) -> Optional[str]:
    if actor.startswith("user:") and actor[5:].isdigit():
        return role_of(int(actor[5:]), review_id)
    return None


def _author_kind(actor: Any) -> str:
    """Who an actor is, whatever name it typed: a guest, a member account, or Hermes itself."""
    text = str(actor or "")
    if text.startswith("guest:"):
        return "guest"
    if text.startswith("user:"):
        return "member"
    return "hermes"


def _round_data(review_id: str) -> Optional[Dict[str, Any]]:
    """`store.load`, or None when the review cannot be read. Fail closed: an unreadable review is not an empty one.

    A caller that has no data of its own must never turn None into `{}` and then into round 1: `view_scope` maps it to
    a restricted, empty allowlist (sees nothing), and every reader that needs content raises instead of inventing a
    review. A pin naming a round that no longer exists is a *valid* data with a missing round, and keeps the documented
    fallback to the full view; only a load failure is treated this way.
    """
    try:
        return store.load(review_id)
    except Exception:
        return None


def _current_round(data: Dict[str, Any]) -> int:
    rounds = data.get("rounds") or []
    return int(rounds[-1]["n"]) if rounds else 1


def _default_scope(review_id: str) -> Scope:
    """The scope of a read that names no round: the whole board, or nothing at all when the review cannot load."""
    data = _round_data(review_id)
    return Scope(_current_round(data)) if data is not None else Scope(1, frozenset())


def _round_by_n(data: Dict[str, Any], n: int) -> Optional[Dict[str, Any]]:
    for r in data.get("rounds") or []:
        if int(r["n"]) == int(n):
            return r
    return None


def view_scope(review_id: str, *, guest: bool, role: Optional[str], round_n: Optional[int] = None,
               data: Optional[Dict[str, Any]] = None) -> Scope:
    """The one read scope for a request. A viewer guest whose share link picks a round reads exactly that round; an
    `?round=` query is intersected with that pinned exact allowlist, never replaces it, so asking for another round
    yields the empty set. A round the link picks that no longer exists falls back to the full view, the same fallback
    state.json uses. Everyone else (owners, members, commenter/reviewer guests, a link left on `latest`) reads every
    round up to the round they are looking at, and `?round=` can only narrow that.

    A load failure is not an empty review: it must never widen a scope, so it yields a restricted, empty allowlist
    (sees nothing), never the unrestricted round-1 view. That is a different case from the missing-round pin above.
    """
    if data is None:
        data = _round_data(review_id)
    if data is None:
        return Scope(1, frozenset())
    current = _current_round(data)
    if guest and role == "viewer":
        link = users.resolve_link_row(review_id) or {}
        pinned = str(link.get("round") or "latest")
        if pinned.isdecimal() and _round_by_n(data, int(pinned)) is not None:
            authorized = int(pinned)
            rounds = {authorized} if round_n is None else {authorized} & {int(round_n)}
            return Scope(authorized, frozenset(rounds), authorized)
    viewed = int(round_n) if round_n is not None else current
    return Scope(max(1, viewed))


def _anchor_resolves(data: Dict[str, Any], round_n: int, anchor: Dict[str, Any]) -> bool:
    """Whether the round still renders what the anchor names. A block ref is any `sections` ref (a block id or a
    generated s<N>), any decision/mock/artifact id, a legacy variant id, or one of the board's reserved surfaces; an
    option must still exist on the block that has options. False here is what marks a thread `detached`."""
    block = str(anchor.get("block") or "")
    if not block:
        return False
    r = _round_by_n(data, round_n)
    if r is None:
        return False
    if block in ("page", "doc", "else", "proto"):
        return True
    index = {str(e.get("id")): e for e in store.round_index(r)}
    option = anchor.get("option")
    if block in index:
        if not option:
            return True
        return any(str(o.get("id")) == str(option) for o in index[block].get("options") or [])
    if block in {str(m.get("id")) for m in store.round_mocks(r)}:
        return True                                   # a context mock or an artifact: pins land on its id
    if block in {str(s.get("ref")) for s in store.round_sections(r)}:
        return True
    return any(str(v.get("id")) == block for v in r.get("variants") or [])


def _threads_rows(review_id: str, conn: Any = None) -> List[Dict[str, Any]]:
    c = conn or db.connect()
    return [dict(r) for r in c.execute("SELECT * FROM threads WHERE review_id = ? ORDER BY created_at, id",
                                       (review_id,)).fetchall()]


def _comments_for(thread_ids: Sequence[str], conn: Any = None) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {tid: [] for tid in thread_ids}
    if not thread_ids:
        return out
    c = conn or db.connect()
    marks = ",".join("?" * len(thread_ids))
    rows = c.execute(f"SELECT * FROM comments WHERE thread_id IN ({marks}) ORDER BY created_at, id",
                     tuple(thread_ids)).fetchall()
    for r in rows:
        out[r["thread_id"]].append(dict(r))
    return out


def _mentions_for(comment_ids: Sequence[str], conn: Any = None) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {cid: [] for cid in comment_ids}
    if not comment_ids:
        return out
    c = conn or db.connect()
    marks = ",".join("?" * len(comment_ids))
    for r in c.execute(f"SELECT comment_id, kind, target FROM mentions WHERE comment_id IN ({marks}) "
                       f"ORDER BY kind, target", tuple(comment_ids)).fetchall():
        out[r["comment_id"]].append({"kind": r["kind"], "target": r["target"]})
    return out


def _reads_for(review_id: str, principal: str, conn: Any = None) -> Dict[str, float]:
    if not principal:
        return {}
    c = conn or db.connect()
    return {r["thread_id"]: float(r["seen_at"]) for r in c.execute(
        "SELECT thread_id, seen_at FROM reads WHERE principal = ? AND review_id = ?", (principal, review_id))}


def _comment_dict(c: Dict[str, Any], mentions: List[Dict[str, str]], principal: str, review_id: str,
                  activated: bool) -> Dict[str, Any]:
    author = c["author"]
    deleted = c["deleted_at"] is not None
    user_gone = author.startswith("user:") and author[5:].isdigit() and users.get_user(int(author[5:])) is None
    author_name = "Former member" if user_gone else c["author_name"]
    return {
        "id": c["id"], "author": author, "author_name": author_name, "author_kind": _author_kind(author),
        "role": _role_for(author, review_id),
        "text": "" if deleted else c["text"], "created_at": c["created_at"], "edited_at": c["edited_at"],
        "deleted_at": c["deleted_at"], "mentions": mentions, "mine": bool(principal) and author == principal,
        "sent_to_hermes": bool(activated and any(m["kind"] == "hermes" for m in mentions)),
    }


def _build(rows: List[Dict[str, Any]], data: Dict[str, Any], viewed_round: int, principal: str) -> List[Dict[str, Any]]:
    ids = [r["id"] for r in rows]
    comments = _comments_for(ids)
    cids = [c["id"] for group in comments.values() for c in group]
    mentions = _mentions_for(cids)
    reads = _reads_for(str(data.get("id") or ""), principal)
    numbers = {r["id"]: i + 1 for i, r in enumerate(rows)}
    out = []
    for r in rows:
        group = [_comment_dict(c, mentions.get(c["id"], []), principal, data.get("id", ""), bool(r["activated"]))
                 for c in comments.get(r["id"], [])]
        live_comments = [c for c in group if c["deleted_at"] is None]
        last = max([c["created_at"] for c in live_comments] or [r["created_at"]])
        seen = reads.get(r["id"])
        unread = bool(principal and live_comments and last > (seen if seen is not None else 0)
                      and any(c["author"] != principal for c in live_comments))
        try:
            anchor = json.loads(r["anchor"] or "{}")
        except ValueError:
            anchor = {}
        carried = int(r["round"]) != int(viewed_round)
        out.append({
            "id": r["id"], "n": numbers[r["id"]], "round": int(r["round"]), "status": r["status"],
            "activated": bool(r["activated"]), "carried": carried,
            "detached": bool(anchor) and not _anchor_resolves(data, viewed_round, anchor),
            "anchor": anchor, "created_at": r["created_at"], "updated_at": r["updated_at"],
            "created_by": r["created_by"], "comments": group, "unread": unread,
        })
    return out


# ---------------------------------------------------------------- reads and activity

def mark_read(review_id: str, principal: str, thread_id: str = "") -> None:
    """Mark one thread (or the activity feed, thread_id '') read for this principal, up to now."""
    if not principal:
        return
    if thread_id:
        _thread_row(review_id, thread_id)
    with db.tx() as conn:
        conn.execute("INSERT INTO reads (principal, review_id, thread_id, seen_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(principal, review_id, thread_id) DO UPDATE SET seen_at = excluded.seen_at",
                     (principal, review_id, thread_id or "", now()))


def _activity_unread(rows: List[Dict[str, Any]], principal: str, cursor: float) -> int:
    return sum(1 for e in rows if e["actor"] != principal and float(e["at"]) > cursor)


def _thread_rounds(review_id: str) -> Dict[str, int]:
    return {r["id"]: int(r["round"]) for r in _threads_rows(review_id)}


def _event_round(row: Dict[str, Any], thread_rounds: Dict[str, int]) -> Optional[int]:
    """The round a stored event belongs to: its own payload first (the client's shape), then the live thread row."""
    try:
        payload = json.loads(row["data"] or "{}")
    except ValueError:
        payload = {}
    n = live.event_round(str(row["kind"]), payload)
    if n is not None:
        return n
    return thread_rounds.get(row["thread_id"])


def activity(review_id: str, principal: str = "", limit: int = 100,
             scope: Optional[Scope] = None) -> List[Dict[str, Any]]:
    """Persisted activity, newest first, with `unread` for this principal against the activity cursor."""
    conn = db.connect()
    lim = max(1, min(int(limit), 500))
    scope = scope or _default_scope(review_id)
    restricted = scope.restricted
    sql = "SELECT * FROM events WHERE review_id = ? AND kind NOT IN (?, ?) ORDER BY id DESC" + ("" if restricted else " LIMIT ?")
    quiet = (live.PIN_REQUESTS, live.BLOCK_PATCHED)   # board plumbing, never an entry in the feed
    args = (review_id, *quiet) if restricted else (review_id, *quiet, lim)
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    thread_rounds = _thread_rounds(review_id) if restricted else {}
    cursor = _reads_for(review_id, principal, conn).get("", 0.0)
    out = []
    for r in rows:
        try:
            payload = json.loads(r["data"] or "{}")
        except ValueError:
            payload = {}
        if restricted and not sees_round(scope, _event_round(r, thread_rounds)):
            continue
        out.append({"id": r["id"], "kind": r["kind"], "thread_id": r["thread_id"], "actor": r["actor"],
                    "actor_kind": _author_kind(r["actor"]),
                    "at": r["at"], "data": payload, "unread": bool(principal and r["actor"] != principal
                                                                   and float(r["at"]) > cursor)})
        if len(out) >= lim:
            break
    return out


def activity_unread(review_id: str, principal: str = "", scope: Optional[Scope] = None) -> int:
    if not principal:
        return 0
    cursor = _reads_for(review_id, principal).get("", 0.0)
    scope = scope or _default_scope(review_id)
    if not scope.restricted:
        row = db.connect().execute(
            "SELECT COUNT(*) AS n FROM events WHERE review_id = ? AND kind NOT IN (?, ?) AND actor != ? AND at > ?",
            (review_id, live.PIN_REQUESTS, live.BLOCK_PATCHED, principal, cursor)).fetchone()
        return int(row["n"])
    thread_rounds = _thread_rounds(review_id)
    rows = [dict(r) for r in db.connect().execute(
        "SELECT * FROM events WHERE review_id = ? AND kind NOT IN (?, ?) AND actor != ? AND at > ?",
        (review_id, live.PIN_REQUESTS, live.BLOCK_PATCHED, principal, cursor)).fetchall()]
    return sum(1 for r in rows if sees_round(scope, _event_round(r, thread_rounds)))


# ---------------------------------------------------------------- read model

def list_threads(review_id: str, principal: str = "", round_n: Optional[int] = None,
                 include_resolved: bool = True, scope: Optional[Scope] = None) -> List[Dict[str, Any]]:
    data = _round_data(review_id)
    if data is None:
        raise ReviewError("review not found", 404)
    scope = scope or Scope(int(round_n) if round_n is not None else _current_round(data))
    viewed = scope.viewed
    rows = [r for r in _threads_rows(review_id) if sees_round(scope, r["round"])]
    if not include_resolved:
        rows = [r for r in rows if r["status"] == "open"]
    built = _build(rows, data, viewed, principal)
    built.sort(key=lambda t: (not t["activated"], -float(t["updated_at"])))
    return built


def counts(review_id: str, principal: str = "", round_n: Optional[int] = None,
           scope: Optional[Scope] = None) -> Dict[str, int]:
    threads = list_threads(review_id, principal, round_n, scope=scope)
    return {"open": sum(1 for t in threads if t["status"] == "open"), "total": len(threads),
            "unread": sum(1 for t in threads if t["unread"]),
            "activity_unread": activity_unread(review_id, principal, scope)}


def rev(review_id: str, scope: Optional[Scope] = None) -> int:
    if scope is None or not scope.restricted:
        row = db.connect().execute("SELECT MAX(id) AS id FROM events WHERE review_id = ?", (review_id,)).fetchone()
        return int(row["id"] or 0)
    thread_rounds = _thread_rounds(review_id)
    rows = db.connect().execute("SELECT id, kind, thread_id, data FROM events WHERE review_id = ? ORDER BY id DESC",
                                (review_id,)).fetchall()
    for r in rows:
        if sees_round(scope, _event_round(dict(r), thread_rounds)):
            return int(r["id"])
    return 0


def summary(review_id: str, scope: Optional[Scope] = None) -> Dict[str, int]:
    """What state.json carries: no comment text, only enough to notice a change."""
    if scope is None:
        row = db.connect().execute("SELECT COUNT(*) AS n FROM threads WHERE review_id = ? AND status = 'open'",
                                   (review_id,)).fetchone()
        return {"open": int(row["n"]), "rev": rev(review_id)}
    open_n = sum(1 for r in _threads_rows(review_id)
                 if r["status"] == "open" and sees_round(scope, r["round"]))
    return {"open": open_n, "rev": rev(review_id, scope)}


def people(review_id: str) -> List[Dict[str, str]]:
    """Everyone who may be mentioned: admins, accounts granted this review, and its guests."""
    conn = db.connect()
    out: Dict[str, Dict[str, str]] = {}
    for r in conn.execute("SELECT id, name FROM users WHERE role = 'admin' ORDER BY name, id").fetchall():
        out[f"user:{r['id']}"] = {"kind": "user", "target": f"user:{r['id']}", "id": str(r["id"]),
                                  "name": r["name"], "role": "owner"}
    rows = conn.execute(
        "SELECT u.id, u.name, g.role, g.review_id FROM grants g JOIN users u ON u.id = g.user_id "
        "WHERE (g.review_id = ? OR g.all_reviews = 1) AND (g.expires_at IS NULL OR g.expires_at > ?) "
        "ORDER BY g.review_id IS NULL", (review_id, now())).fetchall()
    for r in rows:
        key = f"user:{r['id']}"
        if key in out:
            continue
        out[key] = {"kind": "user", "target": key, "id": str(r["id"]), "name": r["name"], "role": r["role"]}
    for r in conn.execute("SELECT id, display_name FROM guests WHERE review_id = ? ORDER BY created_at",
                          (review_id,)).fetchall():
        key = f"guest:{r['id']}"
        out[key] = {"kind": "guest", "target": key, "id": r["id"],
                    "name": r["display_name"] or "Guest", "role": "guest"}
    return list(out.values())


def listing(review_id: str, principal: str = "", round_n: Optional[int] = None,
            scope: Optional[Scope] = None) -> Dict[str, Any]:
    # threads.js drops an event whose id is <= `rev` but absent from `counts` as replayed, so `counts` and `rev` must
    # come from one WAL snapshot: an event committing between the two reads would be marked stale until the next
    # refresh. One deferred read transaction on this thread's connection covers every read below.
    conn = db.connect()
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN")
    try:
        threads = list_threads(review_id, principal, round_n, scope=scope)
        out = {"threads": threads, "counts": counts(review_id, principal, round_n, scope),
               "rev": rev(review_id, scope), "activity": activity(review_id, principal, scope=scope),
               "people": people(review_id)}
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise
    if own_tx:
        conn.execute("COMMIT")
    return out


# ---------------------------------------------------------------- export

def _export_author(comment: Dict[str, Any]) -> Dict[str, str]:
    """The export author shape: the stored name plus guest / member / former, as `export._author_text` reads it."""
    if str(comment.get("author") or "").startswith("guest:"):
        kind = "guest"
    elif comment.get("author_name") == "Former member":
        kind = "former"
    else:
        kind = "member"
    return {"name": str(comment.get("author_name") or ""), "kind": kind}


def _export_label(anchor: Dict[str, Any]) -> str:
    """The board's anchor wording: the target's label when the pin names an element, else the block or option id."""
    target = anchor.get("target")
    if isinstance(target, dict) and str(target.get("label") or "").strip():
        return str(target["label"])
    return str(anchor.get("block") or anchor.get("option") or "")


def _export_at(created_at: Any) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(created_at)))


def threads_for_export(review_id: str, rounds: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """The export view of a review's threads, in `export._normal_thread`'s shape.

    `rounds` is None (every round) or the exact allowlist the request may read: a pinned viewer guest gets exactly its
    link's round. Principal-relative fields (mine, unread) stay out, deleted and empty bodies are dropped, and an
    unreadable review yields no threads: the export fails closed on content, never with a 500 for the whole document.
    """
    data = _round_data(review_id)
    if data is None:
        return []
    rows = _threads_rows(review_id)
    if rounds is not None:
        allowed = {n for n in (ascii_int(r) for r in rounds) if n is not None}
        rows = [r for r in rows if int(r["round"]) in allowed]
    out: List[Dict[str, Any]] = []
    for thread in _build(rows, data, _current_round(data), ""):
        comments = [c for c in thread["comments"] if c["deleted_at"] is None]
        first = comments[0] if comments else None
        if first is None or not str(first.get("text") or ""):
            continue
        anchor = thread["anchor"] if isinstance(thread["anchor"], dict) else {}
        out.append({
            "body": first["text"],
            "anchor": {"label": _export_label(anchor)},
            "author": _export_author(first),
            "at": _export_at(first["created_at"]),
            "resolved": thread["status"] != "open",
            "replies": [{"author": _export_author(c), "at": _export_at(c["created_at"]), "body": c["text"]}
                        for c in comments[1:] if str(c.get("text") or "")],
            "round": int(thread["round"]),
        })
    return out


# ---------------------------------------------------------------- writes

def _idem_comment(conn: Any, thread_id: str, idem: str, actor: str) -> Optional[Dict[str, Any]]:
    if not idem:
        return None
    r = conn.execute("SELECT * FROM comments WHERE thread_id = ? AND idem = ? AND author = ? AND deleted_at IS NULL",
                     (thread_id, idem, actor)).fetchone()
    return dict(r) if r is not None else None


def _idem_any(conn: Any, review_id: str, idem: str, actor: str) -> Optional[Dict[str, Any]]:
    if not idem:
        return None
    r = conn.execute("SELECT t.id FROM comments c JOIN threads t ON t.id = c.thread_id "
                     "WHERE t.review_id = ? AND c.idem = ? AND c.author = ? AND c.deleted_at IS NULL",
                     (review_id, idem, actor)).fetchone()
    return dict(r) if r is not None else None


def _idem(value: Any) -> str:
    text = _text(value, MAX_IDEM).strip()
    return re.sub(r"[^A-Za-z0-9_.:-]", "", text)[:MAX_IDEM]


def _insert_mentions(conn: Any, comment_id: str, mentions: Sequence[Dict[str, str]]) -> None:
    for m in mentions:
        conn.execute("INSERT OR IGNORE INTO mentions (comment_id, kind, target) VALUES (?, ?, ?)",
                     (comment_id, m["kind"], m["target"]))


def _mention_event_targets(mentions: Sequence[Dict[str, str]]) -> List[str]:
    return [m["target"] for m in mentions]


def _publish_mention(review_id: str, thread: Dict[str, Any], actor: str, mentions: Sequence[Dict[str, str]]) -> None:
    if not mentions:
        return
    name = _name_for(actor)
    live.publish(review_id, "mention", thread["id"], actor,
                 {"thread": shared(thread), "actor": actor, "actor_kind": _author_kind(actor), "name": name,
                  "targets": _mention_event_targets(mentions)})


def _thread_dict(review_id: str, thread_id: str, principal: str, round_n: Optional[int] = None) -> Dict[str, Any]:
    data = _round_data(review_id)
    if data is None:
        raise ReviewError("thread not found", 404)
    viewed = int(round_n) if round_n is not None else _current_round(data)
    rows = [r for r in _threads_rows(review_id) if r["id"] == thread_id]
    built = _build(rows, data, viewed, principal)
    if not built:
        raise ReviewError("thread not found", 404)
    return built[0]


def shared(thread: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A thread for a broadcast: principal-relative fields (mine, unread) are dropped; each client derives them."""
    if not thread:
        return thread
    out = dict(thread)
    out.pop("unread", None)
    out["comments"] = [{k: v for k, v in c.items() if k != "mine"} for c in thread.get("comments") or []]
    return out


def create(review_id: str, actor: str, name: str, role: Optional[str], text: Any, anchor: Any = None,
           mentions: Any = None, round_n: Optional[int] = None, idem: Any = "", can_activate: Optional[bool] = None,
           principal: Optional[str] = None) -> Dict[str, Any]:
    """Start a thread with its first comment. `role` is the author's current role on the review."""
    clean = clean_text(text)
    cleaned_anchor = clean_anchor(anchor)
    mention_rows = clean_mentions(review_id, mentions)
    token = _idem(idem)
    data = _round_data(review_id)
    if data is None:
        raise ReviewError("review not found", 404)
    current = _current_round(data)
    rn = int(round_n) if round_n is not None else current
    if _round_by_n(data, rn) is None:
        raise ReviewError("round not found", 404)
    if rn > current:
        rn = current
    author_name = AGENT_NAME if actor == AGENT_ACTOR else users.clean_name(name) or _name_for(actor)
    activated = False if actor == AGENT_ACTOR else _activated(mention_rows, role, can_activate, actor=actor)
    _rate_check(review_id, actor)
    t = now()
    created: Optional[Dict[str, Any]] = None
    with db.tx() as conn:
        found = _idem_any(conn, review_id, token, actor)
        if found is not None:
            tid = found["id"]
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM threads WHERE review_id = ? AND status = 'open'",
                               (review_id,)).fetchone()
            if int(row["n"]) >= MAX_OPEN_THREADS:
                raise ReviewError(f"this review has {MAX_OPEN_THREADS} open threads; resolve some first", 400)
            tid, cid = uuid.uuid4().hex, uuid.uuid4().hex
            conn.execute("INSERT INTO threads (id, review_id, round, anchor, status, activated, carried, created_by, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, 0, ?, ?, ?)",
                         (tid, review_id, rn, json.dumps(cleaned_anchor, ensure_ascii=False, separators=(",", ":")),
                          1 if activated else 0, actor, t, t))
            conn.execute("INSERT INTO comments (id, thread_id, author, author_name, text, idem, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (cid, tid, actor, author_name, clean, token, t))
            _insert_mentions(conn, cid, mention_rows)
            conn.execute("INSERT INTO reads (principal, review_id, thread_id, seen_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(principal, review_id, thread_id) DO UPDATE SET seen_at = excluded.seen_at",
                         (principal or actor, review_id, tid, t))
            created = {"thread_id": tid, "name": author_name, "mentions": mention_rows, "new": True}
    if created is None:
        return _thread_dict(review_id, tid, principal or actor, rn)
    thread = _thread_dict(review_id, tid, principal or actor, rn)
    name_now = _name_for(actor, author_name)
    live.publish(review_id, "thread.created", tid, actor,
                 {"thread": shared(thread), "actor": actor, "actor_kind": _author_kind(actor), "name": name_now})
    _publish_mention(review_id, thread, actor, mention_rows)
    return thread


def reply_comment(review_id: str, thread_id: str, actor: str, name: str, role: Optional[str], text: Any,
                  mentions: Any = None, idem: Any = "", can_activate: Optional[bool] = None,
                  principal: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    """Insert one reply and return (thread, comment_id).

    The id is the row this call inserted, or, on an idempotent retry, the row the same actor already inserted with this
    `idem`. Callers never guess it from the thread (a concurrent reply may be newer or share the author).
    """
    clean = clean_text(text)
    mention_rows = clean_mentions(review_id, mentions)
    token = _idem(idem)
    author_name = users.clean_name(name) or _name_for(actor)
    activated = _activated(mention_rows, role, can_activate, actor=actor)
    _rate_check(review_id, actor)
    t = now()
    changed = False
    with db.tx() as conn:
        row = _thread_row(review_id, thread_id, conn)
        if row["status"] != "open":
            raise ReviewError("This thread is resolved. Reopen it to reply.", 400)
        found = _idem_comment(conn, row["id"], token, actor)
        if found is not None:
            cid = found["id"]
        else:
            n = conn.execute("SELECT COUNT(*) AS n FROM comments WHERE thread_id = ? AND deleted_at IS NULL",
                             (row["id"],)).fetchone()["n"]
            if int(n) >= MAX_COMMENTS:
                raise ReviewError(f"this thread is full ({MAX_COMMENTS} comments)", 400)
            cid = uuid.uuid4().hex
            stamp = _stamp(conn, row["id"], t)
            conn.execute("INSERT INTO comments (id, thread_id, author, author_name, text, idem, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (cid, row["id"], actor, author_name, clean, token, stamp))
            _insert_mentions(conn, cid, mention_rows)
            conn.execute("UPDATE threads SET updated_at = ?, activated = MAX(activated, ?) WHERE id = ?",
                         (stamp, 1 if activated else 0, row["id"]))
            conn.execute("INSERT INTO reads (principal, review_id, thread_id, seen_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(principal, review_id, thread_id) DO UPDATE SET seen_at = excluded.seen_at",
                         (actor if actor == AGENT_ACTOR else (principal or actor), review_id, row["id"], stamp))
            changed = True
    thread = _thread_dict(review_id, thread_id, principal or actor)
    if changed:
        live.publish(review_id, "thread.replied", thread_id, actor,
                     {"thread": shared(thread), "actor": actor, "actor_kind": _author_kind(actor),
                      "name": _name_for(actor, author_name), "comment": cid})
        _publish_mention(review_id, thread, actor, mention_rows)
    return thread, cid


def reply(review_id: str, thread_id: str, actor: str, name: str, role: Optional[str], text: Any,
          mentions: Any = None, idem: Any = "", can_activate: Optional[bool] = None,
          principal: Optional[str] = None) -> Dict[str, Any]:
    return reply_comment(review_id, thread_id, actor, name, role, text, mentions=mentions, idem=idem,
                         can_activate=can_activate, principal=principal)[0]


def edit_comment(review_id: str, comment_id: str, actor: str, text: Any, principal: Optional[str] = None) -> Dict[str, Any]:
    clean = clean_text(text)
    _rate_check(review_id, actor)
    t = now()
    with db.tx() as conn:
        row = _comment_row(review_id, comment_id, conn)
        if row["deleted_at"] is not None:
            raise ReviewError("that comment was deleted", 400)
        if row["author"] != actor:
            raise ReviewError("you can only edit your own comment", 403)
        if row["text"] == clean:
            pass
        else:
            stamp = _stamp(conn, row["thread_id"], t)
            conn.execute("UPDATE comments SET text = ?, edited_at = ? WHERE id = ?", (clean, stamp, row["id"]))
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (stamp, row["thread_id"]))
    thread = _thread_dict(review_id, row["thread_id"], principal or actor)
    if row["text"] != clean:
        live.publish(review_id, "comment.edited", row["thread_id"], actor,
                     {"thread": shared(thread), "actor": actor, "actor_kind": _author_kind(actor),
                      "name": _name_for(actor), "comment": row["id"]})
    return thread


def delete_comment(review_id: str, comment_id: str, actor: str, principal: Optional[str] = None) -> Optional[Dict[str, Any]]:
    _rate_check(review_id, actor)
    t = now()
    thread_gone = False
    thread_id = ""
    thread_round = 0
    with db.tx() as conn:
        row = _comment_row(review_id, comment_id, conn)
        if row["deleted_at"] is not None:
            raise ReviewError("that comment was deleted", 400)
        if row["author"] != actor:
            raise ReviewError("you can only delete your own comment", 403)
        thread_id = row["thread_id"]
        thread_round = int(_thread_row(review_id, thread_id, conn)["round"])
        stamp = _stamp(conn, thread_id, t)
        conn.execute("UPDATE comments SET deleted_at = ? WHERE id = ?", (stamp, row["id"]))
        live_count = conn.execute("SELECT COUNT(*) AS n FROM comments WHERE thread_id = ? AND deleted_at IS NULL",
                                  (thread_id,)).fetchone()["n"]
        if int(live_count) == 0:
            conn.execute("DELETE FROM reads WHERE review_id = ? AND thread_id = ?", (review_id, thread_id))
            conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            thread_gone = True
        else:
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (stamp, thread_id))
    name = _name_for(actor)
    if thread_gone:
        live.publish(review_id, "comment.deleted", thread_id, actor,
                     {"thread": None, "actor": actor, "actor_kind": _author_kind(actor), "name": name,
                      "comment": row["id"], "round": thread_round})
        live.publish(review_id, "thread.deleted", thread_id, actor,
                     {"thread_id": thread_id, "actor": actor, "actor_kind": _author_kind(actor), "name": name,
                      "round": thread_round})
        return None
    thread = _thread_dict(review_id, thread_id, principal or actor)
    live.publish(review_id, "comment.deleted", thread_id, actor,
                 {"thread": shared(thread), "actor": actor, "actor_kind": _author_kind(actor), "name": name,
                  "comment": row["id"]})
    return thread


def set_status(review_id: str, thread_id: str, status: str, actor: str, principal: Optional[str] = None) -> Dict[str, Any]:
    if status not in ("open", "resolved"):
        raise ReviewError("status must be open or resolved", 400)
    _rate_check(review_id, actor)
    t = now()
    with db.tx() as conn:
        row = _thread_row(review_id, thread_id, conn)
        if row["status"] != status:
            conn.execute("UPDATE threads SET status = ?, updated_at = ? WHERE id = ?",
                         (status, _stamp(conn, row["id"], t), row["id"]))
    thread = _thread_dict(review_id, thread_id, principal or actor)
    if row["status"] != status:
        live.publish(review_id, "thread.status", thread_id, actor,
                     {"thread": shared(thread), "actor": actor, "actor_kind": _author_kind(actor), "name": _name_for(actor),
                      "status": status})
    return thread


def touch_thread(review_id: str, thread_id: str) -> Dict[str, Any]:
    """The row for a thread, for a caller that only needs to validate it (typing, read)."""
    return _thread_row(review_id, thread_id)


def by_id(thread_id: str) -> Dict[str, Any]:
    """The row for a thread addressed by its id alone, with its review_id.

    Thread ids are global uuid4 hex primary keys, so an agent tool that names only a thread (MCP `reply` and
    `resolve`) finds its review here and then checks access there; a malformed id and a missing one both read as
    "thread not found".
    """
    r = db.connect().execute("SELECT * FROM threads WHERE id = ?", (_check_id(thread_id, "thread"),)).fetchone()
    if r is None:
        raise ReviewError("thread not found", 404)
    return dict(r)


def type_lease(review_id: str, thread_id: str, actor: str, name: str) -> bool:
    row = _thread_row(review_id, thread_id)
    return live.typing(review_id, thread_id, actor, _name_for(actor, name), int(row["round"]))
