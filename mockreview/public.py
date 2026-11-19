"""`mockreview public status|on|off`: put the loopback board on a public HTTPS URL through Tailscale Funnel.

Every Tailscale command goes through `config.run_tailscale`, is logged at INFO with its argv, return code, stdout and
stderr, and is returned in the JSON under `commands`. Only the board's own port is ever mapped, and a port that already
serves anything but this board (another proxy, a text or path handler, a redirect, a TCP forward) is refused (exit 1)
rather than published. `off` removes the mapping only when the port is exclusively this board's and never runs
`funnel reset`.

Funnel allows HTTPS ports 443, 8443 and 10000; `funnel_port` defaults to 8443 because 443 is usually taken. A public
Funnel request carries no Tailscale identity: v1.98.1 marks it with `Tailscale-Funnel-Request: ?1` and never sets the
`Tailscale-User-*` headers (`ipn/ipnlocal/serve.go:1046-1075`), and the board also refuses a Tailscale identity on any
public host, so reviewers sign in with an invite or a share link, like everyone else.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import api, config, settings
from .store import ReviewError

log = logging.getLogger("mockreview.public")

FUNNEL_PORTS = (443, 8443, 10000)
PROVIDER = "tailscale-funnel"
LOOPBACK_TARGETS = ("127.0.0.1", "localhost")
TIMEOUT = 5
PUBLIC_TAIL = "refusing to make a port that is not only this board public"
REMOVE_TAIL = "refusing to remove a port that is not only this board"

Mapping = Tuple[str, int, str]   # (host, https_port, proxy target)


def _record(args: List[str], proc: "subprocess.CompletedProcess") -> Dict[str, Any]:
    entry = {"argv": list(args), "rc": proc.returncode, "stdout": proc.stdout or "", "stderr": proc.stderr or ""}
    log.info("tailscale %s: rc=%s stdout=%s stderr=%s", " ".join(args), entry["rc"],
             entry["stdout"].strip(), entry["stderr"].strip())
    return entry


def _run(args: List[str], commands: List[Dict[str, Any]]) -> "subprocess.CompletedProcess":
    proc = config.run_tailscale(list(args), timeout=TIMEOUT)
    commands.append(_record(args, proc))
    return proc


def _settings_port(port: Optional[int]) -> int:
    """The --port flag, else funnel_port; only the three ports Funnel allows."""
    value = settings.get("funnel_port") if port is None else port
    allowed = "443, 8443 and 10000"
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ReviewError(f"Funnel allows only HTTPS ports {allowed}; got {value!r}")
    if value not in FUNNEL_PORTS:
        raise ReviewError(f"Funnel allows only HTTPS ports {allowed}; got {value}")
    return value


def _url(host: str, https_port: int) -> str:
    return f"https://{host}" if https_port == 443 else f"https://{host}:{https_port}"


def _serves_board(proxy: str, board_port: int) -> bool:
    try:
        target = urlsplit(proxy if "://" in proxy else "http://" + proxy)
        return target.hostname in LOOPBACK_TARGETS and target.port == board_port
    except ValueError:
        return False


def _mappings(status: Dict[str, Any]) -> List[Mapping]:
    """Every Web entry with a `/` proxy target: (host, https_port, proxy)."""
    web = status.get("Web")
    out: List[Mapping] = []
    if not isinstance(web, dict):
        return out
    for key in sorted(web):
        host, _, https_port = str(key).rpartition(":")
        if not host or not https_port.isdigit():
            continue
        handler = ((web.get(key) or {}).get("Handlers") or {}).get("/") or {}
        proxy = str(handler.get("Proxy") or "")
        if proxy:
            out.append((host, int(https_port), proxy))
    return out


def _handler_kind(handler: Any) -> Tuple[str, str]:
    """(kind, target) of one serve handler: Proxy, Path, Text, Redirect, or whatever key it carries."""
    if not isinstance(handler, dict):
        return "", ""
    for kind in ("Proxy", "Path", "Text", "Redirect"):
        if kind in handler:
            return kind, str(handler.get(kind) or "")
    for kind in sorted(handler):
        return str(kind), str(handler.get(kind) or "")
    return "", ""


def _port_handlers(status: Dict[str, Any], https_port: int) -> List[Tuple[str, str, str]]:
    """Every handler on one HTTPS port, from every Web entry whose key ends in `:<https_port>`: (path, kind, target)."""
    web = status.get("Web")
    out: List[Tuple[str, str, str]] = []
    if not isinstance(web, dict):
        return out
    for key in sorted(web):
        host, _, port = str(key).rpartition(":")
        if not host or not port.isdigit() or int(port) != https_port:
            continue
        handlers = (web.get(key) or {}).get("Handlers") or {}
        if not isinstance(handlers, dict):
            continue
        for path in sorted(handlers):
            kind, target = _handler_kind(handlers[path])
            out.append((str(path), kind, target))
    return out


def _foreign_handler(status: Dict[str, Any], https_port: int, board_port: int,
                     tail: str = PUBLIC_TAIL) -> Optional[str]:
    """The refusal sentence for a port that is not exclusively this board's, else None."""
    for path, kind, target in _port_handlers(status, https_port):
        if kind != "Proxy":
            what = f"a {kind} handler" if kind else "not a proxy"
            return f"port {https_port} already serves {path} ({what}); {tail}"
        if not _serves_board(target, board_port):
            what = target if path == "/" else f"{path} -> {target}"
            if tail == PUBLIC_TAIL:
                return f"port {https_port} already serves {what}; refusing to replace another app's route"
            return f"port {https_port} already serves {what}; {tail}"
    return None


def _foreign_tcp(status: Dict[str, Any], https_port: int, tail: str = PUBLIC_TAIL) -> Optional[str]:
    """The refusal sentence when TCP[<port>] is not a plain HTTPS/HTTP serve entry, else None."""
    tcp = status.get("TCP")
    if not isinstance(tcp, dict) or str(https_port) not in tcp:
        return None
    entry = tcp.get(str(https_port))
    if isinstance(entry, dict) and (entry.get("HTTPS") is True or entry.get("HTTP") is True) \
            and not entry.get("TCPForward"):
        return None
    if isinstance(entry, dict) and entry.get("TCPForward"):
        what = f" (a TCPForward to {entry['TCPForward']})"
    else:
        keys = ", ".join(sorted(str(k) for k in entry)) if isinstance(entry, dict) else ""
        what = f" ({keys})" if keys else ""
    return f"port {https_port} already serves TCP {https_port}{what}; {tail}"


def _funnel_enabled(status: Dict[str, Any], host: str, https_port: int) -> bool:
    allow = status.get("AllowFunnel")
    return isinstance(allow, dict) and allow.get(f"{host}:{https_port}") is True


def _find_mapping(status: Dict[str, Any], board_port: int, prefer_port: int) -> Optional[Mapping]:
    """A Funnel-enabled mapping to the board: funnel_port first, then any other one."""
    candidates = [m for m in _mappings(status)
                  if _serves_board(m[2], board_port) and _funnel_enabled(status, m[0], m[1])]
    for m in candidates:
        if m[1] == prefer_port:
            return m
    return candidates[0] if candidates else None


def serve_probe(commands: Optional[List[Dict[str, Any]]] = None) -> Optional[bool]:
    """`tailscale serve status --json`'s answer for this board's port: True/False when the CLI answered, None when
    the probe itself failed (no CLI, a non-zero exit such as a stopped tailscaled, unreadable JSON)."""
    commands = [] if commands is None else commands
    board_port = config.port()
    try:
        proc = _run(["serve", "status", "--json"], commands)
    except (OSError, ReviewError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        raw = json.loads(getattr(proc, "stdout", "") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return any(_serves_board(m[2], board_port) for m in _mappings(raw))


def fronts_board(commands: Optional[List[Dict[str, Any]]] = None) -> bool:
    """True when `tailscale serve` maps this board's port to loopback, Funnel or not.

    Serve forwards whatever `X-Forwarded-*` headers the client sent over a loopback connection and strips only
    `Tailscale-*` ones, so a tailnet device with no Serve user identity - a tagged node sends no `Tailscale-User-Login`
    - would be believed by `forwarded_identity`. Asked of the CLI, never guessed: unavailable Tailscale fronts
    nothing. `_mappings` reads the same `Web` entries `serve status --json` and `funnel status --json` both use.
    """
    answer = serve_probe(commands)
    return bool(answer)


def status(commands: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The Funnel mapping for this board, never raising: unavailable Tailscale is `ok: true, available: false`."""
    commands = [] if commands is None else commands
    funnel_port = settings.get("funnel_port")
    board_port = config.port()
    out: Dict[str, Any] = {"ok": True, "provider": PROVIDER, "available": False, "port": funnel_port,
                           "board_port": board_port, "enabled": False, "url": "", "mapping": None,
                           "public_url": config.public_url(), "status": None, "commands": commands}
    try:
        proc = _run(["funnel", "status", "--json"], commands)
    except FileNotFoundError:
        out["detail"] = "the Tailscale CLI (tailscale) is not installed or not on PATH"
        return out
    except subprocess.TimeoutExpired:
        out["detail"] = "tailscale funnel status timed out"
        return out
    if proc.returncode != 0:
        out["detail"] = (proc.stderr or proc.stdout or "tailscale funnel status failed").strip()[:500]
        return out
    try:
        raw = json.loads(proc.stdout or "{}")
    except ValueError:
        out["detail"] = "tailscale funnel status did not return JSON"
        return out
    if not isinstance(raw, dict):
        out["detail"] = "tailscale funnel status did not return a JSON object"
        return out
    out["available"] = True
    out["status"] = raw
    found = _find_mapping(raw, board_port, funnel_port)
    if found:
        host, https_port, proxy = found
        out.update(enabled=True, url=_url(host, https_port),
                   mapping={"host": host, "https_port": https_port, "proxy": proxy})
    return out


def _fallback_url(https_port: int) -> str:
    """The node name from public_base when the post-enable status has no mapping to show yet."""
    base = config.public_base()
    try:
        host = urlsplit(base).hostname
    except ValueError:
        host = None
    return _url(host, https_port) if host else ""


def enable(port: Optional[int] = None, commands: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Map one Funnel HTTPS port to the board. Refuses a port that is not exclusively this board's."""
    if settings.get("forwarded_identity"):
        raise ReviewError(
            "forwarded_identity is on: on a public host the board would sign anyone in from an X-Forwarded-Email "
            "header, because Funnel and any trusted proxy connect from a trusted address. Turn it off first: "
            "hermes config set plugins.entries.mock-review.settings.forwarded_identity false (then restart the "
            "board). A public review signs people in with an invite, a share link or Cloudflare Access.")
    commands = [] if commands is None else commands
    want = _settings_port(port)
    board_port = config.port()
    before = status(commands)
    if not before["available"]:
        raise ReviewError(before.get("detail") or "Tailscale Funnel is not available")
    refusal = (_foreign_handler(before["status"] or {}, want, board_port)
               or _foreign_tcp(before["status"] or {}, want))
    if refusal:
        raise ReviewError(refusal)
    args = ["funnel", "--bg", "--yes", f"--https={want}", f"http://127.0.0.1:{board_port}"]
    try:
        proc = _run(args, commands)
    except FileNotFoundError:
        raise ReviewError("the Tailscale CLI (tailscale) is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise ReviewError("tailscale funnel timed out")
    if proc.returncode != 0:
        raise ReviewError(f"tailscale funnel failed: "
                          f"{(proc.stderr or proc.stdout or 'exit ' + str(proc.returncode)).strip()}")
    after = status(commands)
    url, mapping = "", None
    if after["available"]:
        found = _find_mapping(after["status"] or {}, board_port, want)
        if found:
            host, https_port, proxy = found
            url = _url(host, https_port)
            mapping = {"host": host, "https_port": https_port, "proxy": proxy}
    if not url:
        url = _fallback_url(want)
    res: Dict[str, Any] = {"ok": True, "provider": PROVIDER, "available": True, "port": want,
                           "board_port": board_port, "enabled": True, "url": url, "mapping": mapping,
                           "public_url": config.public_url(), "server_up": api.server_up(),
                           "commands": commands, "status": after.get("status")}
    notes = []
    if not config.public_url() and url:
        notes.append("public_url is not set: review links use the Funnel URL; `hermes mockreview public off` removes "
                     "the route")
    if settings.get("tailscale_identity"):
        notes.append("a Funnel request carries no proxy identity at all and the board never accepts one on a public "
                     "host, so reviewers sign in with an invite, a share link or Cloudflare Access "
                     "(docs/public-hosting.md)")
    if notes:
        res["note"] = "; ".join(notes)
    return res


def disable(port: Optional[int] = None, commands: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Remove this board's Funnel mapping, only when the port is exclusively this board's. Another app's route is
    never touched."""
    commands = [] if commands is None else commands
    want = _settings_port(port)
    board_port = config.port()
    before = status(commands)
    if not before["available"]:
        raise ReviewError(before.get("detail") or "Tailscale Funnel is not available")
    handlers = _port_handlers(before["status"] or {}, want)
    mine = [h for h in handlers if h[1] == "Proxy" and _serves_board(h[2], board_port)]
    if not mine:
        tcp = (before["status"] or {}).get("TCP")
        if isinstance(tcp, dict) and str(want) in tcp and _foreign_tcp(before["status"] or {}, want):
            raise ReviewError(f"port {want} serves a TCP entry, not this board; "
                              "refusing to remove another app's route")
        if not handlers:
            return {"ok": True, "provider": PROVIDER, "available": True, "port": want, "board_port": board_port,
                    "enabled": False, "url": "", "mapping": None, "public_url": config.public_url(),
                    "status": before.get("status"), "commands": commands,
                    "note": f"nothing is mapped on HTTPS port {want}"}
        path, kind, target = handlers[0]
        what = f"{path} -> {target}" if kind == "Proxy" else (f"{path} (a {kind} handler)" if kind else path)
        raise ReviewError(f"port {want} serves {what}, not this board; refusing to remove another app's route")
    refusal = (_foreign_handler(before["status"] or {}, want, board_port, REMOVE_TAIL)
               or _foreign_tcp(before["status"] or {}, want, REMOVE_TAIL))
    if refusal:
        raise ReviewError(refusal)
    args = ["funnel", f"--https={want}", "off"]
    try:
        proc = _run(args, commands)
    except FileNotFoundError:
        raise ReviewError("the Tailscale CLI (tailscale) is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise ReviewError("tailscale funnel timed out")
    if proc.returncode != 0:
        raise ReviewError(f"tailscale funnel off failed: "
                          f"{(proc.stderr or proc.stdout or 'exit ' + str(proc.returncode)).strip()}")
    after = status(commands)
    return {"ok": True, "provider": PROVIDER, "available": True, "port": want, "board_port": board_port,
            "enabled": False, "url": "", "mapping": None, "public_url": config.public_url(),
            "status": after.get("status"), "commands": commands}
