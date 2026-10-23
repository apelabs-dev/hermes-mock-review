"""Paths and runtime settings. Each value resolves override > env var > config.yaml > default (see settings.py)."""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Optional

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


def public_base() -> str:
    explicit = settings.get("public_base").rstrip("/")
    if explicit:
        return explicit
    import json
    import re
    import subprocess
    try:
        proc = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
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
