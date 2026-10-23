"""Local board server (127.0.0.1:8790) plus the background retry / here.now poll worker.

Two ways to run it, sharing make_server() and Worker: `mockreview serve` / `hermes mockreview serve` in the
foreground (serve() below), or inside the Hermes gateway process when autostart is on (autostart.py).

Routes:
    GET  /healthz
    GET  /                                  list of reviews
    GET  /r/<id>                            -> 301 /r/<id>/
    GET  /r/<id>/                           board (index.html)
    GET  /r/<id>/{board.js,board.css,favicon.svg}
    GET  /r/<id>/state.json                 public review state
    GET  /r/<id>/rounds/<n>/<v>/<path>      variant files (served sandboxed)
    POST /r/<id>/api/submit                 {"round", "token", "amend", "feedback"}
"""

from __future__ import annotations

import errno
from contextvars import copy_context
import html
import ipaddress
import json
import logging
import mimetypes
import signal
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from . import api, config, herenow, owner, secrets, service, settings, store
from .store import ReviewError

log = logging.getLogger("mockreview.server")

BOARD_STATIC = {"board.js": "text/javascript; charset=utf-8", "board.css": "text/css; charset=utf-8",
                "favicon.svg": "image/svg+xml"}
BOARD_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
             "frame-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
             "frame-ancestors 'none'")
# Variant pages are untrusted mock HTML: always an opaque origin, even when opened directly.
VARIANT_CSP = "sandbox allow-scripts"


LOCAL_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "X-Real-IP",
                 "Tailscale-User-Login", "Tailscale-User-Name")
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
REQUEST_TIMEOUT_S = 30     # a connection silent this long is dropped (slow / idle clients)
MAX_CONNECTIONS = 64       # concurrent connections per server; more are closed at once


# ---------------------------------------------------------------- Host / Origin allowlist (DNS-rebinding guard)

@dataclass(frozen=True)
class AllowedHosts:
    names: FrozenSet[str]
    ip_literals: bool = False      # bound to a wildcard: any IP-literal Host is this machine on some interface


LOOPBACK_ONLY = AllowedHosts(frozenset({"localhost", "127.0.0.1", "::1"}))


def hostname_of(value: Optional[str]) -> str:
    """The hostname of a Host header / authority, lowercased, port and trailing dot stripped ('' if malformed).
    'Example.COM.:8790' -> 'example.com', '[::1]:8000' -> '::1'."""
    v = (value or "").strip().lower()
    if not v or any(c in v for c in " /@\\?#"):
        return ""
    if v.startswith("["):
        end = v.find("]")
        rest = v[end + 1:] if end > 0 else "x"
        if end <= 0 or (rest and not (rest.startswith(":") and rest[1:].isdigit())):
            return ""
        return v[1:end]
    if v.count(":") > 1:           # an IPv6 literal must be bracketed in a Host header
        return ""
    host, _, port = v.partition(":")
    if port and not port.isdigit():
        return ""
    return host.rstrip(".")


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def host_allowed(host_header: Optional[str], allowed: AllowedHosts) -> bool:
    name = hostname_of(host_header)
    if not name:
        return False
    return name in allowed.names or (allowed.ip_literals and _is_ip(name))


def build_allowed_hosts(bind_host: str) -> AllowedHosts:
    """Loopback names, the bind host when it is a specific address, and the hostnames of the configured public_base,
    local_base and lan_address. Read once per server start: changing those settings needs a restart. A wildcard
    bind (0.0.0.0) also accepts any IP-literal Host (a DNS-rebinding page always carries a DNS name)."""
    names = set(LOOPBACK_ONLY.names)
    wildcard = bind_host in WILDCARD_HOSTS
    if not wildcard:
        names.add(hostname_of(f"[{bind_host}]" if ":" in bind_host else bind_host))
    for name, read in (("public_base", config.public_base), ("local_base", config.local_base)):
        try:
            h = urllib.parse.urlsplit(read()).hostname
        except Exception as e:  # a bad setting must not stop the server; the name is just not allowed
            log.warning("host allowlist: cannot read %s: %s", name, e)
            continue
        if h:
            names.add(h.lower().rstrip("."))
    try:
        lan = settings.get("lan_address").strip()
    except Exception:
        lan = ""
    if lan:
        names.add(hostname_of(f"[{lan}]" if ":" in lan and not lan.startswith("[") else lan))
    names.discard("")
    return AllowedHosts(frozenset(names), ip_literals=wildcard)


def origin_allowed(origin: str, allowed: AllowedHosts) -> bool:
    """An Origin header must be exactly http(s)://<allowed host>[:port]."""
    try:
        parts = urllib.parse.urlsplit(origin.strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or parts.path or parts.query or parts.fragment:
        return False
    return host_allowed(parts.netloc, allowed)


def local_request(peer: str, headers: Any) -> bool:
    """From this computer and not through a proxy (tailscale serve adds forwarding headers). A DNS-rebinding page
    also arrives from 127.0.0.1 without proxy headers, which is why the Host check runs first."""
    return peer in LOCAL_PEERS and not any(headers.get(h) for h in PROXY_HEADERS)


class Handler(BaseHTTPRequestHandler):
    """Every request first passes the Host allowlist (else 421 Misdirected Request). A POST with an Origin header
    must come from an allowed origin (else 403). The index (GET / and HEAD /, it lists every review) and
    /api/owner answer local requests only (local_request()); anyone else gets 404. Reviews stay reachable by
    their own /r/<id>/ URLs."""

    server_version = "mock-review/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_S

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    # ---- helpers -----------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _error(self, e: ReviewError) -> None:
        if self.path.startswith("/r/") and "/api/" in self.path:
            self._json(e.status, {"ok": False, "error": e.message})
        else:
            self._send(e.status, f"{e.status} {html.escape(e.message)}\n".encode(), "text/plain; charset=utf-8")

    def _parts(self):
        path = urllib.parse.urlsplit(self.path).path
        return [urllib.parse.unquote(p) for p in path.split("/")][1:]

    def _allowed(self) -> AllowedHosts:
        return getattr(self.server, "allowed_hosts", None) or LOOPBACK_ONLY

    def _host_ok(self) -> bool:
        """The Host allowlist, before any routing; answers 421 itself when the Host is missing or not ours."""
        hosts = self.headers.get_all("Host") or []
        if len(hosts) == 1 and host_allowed(hosts[0], self._allowed()):
            return True
        self.close_connection = True   # a POST body is unread
        self._send(421, b"421 Misdirected Request\n", "text/plain; charset=utf-8")
        return False

    def _local(self) -> bool:
        return local_request(self.client_address[0] if self.client_address else "", self.headers)

    # ---- GET ---------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        try:
            self._route_get(self._parts())
        except ReviewError as e:
            self._error(e)
        except Exception:
            log.exception("GET %s failed", self.path)
            self._send(500, b"internal error\n", "text/plain")

    def _route_get(self, parts) -> None:
        if parts == ["healthz"]:
            return self._json(200, {"ok": True, "service": "mock-review"})
        if parts == ["api", "owner"]:
            return self._owner()
        if parts in ([""], []):
            return self._index()
        if parts and parts[-1] == "favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if len(parts) < 2 or parts[0] != "r":
            raise ReviewError("not found", 404)
        rid = store.validate_id(parts[1])
        if not store.exists(rid):
            raise ReviewError("review not found", 404)
        rest = parts[2:]
        if not rest:
            return self._send(301, b"", "text/plain", {"Location": f"/r/{rid}/"})
        if rest == [""] or rest == ["index.html"]:
            body = (config.BOARD_DIR / "index.html").read_bytes()
            return self._send(200, body, "text/html; charset=utf-8",
                              {"Content-Security-Policy": BOARD_CSP, "Cache-Control": "no-cache"})
        if len(rest) == 1 and rest[0] in BOARD_STATIC:
            return self._send(200, (config.BOARD_DIR / rest[0]).read_bytes(), BOARD_STATIC[rest[0]],
                              {"Cache-Control": "no-cache"})
        if rest == ["state.json"]:
            return self._json(200, store.public_state(store.load(rid), "local"))
        if len(rest) >= 3 and rest[0] == "rounds":
            f = store.safe_variant_file(rid, rest[1], rest[2], "/".join(rest[3:]))
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            return self._send(200, f.read_bytes(), ctype,
                              {"Content-Security-Policy": VARIANT_CSP, "Cache-Control": "no-cache"})
        raise ReviewError("not found", 404)

    def _owner(self) -> None:
        """Who owns storage and wake-ups. For the local tools only: the data root and wake-route status must not be
        readable over the tailnet proxy (tailscale serve adds forwarding headers; a proxied request carries one)."""
        if not self._local():
            raise ReviewError("not found", 404)
        self._json(200, owner.describe())

    def _index(self) -> None:
        """The list of every review: local requests only, 404 for anyone else (a LAN or tailnet visitor needs the
        review's own URL)."""
        if not self._local():
            raise ReviewError("not found", 404)
        rows = []
        for rid in reversed(store.list_ids()):
            try:
                d = store.load(rid)
            except Exception:
                continue
            rows.append(
                f'<li><a href="/r/{html.escape(rid)}/">{html.escape(d["title"])}</a>'
                f'<span>{html.escape(d["mode"])} · {html.escape(d["status"])} · '
                f'{len(d["rounds"])} round{"s" if len(d["rounds"]) != 1 else ""}</span></li>'
            )
        body = (
            '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>mock-review</title><style>body{font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:640px;'
            'margin:48px auto;padding:0 20px;color:#18181b;background:#f7f7f5}h1{font-size:20px}ul{list-style:none;'
            'padding:0}li{display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-bottom:1px solid #e4e4e0}'
            'span{color:#71717a;font-size:13px}a{color:#4f46e5;text-decoration:none;font-weight:600}'
            '@media(prefers-color-scheme:dark){body{background:#0f0f12;color:#ececf1}li{border-color:#2a2a31}a{color:#a5a4ff}}'
            '</style><h1>Mock reviews</h1><ul>' + ("".join(rows) or "<li>No reviews yet.</li>") + "</ul>"
        ).encode()
        self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": BOARD_CSP})

    # ---- POST --------------------------------------------------------
    def do_POST(self) -> None:
        if not self._host_ok():
            return
        try:
            parts = self._parts()
            if len(parts) != 4 or parts[0] != "r" or parts[2:] != ["api", "submit"]:
                raise ReviewError("not found", 404)
            rid = store.validate_id(parts[1])
            if not store.exists(rid):
                raise ReviewError("review not found", 404)
            origin = self.headers.get("Origin")
            if origin is not None:   # browsers send it on a cross-origin POST; curl and the CLI don't
                if origin.strip().lower() == "null":
                    raise ReviewError("requests from sandboxed frames are not accepted", 403)
                if not origin_allowed(origin, self._allowed()):
                    raise ReviewError("cross-origin requests are not accepted", 403)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise ReviewError("Content-Type must be application/json", 415)
            length = self.headers.get("Content-Length")
            if length is None or not length.isdigit():
                raise ReviewError("Content-Length required", 411)
            if int(length) > config.MAX_BODY:
                self.close_connection = True
                raise ReviewError("Feedback is too large (2 MB max).", 413)
            raw = self.rfile.read(int(length))
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ReviewError("body is not valid JSON")
            if not isinstance(body, dict):
                raise ReviewError("body must be a JSON object")
            res = service.submit(rid, body.get("round"), body.get("feedback"), body.get("token"),
                                 amend=body.get("amend") is True, source="local")
            self._json(200, res)
        except ReviewError as e:
            self.close_connection = True  # the body may be unread; don't reuse the socket
            self._json(e.status, {"ok": False, "error": e.message})
        except Exception:
            self.close_connection = True
            log.exception("POST %s failed", self.path)
            self._json(500, {"ok": False, "error": "internal error"})


class Worker(threading.Thread):
    """Retries pending notifications, polls here.now Site Data while a here.now review is open, and runs the
    housekeeping pass (auto_close_idle_days / keep_closed_days) every housekeeping_seconds. Each tick runs in the
    owning profile's secret scope (secrets.owner_scope; owner_home is read once, here)."""

    def __init__(self, tick: float = 5.0):
        super().__init__(name="mockreview-worker", daemon=True)
        self._context = copy_context()
        self.owner_home = config.hermes_home()
        self.stop_event = threading.Event()
        self.tick = tick
        self.last_poll = 0.0
        self.last_housekeeping = 0.0
        self.last_stalled = 0.0

    def run(self) -> None:
        self._context.copy().run(self._run)

    def _ticks(self):
        """Yield once per tick until stopped, inside the owning profile's secret scope, rebuilt every tick."""
        while not self.stop_event.wait(self.tick):
            with secrets.owner_scope(self.owner_home):
                yield

    def _run(self) -> None:
        for _ in self._ticks():
            try:
                service.retry_pending()
            except Exception:
                log.exception("retry pass failed")
            if settings.get("stale_after_minutes") and time.time() - self.last_stalled >= service.STALLED_SCAN_SECONDS:
                self.last_stalled = time.time()
                try:
                    service.check_stalled()
                except Exception:
                    log.exception("stalled-notice pass failed")
            if time.time() - self.last_poll >= config.poll_seconds():
                self.last_poll = time.time()
                try:
                    if herenow.open_reviews():
                        herenow.poll_all()
                except Exception:
                    log.exception("here.now poll failed")
            try:
                if time.time() - self.last_housekeeping >= settings.get("housekeeping_seconds"):
                    self.last_housekeeping = time.time()
                    res = api.housekeeping()
                    if res["closed"] or res["pruned"]:
                        log.info("housekeeping: closed %s, pruned %s", res["closed"], res["pruned"])
            except Exception:
                log.exception("housekeeping failed")


ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
_ADDR_IN_USE = {errno.EADDRINUSE, 48, 98, 10048}   # macOS, Linux, Windows (WSAEADDRINUSE)


class BoardHTTPServer(ThreadingHTTPServer):
    """allowed_hosts (the Host / Origin allowlist) is computed once, here: a changed public_base, local_base or
    lan_address takes effect at the next server start. At most MAX_CONNECTIONS connections are handled at once;
    one more is closed straight away (each handler thread also drops a client silent for REQUEST_TIMEOUT_S)."""

    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that is already in use: bind exclusively there.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, server_address: Tuple[Any, ...], handler: Any, bind_and_activate: bool = True):
        # Capture the owner/plugin scope before crossing either thread boundary.
        self._context = copy_context()
        self.owner_home = config.hermes_home()   # the profile whose secrets each request binds (secrets.owner_scope)
        self.allowed_hosts = build_allowed_hosts(str(server_address[0]))
        self.max_connections = MAX_CONNECTIONS
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._full_warned: Optional[float] = None
        super().__init__(server_address, handler, bind_and_activate)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            now = time.monotonic()
            if self._full_warned is None or now - self._full_warned >= 60:   # at most one warning a minute
                self._full_warned = now
                log.warning("%d connections open: closing new ones until some finish", self.max_connections)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)   # starts process_request_thread
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            # Each request needs its own copy: a Context cannot be entered concurrently.
            self._context.copy().run(self._scoped_request_thread, request, client_address)
        finally:
            self._slots.release()

    def _scoped_request_thread(self, request: Any, client_address: Any) -> None:
        with secrets.owner_scope(self.owner_home):
            super().process_request_thread(request, client_address)

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class BoardHTTPServer6(BoardHTTPServer):
    address_family = socket.AF_INET6


def addr_in_use(e: BaseException) -> bool:
    return isinstance(e, OSError) and (e.errno in _ADDR_IN_USE or getattr(e, "winerror", None) == 10048)


def display_url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def make_server(host: Optional[str] = None, port: Optional[int] = None) -> ThreadingHTTPServer:
    """Bind host:port now. Raises OSError when it can't, SystemExit for a host outside the allow-list."""
    host = config.host() if host is None else host
    port = config.port() if port is None else port
    if host not in ALLOWED_HOSTS:
        raise SystemExit("mock-review binds to loopback (expose it with `tailscale serve`) or, for local links "
                         "on your network, 0.0.0.0 — nothing else.")
    if host == "0.0.0.0":
        log.warning("listening on every network interface: any device on this network can open reviews and "
                    "send feedback (no login)")
    cls = BoardHTTPServer6 if ":" in host else BoardHTTPServer
    return cls((host, port), Handler)


def log_settings() -> None:
    info = settings.describe()
    log.info("settings: config %s read with %s%s", info["config"], info["reader"],
             f" — IGNORED: {info['error']}" if info["error"] else "")


def install_stop_signals(stop: Callable[[int, Any], None]) -> List[Tuple[int, Any]]:
    """SIGTERM (and SIGBREAK, Ctrl+Break on Windows) where they exist, from the main thread only; returns what to
    restore. Ctrl+C stays a KeyboardInterrupt: SIGINT gets Python's default handler back if it was ignored."""
    if threading.current_thread() is not threading.main_thread():
        return []
    installed: List[Tuple[int, Any]] = []
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            installed.append((sig, signal.signal(sig, stop)))
        except (OSError, ValueError):
            pass
    sigint = getattr(signal, "SIGINT", None)
    if sigint is not None and signal.getsignal(sigint) in (signal.SIG_IGN, None):
        try:
            installed.append((sigint, signal.signal(sigint, signal.default_int_handler)))
        except (OSError, ValueError):
            pass
    return installed


def serve(host: Optional[str] = None, port: Optional[int] = None, worker: bool = True) -> int:
    """The foreground server: serve_forever in this thread until Ctrl+C or SIGTERM. No fork, no service manager.
    Returns the exit code: 0 after a clean stop, 2 when host:port can't be bound."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = config.host() if host is None else host
    port = config.port() if port is None else port
    log_settings()
    try:
        srv = make_server(host, port)
    except OSError as e:
        where = display_url(host, port)
        if addr_in_use(e):
            print(f"mock-review: {where} is already in use (is the gateway's autostart board running? run "
                  "`hermes mockreview owner`), or pass another --port.", file=sys.stderr)
        else:
            print(f"mock-review: cannot listen on {where}: {e.strerror or e}", file=sys.stderr)
        return 2
    except SystemExit as e:
        print(f"mock-review: {e}", file=sys.stderr)
        return 2
    owner.mark_owner_process()
    w = Worker() if worker else None
    if w:
        w.start()

    def _stop(signum, _frame):
        log.info("signal %s, shutting down", signum)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    restore = install_stop_signals(_stop)
    log.info("mock-review serving on %s (data: %s)", display_url(host, port), config.data_root())
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        if w:
            w.stop_event.set()
        srv.server_close()
        owner.mark_owner_process(False)
        for sig, prev in restore:
            try:
                signal.signal(sig, prev)
            except (OSError, ValueError, TypeError):
                pass
        if w:
            w.join(5)
        log.info("stopped")
    return 0
