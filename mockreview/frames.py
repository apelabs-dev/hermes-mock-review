"""Signed per-round frame tokens.

A round page runs in a sandboxed frame, so it has an opaque origin: its subresource requests and its own navigations
are cross-site, and a `SameSite=Lax` session cookie is not sent with them. The token is that document's own credential:
one signed value per (viewer, review, round), in the path, so every URL a prototype derives relatively inherits it.

The token names one review and one round and is bound to the viewer's live session (or to a loopback owner). A viewer
signed in by a trusted proxy with no session cookie binds its account instead (`user:<id>`), re-resolved on every
frame request. A session or account token's lifetime is its binding, so it dies the moment its session, share-link
generation, grant or account does; only a `local` token, which has no row to consult, carries `FRAME_TOKEN_SECONDS`.

The payload's `s` is a viewer pseudonym the prototype can read from its own URL: a session `id_hash` (already a
digest, never the cookie), `user:<id>`, or `local`. So an artifact learns that its viewer is the viewer of the round,
and nothing more.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import access, auth, users

# The only clock, and only for a `local` token: it has no session row to consult. A session- or account-bound token
# lives as long as its binding, because a frame keeps its URL (and so its token) for as long as the tab is open, and
# that is the bug class this fixes.
FRAME_TOKEN_SECONDS = 2 * 3600
FRAME_SALT = "mock-review.frame"
LOCAL = "local"
USER_PREFIX = "user:"
MAX_TOKEN = 512


def _serializer() -> Any:
    return users.itsdangerous().URLSafeTimedSerializer(users.signing_key(), salt=FRAME_SALT)


def account_binding(user_id: int) -> str:
    """The binding for a viewer signed in by a trusted-proxy identity with no session cookie."""
    return USER_PREFIX + str(int(user_id))


def _account_id(binding: str) -> Optional[int]:
    text = binding[len(USER_PREFIX):]
    if not text.isascii() or not text.isdigit() or len(text) > 18:
        return None
    uid = int(text)
    return uid if uid > 0 else None


def mint(review_id: str, round_n: int, session_hash: str) -> str:
    """One token for this review, round and viewer. `session_hash` is a session id_hash, "local" for the loopback
    owner's sessionless request, or account_binding(user_id); the session cookie value never enters the payload.
    The payload is exactly `{r, n, s, v}`: the actor is the viewer's pseudonym in `s`, never a separate claim."""
    return _serializer().dumps({"r": review_id, "n": int(round_n), "s": session_hash, "v": 1})


def _session_who(row: Dict[str, Any]) -> Optional["auth.Who"]:
    """The principal a live session row names, rebuilt the way auth.identify() builds it for that session."""
    if row.get("user_id") is not None:
        user = users.get_user(int(row["user_id"]))
        if user is None:
            return None
        return auth.Who(auth.principal_for(user), user=user, session=row, source="session")
    if row.get("guest_id") is not None:
        guest = users.guest(str(row["guest_id"]))
        if guest is None:
            return None
        principal = access.Principal("guest", guest_review=guest["review_id"],
                                     guest_generation=int(guest["generation"]))
        return auth.Who(principal, session=row, guest=guest, source="share")
    return None


def _payload(token: str) -> Optional[Dict[str, Any]]:
    """The signed payload, without an age check: a session or account token lives as long as its binding."""
    try:
        data = _serializer().loads(token)
    except Exception:                       # bad signature, a different serializer: invalid
        return None
    return data if isinstance(data, dict) else None


def _within_clock(token: str) -> bool:
    """Whether a `local` token is inside FRAME_TOKEN_SECONDS; it is the only binding with a clock."""
    try:
        _serializer().loads(token, max_age=FRAME_TOKEN_SECONDS)
    except Exception:                       # expired or malformed: both invalid
        return False
    return True


@dataclass(frozen=True)
class Grant:
    """A valid token's decision and the identity it speaks for (the session/account it is bound to)."""
    decision: "access.Decision"
    who: "auth.Who"


def validate(token: str, review_id: str, round_text: str, who: "auth.Who", local: bool,
             review_exists: bool) -> Optional["Grant"]:
    """The token's own decision and identity when it is valid for exactly this request, review and round; None
    otherwise. The grant tells the caller *who* the token is, never just that it is valid, so a cookieless frame
    request can build its read scope from the token's binding instead of the anonymous request.

    A valid signature alone is never enough. A request that carries a session must present that session's token; a
    request without one is re-resolved from the token's own session row and must still pass auth.decide/access.can,
    so a signed-out session, a regenerated share link and a revoked grant all stop loading: a session or account
    token dies with its binding, not with a clock. A "local" token is only accepted from this computer, inside its
    one clock, because it has no row to consult. The caller keeps today's decision when this returns None.
    """
    if not token or len(token) > MAX_TOKEN:
        return None
    data = _payload(token)
    if data is None or data.get("v") != 1:
        return None
    if not isinstance(data.get("r"), str) or data["r"] != review_id:
        return None
    try:
        round_n = int(round_text)
    except (TypeError, ValueError):
        return None
    if data.get("n") != round_n:
        return None
    binding = data.get("s")
    if not isinstance(binding, str) or not binding:
        return None
    if binding == LOCAL and not _within_clock(token):
        return None
    if binding.startswith(USER_PREFIX):
        uid = _account_id(binding)
        if uid is None:
            return None
        if who.principal.signed_in and (who.user is None or int(who.user["id"]) != uid):
            return None
        user = users.get_user(uid)
        if user is None:
            return None
        token_who = auth.Who(auth.principal_for(user), user=user, source="session")
        decision = auth.decide(token_who, review_id, review_exists)
        return Grant(decision, token_who) if access.can(decision, "read") else None
    if who.session is not None:
        if not hmac.compare_digest(binding.encode(), str(who.session["id_hash"]).encode()):
            return None
        return Grant(access.Decision(access.OK, None), who)
    if binding == LOCAL:
        return Grant(access.Decision(access.OK, None), who) if local else None
    row = users.live_session(binding)
    if row is None:
        return None
    token_who = _session_who(row)
    if token_who is None:
        return None
    decision = auth.decide(token_who, review_id, review_exists)
    return Grant(decision, token_who) if access.can(decision, "read") else None
