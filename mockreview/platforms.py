"""Which chat a board can wake, and how: the one classifier for an origin platform.

A board wakes the chat that created it. How depends on the origin's platform:

deliver      a messaging chat a Hermes webhook route can deliver to (telegram, discord, slack, matrix, email, irc, and
             every other adapter Hermes registers). Send POSTs the signed payload to `<webhook_route>-<platform>`,
             which runs the wake prompt and sends the answer to the chat; the instant ack and the stalled ping go to
             `<webhook_route>-<platform>-notice` (notify.py).
session      `local`: a Hermes Desktop chat, or another local session the gateway serves. No webhook route can deliver
             there, so the board server, running inside the gateway, queues the same wake prompt into that session
             through the plugin API `inject_message(session_key=...)` (sessionwake.py).
unsupported  a Hermes surface that is not a chat anyone reads (the ingress endpoints, the stateless API server, the
             agent-protocol adapters whose outbound send is a no-op), and any name this Hermes does not know.

The names below are the only hand-kept list: `local` and the surfaces that are not a chat. Every other name is asked of
the running Hermes (`gateway.webhook_targets.is_known_platform`, the registry webhook routes are validated against),
imported lazily: an import that fails for any reason but absence, or a check that raises, refuses (fail-closed).
Outside Hermes (no Hermes installed, or the MCP server, which never loads Hermes's plugins) nothing can be asked, so an
explicit origin is taken as a deliver platform and the receiver at `webhook_url` decides. Nothing here ever infers an origin: `session_origin` reads the
calling session's own context, which Hermes binds for that turn.
"""

from __future__ import annotations

import re
import sys
from typing import Callable, Dict, Optional, Tuple

DELIVER = "deliver"
SESSION = "session"
UNSUPPORTED = "unsupported"

LOCAL = "local"

# Hermes platforms that are not a chat a board can wake, each with the reason the refusal gives.
NOT_A_CHAT: Dict[str, str] = {
    "webhook": "it is Hermes's webhook ingress, which receives events and is not a chat anyone reads",
    "msgraph_webhook": "it is Hermes's Microsoft Graph change-notification ingress, not a chat",
    "api_server": ("it is Hermes's OpenAI-compatible API server: stateless, every request carries its own history, "
                   "so there is no live chat to wake (waking it needs a Hermes core change)"),
    "a2a": ("it is the agent-to-agent protocol adapter: a wake's answer would be dropped, because the adapter only "
            "answers a pending A2A request"),
    "raft": ("it is the Raft bridge, whose adapter sends nothing (the agent posts through the Raft CLI), so a wake's "
             "answer would never reach the chat"),
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _hermes_knows(platform: str) -> Optional[bool]:
    """Does the running Hermes deliver webhook routes to `platform`? None when this is not a Hermes process.

    Imported lazily. A missing `gateway` package means no Hermes (None); any other failure, an import error inside
    Hermes or a check that raises, is a refusal (False), never a guess. A plugin platform is known only after plugin
    discovery, so a miss runs discovery and asks again, as Hermes's own `hermes webhook subscribe` does
    (hermes_cli/webhook.py, _is_known_delivery_target). Only a Hermes process (the gateway, a `hermes` command) has
    loaded the plugin system; anywhere else (the MCP server run with Hermes's interpreter) discovery is not started
    here, and a miss is None: nothing can be asked."""
    try:
        from gateway import webhook_targets
    except ModuleNotFoundError as e:
        if e.name in ("gateway", "gateway.webhook_targets"):
            return None
        return False
    except Exception:
        return False
    try:
        if webhook_targets.is_known_platform(platform):
            return True
        plugins = sys.modules.get("hermes_cli.plugins")
        if plugins is None:
            return None
        plugins.discover_plugins()                 # idempotent; joins a discovery already in flight
        return bool(webhook_targets.is_known_platform(platform))
    except Exception:
        return False


def classify(platform: str) -> Tuple[str, str]:
    """(kind, why) for an origin platform: kind is DELIVER, SESSION or UNSUPPORTED; `why` explains UNSUPPORTED."""
    name = platform if isinstance(platform, str) else ""
    if name == LOCAL:
        return SESSION, ""
    if name in NOT_A_CHAT:
        return UNSUPPORTED, NOT_A_CHAT[name]
    if not _NAME_RE.fullmatch(name):
        return UNSUPPORTED, "it is not a platform name"
    if _hermes_knows(name) is False:
        return UNSUPPORTED, ("this Hermes has no chat platform by that name that a webhook route can deliver to (is "
                             "its platform plugin installed and enabled?)")
    return DELIVER, ""


def is_session(origin: object) -> bool:
    """True when a stored origin is woken inside its own session (a Desktop chat), not through a webhook route."""
    return isinstance(origin, dict) and origin.get("platform") == LOCAL


def unsupported_message(platform: str, why: str) -> str:
    return (f"this chat cannot be woken: origin platform {platform!r} is not a chat mock-review can wake ({why}). "
            "Create the board from a messaging chat or a Hermes Desktop chat, or pass wake=false for a demo board "
            "nobody is woken by.")


def require_wakeable(origin: Dict[str, str]) -> str:
    """The origin's kind, or ReviewError (409) when a waking board could never reach it."""
    from .store import ReviewError
    kind, why = classify(origin.get("platform", ""))
    if kind == UNSUPPORTED:
        raise ReviewError(unsupported_message(origin.get("platform", ""), why), 409)
    if kind == SESSION and not origin.get("session_key"):
        raise ReviewError(
            "a Desktop (local) origin is woken inside its own session and needs that session's key, which only the "
            "calling Desktop chat has: create the board from that chat without origin_* (it reads them from the "
            "session), or pass wake=false for a demo board.", 409)
    return kind


# ---------------------------------------------------------------- the calling session, read from its own context

TUI_PROCESS = ("this session runs in a Hermes process of its own (a terminal UI, or a standalone `hermes dashboard`), "
               "which the board server inside the gateway cannot reach")


def _session_env() -> Optional[Callable[[str, str], str]]:
    """Hermes's reader of the calling turn's session context, or None outside Hermes."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return None
    return get_session_env


def session_origin() -> Tuple[Optional[Dict[str, str]], str]:
    """The origin of the session whose turn is calling, from the context Hermes binds for that turn, or (None, why).

    Inside a gateway turn Hermes binds HERMES_SESSION_PLATFORM, _CHAT_ID, _THREAD_ID and _KEY (a Desktop chat:
    platform `local`, the chat id is the session id, and the key is the route key the plugin wakes it by). A terminal
    UI running in its own process has only the key, and the board server cannot reach that process; no session at all
    (the CLI, the MCP server, tests) has nothing. Values are taken as Hermes gave them; `store.normalize_origin`
    validates them like explicit arguments."""
    get_session_env = _session_env()
    if get_session_env is None:
        return None, "no Hermes session context in this process"
    try:
        platform = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip()
        chat_id = str(get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
        thread_id = str(get_session_env("HERMES_SESSION_THREAD_ID", "") or "").strip()
        key = str(get_session_env("HERMES_SESSION_KEY", "") or "").strip()
    except Exception:
        return None, "the Hermes session context could not be read"
    if not platform or not chat_id:
        return None, (TUI_PROCESS if key else "no calling chat in this context")
    origin = {"platform": platform, "chat_id": chat_id}
    if thread_id:
        origin["thread_id"] = thread_id
    if platform == LOCAL:
        if not key:
            return None, "the Desktop session has no route key in its context"
        origin["session_key"] = key
    return origin, ""
