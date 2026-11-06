"""The only secret access route used by foreground and background operations.

Inside Hermes the caller's profile scope is the only source; outside it (the stdio MCP server has no gateway and no
profile scope) the process environment is the deployment's secret store.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import logging
import os
from pathlib import Path
import sys
from typing import Iterator, Set, Tuple

log = logging.getLogger("mockreview.secrets")

_build_failures: Set[Tuple[str, str]] = set()   # (home, exception class) already logged
_logged_env_secrets: Set[str] = set()


def _scope_module_available() -> bool:
    """True when `agent.secret_scope` exists, whether or not importing it succeeds.

    Absence is decided by the module spec, never by catching ImportError from the import itself: an ImportError raised
    inside `agent.secret_scope` (a broken install, a moved internal module) or a missing `get_secret` symbol is a
    failure, not absence, and must not fall back to the process environment. Under multiplexing a broken scope that
    fell back would read whatever the starting process had in its environment, which is the leak this guard exists to
    stop. A module already in `sys.modules` counts as available, so a bound scope object keeps working.
    """
    if sys.modules.get("agent.secret_scope") is not None:
        return True
    try:
        return importlib.util.find_spec("agent.secret_scope") is not None
    except (ImportError, AttributeError, ValueError):
        return False


def get_secret(name: str) -> str:
    """Resolve within the caller's Hermes context; outside Hermes, fall back to the process environment.

    Inside Hermes the scoped value is the only source: never read the environment, never leak another profile's
    value. The stdio MCP server has no gateway and no profile scope, so there the environment is the deployment's
    secret store. A scope that exists but cannot be imported is a failure, not absence: it fails closed instead of
    reading the environment, because a half-installed Hermes must never turn into a silent profile-scope leak.
    """
    if not _scope_module_available():
        value = (os.environ.get(name) or "").strip()
        if value and name not in _logged_env_secrets:
            _logged_env_secrets.add(name)
            log.info("secret %s resolved from the environment (no Hermes secret scope available)", name)
        return value
    try:
        from agent.secret_scope import get_secret as scoped_secret
    except ImportError as e:
        key = (name, type(e).__name__)
        if key not in _build_failures:
            _build_failures.add(key)
            log.warning("secret %s cannot be read: agent.secret_scope raised %s; failing closed, the environment is "
                        "not used while a scope module exists", name, type(e).__name__)
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
