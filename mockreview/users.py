"""Users, sign-in links, sessions, grants, share links, guests, access tokens, audit and throttling (all in db.py).

There are no passwords. A person signs in by:
- an invite link (single use, 7 days by default): creates a member (or admin) and signs them in;
- a sign-in link from the shell (`mockreview login` / `mockreview admin login-link`): single use, 15 minutes;
- a trusted proxy identity already linked to a user (auth.py decides when a proxy is trusted);
- loopback = owner (auth.py): a request from this computer, not through a proxy, is the install's owner account.

Link tokens are itsdangerous URLSafeTimedSerializer payloads signed with a random key kept in the database. The
signature only proves the server minted the token; single use, revocation and expiry are enforced in SQLite, where only a
hash of the token's nonce is stored. Session ids and access tokens are random and stored hashed too.

Every change to a role, a grant, an invite, a share link or an access token writes an audit row in the same
transaction. The audit actor is an account (`user:<id>`), `cli` or `owner`, never a guest's display name.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config, db, store, streams, wakeguard
from .store import ReviewError

INSTANCE_ROLES = ("admin", "member")
REVIEW_ROLES = ("reviewer", "commenter", "viewer")
SEATS = 25                          # live seats per review, the owner's included
OWNER_SEAT = 1                      # the owner's reserved seat
SESSION_SECONDS = 30 * 86400          # sliding: every use pushes the expiry out again
SESSION_TOUCH_SECONDS = 60            # at most one sliding-expiry write a minute per session
INVITE_DAYS = 7
LOGIN_LINK_SECONDS = 15 * 60
GUEST_SECONDS = 365 * 86400           # a guest session ends with its link generation (or the link's own expiry)
SEAT_IDLE = 900.0                     # seconds: a guest session idle longer than this stops holding a seat
THROTTLE_FAILURES = 10                # failed sign-in / invite attempts per IP per window ...
THROTTLE_WINDOW = 60
THROTTLE_BLOCK = 60                   # ... then this many seconds of 429
MAX_NAME = 60
MAX_GUEST_NAME = 40
GUEST_ADJECTIVES = (
    "Amber", "Blue", "Bold", "Brave", "Bright", "Calm", "Clever", "Coral", "Crimson", "Curious",
    "Daring", "Eager", "Fancy", "Fleet", "Gentle", "Glad", "Golden", "Happy", "Hidden", "Honest",
    "Jolly", "Keen", "Kind", "Lively", "Lucky", "Mellow", "Merry", "Mighty", "Noble", "Olive",
    "Patient", "Playful", "Proud", "Quiet", "Rapid", "Ready", "Rosy", "Rustic", "Silent", "Silver",
    "Sleek", "Snowy", "Soft", "Solar", "Steady", "Sunny", "Swift", "Teal", "Tidy", "Violet",
)
GUEST_ANIMALS = (
    "Badger", "Bear", "Beaver", "Bison", "Crane", "Deer", "Dove", "Eagle", "Elk", "Falcon",
    "Finch", "Fox", "Gecko", "Goose", "Hare", "Hawk", "Heron", "Ibex", "Koala", "Lark",
    "Lemur", "Leopard", "Lion", "Loon", "Lynx", "Marmot", "Moose", "Otter", "Owl", "Panda",
    "Parrot", "Puffin", "Quail", "Rabbit", "Raven", "Robin", "Seal", "Sparrow", "Stag", "Swan",
    "Tiger", "Toucan", "Turtle", "Walrus", "Wren", "Yak", "Zebra", "Antelope", "Bobcat", "Cheetah",
)
UNSET = object()                      # "leave the stored value alone" (expires absent from a link update)
DEAD_LINK = "This link is not valid. Ask the person who sent it for a new one."
OWNER_SOURCE, OWNER_SUBJECT = "loopback", "owner"
SIGNER_MISSING = ("Sign-in links need the itsdangerous package, which is not installed. Run `hermes pm repair` (or "
                  "reinstall the plugin) so Hermes installs the dependencies in its pyproject.toml, then restart Hermes.")


class AuthError(ReviewError):
    pass


def now() -> float:
    return time.time()


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_name(value: Any, limit: int = MAX_NAME) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return re.sub(r"\s+", " ", text)[:limit]


def itsdangerous() -> Any:
    """The itsdangerous module. Imported on use, so the plugin's tools and commands still load in a Hermes that has
    not installed it yet; the server refuses to start without it (auth.check_startup)."""
    try:
        import itsdangerous as module
    except ImportError:
        raise AuthError(SIGNER_MISSING, 503) from None
    return module


def _timed(conn: sqlite3.Connection, purpose: str) -> Any:
    return itsdangerous().URLSafeTimedSerializer(_signing_key(conn), salt="mock-review." + purpose)


def _row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(r) if r is not None else None


# ---------------------------------------------------------------- signing key and audit

def _signing_key(conn: sqlite3.Connection) -> str:
    r = conn.execute("SELECT value FROM meta WHERE key = 'signing_key'").fetchone()
    if r:
        return r["value"]
    key = secrets.token_urlsafe(32)
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('signing_key', ?)", (key,))
    return conn.execute("SELECT value FROM meta WHERE key = 'signing_key'").fetchone()["value"]


def signing_key() -> str:
    with db.tx() as conn:
        return _signing_key(conn)


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str, **detail: Any) -> None:
    conn.execute("INSERT INTO audit (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                 (now(), actor, action, target, json.dumps(detail, sort_keys=True)))


def audit_rows(limit: int = 200) -> List[Dict[str, Any]]:
    rows = db.connect().execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


def record_export(actor: str, review_id: str, fmt: str, round_n: int, comments: bool) -> None:
    """One audit row per export. `actor` is who.actor (an account or guest id), never a display name."""
    with db.tx() as conn:
        audit(conn, actor, "export", f"review:{review_id}", format=fmt, round=round_n, comments=bool(comments))


# ---------------------------------------------------------------- users

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    return _row(db.connect().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def find_user(ref: str) -> Dict[str, Any]:
    """A user by id, email or exact name (the CLI's argument)."""
    conn = db.connect()
    r = None
    if str(ref).isdigit():
        r = conn.execute("SELECT * FROM users WHERE id = ?", (int(ref),)).fetchone()
    if r is None:
        rows = conn.execute("SELECT * FROM users WHERE email = ? OR name = ? COLLATE NOCASE",
                            (str(ref), str(ref))).fetchall()
        if len(rows) > 1:
            raise AuthError(f"{len(rows)} users match {ref!r}: use the id from `mockreview users list`", 409)
        r = rows[0] if rows else None
    if r is None:
        raise AuthError("no such user", 404)
    return dict(r)


def list_users() -> List[Dict[str, Any]]:
    conn = db.connect()
    out = []
    for r in conn.execute("SELECT * FROM users ORDER BY id").fetchall():
        sources = [i["source"] for i in conn.execute(
            "SELECT source FROM identities WHERE user_id = ? ORDER BY id", (r["id"],)).fetchall()]
        out.append({**dict(r), "sources": sources})
    return out


def _admins(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]


def _create_user(conn: sqlite3.Connection, name: str, role: str, source: str, subject: str,
                 email: str = "") -> int:
    if role not in INSTANCE_ROLES:
        raise AuthError("role must be admin or member")
    cur = conn.execute("INSERT INTO users (name, email, role, source, created_at) VALUES (?, ?, ?, ?, ?)",
                       (clean_name(name) or "Member", clean_name(email, 254), role, source, now()))
    uid = int(cur.lastrowid)
    conn.execute("INSERT INTO identities (user_id, source, subject, created_at) VALUES (?, ?, ?, ?)",
                 (uid, source, subject, now()))
    return uid


def owner_user() -> Dict[str, Any]:
    """The install's owner account (loopback = owner and `mockreview login`). Created on first use: as admin when the
    install has no admin yet, else as the account it already is."""
    conn = db.connect()
    r = conn.execute("SELECT u.* FROM users u JOIN identities i ON i.user_id = u.id WHERE i.source = ? AND i.subject = ?",
                     (OWNER_SOURCE, OWNER_SUBJECT)).fetchone()
    if r:
        return dict(r)
    with db.tx() as conn:
        r = conn.execute("SELECT u.* FROM users u JOIN identities i ON i.user_id = u.id "
                         "WHERE i.source = ? AND i.subject = ?", (OWNER_SOURCE, OWNER_SUBJECT)).fetchone()
        if r:
            return dict(r)
        uid = _create_user(conn, "Owner", "admin", OWNER_SOURCE, OWNER_SUBJECT)
        audit(conn, "owner", "user.create", f"user:{uid}", role="admin", source=OWNER_SOURCE)
    return get_user(uid)  # type: ignore[return-value]


def owner_signed_in_elsewhere() -> bool:
    """True once the owner account has signed in from another device.

    The signal is a `login_link.use` audit row for the owner: a sign-in link (`hermes mockreview login`, the one
    setup prints, or the one a review result carries) is the only way a browser that cannot be trusted as loopback
    becomes the owner, and using one records that row in the same transaction that opens the session. The audit log
    is append-only, so the answer stays true after a sign-out, a session expiry or a restart: the device has signed
    in once, which is all the first-run note needs to know. Read fresh on every call, never cached.
    """
    uid = int(owner_user()["id"])
    row = db.connect().execute("SELECT 1 FROM audit WHERE action = 'login_link.use' AND target = ? LIMIT 1",
                               (f"user:{uid}",)).fetchone()
    return row is not None


def user_for_identity(source: str, subject: str) -> Optional[Dict[str, Any]]:
    r = db.connect().execute("SELECT u.* FROM users u JOIN identities i ON i.user_id = u.id "
                             "WHERE i.source = ? AND i.subject = ?", (source, subject)).fetchone()
    return _row(r)


def _identity_user(conn: sqlite3.Connection, identity: Optional[Dict[str, str]]) -> Optional[int]:
    """The account a trusted proxy identity is already linked to, if any."""
    if not identity:
        return None
    r = conn.execute("SELECT user_id FROM identities WHERE source = ? AND subject = ?",
                     (identity["source"], identity["subject"])).fetchone()
    return int(r["user_id"]) if r else None


def _link_identity(conn: sqlite3.Connection, user_id: int, identity: Optional[Dict[str, str]]) -> None:
    """Remember a trusted proxy identity for this user, unless it already belongs to someone."""
    if not identity:
        return
    conn.execute("INSERT OR IGNORE INTO identities (user_id, source, subject, created_at) VALUES (?, ?, ?, ?)",
                 (user_id, identity["source"], identity["subject"], now()))


def set_role(actor: str, user_id: int, role: str) -> Dict[str, Any]:
    if role not in INSTANCE_ROLES:
        raise AuthError("role must be admin or member")
    with db.tx() as conn:
        u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if u is None:
            raise AuthError("no such user", 404)
        if u["role"] == role:
            return dict(u)
        if u["role"] == "admin" and _admins(conn) <= 1:
            raise AuthError("refused: this is the last admin. Promote someone else first.", 409)
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        hashes = [r["id_hash"] for r in conn.execute("SELECT id_hash FROM sessions WHERE user_id = ?", (user_id,))]
        audit(conn, actor, "user.role", f"user:{user_id}", old=u["role"], new=role)
    _close_streams(user_id, hashes)
    return get_user(user_id)  # type: ignore[return-value]


def delete_user(actor: str, user_id: int) -> Dict[str, Any]:
    with db.tx() as conn:
        u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if u is None:
            raise AuthError("no such user", 404)
        if u["role"] == "admin" and _admins(conn) <= 1:
            raise AuthError("refused: this is the only admin. Promote someone else before deleting this account.", 409)
        hashes = [r["id_hash"] for r in conn.execute("SELECT id_hash FROM sessions WHERE user_id = ?", (user_id,))]
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        audit(conn, actor, "user.delete", f"user:{user_id}", role=u["role"])
    _close_streams(user_id, hashes)
    return {"ok": True, "deleted": user_id}


def _close_streams(user_id: int, hashes: List[str]) -> None:
    """End this user's live streams after an access change, so a client that lost access reconnects and is refused
    instead of reading on. A stream signed in by a linked proxy identity has no session hash, so its actor closes it
    too. Callers read the hashes inside their transaction and call this after it commits."""
    streams.close_sessions(hashes)
    streams.close_actor(f"user:{user_id}")


# ---------------------------------------------------------------- invites and sign-in links

def _parse_days(value: Any, default: float) -> float:
    """'7d', '12h', '30m' or a number of days -> seconds."""
    if value in (None, ""):
        return default * 86400
    text = str(value).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([dhm]?)", text)
    if not m:
        raise AuthError("expiry must look like 7d, 12h or 30m")
    n = float(m.group(1)) * {"d": 86400, "h": 3600, "m": 60, "": 86400}[m.group(2)]
    if not 60 <= n <= 365 * 86400:
        raise AuthError("expiry must be between 1 minute and 365 days")
    return n


def parse_duration(value: Any, default_days: float) -> float:
    return _parse_days(value, default_days)


def _mint(conn: sqlite3.Connection, purpose: str, created_by: str, expires_at: float, **cols: Any) -> Tuple[int, str]:
    nonce = secrets.token_urlsafe(24)
    names = ["nonce_hash", "purpose", "created_by", "created_at", "expires_at"] + list(cols)
    values = [digest(nonce), purpose, created_by, now(), expires_at] + list(cols.values())
    cur = conn.execute(f"INSERT INTO invites ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", values)
    token = _timed(conn, purpose).dumps({"i": int(cur.lastrowid), "n": nonce})
    return int(cur.lastrowid), token


def create_invite(actor: str, role: str = "member", review_id: Optional[str] = None, review_role: Optional[str] = None,
                  all_reviews: bool = False, expires: Any = None, label: str = "",
                  grant_expires: Any = None) -> Dict[str, Any]:
    """A single-use invite. With review_id (or all_reviews) the new account also gets that grant (review_role,
    default commenter). A Reviewer `review_role` at mint is the owner's confirmation that this person may wake Hermes,
    stored on the invite and copied to the grant on redemption; any other role records no confirmation. Returns the
    link path (`/invite/<token>`) once; only a hash of its nonce is kept."""
    if role not in INSTANCE_ROLES:
        raise AuthError("role must be admin or member")
    if review_id and all_reviews:
        raise AuthError("pass --review or --all-reviews, not both")
    if review_role is not None and review_role not in REVIEW_ROLES:
        raise AuthError("review role must be reviewer, commenter or viewer")
    if (review_id or all_reviews) and review_role is None:
        review_role = "commenter"
    if review_role and not (review_id or all_reviews):
        raise AuthError("a review role needs --review <id> or --all-reviews")
    ttl = _parse_days(expires, INVITE_DAYS)
    grant_until = now() + _parse_days(grant_expires, 0) if grant_expires else None
    with db.tx() as conn:
        iid, token = _mint(conn, "invite", actor, now() + ttl, role=role, review_id=review_id,
                           all_reviews=1 if all_reviews else 0, review_role=review_role,
                           grant_expires_at=grant_until, label=clean_name(label),
                           wake_ok=1 if review_role == "reviewer" else 0)
        audit(conn, actor, "invite.create", f"invite:{iid}", role=role, review_id=review_id,
              all_reviews=bool(all_reviews), review_role=review_role, expires_at=now() + ttl,
              wake_ok=review_role == "reviewer")
    return {"ok": True, "id": iid, "path": f"/invite/{token}", "role": role, "review_id": review_id,
            "review_role": review_role, "expires_at": now() + ttl}


def list_invites() -> List[Dict[str, Any]]:
    rows = db.connect().execute(
        "SELECT id, role, review_id, all_reviews, review_role, label, created_by, created_at, expires_at, used_at, "
        "used_by, revoked_at FROM invites WHERE purpose = 'invite' ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["state"] = ("used" if d["used_at"] else "revoked" if d["revoked_at"]
                      else "expired" if d["expires_at"] <= now() else "open")
        out.append(d)
    return out


def revoke_invite(actor: str, invite_id: int) -> Dict[str, Any]:
    with db.tx() as conn:
        cur = conn.execute("UPDATE invites SET revoked_at = ? WHERE id = ? AND purpose = 'invite' "
                           "AND used_at IS NULL AND revoked_at IS NULL", (now(), invite_id))
        if cur.rowcount != 1:
            raise AuthError("no open invite with that id", 404)
        audit(conn, actor, "invite.revoke", f"invite:{invite_id}")
    return {"ok": True, "revoked": invite_id}


def create_login_link(actor: str, user_id: Optional[int] = None, ttl: float = LOGIN_LINK_SECONDS) -> Dict[str, Any]:
    """A single-use sign-in link for an existing account (default: the owner account)."""
    uid = user_id if user_id is not None else owner_user()["id"]
    if get_user(uid) is None:
        raise AuthError("no such user", 404)
    with db.tx() as conn:
        iid, token = _mint(conn, "login", actor, now() + ttl, role="member", login_user_id=uid)
        audit(conn, actor, "login_link.create", f"user:{uid}", link=iid)
    return {"ok": True, "path": f"/login/{token}", "user_id": uid, "expires_at": now() + ttl}


def check_link(purpose: str, token: str) -> Dict[str, Any]:
    """The open invite / sign-in link row for this token, or AuthError(DEAD_LINK). Read only."""
    row = _link_row(db.connect(), purpose, token)
    if row is None:
        raise AuthError(DEAD_LINK, 404)
    return row


def _link_row(conn: sqlite3.Connection, purpose: str, token: str) -> Optional[Dict[str, Any]]:
    try:
        data = _timed(conn, purpose).loads(token, max_age=366 * 86400)
        iid, nonce = int(data["i"]), str(data["n"])
    except (itsdangerous().BadSignature, KeyError, TypeError, ValueError):
        return None
    r = conn.execute("SELECT * FROM invites WHERE id = ? AND purpose = ?", (iid, purpose)).fetchone()
    if r is None or not secrets.compare_digest(r["nonce_hash"], digest(nonce)):
        return None
    if r["used_at"] is not None or r["revoked_at"] is not None or r["expires_at"] <= now():
        return None
    return dict(r)


def redeem(purpose: str, token: str, name: str = "", identity: Optional[Dict[str, str]] = None,
           user_agent: str = "") -> Dict[str, Any]:
    """Use an invite or sign-in link once: marks it used, creates the account (invite) and its grant, and opens a
    session. An invite also links its trusted proxy identity. A sign-in link deliberately leaves the proxy identity
    alone: it only proves one sign-in, so linking it would sign the device back in on every later request and
    "sign out everywhere" could never revoke it. Any failure is the same AuthError(DEAD_LINK).

    A sign-in link used through a proxy identity that belongs to another account is refused and stays usable: the
    session would be for the link's account while every request names the other one. An invite to an account that
    already exists (its proxy identity is linked) never demotes it: an admin invite promotes a member, any other
    keeps the role it has."""
    conflict: Dict[str, Any] = {}
    try:
        with db.tx() as conn:
            row = _link_row(conn, purpose, token)
            if row is None:
                raise AuthError(DEAD_LINK, 404)
            held_by = _identity_user(conn, identity)
            if purpose == "login" and held_by is not None and held_by != int(row["login_user_id"]):
                conflict.update(actor=f"user:{held_by}", target=f"user:{row['login_user_id']}", link=row["id"],
                                source=(identity or {}).get("source"))
                raise AuthError(DEAD_LINK, 404)
            if conn.execute("UPDATE invites SET used_at = ? WHERE id = ? AND used_at IS NULL",
                            (now(), row["id"])).rowcount != 1:
                raise AuthError(DEAD_LINK, 404)
            if purpose == "login":
                uid = int(row["login_user_id"])
                audit(conn, f"user:{uid}", "login_link.use", f"user:{uid}", link=row["id"])
            else:
                if held_by is not None:
                    uid = held_by
                    old_role = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()["role"]
                    if row["role"] == "admin" and old_role != "admin":
                        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (uid,))
                        audit(conn, row["created_by"], "user.role", f"user:{uid}", old=old_role, new="admin",
                              invite=row["id"])
                else:
                    src, sub = ((identity["source"], identity["subject"]) if identity
                                else ("invite", secrets.token_hex(8)))
                    uid = _create_user(conn, clean_name(name) or (identity or {}).get("name") or "Member",
                                       row["role"], src, sub, (identity or {}).get("email", ""))
                    audit(conn, f"user:{uid}", "user.create", f"user:{uid}", role=row["role"], source=src,
                          invite=row["id"])
                if row["review_role"]:
                    _grant(conn, f"invite:{row['id']}", uid, row["review_role"], row["review_id"],
                           bool(row["all_reviews"]), row["grant_expires_at"], wake_ok=bool(row["wake_ok"]))
                audit(conn, f"user:{uid}", "invite.use", f"invite:{row['id']}")
            conn.execute("UPDATE invites SET used_by = ? WHERE id = ?", (uid, row["id"]))
            token_, csrf = _new_session(conn, user_id=uid, user_agent=user_agent)
    except AuthError:
        if conflict:      # the refusal's own transaction rolled back: record it on its own
            with db.tx() as conn:
                audit(conn, conflict.pop("actor"), "login_link.identity_conflict", conflict.pop("target"), **conflict)
        raise
    return {"ok": True, "user_id": uid, "session": token_, "csrf": csrf, "review_id": row["review_id"]}


# ---------------------------------------------------------------- sessions

def _new_session(conn: sqlite3.Connection, user_id: Optional[int] = None, guest_id: Optional[str] = None,
                 user_agent: str = "", lifetime: float = SESSION_SECONDS) -> Tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    t = now()
    conn.execute("INSERT INTO sessions (id_hash, user_id, guest_id, csrf, created_at, last_seen, expires_at, "
                 "user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (digest(token), user_id, guest_id, csrf, t, t, t + lifetime, clean_name(user_agent, 200)))
    return token, csrf


def session(token: str) -> Optional[Dict[str, Any]]:
    """The live session for a cookie value, touched at most once a minute: a user session's sliding expiry is pushed
    out (and its cookie re-issued), while a guest session only records that it was used, because its expiry ends with
    the share link generation and must never move."""
    if not token or len(token) > 200:
        return None
    conn = db.connect()
    r = conn.execute("SELECT * FROM sessions WHERE id_hash = ?", (digest(token),)).fetchone()
    if r is None or r["expires_at"] <= now():
        return None
    s = dict(r)
    s["renewed"] = False
    if now() - s["last_seen"] >= SESSION_TOUCH_SECONDS:
        t = now()
        with db.tx() as c:
            if s["user_id"] is not None:
                c.execute("UPDATE sessions SET last_seen = ?, expires_at = ? WHERE id_hash = ?",
                          (t, t + SESSION_SECONDS, s["id_hash"]))
                s.update(last_seen=t, expires_at=t + SESSION_SECONDS, renewed=True)  # the cookie's Max-Age moves too
            else:
                # A guest's expiry never moves (it ends with the link generation); only its seat's idleness is read.
                c.execute("UPDATE sessions SET last_seen = ? WHERE id_hash = ?", (t, s["id_hash"]))
                s.update(last_seen=t)
    return s


def session_row(token: str) -> Optional[Dict[str, Any]]:
    """The stored session a cookie value names, live or not, without touching it (sign-out ends it even when a proxy
    identity, not this cookie, signed the request in)."""
    if not token or len(token) > 200:
        return None
    return _row(db.connect().execute("SELECT id_hash, user_id, guest_id FROM sessions WHERE id_hash = ?",
                                     (digest(token),)).fetchone())


def live_session(id_hash: str) -> Optional[Dict[str, Any]]:
    """A session by its id_hash that has not expired. session_row() deliberately does not check expiry (sign-out must
    still find the dead row it is ending); a frame token must only ever resolve a live one."""
    if not id_hash or len(id_hash) > 64:
        return None
    return _row(db.connect().execute("SELECT * FROM sessions WHERE id_hash = ? AND expires_at > ?",
                                     (id_hash, now())).fetchone())


def revoked_guest_session(token: str) -> Optional[str]:
    """The review a revoked guest session belonged to, if this cookie was one: its access is dead, not unknown."""
    if not token or len(token) > 200:
        return None
    r = db.connect().execute("SELECT review_id FROM revoked_sessions WHERE id_hash = ?",
                             (digest(token),)).fetchone()
    return r["review_id"] if r is not None else None


def list_sessions(user_id: int, current_hash: str = "") -> List[Dict[str, Any]]:
    rows = db.connect().execute("SELECT id_hash, created_at, last_seen, expires_at, user_agent FROM sessions "
                                "WHERE user_id = ? AND expires_at > ? ORDER BY last_seen DESC",
                                (user_id, now())).fetchall()
    return [{"id": r["id_hash"][:16], "created_at": r["created_at"], "last_seen": r["last_seen"],
             "device": r["user_agent"], "current": r["id_hash"] == current_hash} for r in rows]


def end_session(id_hash: str) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
    streams.close_sessions([id_hash])


def sign_out_others(user_id: int, current_hash: str = "") -> Dict[str, Any]:
    """End every other session of this user and drop their live streams."""
    with db.tx() as conn:
        hashes = [r["id_hash"] for r in conn.execute(
            "SELECT id_hash FROM sessions WHERE user_id = ? AND id_hash != ?", (user_id, current_hash))]
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND id_hash != ?", (user_id, current_hash))
        audit(conn, f"user:{user_id}", "session.sign_out_others", f"user:{user_id}", count=len(hashes))
    streams.close_sessions(hashes)
    return {"ok": True, "signed_out": len(hashes)}


# ---------------------------------------------------------------- grants

def _grant(conn: sqlite3.Connection, actor: str, user_id: int, role: str, review_id: Optional[str],
           all_reviews: bool, expires_at: Optional[float], *, wake_ok: bool) -> None:
    """Write one grant. `wake_ok` is the owner's confirmation and is never derived here: a fresh `users.grant`
    supplies it, a redemption copies the invite's stored value (0 for an invite minted before the opt-in)."""
    if role not in REVIEW_ROLES:
        raise AuthError("review role must be reviewer, commenter or viewer")
    if bool(review_id) == bool(all_reviews):
        raise AuthError("a grant is for one review (--review) or for all reviews (--all-reviews)")
    conn.execute("DELETE FROM grants WHERE user_id = ? AND COALESCE(review_id, '*') = ?",
                 (user_id, review_id or "*"))
    conn.execute("INSERT INTO grants (review_id, all_reviews, user_id, role, expires_at, granted_by, created_at, "
                 "wake_ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (review_id, 1 if all_reviews else 0, user_id, role, expires_at, actor, now(),
                  1 if wake_ok else 0))
    audit(conn, actor, "grant.set", f"user:{user_id}", review_id=review_id, all_reviews=bool(all_reviews), role=role,
          expires_at=expires_at, wake_ok=bool(wake_ok))


def grant(actor: str, user_id: int, role: str, review_id: Optional[str] = None, all_reviews: bool = False,
          expires: Any = UNSET) -> Dict[str, Any]:
    """Give a person a role. `expires` absent (UNSET) keeps the grant's stored expiry; present and None / "" / "never"
    means it never expires."""
    if get_user(user_id) is None:
        raise AuthError("no such user", 404)
    with db.tx() as conn:
        if expires is UNSET:
            row = conn.execute("SELECT expires_at FROM grants WHERE user_id = ? AND COALESCE(review_id, '*') = ?",
                               (user_id, review_id or "*")).fetchone()
            until = row["expires_at"] if row is not None else None
        else:
            until = None if not expires or str(expires).strip().lower() == "never" else \
                now() + _parse_days(expires, 0)
        _grant(conn, actor, user_id, role, review_id, all_reviews, until, wake_ok=role == "reviewer")
        hashes = [r["id_hash"] for r in conn.execute("SELECT id_hash FROM sessions WHERE user_id = ?", (user_id,))]
    _close_streams(user_id, hashes)      # a re-grant can be a downgrade; closing only costs the client a reconnect
    return {"ok": True, "user_id": user_id, "review_id": review_id, "all_reviews": bool(all_reviews), "role": role,
            "expires_at": until}


def revoke_grant(actor: str, user_id: int, review_id: Optional[str] = None, all_reviews: bool = False) -> Dict[str, Any]:
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM grants WHERE user_id = ? AND COALESCE(review_id, '*') = ?",
                           (user_id, review_id or "*"))
        if cur.rowcount == 0:
            raise AuthError("no such grant", 404)
        hashes = [r["id_hash"] for r in conn.execute("SELECT id_hash FROM sessions WHERE user_id = ?", (user_id,))]
        audit(conn, actor, "grant.revoke", f"user:{user_id}", review_id=review_id, all_reviews=bool(all_reviews))
    _close_streams(user_id, hashes)
    return {"ok": True}


def grant_role(user_id: int, review_id: str) -> Optional[str]:
    """The user's live role on this review (a review's own grant wins over an all-reviews grant), read fresh."""
    rows = db.connect().execute(
        "SELECT role, review_id FROM grants WHERE user_id = ? AND (review_id = ? OR all_reviews = 1) "
        "AND (expires_at IS NULL OR expires_at > ?) ORDER BY review_id IS NULL", (user_id, review_id, now())).fetchall()
    return rows[0]["role"] if rows else None


def granted_review_ids(user_id: int) -> Tuple[bool, List[str]]:
    rows = db.connect().execute("SELECT review_id, all_reviews FROM grants WHERE user_id = ? AND "
                                "(expires_at IS NULL OR expires_at > ?)", (user_id, now())).fetchall()
    return any(r["all_reviews"] for r in rows), [r["review_id"] for r in rows if r["review_id"]]


def wake_reason(review_id: str, actor: str) -> str:
    """Why this actor's current access to the review does not include the owner's confirmation that its Reviewer role
    may wake Hermes, or "" when it does. Read fresh from the database.

    An admin is the owner. A member follows the effective live grant, the review's own winning over an all-reviews one
    (as in grant_role). A guest needs one row that binds the guest to the review, its link generation, an active and
    unexpired link that still admits `anyone`, and the link's confirmation; a guest row whose link stopped, rotated,
    expired or turned members-only answers REVIEWER_WAKE_ENDED, and a live link without confirmation answers
    REVIEWER_WAKE_UNCONFIRMED. Anything else, or an unreadable row, fails closed with REVIEWER_WAKE_UNCONFIRMED."""
    unconfirmed = wakeguard.REVIEWER_WAKE_UNCONFIRMED
    if not isinstance(actor, str):
        return unconfirmed
    try:
        conn = db.connect()
        if actor.startswith("guest:"):
            row = conn.execute(
                "SELECT g.generation AS guest_generation, l.generation AS link_generation, l.active, l.expires_at, "
                "l.audience, l.wake_ok FROM guests g JOIN share_links l ON l.review_id = g.review_id "
                "WHERE g.id = ? AND g.review_id = ?", (actor[6:], review_id)).fetchone()
            if row is None:
                return wakeguard.REVIEWER_WAKE_ENDED
            if (int(row["active"] or 0) != 1
                    or int(row["guest_generation"]) != int(row["link_generation"])
                    or (row["expires_at"] is not None and float(row["expires_at"]) <= now())
                    or row["audience"] != "anyone"):
                return wakeguard.REVIEWER_WAKE_ENDED
            return "" if int(row["wake_ok"] or 0) == 1 else unconfirmed
        if actor.startswith("user:"):
            uid = actor[5:]
            if not uid.isdigit():
                return unconfirmed
            user = conn.execute("SELECT role FROM users WHERE id = ?", (int(uid),)).fetchone()
            if user is None:
                return unconfirmed
            if user["role"] == "admin":
                return ""
            grant = conn.execute(
                "SELECT wake_ok FROM grants WHERE user_id = ? AND (review_id = ? OR all_reviews = 1) "
                "AND (expires_at IS NULL OR expires_at > ?) ORDER BY review_id IS NULL",
                (int(uid), review_id, now())).fetchone()
            return "" if grant is not None and int(grant["wake_ok"] or 0) == 1 else unconfirmed
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return unconfirmed
    return unconfirmed


def wake_ok_for(review_id: str, actor: str) -> bool:
    """Whether this actor's current access to the review includes the owner's confirmation that its Reviewer role may
    wake Hermes (`wake_reason` is empty)."""
    return wake_reason(review_id, actor) == ""


# ---------------------------------------------------------------- share links and guests

def _live_guest_seats(conn: sqlite3.Connection, review_id: str) -> int:
    """The seats this review's guests hold: a guest whose session was used in the last SEAT_IDLE seconds. One count
    for both seats() and join_as_guest(), so the two can never drift."""
    return int(conn.execute(
        "SELECT COUNT(DISTINCT g.id) FROM guests g JOIN sessions s ON s.guest_id = g.id "
        "WHERE g.review_id = ? AND s.expires_at > ? AND s.last_seen > ?",
        (review_id, now(), now() - SEAT_IDLE)).fetchone()[0])


def seats(review_id: str) -> Dict[str, int]:
    """Live seats on this review: the owner's reserved one plus each guest whose session was used in the last
    SEAT_IDLE seconds. A guest idle longer than that may briefly push a review over SEATS when it comes back: an
    over-admission, never the lockout every stored guest session used to cause."""
    live = _live_guest_seats(db.connect(), review_id)
    return {"live": OWNER_SEAT + live, "max": SEATS}


def review_people(review_id: str) -> List[Dict[str, Any]]:
    """Who is on this review: every admin as the owner, then every live grant (a review's own or all reviews).
    `direct_*` is the review-scoped grant, `inherited_*` the all-reviews one, and `role`/`expires_at` the effective
    access (the review's own grant wins, as in grant_role). No session, token or hash data."""
    conn = db.connect()
    admins = conn.execute("SELECT id, name, email FROM users WHERE role = 'admin' ORDER BY name COLLATE NOCASE, id"
                          ).fetchall()
    listed = {int(r["id"]) for r in admins}
    people = [{"user_id": int(r["id"]), "name": r["name"], "email": r["email"], "role": "owner",
               "expires_at": None, "all_reviews": False, "direct_role": None, "direct_expires_at": None,
               "inherited_role": None, "inherited_expires_at": None, "wake_ok": True} for r in admins]
    grants = conn.execute(
        "SELECT g.user_id, g.role, g.expires_at, g.all_reviews, g.review_id, g.wake_ok, u.name, u.email "
        "FROM grants g JOIN users u ON u.id = g.user_id WHERE (g.review_id = ? OR g.all_reviews = 1) "
        "AND (g.expires_at IS NULL OR g.expires_at > ?) ORDER BY g.review_id IS NULL, u.name COLLATE NOCASE, u.id",
        (review_id, now())).fetchall()
    by_user: Dict[int, Dict[str, Any]] = {}
    for r in grants:
        uid = int(r["user_id"])
        if uid in listed:
            continue
        entry = by_user.setdefault(uid, {"user_id": uid, "name": r["name"], "email": r["email"],
                                         "direct": None, "inherited": None})
        grant_ = {"role": r["role"], "expires_at": r["expires_at"], "wake_ok": bool(int(r["wake_ok"] or 0))}
        if r["all_reviews"]:
            entry["inherited"] = grant_
        elif r["review_id"] == review_id:
            entry["direct"] = grant_
    for entry in by_user.values():
        direct, inherited = entry.pop("direct"), entry.pop("inherited")
        effective = direct or inherited
        people.append({"user_id": entry["user_id"], "name": entry["name"], "email": entry["email"],
                       "role": effective["role"], "expires_at": effective["expires_at"],
                       "all_reviews": inherited is not None,
                       "direct_role": direct["role"] if direct else None,
                       "direct_expires_at": direct["expires_at"] if direct else None,
                       "inherited_role": inherited["role"] if inherited else None,
                       "inherited_expires_at": inherited["expires_at"] if inherited else None,
                       "wake_ok": effective["wake_ok"]})
    return people


def share_base_url(review_id: str) -> str:
    """The one place a share link's base is decided, from the review's own host mode:

    - `local`: `config.local_link_base()`, the same base `store.review_url()` gives a local review, so the review and
      its share link agree (it follows `lan_address` when the board is reachable beyond loopback). Never a public
      or Funnel host.
    - `tailnet` (stored as `tailscale`): `config.public_base()`, the tailnet name. Never `public_url` or the Funnel
      host.
    - `public`: `config.public_link_base()`: `public_url`, then the Funnel URL, then `public_base`.
    - unknown or missing review (a link row read after its review went away): the public walk, then the local base.
      Nothing else builds a share URL."""
    try:
        mode = store.load(review_id).get("mode", "")
    except (ReviewError, ValueError, OSError, KeyError, TypeError):
        mode = ""
    if mode == "local":
        base = config.local_link_base()
    elif mode in ("tailnet", "tailscale"):
        base = config.public_base() or config.local_link_base()
    elif mode == "public":
        base = config.public_link_base() or config.local_link_base()
    else:
        base = config.public_link_base() or config.public_base() or config.local_link_base()
    return base.rstrip("/")


def share_url(review_id: str, path: str) -> str:
    return share_base_url(review_id) + path


def _share_serializer(conn: sqlite3.Connection) -> Any:
    return itsdangerous().URLSafeSerializer(_signing_key(conn), salt="mock-review.share")


def _link_path(conn: sqlite3.Connection, review_id: str, generation: int) -> str:
    return "/s/" + _share_serializer(conn).dumps([review_id, generation])


def _link_row_dict(conn: sqlite3.Connection, r: sqlite3.Row, url: bool = True) -> Dict[str, Any]:
    path = _link_path(conn, r["review_id"], r["generation"])
    row = {"review_id": r["review_id"], "guest_role": r["guest_role"], "generation": r["generation"],
           "expires_at": r["expires_at"], "audience": r["audience"], "round": r["round"],
           "wake_ok": bool(int(r["wake_ok"] or 0)), "path": path}
    if url:
        row["url"] = share_url(r["review_id"], path)
    return row


def share_link(review_id: str) -> Optional[Dict[str, Any]]:
    conn = db.connect()
    r = conn.execute("SELECT * FROM share_links WHERE review_id = ?", (review_id,)).fetchone()
    if r is None or not r["active"]:
        return None
    return _link_row_dict(conn, r)


def _audience(value: Any) -> str:
    if value not in ("anyone", "members"):
        raise AuthError("audience must be anyone or members")
    return str(value)


def _version(value: Any) -> str:
    """The link's version picker: "latest" or a round number as text."""
    text = str(value)
    if text == "latest" or (text.isdecimal() and 1 <= int(text) <= 1000):
        return text
    raise AuthError("round must be latest or a round number between 1 and 1000")


def _set_link(actor: str, review_id: str, action: str, guest_role: Optional[str] = None,
              expires: Any = UNSET, audience: Optional[str] = None, round: Optional[str] = None) -> Dict[str, Any]:
    """Create, regenerate, update or stop the review's one link.

    `expires` absent (UNSET) leaves the stored expiry alone; present and None / "" / "never" means it never expires.
    `audience` and `round` are validated here; the server also checks the round exists in the review. A change to the
    stored `guest_role` or `round` closes the review's open guest streams, so the board reconnects under the new scope.
    Passing `guest_role` also records the owner's confirmation that a Reviewer link may wake Hermes (and clears it for
    any other role); leaving it out keeps the stored role and its confirmation."""
    if guest_role is not None and guest_role not in REVIEW_ROLES:
        raise AuthError("guest role must be reviewer, commenter or viewer")
    if audience is not None:
        audience = _audience(audience)
    if round is not None:
        round = _version(round)
    until = None if expires is UNSET else (None if expires in (None, "", "never") else now() + _parse_days(expires, 0))
    old_generation = None
    revoke_guests = False
    guests_changed = False
    hashes = []
    with db.tx() as conn:
        r = conn.execute("SELECT * FROM share_links WHERE review_id = ?", (review_id,)).fetchone()
        before = (r["guest_role"], r["round"]) if r is not None else None
        if action == "create" and r is not None and r["active"]:
            result = {"ok": True, **_link_row_dict(conn, r, url=False)}
        else:
            if action == "create":
                gen = (r["generation"] + 1) if r is not None else 1
                role = guest_role or "commenter"
                aud = audience or "anyone"
                revoke_guests = aud == "members"
                version = round or "latest"
                wake = 1 if role == "reviewer" else 0
                conn.execute("INSERT OR REPLACE INTO share_links (review_id, active, audience, guest_role, "
                             "expires_at, generation, round, created_by, created_at, wake_ok) "
                             "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (review_id, aud, role, until, gen, version, actor, now(), wake))
                audit(conn, actor, "share.create", f"review:{review_id}", guest_role=role, generation=gen,
                      expires_at=until, audience=aud, round=version)
            elif r is None or not r["active"]:
                raise AuthError("this review has no share link", 404)
            elif action == "regenerate":
                old_generation = r["generation"]
                role = guest_role or r["guest_role"]
                stored_expiry = until if expires is not UNSET else r["expires_at"]
                aud = audience or r["audience"]
                revoke_guests = aud == "members"
                version = round or r["round"]
                wake = (1 if role == "reviewer" else 0) if guest_role is not None else int(r["wake_ok"])
                conn.execute("UPDATE share_links SET generation = generation + 1, guest_role = ?, expires_at = ?, "
                             "audience = ?, round = ?, wake_ok = ? WHERE review_id = ?",
                             (role, stored_expiry, aud, version, wake, review_id))
                audit(conn, actor, "share.regenerate", f"review:{review_id}", generation=r["generation"] + 1,
                      guest_role=role, expires_at=stored_expiry, audience=aud, round=version)
            elif action == "update":
                role = guest_role or r["guest_role"]
                stored_expiry = until if expires is not UNSET else r["expires_at"]
                aud = audience or r["audience"]
                revoke_guests = aud == "members"
                version = round or r["round"]
                wake = (1 if role == "reviewer" else 0) if guest_role is not None else int(r["wake_ok"])
                conn.execute("UPDATE share_links SET guest_role = ?, expires_at = ?, audience = ?, round = ?, "
                             "wake_ok = ? WHERE review_id = ?", (role, stored_expiry, aud, version, wake, review_id))
                audit(conn, actor, "share.update", f"review:{review_id}", guest_role=role, expires_at=stored_expiry,
                      audience=aud, round=version)
            elif action == "stop":
                old_generation = r["generation"]
                conn.execute("UPDATE share_links SET active = 0 WHERE review_id = ?", (review_id,))
                audit(conn, actor, "share.stop", f"review:{review_id}", generation=r["generation"])
            else:
                raise AuthError("action must be create, regenerate, update or stop")
            if old_generation is not None or revoke_guests:
                hashes = [x["id_hash"] for x in conn.execute(
                    "SELECT s.id_hash FROM sessions s JOIN guests g ON g.id = s.guest_id WHERE g.review_id = ?",
                    (review_id,))]
                if revoke_guests:
                    conn.execute("DELETE FROM revoked_sessions WHERE revoked_at < ?", (now() - 31 * 86400,))
                    conn.executemany("INSERT OR REPLACE INTO revoked_sessions (id_hash, review_id, revoked_at) "
                                     "VALUES (?, ?, ?)", [(h, review_id, now()) for h in hashes])
                    conn.executemany("DELETE FROM sessions WHERE id_hash = ?", [(h,) for h in hashes])
                    conn.execute("DELETE FROM guests WHERE review_id = ?", (review_id,))
                else:
                    conn.execute("DELETE FROM guests WHERE review_id = ? AND generation <= ?",
                                 (review_id, old_generation))
            r2 = conn.execute("SELECT * FROM share_links WHERE review_id = ?", (review_id,)).fetchone()
            result = {"ok": True, **_link_row_dict(conn, r2, url=False)} if r2["active"] else \
                {"ok": True, "stopped": True}
            if before is not None and (r2["guest_role"], r2["round"]) != before:
                guests_changed = True
            if revoke_guests:
                generation = int(r2["generation"])
    if result.get("path"):
        result["url"] = share_url(review_id, result["path"])
    if old_generation is not None or revoke_guests:
        streams.close_sessions(hashes)
        streams.close_link(review_id, old_generation if old_generation is not None else generation)
    if guests_changed:
        streams.close_guests(review_id)
    return result


# Every table that carries a review id, in the order purge_review clears them. `comments` and `mentions` have no review
# id of their own: they go with their thread (ON DELETE CASCADE), and a guest's `sessions` go with the guest. `grants`
# rows with all_reviews = 1 and the `audit` log are not about one review, so they stay.
REVIEW_TABLES = ("threads", "reads", "events", "guests", "revoked_sessions", "share_links", "invites", "grants")


def purge_review(review_id: str, actor: str = "owner") -> Dict[str, int]:
    """Delete everything the access database holds for one review, in one transaction, and end its guests' live
    streams. The streams end immediately only in the board server's own process, where the live-stream closer is
    installed. Called from the Desktop backend or the CLI, which may run in another process, the streams end at their
    own access recheck (`live.py` `RECHECK_EVERY`, 5 seconds). Returns how many rows each table lost (comments and
    mentions counted before they cascade). Safe to call again: a review with no rows deletes nothing."""
    store.validate_id(review_id)
    with db.tx() as conn:
        counts = {
            "comments": int(conn.execute("SELECT COUNT(*) FROM comments WHERE thread_id IN "
                                         "(SELECT id FROM threads WHERE review_id = ?)", (review_id,)).fetchone()[0]),
            "mentions": int(conn.execute("SELECT COUNT(*) FROM mentions WHERE comment_id IN (SELECT c.id FROM comments c "
                                         "JOIN threads t ON t.id = c.thread_id WHERE t.review_id = ?)",
                                         (review_id,)).fetchone()[0]),
        }
        hashes = [x["id_hash"] for x in conn.execute(
            "SELECT s.id_hash FROM sessions s JOIN guests g ON g.id = s.guest_id WHERE g.review_id = ?", (review_id,))]
        for table in REVIEW_TABLES:
            counts[table] = conn.execute(f"DELETE FROM {table} WHERE review_id = ?", (review_id,)).rowcount
        audit(conn, actor, "review.delete", f"review:{review_id}", **{k: v for k, v in counts.items() if v})
    streams.close_sessions(hashes)
    streams.close_guests(review_id)
    return counts


def share_link_from(conn: sqlite3.Connection, r: sqlite3.Row) -> Dict[str, Any]:
    return _link_row_dict(conn, r)


def share(actor: str, review_id: str, action: str = "create", guest_role: Optional[str] = None,
          expires: Any = UNSET, audience: Optional[str] = None, round: Optional[str] = None) -> Dict[str, Any]:
    return _set_link(actor, review_id, action, guest_role, expires, audience, round)


def resolve_share_token(token: str) -> Optional[Dict[str, Any]]:
    """The live share link this token names (its review id and generation must be the current ones), else None."""
    conn = db.connect()
    try:
        rid, gen = _share_serializer(conn).loads(token)
        rid, gen = str(rid), int(gen)
    except (itsdangerous().BadSignature, TypeError, ValueError):
        return None
    r = conn.execute("SELECT * FROM share_links WHERE review_id = ?", (rid,)).fetchone()
    if r is None or not r["active"] or r["generation"] != gen:
        return None
    if r["expires_at"] is not None and r["expires_at"] <= now():
        return None
    return dict(r)


def resolve_link_row(review_id: str) -> Optional[Dict[str, Any]]:
    """The review's live share link row: active and not expired."""
    r = db.connect().execute("SELECT * FROM share_links WHERE review_id = ?", (review_id,)).fetchone()
    if r is None or not r["active"] or (r["expires_at"] is not None and r["expires_at"] <= now()):
        return None
    return dict(r)


def _guest_name(conn: sqlite3.Connection, review_id: str) -> str:
    """A cosmetic Adjective Animal name, unique among this review's guests that still hold a live session."""
    taken = {str(r["display_name"]) for r in conn.execute(
        "SELECT DISTINCT g.display_name FROM guests g JOIN sessions s ON s.guest_id = g.id "
        "WHERE g.review_id = ? AND s.expires_at > ?", (review_id, now()))}
    base = ""
    for _ in range(8):
        name = secrets.choice(GUEST_ADJECTIVES) + " " + secrets.choice(GUEST_ANIMALS)
        if name not in taken:
            return name
        base = name
    if not base:
        base = secrets.choice(GUEST_ADJECTIVES) + " " + secrets.choice(GUEST_ANIMALS)
    for n in range(2, 100):
        name = f"{base} {n}"
        if name not in taken:
            return name[:MAX_GUEST_NAME]
    return base[:MAX_GUEST_NAME]


def join_as_guest(link: Dict[str, Any], user_agent: str = "") -> Dict[str, Any]:
    """A new guest of this link generation: a server-assigned connection id and a session bound to the generation.
    Refused once every live seat is taken (the owner's is reserved). A seat is a guest whose session was used in the
    last SEAT_IDLE seconds, so a drive-by fetch cannot hold one for a year; an idle guest that comes back may
    briefly push the review over SEATS (an over-admission, never a lockout)."""
    gid = "g_" + secrets.token_urlsafe(12)
    lifetime = GUEST_SECONDS if link.get("expires_at") is None else max(60.0, link["expires_at"] - now())
    with db.tx() as conn:
        live = _live_guest_seats(conn, link["review_id"])
        if OWNER_SEAT + live >= SEATS:
            raise AuthError("This link is full. Ask the owner for a new one.", 409)
        name = _guest_name(conn, link["review_id"])
        conn.execute("INSERT INTO guests (id, review_id, generation, display_name, created_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (gid, link["review_id"], link["generation"], name, now()))
        token, csrf = _new_session(conn, guest_id=gid, user_agent=user_agent, lifetime=lifetime)
    return {"guest_id": gid, "session": token, "csrf": csrf}


def guest(guest_id: str) -> Optional[Dict[str, Any]]:
    return _row(db.connect().execute("SELECT * FROM guests WHERE id = ?", (guest_id,)).fetchone())


def set_guest_name(guest_id: str, name: Any) -> Dict[str, Any]:
    """The name other people see next to a guest's comments. Cosmetic: never used for access or audit."""
    value = clean_name(name, MAX_GUEST_NAME)
    with db.tx() as conn:
        conn.execute("UPDATE guests SET display_name = ? WHERE id = ?", (value, guest_id))
    return {"ok": True, "name": value}


# ---------------------------------------------------------------- access tokens (MCP)

TOKEN_PREFIX = "mrp_"


def create_token(user_id: int, name: str) -> Dict[str, Any]:
    if get_user(user_id) is None:
        raise AuthError("no such user", 404)
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with db.tx() as conn:
        cur = conn.execute("INSERT INTO access_tokens (user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                           (user_id, clean_name(name) or "token", digest(raw), now()))
        audit(conn, f"user:{user_id}", "token.create", f"token:{cur.lastrowid}", name=clean_name(name) or "token")
    return {"ok": True, "id": int(cur.lastrowid), "token": raw}


def list_tokens(user_id: int) -> List[Dict[str, Any]]:
    rows = db.connect().execute("SELECT id, name, created_at, last_used_at FROM access_tokens WHERE user_id = ? "
                                "AND revoked_at IS NULL ORDER BY id", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke_token(user_id: int, token_id: int) -> Dict[str, Any]:
    with db.tx() as conn:
        cur = conn.execute("UPDATE access_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                           (now(), token_id, user_id))
        if cur.rowcount != 1:
            raise AuthError("no such token", 404)
        audit(conn, f"user:{user_id}", "token.revoke", f"token:{token_id}")
    return {"ok": True, "revoked": token_id}


def token_user(raw: str) -> Optional[Dict[str, Any]]:
    """The account an access token belongs to (None for an unknown or revoked token)."""
    if not raw or not raw.startswith(TOKEN_PREFIX) or len(raw) > 200:
        return None
    conn = db.connect()
    r = conn.execute("SELECT * FROM access_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                     (digest(raw),)).fetchone()
    if r is None:
        return None
    with db.tx() as c:
        c.execute("UPDATE access_tokens SET last_used_at = ? WHERE id = ?", (now(), r["id"]))
    return get_user(r["user_id"])


# ---------------------------------------------------------------- throttling

def throttled(ip: str) -> float:
    """Seconds this IP must still wait before another sign-in or invite attempt (0: go ahead)."""
    r = db.connect().execute("SELECT blocked_until FROM login_throttle WHERE ip = ?", (ip,)).fetchone()
    return max(0.0, r["blocked_until"] - now()) if r else 0.0


def record_failure(ip: str) -> None:
    """One failed attempt; the THROTTLE_FAILURES-th inside THROTTLE_WINDOW blocks the IP for THROTTLE_BLOCK."""
    t = now()
    with db.tx() as conn:
        r = conn.execute("SELECT * FROM login_throttle WHERE ip = ?", (ip,)).fetchone()
        if r is None or t - r["window_start"] >= THROTTLE_WINDOW:
            conn.execute("INSERT OR REPLACE INTO login_throttle (ip, window_start, failures, blocked_until) "
                         "VALUES (?, ?, 1, ?)", (ip, t, r["blocked_until"] if r else 0))
            return
        failures = r["failures"] + 1
        blocked = t + THROTTLE_BLOCK if failures >= THROTTLE_FAILURES else r["blocked_until"]
        conn.execute("UPDATE login_throttle SET failures = ?, blocked_until = ? WHERE ip = ?", (failures, blocked, ip))
