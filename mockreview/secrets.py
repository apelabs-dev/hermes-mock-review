"""The only secret access route used by foreground and background operations."""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
from typing import Iterator, Set, Tuple

log = logging.getLogger("mockreview.secrets")

_build_failures: Set[Tuple[str, str]] = set()   # (home, exception class) already logged


def get_secret(name: str) -> str:
    """Resolve within the caller's Hermes context; never fall back to another store."""
    try:
        from agent.secret_scope import get_secret as scoped_secret
    except ImportError:
        return ""
    return (scoped_secret(name) or "").strip()


@contextmanager
def owner_scope(home: Path) -> Iterator[None]:
    """Bind the owning profile's secret scope for one board request or Worker tick, on a multiplexing gateway only.

    Hermes binds no scope around the webhook connect that starts the board, and a factory rewired during another
    profile's turn would capture that profile's scope, so under multiplexing the board always binds its own: the
    profile whose config and store it serves (`home`, captured once at server start). Rebuilt on every entry:
    Hermes memoizes the .env parse on the open file's fstat signature and caches the secret-source and managed
    snapshots, so a rebuild costs one open and fstat, a rotated value is seen on the next request, and concurrent
    request threads never share one mutable mapping. Anywhere else (no Hermes, multiplexing off) this does nothing,
    so get_secret keeps today's behaviour. A failed build binds nothing: the secret read then fails closed in Hermes.
    """
    try:
        from agent.secret_scope import (build_profile_secret_scope, is_multiplex_active, reset_secret_scope,
                                        set_secret_scope)
    except ImportError:
        yield
        return
    token = None
    if is_multiplex_active():
        try:
            token = set_secret_scope(build_profile_secret_scope(Path(home)), profile_home=str(home))
        except Exception as e:   # never the message: it could quote a value
            key = (str(home), type(e).__name__)
            if key not in _build_failures:
                _build_failures.add(key)
                log.warning("cannot build the secret scope of the profile at %s (%s); secret reads by the board "
                            "fail until it builds", home, type(e).__name__)
    try:
        yield
    finally:
        if token is not None:
            reset_secret_scope(token)
