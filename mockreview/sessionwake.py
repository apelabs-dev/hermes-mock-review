"""Wake a Desktop chat inside its own session: the `session` kind of platforms.py.

A Desktop chat (origin platform `local`) has no webhook route: Hermes's webhook adapter delivers only to messaging
adapters. Instead the board server, running inside the Hermes gateway (autostart), queues the wake prompt into the
chat's own session through the plugin API `ctx.inject_message(prompt, session_key=<key>)`. Hermes runs it as a user
turn in that session, behind any turn already running there. The prompt is the one a webhook route renders
(wakeprompt.py), filled from the same payload.

Three things must hold. `status()` names the first of the first two that does not; Hermes checks the third:
- the board server runs inside the gateway (`autostart: true`). Only the gateway process can queue a turn into a
  Desktop chat; the standalone `hermes mockreview serve` and a terminal UI's own process cannot;
- `plugins.entries.mock-review.allow_gateway_injection: true`, the consent Hermes requires before a plugin may queue a
  turn (`hermes mockreview setup` asks for it);
- the chat is live in this profile's gateway.

inject_message answers only True (Hermes scheduled the turn) or False; Hermes logs its reason. A False is explained
here by elimination and retried like any failed wake. True is as far as a plugin can see: a chat that closes before
the turn starts loses it, and Hermes logs that.

Hermes keeps no duplicate filter for injected turns (a webhook route keeps one for an hour, keyed by the delivery id),
so this module keeps one per process: a delivery id injected in the last hour is answered "duplicate", not injected
again.

The session's turn runs with that session's own tools, not the webhook route's safe set, so a board that wakes a
Desktop chat is allowed only while its Send button is gated (wakeguard).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import wakeprompt

log = logging.getLogger("mockreview.sessionwake")

DUPLICATE_SECONDS = 3600.0

NOT_IN_GATEWAY = ("the board server is not running inside the Hermes gateway, so it cannot reach a Desktop chat (set "
                  "autostart: true and restart the gateway; `hermes mockreview serve` runs outside it)")
INJECTION_OFF = ("allow_gateway_injection is off: Hermes lets a plugin queue a turn into a chat only with "
                 "plugins.entries.mock-review.allow_gateway_injection: true (run `hermes mockreview setup`)")
NO_API = "this Hermes has no plugin inject_message API (upgrade Hermes)"
NOT_LIVE = ("Hermes refused the wake: the Desktop chat is not live in this profile's gateway (it was closed, or "
            "another profile serves it); it is retried, and goes through once the chat is open again")
NO_NOTICE = ("a Desktop chat shows the wake itself as a turn in that chat, and Hermes has no plugin API that posts a "
             "notice into a named session without waking it (needs a Hermes core change)")

_lock = threading.Lock()
_ctx: Any = None
_seen: Dict[str, float] = {}


def attach(ctx: Any) -> None:
    """The plugin context of the gateway this board server runs in (autostart.gateway_factory)."""
    global _ctx
    with _lock:
        _ctx = ctx


def _calling_home() -> Path:
    """The Hermes home of the calling context: Hermes's context-local profile home under multiplexing, else
    HERMES_HOME (config.hermes_home())."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        from . import config
        return config.hermes_home()


def owns_home(owner_home: Any) -> bool:
    """True when the calling context belongs to the profile whose home owns the board server. Under profile
    multiplexing one gateway serves many profiles and every profile's webhook connect reaches gateway_factory; only
    the owner's plugin context may be attached, or the owner's Desktop wakes would go through another profile's."""
    if owner_home is None:
        return False
    try:
        return _calling_home().expanduser().resolve() == Path(owner_home).expanduser().resolve()
    except OSError:
        return False


def detach() -> None:
    global _ctx
    with _lock:
        _ctx = None
        _seen.clear()


def attached() -> bool:
    return _ctx is not None


def _consent(ctx: Any) -> Optional[bool]:
    """Hermes's own consent check, when this Hermes has it (diagnosis only: inject_message checks it again)."""
    check = getattr(ctx, "_gateway_injection_allowed", None)
    if not callable(check):
        return None
    try:
        return bool(check())
    except Exception:
        return None


def status() -> Tuple[bool, str]:
    """(ready, why not) for waking a Desktop chat from this process."""
    from . import owner
    ctx = _ctx
    if ctx is None or not owner.is_owner_process():
        return False, NOT_IN_GATEWAY
    if not callable(getattr(ctx, "inject_message", None)):
        return False, NO_API
    if _consent(ctx) is False:
        return False, INJECTION_OFF
    return True, ""


def status_dict() -> Dict[str, Any]:
    ready, why = status()
    return {"ready": ready, "error": why or None}


def prompt(payload: Dict[str, Any]) -> str:
    return wakeprompt.render(wakeprompt.PROMPT_TEMPLATE, payload)


def _recent(delivery_id: str, now: float) -> bool:
    with _lock:
        for k in [k for k, t in _seen.items() if now - t > DUPLICATE_SECONDS]:
            del _seen[k]
        return delivery_id in _seen


def send(payload: Dict[str, Any], delivery_id: str) -> Tuple[bool, str]:
    """Queue the wake prompt into the Desktop chat named by the payload's session_key: (woken, detail)."""
    key = payload.get("session_key")
    if not isinstance(key, str) or not key:
        # permanent like every other cannot-wake origin (notify.CANNOT_WAKE): only a new board fixes it
        return False, "cannot wake: this Desktop origin has no session key (made before Desktop wakes): recreate it"
    ready, why = status()
    if not ready:
        return False, why
    now = time.time()
    if _recent(delivery_id, now):
        return True, "duplicate (already queued in the Desktop chat)"
    ctx = _ctx
    try:
        ok = ctx.inject_message(prompt(payload), session_key=key)
    except Exception as e:
        log.exception("inject_message raised for %s", delivery_id)
        return False, f"inject_message raised: {type(e).__name__}: {e}"
    if ok is True:
        with _lock:
            _seen[delivery_id] = now
        return True, "queued in the Desktop chat"
    # Hermes says only False. Name what this process can see; the rest is Hermes's live-session check.
    ready, why = status()
    return False, (why if not ready else NOT_LIVE)
