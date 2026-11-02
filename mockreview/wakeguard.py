"""When a board may wake a session that wake_toolsets gave tools.

wake_toolsets lets the session Send wakes use real tools (terminal, files), and that session acts on text a reviewer
typed on the board, with nobody watching. So a waking board is only allowed while the way to its Send button is gated:

- here.now: only with the password on. The password gates the whole site, Site Data writes included, and the submit
  token is only in the gated state.json, so without the password nobody can submit. A board with the password off is
  open to anyone holding the link.
- tailnet / local: only while the board server listens on loopback (tailscale serve or this computer). With `host` set
  beyond loopback (0.0.0.0, `--hosting lan`) anyone on the network can open every board and press Send, no login.

A board with wake off is always allowed: it wakes nobody.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import config, settings


def granted() -> list:
    return list(settings.get("wake_toolsets") or [])


def lan_reason() -> Optional[str]:
    if config.lan_shared():
        return (f"the board server listens beyond loopback (host {config.host()}), so anyone on the network can "
                "press Send with no login")
    return None


def exposure(mode: str, wake: bool, password_on: bool) -> Optional[str]:
    """Why this board must not wake a session with wake_toolsets, or None when it may."""
    if not wake or not granted():
        return None
    lan = lan_reason()
    if lan:
        return lan
    if mode == "herenow" and not password_on:
        return "this here.now board has no password, so anyone holding the link types the feedback"
    return None


def password_on(data: Dict[str, Any]) -> bool:
    """The review's here.now password is wanted (a legacy review without the key has none)."""
    return (data.get("herenow") or {}).get("password_enabled") is True


def password_applied(data: Dict[str, Any]) -> bool:
    """here.now confirmed the password gate is live on the site."""
    hn = data.get("herenow") or {}
    return (hn.get("password_enabled") is True and hn.get("password_status") == "applied"
            and hn.get("password_applied") is True)


def review_exposure(data: Dict[str, Any], live: bool = False) -> Optional[str]:
    """exposure() for a stored review. live=True (at wake time) also needs here.now to have confirmed the gate."""
    if data.get("wake") is False:
        return None
    gated = password_applied(data) if live else password_on(data)
    reason = exposure(data.get("mode", ""), True, gated)
    if reason and live and data.get("mode") == "herenow" and password_on(data) and not lan_reason():
        reason = "here.now has not confirmed this board's password gate (password_status " \
                 f"{(data.get('herenow') or {}).get('password_status', 'legacy')})"
    return reason


def message(reason: str) -> str:
    return (f"refusing a board that wakes Hermes while wake_toolsets is set: {reason}, and that feedback would reach "
            f"the woken session's tools ({', '.join(granted())}). Keep the here.now password on, host on loopback "
            "(tailnet or local), pass wake off for this board, or empty wake_toolsets and re-run "
            "`hermes mockreview setup`.")
