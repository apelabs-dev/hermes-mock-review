"""The install's SQLite store: users, sign-in, grants, share links, guests, access tokens, audit, throttling.

stdlib sqlite3 in WAL mode with a busy timeout, one connection per thread and database file. The schema is versioned
with PRAGMA user_version: migrate() runs every missing step in order, each in its own transaction, so running it again
(or from two processes at once) changes nothing. Review files stay files under the data root; this database sits
beside them as `access.sqlite3`.

Migration numbers:
    1  users, identities, sessions, grants, invites, share_links, guests, access_tokens, audit, login_throttle, meta
    2  comments: threads, comments, mentions, reads
    3  events (the SSE replay log)
    4  share_links.round: the version picker a viewer guest sees (latest or a round number)
    5  revoked_sessions: guest sessions ended by an access change, so a stale cookie reads as revoked
Never renumber a released step; add a new one.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from . import config

DB_NAME = "access.sqlite3"
BUSY_TIMEOUT_MS = 5000

_SCHEMA_1 = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    source TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE identities (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    subject TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (source, subject)
);
CREATE TABLE guests (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE sessions (
    id_hash TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    guest_id TEXT REFERENCES guests(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    CHECK ((user_id IS NULL) != (guest_id IS NULL))
);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE TABLE grants (
    id INTEGER PRIMARY KEY,
    review_id TEXT,
    all_reviews INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('reviewer', 'commenter', 'viewer')),
    expires_at REAL,
    granted_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    CHECK ((review_id IS NULL) = (all_reviews = 1))
);
CREATE UNIQUE INDEX grants_one ON grants(user_id, COALESCE(review_id, '*'));
CREATE TABLE invites (
    id INTEGER PRIMARY KEY,
    nonce_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    review_id TEXT,
    all_reviews INTEGER NOT NULL DEFAULT 0,
    review_role TEXT CHECK (review_role IN ('reviewer', 'commenter', 'viewer')),
    grant_expires_at REAL,
    label TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT 'invite' CHECK (purpose IN ('invite', 'login')),
    login_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    used_by INTEGER,
    revoked_at REAL
);
CREATE TABLE share_links (
    review_id TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    audience TEXT NOT NULL DEFAULT 'anyone' CHECK (audience IN ('anyone', 'members')),
    guest_role TEXT NOT NULL CHECK (guest_role IN ('reviewer', 'commenter', 'viewer')),
    expires_at REAL,
    generation INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE access_tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);
CREATE TABLE audit (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE login_throttle (
    ip TEXT PRIMARY KEY,
    window_start REAL NOT NULL,
    failures INTEGER NOT NULL,
    blocked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_SCHEMA_2 = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    anchor TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    activated INTEGER NOT NULL DEFAULT 0,
    carried INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX threads_by_review ON threads(review_id, status, updated_at);
CREATE TABLE comments (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,
    idem TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    edited_at REAL,
    deleted_at REAL
);
CREATE INDEX comments_by_thread ON comments(thread_id, created_at);
CREATE UNIQUE INDEX comments_idem ON comments(thread_id, idem) WHERE idem != '';
CREATE TABLE mentions (
    comment_id TEXT NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('user', 'guest', 'hermes')),
    target TEXT NOT NULL,
    PRIMARY KEY (comment_id, kind, target)
);
CREATE TABLE reads (
    principal TEXT NOT NULL,
    review_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    seen_at REAL NOT NULL,
    PRIMARY KEY (principal, review_id, thread_id)
);
"""

_SCHEMA_3 = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    at REAL NOT NULL,
    UNIQUE (review_id, seq)
);
CREATE INDEX events_by_review ON events(review_id, id);
"""

# A link's version picker: "latest" or the number of one round, as text (validated by users._set_link).
_MIGRATE_4 = "ALTER TABLE share_links ADD COLUMN round TEXT NOT NULL DEFAULT 'latest'"

# Guest sessions ended by an access change. Their rows (and their guests) are deleted; the hash is kept so the stale
# cookie answers like a dead link instead of the sign-in page. One row per revoked guest session, pruned after 31 days.
_MIGRATE_5 = """
CREATE TABLE revoked_sessions (
    id_hash TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    revoked_at REAL NOT NULL
);
"""

# (version, SQL). Index i holds version i + 1.
MIGRATIONS: List[Tuple[int, str]] = [
    (1, _SCHEMA_1),
    (2, _SCHEMA_2),
    (3, _SCHEMA_3),
    (4, _MIGRATE_4),
    (5, _MIGRATE_5),
]

_local = threading.local()
_migrated: Dict[str, int] = {}
_migrate_lock = threading.Lock()


def path() -> Path:
    return config.data_root() / DB_NAME


def _open(p: Path) -> sqlite3.Connection:
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    _wal(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _wal(conn: sqlite3.Connection) -> None:
    """Switch the file to WAL unless it already is. SQLite answers a journal-mode change it cannot make at once with
    SQLITE_BUSY, without calling the busy handler, so a race with another connection (another process opening the
    same fresh file) is retried here."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, steps: Optional[List[Tuple[int, str]]] = None) -> int:
    """Run every step newer than the file's user_version, in order, each in one IMMEDIATE transaction that re-reads the
    version first (a second process that migrated meanwhile makes this a no-op). Returns the final version."""
    for n, sql in steps if steps is not None else MIGRATIONS:
        if version(conn) >= n:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            if version(conn) < n:
                for stmt in _statements(sql):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {int(n)}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return version(conn)


def _statements(sql: str) -> Iterator[str]:
    """executescript() would COMMIT the open transaction, so split the (comment-free, trigger-free) schema here."""
    for part in sql.split(";"):
        if part.strip():
            yield part.strip()


def connect() -> sqlite3.Connection:
    """This thread's connection to this data root's database, migrated on first use. Until a file is migrated, opening
    it (the switch to WAL included) and migrating it happen under one lock, so threads racing to open a fresh data
    root take turns."""
    p = str(path().resolve())      # resolve() is not strict: the same key before and after the directory exists
    conns: Dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    _local.conns = conns
    conn = conns.get(p)
    if _migrated.get(p) != MIGRATIONS[-1][0]:
        with _migrate_lock:
            if conn is None:
                conn = conns[p] = _open(Path(p))
            if _migrated.get(p) != MIGRATIONS[-1][0]:
                _migrated[p] = migrate(conn)
    elif conn is None:
        conn = conns[p] = _open(Path(p))
    return conn


@contextlib.contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """One write transaction (BEGIN IMMEDIATE: the write lock is taken up front, so a read-then-write inside cannot
    lose a race). Commits on success, rolls back on any exception."""
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def close_all() -> None:
    """Close this thread's connections (tests, and threads that are about to end)."""
    for conn in (getattr(_local, "conns", None) or {}).values():
        with contextlib.suppress(Exception):
            conn.close()
    _local.conns = {}


def forget_migrations() -> None:
    _migrated.clear()

