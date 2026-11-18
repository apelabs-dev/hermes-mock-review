"""When a board may wake a session that wake_toolsets gave tools.

wake_toolsets lets the session Send wakes use real tools (terminal, files), and that session acts on text a reviewer
typed on the board, with nobody watching. So a waking board is only allowed while the way to its Send button is gated:
the board server listens on loopback (tailscale serve or this computer), no `public_url` is configured, and the board
is not `public`. The address that matters is the one the live server actually bound (its runtime descriptor), not the
setting it was configured with. A board reachable beyond loopback can be opened and used by anyone who can get a
session or guest link; a `public` board has the same problem over the internet, and while `public_url` is set the same
loopback port is served through that proxy, so every board on it is reachable from the internet too.

A board with wake off is always allowed: it wakes nobody.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import config, runtime, settings

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


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


def exposure(mode: str, wake: bool) -> Optional[str]:
    """Why this board must not wake a session with wake_toolsets, or None when it may."""
    if not wake or not granted():
        return None
    if mode == "public" or config.public_url():
        return "the board is on a public URL, so anyone who can get a session or guest link can press Send"
    return lan_reason()


def review_exposure(data: Dict[str, Any]) -> Optional[str]:
    """exposure() for a stored review: its mode and its wake flag, plus the public_url setting, are all that matter."""
    if data.get("wake") is False:
        return None
    return exposure(data.get("mode", ""), True)


def message(reason: str) -> str:
    return (f"refusing a board that wakes Hermes while wake_toolsets is set: {reason}, and that feedback would reach "
            f"the woken session's tools ({', '.join(granted())}). Keep Send gated (server on loopback, no public URL "
            "and not a public board), pass wake off for this board, or empty wake_toolsets and re-run `hermes "
            "mockreview setup`.")
