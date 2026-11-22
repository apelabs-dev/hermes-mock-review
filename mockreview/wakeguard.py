"""When a board may wake Hermes, in two gates: one for the board, one for each request.

The board gate (`exposure`) decides whether a board may wake a session with tools at all. wake_toolsets lets the
session a wake reaches use real tools (terminal, files), and that session acts on text someone typed on the board,
with nobody watching. So a board that wakes such a session is only allowed while the server listens on loopback
(tailscale serve, Funnel or this computer), no `public_url` is configured, and the board is not `public`. The address
that matters is the one the live server actually bound (its runtime descriptor), not the setting it was configured
with. A board reachable beyond loopback can be opened by anyone who can get a session or guest link. A `public` board,
or any board while `public_url` is set, is reachable through a proxy that leaves no marker this server can trust on
each request, so it is refused too. A board that wakes a Desktop chat is held to the same gate whatever wake_toolsets
says: that chat is woken inside its own session (sessionwake.py), which runs the text with every tool the session has,
not the webhook route's safe set. A board with wake off is always allowed: it wakes nobody.

A Tailscale Funnel mapping does not refuse a board here: Tailscale marks every Funnel request (`auth.via_funnel`), so
the request gate below tells it apart from the tailnet and this computer.

The request gate (`request_gate`) runs on every Send, pin fix and @Hermes mention, after the board gate allowed the
board. It reads who is asking (the review role) and how the request arrived (`auth.private_ingress`):
- the owner wakes Hermes only from private ingress (this computer or the tailnet). From the Funnel URL or any other
  public path the owner's Send or pin fix is saved and Hermes is not woken; Amend from the tailnet sends it. The
  owner's mention instead follows the reviewer rule below.
- Send, amend and Build are the owner's alone: the server answers 403 to anyone else.
- a pin fix from anyone but the owner is saved quietly and never wakes.
- a reviewer who is not the owner (a guest link's reviewer role or a member's reviewer grant), or the owner off
  private ingress, wakes Hermes with an @Hermes mention only when the board wakes a messaging chat through its webhook
  route and wake_toolsets is empty, from any ingress. A Desktop chat or a route with tools keeps that mention saved.
  `_deliver_thread_wakes` checks the same rule again when it sends, because the settings can change after the mention
  was queued.
- a commenter or viewer never wakes Hermes.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import config, runtime, settings

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

# The request gate's kinds: Send / amend / Build (`api/submit`), a pin fix (`api/pin-fix`), an @Hermes mention.
SUBMIT, PIN_FIX, MENTION = "submit", "pin-fix", "mention"

# What the board shows when a request is saved and Hermes is not woken, and the one refusal.
FUNNEL_NO_WAKE = ("sent from the public link: saved, and Hermes wasn't woken. Open the board on the tailnet to send it "
                  "to Hermes")
OUTSIDE_NO_WAKE = ("sent from outside this computer and the tailnet: saved, and Hermes wasn't woken. Open the board on "
                   "the tailnet to send it to Hermes")
TOOLS_NO_WAKE = "saved; Hermes wasn't woken because this board's chat has tools a guest may not trigger"
PIN_FIX_OWNER_ONLY = "saved for the owner; a pin fix from anyone but the owner doesn't wake Hermes"
MENTION_ROLE_NO_WAKE = "saved; only the owner or a reviewer can wake Hermes with a mention"
SUBMIT_OWNER_ONLY = ("only the owner sends feedback to Hermes; your comments and pins stay on the board for them")


def granted() -> list:
    return list(settings.get("wake_toolsets") or [])


def _alive(pid: Any) -> bool:
    """True while a process with this pid exists. Signal 0 is never sent on Windows: there it ends the process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def bound_host() -> str:
    """The address the live board server for this store actually bound, from its descriptor; else the host setting.

    A descriptor a dead server left behind says nothing about today's listener, so its host counts only while its
    pid is alive.
    """
    info = runtime.read(config.data_root()) or {}
    host = str(info.get("host") or "")
    if host and _alive(info.get("pid")):
        return host
    return config.host()


def lan_reason() -> Optional[str]:
    host = bound_host()
    if host not in config.LOOPBACK:
        return (f"the board server listens beyond loopback (host {host}), so anyone who can get a session or guest "
                "link can press Send")
    return None


def exposure(mode: str, wake: bool, session: bool = False) -> Optional[str]:
    """Why this board must not wake a session with tools, or None when it may. `session`: the origin is a Desktop
    chat, woken inside its own session (checked whatever wake_toolsets says). A Funnel mapping is not a reason: the
    request gate holds every request that arrives through it."""
    if not wake or not (session or granted()):
        return None
    if mode == "public" or config.public_url():
        return "the board is on a public URL, so anyone who can get a session or guest link can press Send"
    return lan_reason()


def review_exposure(data: Dict[str, Any]) -> Optional[str]:
    """exposure() for a stored review: its mode, its wake flag and its origin, plus the public_url setting and the
    bound host, are all that matter."""
    if data.get("wake") is False:
        return None
    from . import platforms
    return exposure(data.get("mode", ""), True, platforms.is_session(data.get("origin")))


def message(reason: str, session: bool = False) -> str:
    if session and not granted():
        return (f"refusing a board that wakes a Desktop chat: {reason}. A Desktop chat is woken inside its own "
                "session, which acts on the text it is sent with every tool that session has. Keep the board gated "
                "(server on loopback, no public URL and not a public board; Funnel is fine, because a request through "
                "it never wakes Hermes), or pass wake off for this board.")
    return (f"refusing a board that wakes Hermes while wake_toolsets is set: {reason}, and that text would reach the "
            f"woken session's tools ({', '.join(granted())}). Keep the board gated (server on loopback, no public URL "
            "and not a public board; Funnel is fine, because a request through it never wakes Hermes), pass wake off "
            "for this board, or empty wake_toolsets and re-run `hermes mockreview setup`.")


def reviewer_mention_reason(origin: Any) -> str:
    """Why a reviewer who is not the owner may not wake this board's chat now, or "" when they may: only a messaging
    chat woken through its webhook route, and only while wake_toolsets gives that route no tools."""
    from . import platforms
    if platforms.is_session(origin) or granted():
        return TOOLS_NO_WAKE
    return ""


def request_gate(kind: str, role: Optional[str], private: bool, origin: Any, funnel: bool = False) -> str:
    """The request gate as one pure rule: "" when this request may wake Hermes, else why it is saved instead.

    `kind` is SUBMIT, PIN_FIX or MENTION; `role` the effective review role (access.Decision.role: "owner" for an
    admin or the loopback owner, a member's grant, a guest link's guest_role); `private` is auth.private_ingress;
    `funnel` (auth.via_funnel) only picks the wording of the owner's message; `origin` is the review's stored origin.
    For SUBMIT anything but "" is a refusal (the route answers 403 with SUBMIT_OWNER_ONLY); for the other two it is
    the `detail` of a saved request. An owner's mention off private ingress follows the reviewer rule. Unknown kinds
    and roles never wake."""
    if kind == MENTION:
        if role == "owner" and private:
            return ""
        if role in ("owner", "reviewer"):
            return reviewer_mention_reason(origin)
        return MENTION_ROLE_NO_WAKE
    if role == "owner":
        if private:
            return ""
        return FUNNEL_NO_WAKE if funnel else OUTSIDE_NO_WAKE
    if kind == PIN_FIX:
        return PIN_FIX_OWNER_ONLY
    return SUBMIT_OWNER_ONLY
