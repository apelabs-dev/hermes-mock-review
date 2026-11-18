"""The hook that ends live streams when access ends.

Signing out other devices ends those sessions; regenerating or stopping a share link ends that link's guests; changing
a link's round or guest role ends its guests too, so the board reconnects and recomputes its read scope. All of these
must also drop any live stream (SSE) the ended session still holds. The live-stream layer installs a closer
here with set_closer(); until it does, the calls are no-ops. A closer must not raise: errors are logged and swallowed,
because the access change itself has already been committed.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional, Protocol

log = logging.getLogger("mockreview.streams")


class Closer(Protocol):
    def close_sessions(self, session_ids: Iterable[str]) -> None:
        """End the streams opened by these sessions (ids are the stored session hashes)."""

    def close_link(self, review_id: str, generation: int) -> None:
        """End the guest streams that joined review_id through share link `generation` (or an older one)."""

    def close_guests(self, review_id: str) -> None:
        """End every guest stream of review_id, whatever link generation it joined through."""

    def close_actor(self, actor: str) -> None:
        """End every stream opened by this actor (`user:<id>` for an account, `guest:<id>` for a guest)."""


class _NoOp:
    def close_sessions(self, session_ids: Iterable[str]) -> None:
        return None

    def close_link(self, review_id: str, generation: int) -> None:
        return None

    def close_guests(self, review_id: str) -> None:
        return None

    def close_actor(self, actor: str) -> None:
        return None


_lock = threading.Lock()
_closer: Closer = _NoOp()


def set_closer(closer: Optional[Closer]) -> None:
    """Install the stream registry's closer (None restores the no-op)."""
    global _closer
    with _lock:
        _closer = closer if closer is not None else _NoOp()


def close_sessions(session_ids: Iterable[str]) -> None:
    ids = [i for i in session_ids if i]
    if not ids:
        return
    try:
        _closer.close_sessions(ids)
    except Exception:
        log.exception("closing streams for %d session(s) failed", len(ids))


def close_link(review_id: str, generation: int) -> None:
    try:
        _closer.close_link(review_id, generation)
    except Exception:
        log.exception("closing guest streams of a share link failed")


def close_guests(review_id: str) -> None:
    try:
        _closer.close_guests(review_id)
    except Exception:
        log.exception("closing guest streams of a review failed")


def close_actor(actor: str) -> None:
    if not actor:
        return
    try:
        _closer.close_actor(actor)
    except Exception:
        log.exception("closing streams of actor %s failed", actor)
