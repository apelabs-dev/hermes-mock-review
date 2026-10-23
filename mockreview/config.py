"""Paths and runtime settings. Each value resolves override > env var > config.yaml > default (see settings.py)."""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Optional, Tuple

from . import settings
from .settings import hermes_home  # noqa: F401  (re-exported; callers use config.hermes_home())

PACKAGE_DIR = Path(__file__).resolve().parent
BOARD_DIR = PACKAGE_DIR / "board"

HOST = "127.0.0.1"   # built-in defaults; the effective values are host() / port()
PORT = 8790
MAX_BODY = 2 * 1024 * 1024
HERENOW_CLIENT = "hermes/mock-review"
HERENOW_API = "https://here.now/api/v1"


def _path_setting(name: str, default: Path) -> Path:
    v = settings.get(name)
    return Path(v).expanduser() if v else default


_bound_root: "contextvars.ContextVar[Optional[Path]]" = contextvars.ContextVar("mockreview_bound_root", default=None)


def bound_root() -> Optional[Path]:
    """The board server's store this call was explicitly bound to (owner.bound()), else None."""
    return _bound_root.get()


def own_data_root() -> Path:
    """This process's own store: data_dir / MOCKREVIEW_HOME, else $HERMES_HOME/workspace/mock-reviews."""
    return _path_setting("data_dir", hermes_home() / "workspace" / "mock-reviews")


def data_root() -> Path:
    b = _bound_root.get()
    return b if b is not None else own_data_root()


def _route_url(suffix: str) -> str:
    base = settings.get("webhook_url").rstrip("/")
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(base)
    path = parts.path.rsplit("/", 1)[0] + "/" + route_name() + suffix
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def webhook_url(platform: str = "") -> str:
    if not platform:
        return settings.get("webhook_url").rstrip("/")
    return _route_url("-" + platform)


def notice_route_name(platform: str) -> str:
    return f"{route_name()}-{platform}-notice"


def notice_webhook_url(platform: str) -> str:
    """The deliver-only notice route `<webhook_route>-<platform>-notice`, built the same way as the wake route.
    The platform is required: there is no notice route without one, and the wake URL is never a stand-in."""
    if not platform:
        raise ValueError("a notice route needs the origin platform")
    return _route_url("-" + platform + "-notice")


def route_name() -> str:
    return settings.get("webhook_route")


TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
TAILSCALE_WORKING = "tailscale"   # the CLI that last answered; commands printed for the user name this one


def run_tailscale(args: list, timeout: float = 5) -> "subprocess.CompletedProcess":
    """Run the tailscale CLI with `args`. On macOS the `tailscale` symlink the app installs can crash ("The current
    bundleIdentifier is unknown to the registry"); when it fails or is missing, the app's own binary is tried. Raises
    FileNotFoundError when no CLI exists and lets subprocess.TimeoutExpired through; otherwise returns the last result."""
    import os
    import subprocess
    import sys
    exes = ["tailscale"]
    if sys.platform == "darwin" and os.path.exists(TAILSCALE_APP):
        exes.append(TAILSCALE_APP)
    last = None
    for exe in exes:
        try:
            proc = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
        except FileNotFoundError:
            continue
        if proc.returncode == 0:
            global TAILSCALE_WORKING
            TAILSCALE_WORKING = exe
            return proc
        last = proc
    if last is None:
        raise FileNotFoundError("tailscale")
    return last


def serve_route(name: str, board_port: int, serve_status: Optional[dict]) -> Tuple[str, Optional[str]]:
    """(public URL, command or None) for reaching the board at 127.0.0.1:<board_port> through `tailscale serve`.

    `serve_status` is the parsed `tailscale serve status --json` (None when unknown). An HTTPS port of this node that
    already proxies to the board wins, and no command is needed. Otherwise the command adds a route without touching
    the existing ones: the default HTTPS port 443 only when nothing serves on it, else the board's own port (or the next
    free one). Unknown status: the board's own port, never 443, so an existing root route is never replaced."""
    from urllib.parse import urlsplit
    name = name.lower()
    web = (serve_status or {}).get("Web") or {}
    tcp = (serve_status or {}).get("TCP") or {}
    for key in sorted(web) if isinstance(web, dict) else []:
        host, _, https_port = str(key).rpartition(":")
        handler = ((web.get(key) or {}).get("Handlers") or {}).get("/") or {}
        target = str(handler.get("Proxy") or "")
        if not target or host.lower() != name or not https_port.isdigit():
            continue
        t = urlsplit(target if "://" in target else "http://" + target)
        try:
            tport = t.port
        except ValueError:
            continue
        if t.hostname in ("127.0.0.1", "localhost") and tport == board_port:
            return (f"https://{name}" if https_port == "443" else f"https://{name}:{https_port}"), None
    used = {str(k) for k in tcp} if isinstance(tcp, dict) else set()
    if serve_status is not None and "443" not in used:
        return f"https://{name}", f"tailscale serve --bg {board_port}"
    https_port = next(p for p in range(board_port, board_port + 100) if str(p) not in used)
    return f"https://{name}:{https_port}", f"tailscale serve --bg --https={https_port} http://127.0.0.1:{board_port}"


def public_base() -> str:
    explicit = settings.get("public_base").rstrip("/")
    if explicit:
        return explicit
    import json
    import re
    import subprocess
    try:
        proc = run_tailscale(["status", "--json"])
        status = json.loads(proc.stdout) if proc.returncode == 0 else {}
        if not isinstance(status, dict) or not isinstance(status.get("Self"), dict):
            return ""
        name = str(status["Self"].get("DNSName") or "").rstrip(".")
        if status.get("BackendState") == "Running" and re.fullmatch(r"[a-zA-Z0-9.-]+", name):
            return f"https://{name}:{port()}"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return ""


def host() -> str:
    return settings.get("host")


def port() -> int:
    return settings.get("port")


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def local_base() -> str:
    h = host()
    if h == "0.0.0.0":
        h = "127.0.0.1"
    elif h == "::1":
        h = "[::1]"
    return (settings.get("local_base") or f"http://{h}:{port()}").rstrip("/")


def detect_lan_ip() -> str:
    """This computer's address on its network (the interface the default route uses). No packet is sent."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed, connect() on UDP only picks the interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_shared() -> bool:
    """True when the server listens beyond loopback, so local links open on other devices."""
    return host() not in LOOPBACK


def local_link_base() -> str:
    """Start of a local-mode review link: this computer's own address, no Tailscale."""
    addr = settings.get("lan_address").strip()
    if not addr:
        addr = detect_lan_ip() if lan_shared() else "127.0.0.1"
    if ":" in addr and not addr.startswith("["):
        addr = f"[{addr}]"
    return f"http://{addr}:{port()}"


def herenow_publish_script() -> Path:
    """Resolve only an installed official here-now skill inside the active Hermes home."""
    skills = (hermes_home() / "skills").resolve()
    candidates = [skills / "productivity" / "here-now" / "scripts" / "publish.sh",
                  skills / "here-now" / "scripts" / "publish.sh"]
    candidates.extend(sorted(skills.glob("**/here-now/scripts/publish.sh")))
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(skills) and resolved.is_file():
            return resolved
    raise FileNotFoundError(
        "install-here-now-skill: run `hermes skills install official/productivity/here-now` "
        "in the active HERMES_HOME")


def poll_seconds() -> float:
    return settings.get("poll_seconds")


def retry_seconds() -> float:
    return settings.get("retry_seconds")
