"""Local board server (127.0.0.1:8790) plus the background retry / stalled-notice worker.

Two ways to run it, sharing make_server() and Worker: `mockreview serve` / `hermes mockreview serve` in the
foreground (serve() below), or inside the Hermes gateway process when autostart is on (autostart.py).

Routes (every one behind the Host allowlist and the access layer: auth.py, access.py):
    GET  /healthz
    GET  /                                  the reviews this person may open (sign-in page when nobody is signed in)
    GET  /signin                            the sign-in page
    GET  /login/<token>, /invite/<token>    confirmation form; POST uses the link once and opens a session
    GET  /s/<token>                         a share link: guest session for its generation, then the board
    GET  /settings, /_/settings.{js,css}    Settings (Members, Invites for admins; Devices for everyone)
    GET  /api/me|sessions|tokens|users|invites
    POST /api/logout, /api/sessions/sign-out-others, /api/tokens, /api/tokens/<id>/revoke,
         /api/invites, /api/invites/<id>/revoke, /api/users/<id>/role, /api/users/<id>/delete
    GET  /api/owner                         local requests only
    GET  /r/<id>                            -> 301 /r/<id>/
    GET  /r/<id>/                           board (index.html)
    GET  /r/<id>/{board.js,board.css,favicon.svg,vendor/purify.min.js}
    GET  /r/<id>/state.json                 public review state plus `me` (role and capabilities)
    GET  /r/<id>/rounds/<n>/<v>/<path>      variant files, and an artifact page at rounds/<n>/a/blocks/<id>.html
                                            (served sandboxed)
    GET  /r/<id>/api/share                  the share view: link, seats, people, rounds (owner)
    POST /r/<id>/api/submit                 {"round", "token", "amend", "feedback"} (owner; wakes only from private
                                            ingress, see wakeguard.request_gate)
    POST /r/<id>/api/share                  {"action": create|regenerate|update|stop, "guest_role", "expires",
                                             "audience", "round"} -> the share view (owner)
    POST /r/<id>/api/people                 {"action": invite|grant|revoke, ...} (owner)
    POST /r/<id>/api/guest-name             {"name"} (a guest of this review)
"""

from __future__ import annotations

import datetime
import errno
from contextvars import copy_context
import hmac
import html
import ipaddress
import json
import logging
import mimetypes
import os
import signal
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple, Union

from . import (access, api, auth, config, export, frames, live, owner, pages, runtime, secrets, service, settings, sse,
               store, threads, users, wakeguard)
from .store import ReviewError

log = logging.getLogger("mockreview.server")


def _link_csrf(server: Any, purpose: str, token: str) -> str:
    """The confirmation form's token: HMAC(purpose:token) under the server's per-start runtime secret.

    Bound to the link itself, not to the shared `mr_csrf` cookie. Cookies are host-scoped and ports don't count, so
    one browser reaching the board on two routes shares one `mr_csrf`: a share link (or a board tab) opened in the
    same browser used to overwrite the value this form posts, and pressing Sign in failed with a raw JSON 403. The
    token is unguessable without the link (the secret never leaves the host) and changes when the server restarts,
    which re-renders the form instead of failing the click.
    """
    secret = getattr(server, "runtime_secret", "")
    if not secret:
        raise RuntimeError("the board server has no runtime secret: build it with make_server()")
    return runtime.mac(secret, f"{purpose}:{token}")


def _age(iso: object) -> str:
    """A short, stable age for the review list: just now / N min ago / N h ago / N d ago / Mon D."""
    try:
        stamp = str(iso or "").replace("Z", "+00:00")
        then = datetime.datetime.fromisoformat(stamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=datetime.timezone.utc)
        seconds = max(0, int((datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()))
    except (TypeError, ValueError):
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%d min ago" % (seconds // 60)
    if seconds < 86400:
        return "%d h ago" % (seconds // 3600)
    if seconds < 7 * 86400:
        return "%d d ago" % (seconds // 86400)
    return then.strftime("%b %d").replace(" 0", " ")



BOARD_STATIC = {"anchors.js": "text/javascript; charset=utf-8", "lib/anchors.js": "text/javascript; charset=utf-8",
                "avatar.js": "text/javascript; charset=utf-8",
                "board.js": "text/javascript; charset=utf-8", "board.css": "text/css; charset=utf-8",
                "tw.css": "text/css; charset=utf-8",
                "threads.js": "text/javascript; charset=utf-8",
                "favicon.svg": "image/svg+xml", "vendor/purify.min.js": "text/javascript; charset=utf-8",
                "vendor/snapdom.min.js": "text/javascript; charset=utf-8",
                "vendor/turndown.min.js": "text/javascript; charset=utf-8",
                "export-print.js": "text/javascript; charset=utf-8",
                "export-capture.js": "text/javascript; charset=utf-8"}
BOARD_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
             "frame-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
             "frame-ancestors 'none'")
UI_STATIC = {"settings.js": "text/javascript; charset=utf-8", "settings.css": "text/css; charset=utf-8",
             "avatar.js": "text/javascript; charset=utf-8",
             "tw.css": "text/css; charset=utf-8", "favicon.svg": "image/svg+xml"}
# POST /r/<id>/api/<action> and the capability each needs (access.CAPS).
REVIEW_POSTS = {"submit": "submit", "share": "manage", "people": "manage", "guest-name": "read", "pin-fix": "submit"}
# The only routes a personal access token may authenticate (the stdio MCP server's two writes). Everywhere else a
# bearer is refused outright, never downgraded to the session or the loopback owner.
TOKEN_ROUTES = frozenset({"reply", "status"})


def token_route(parts: List[str]) -> bool:
    """True only for POST /r/<id>/api/threads/<tid>/{reply,status}: what an agent's token is for."""
    return (len(parts) == 6 and parts[0] == "r" and parts[2] == "api"
            and parts[3] == "threads" and parts[5] in TOKEN_ROUTES)


def _mentions_hermes(comment: Dict[str, Any]) -> bool:
    """True when this comment itself carries the hermes mention; a thread's activated flag may predate it."""
    return any(m.get("kind") == "hermes" for m in comment.get("mentions") or [])


def thread_post_cap(rest: List[str]) -> Optional[str]:
    """The capability a thread route needs, or None when the path is not one of them."""
    if rest == ["threads"]:
        return "comment"
    if len(rest) == 3 and rest[0] == "threads" and rest[2] in ("reply", "status", "read"):
        return "comment" if rest[2] != "read" else "read"
    if len(rest) == 2 and rest[0] == "comments":
        return "comment"
    if len(rest) == 3 and rest[0] == "comments" and rest[2] == "delete":
        return "comment"
    if rest == ["typing"]:
        return "comment"
    if rest == ["activity", "read"]:
        return "read"
    return None
API_MAX_BODY = 64 * 1024
BODY_DRAIN_MAX = 1024 * 1024   # unread request bytes consumed before a refused response closes the socket
DEAD_JSON = {"ok": False, "error": "not found"}
# Variant pages are untrusted mock HTML: always an opaque origin, even when opened directly.
VARIANT_CSP = "sandbox allow-scripts"
# The artifact-grade policy for round documents and dead ends under a round: a page can navigate its own frame to
# another file of the round, and that document must have no network either. A dead end's header is chosen from the path
# shape alone, never from the review having an artifact. (A header on an image or a video is ignored by the browser, so
# mock media loads.)
ARTIFACT_ROUND_CSP = VARIANT_CSP + "; " + store.ARTIFACT_CSP

RANGE_UNSATISFIABLE = "unsatisfiable"


def _is_ascii_digits(s: str) -> bool:
    return s.isascii() and s.isdigit()


def _ascii_int(value: str) -> Optional[int]:
    return threads.ascii_int(value.strip(), signed=True)


def _ascii_id(value: Optional[str]) -> Optional[int]:
    return threads.ascii_int(value)


def parse_range_header(value: str, size: int) -> Optional[Union[Tuple[int, int], str]]:
    """One `bytes` range as an inclusive (start, end) pair, RANGE_UNSATISFIABLE, or None to ignore the header.

    A media element sends exactly one of the three `bytes` forms (`N-M`, `N-`, `-N`), so only those are implemented.
    Anything else (several ranges, another unit, a malformed header, a last byte before the first) answers 200 with the
    whole file, which HTTP allows; a range no byte of this file can satisfy answers 416 instead. A digit cap keeps a
    pathological header out of int()'s own limit in newer Pythons.
    """
    spec = (value or "").strip()
    if not spec.startswith("bytes="):
        return None
    parts = spec[6:].strip().split(",")
    if len(parts) != 1:
        return None
    first, dash, last = parts[0].partition("-")
    first, last = first.strip(), last.strip()
    if not dash or (first and not _is_ascii_digits(first)) or (last and not _is_ascii_digits(last)):
        return None
    if not first and not last:
        return None
    if first:
        start = int(first) if len(first) <= 18 else size
        if start >= size:
            return RANGE_UNSATISFIABLE
        if not last:
            return start, size - 1
        end = int(last) if len(last) <= 18 else size - 1
        if end < start:
            return None   # an invalid spec (last before first): HTTP lets the server ignore the header
        return start, min(end, size - 1)
    wanted = int(last) if len(last) <= 18 else size
    if wanted == 0 or size == 0:
        return RANGE_UNSATISFIABLE
    return max(0, size - wanted), size - 1


LOCAL_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "X-Real-IP",
                 "Tailscale-User-Login", "Tailscale-User-Name", "Tailscale-Funnel-Request", "Cf-Access-Jwt-Assertion",
                 "Cf-Connecting-Ip", "X-Forwarded-User", "X-Forwarded-Email")
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
REQUEST_TIMEOUT_S = 30     # a connection silent this long is dropped (slow / idle clients)
MAX_CONNECTIONS = 64       # concurrent connections per server; more are closed at once


# ---------------------------------------------------------------- Host / Origin allowlist (DNS-rebinding guard)

@dataclass(frozen=True)
class AllowedHosts:
    names: FrozenSet[str]
    ip_literals: bool = False      # bound to a wildcard: any IP-literal Host is this machine on some interface
    public_names: FrozenSet[str] = frozenset()   # hosts reached through a public URL: never the loopback owner
                                                   # and never the full /healthz body


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


def _url_hostname(name: str, read: Callable[[], str]) -> str:
    """The hostname of one configured URL, lowercased; '' when it is unset or unreadable. A bad setting must not stop
    the server, so a failure is logged and only means that name is not allowed."""
    try:
        h = urllib.parse.urlsplit(read()).hostname
    except Exception as e:
        log.warning("host allowlist: cannot read %s: %s", name, e)
        return ""
    return (h or "").lower().rstrip(".")


def build_allowed_hosts(bind_host: str) -> AllowedHosts:
    """Loopback names, the bind host when it is a specific address, and the hostnames of the configured public_url,
    the Tailscale Funnel URL, public_base, local_base and lan_address. Read once per server start: changing those
    settings needs a restart. A wildcard bind (0.0.0.0) also accepts any IP-literal Host (a DNS-rebinding page always
    carries a DNS name). The public_url and Funnel names are also kept in public_names, minus any loopback name: a
    request on one of them is never the loopback owner. public_base is not public: it is the tailnet Serve address and
    Serve keeps the tailnet Host, so counting it public would refuse every tailnet identity on the one host it
    forwards."""
    names = set(LOOPBACK_ONLY.names)
    wildcard = bind_host in WILDCARD_HOSTS
    if not wildcard:
        names.add(hostname_of(f"[{bind_host}]" if ":" in bind_host else bind_host))
    public = set()
    for name, read in (("public_url", config.public_url), ("funnel_url", config.funnel_url),
                       ("public_base", config.public_base), ("local_base", config.local_base)):
        h = _url_hostname(name, read)
        if not h:
            continue
        names.add(h)
        if name in ("public_url", "funnel_url"):
            public.add(h)
    try:
        lan = settings.get("lan_address").strip()
    except Exception:
        lan = ""
    if lan:
        names.add(hostname_of(f"[{lan}]" if ":" in lan and not lan.startswith("[") else lan))
    names.discard("")
    public -= set(LOOPBACK_ONLY.names)
    public.discard("")
    return AllowedHosts(frozenset(names), ip_literals=wildcard, public_names=frozenset(public))


def _request_port(host_header: Optional[str], secure: bool) -> Optional[int]:
    """The port this request reached: the one in Host, else 443 for https / 80 for http. None for a malformed Host.
    `hostname_of` has already validated the authority, so this only reads back the port it accepted."""
    if not hostname_of(host_header):
        return None
    value = (host_header or "").strip()
    if value.startswith("["):
        rest = value[value.find("]") + 1:]
        port = rest[1:] if rest.startswith(":") else ""
    else:
        _, sep, port = value.partition(":")
        port = port if sep else ""
    if not port:
        return 443 if secure else 80
    return int(port) if _is_ascii_digits(port) else None


def origin_allowed(origin: str, allowed: AllowedHosts, request_host: str, secure: bool) -> bool:
    """An Origin header must name this very server: http(s), an allowed host, and the same scheme and port the request
    reached. A host-only match let http://localhost:3000 post to a board on 127.0.0.1:8790: the Origin's hostname was
    allowed, so a planted mr_csrf cookie could mint an admin invite; only Sec-Fetch-Site stopped it."""
    try:
        parts = urllib.parse.urlsplit(origin.strip())
        origin_port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or parts.path or parts.query or parts.fragment:
        return False
    if not host_allowed(parts.netloc, allowed):
        return False
    if origin_port is None:
        origin_port = 443 if parts.scheme == "https" else 80
    want_port = _request_port(request_host, secure)
    return (want_port is not None and parts.scheme == ("https" if secure else "http")
            and origin_port == want_port)


def visible_rounds(rid: str, data: Dict[str, Any], who: "auth.Who", decision: "access.Decision") -> List[int]:
    """The round numbers this request may see, in stored order. Everyone but a viewer guest sees every round; a viewer
    guest is pinned to the share link's round. Exactly state.json's answer, so an export can never show more than the
    board does: a pin that names an existing round narrows to that one, and a link left on `latest` (or a pin naming a
    round that no longer exists, which state.json already falls back to the full view for) sees every round. This asks
    `threads.sees_round`, the same gate state.json, the threads API, SSE and raw round files use."""
    scope = threads.view_scope(rid, guest=who.principal.kind == "guest", role=decision.role, data=data)
    return [r["n"] for r in data["rounds"] if threads.sees_round(scope, r["n"])]


def local_request(peer: str, headers: Any) -> bool:
    """From this computer and not through a proxy (tailscale serve adds forwarding headers). A DNS-rebinding page
    also arrives from 127.0.0.1 without proxy headers, which is why the Host check runs first."""
    return peer in LOCAL_PEERS and not any(headers.get(h) for h in PROXY_HEADERS)


def redact_link_tokens(msg: str) -> str:
    """Redact the token in every spelling the router accepts: _parts() unquotes each path segment, so /%69nvite/x
    routes to /invite/x and a literal prefix match would log the token. Each path-like candidate is percent-decoded
    once; one that decodes to a link route keeps the route and loses the token. A `~<token>` round-frame segment is
    redacted too, whatever its spelling. The rest of the line is untouched."""
    prefixes = ("/login/", "/invite/", "/s/")
    out = []
    i = 0
    while i < len(msg):
        if msg[i] != "/":
            out.append(msg[i])
            i += 1
            continue
        j = i + 1
        while j < len(msg) and not msg[j].isspace() and msg[j] not in "\"'":
            j += 1
        candidate = msg[i:j]
        decoded = urllib.parse.unquote(candidate)
        for prefix in prefixes:
            if decoded.startswith(prefix):
                candidate = prefix + "…"
                break
        else:
            pieces = candidate.split("/")
            for k, piece in enumerate(pieces):
                if k and len(urllib.parse.unquote(piece)) > 1 and urllib.parse.unquote(piece).startswith("~"):
                    pieces[k] = "~…"
            candidate = "/".join(pieces)
        out.append(candidate)
        i = j
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    """Every request first passes the Host allowlist (else 421 Misdirected Request). Every POST must carry an allowed
    Origin (or none), must not be cross-site by Fetch Metadata, and must pass the double-submit CSRF token (else 403),
    except the two thread writes that carry a personal access token as a bearer (see token_route).
    Then the access layer: auth.identify() names the person (read fresh on every request) and auth.decide() /
    access.resolve() decides each review. Nobody signed in gets the sign-in page; a review that is missing or not
    theirs gets the same dead response. /api/owner answers local requests only."""

    server_version = "mock-review/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_S

    def log_message(self, fmt: str, *args: Any) -> None:
        msg = fmt % args
        try:
            target = getattr(self, "requestline", "").split()[1]
            parts = self._parts()
            if target and len(parts) == 2 and parts[0] in ("login", "invite", "s"):
                msg = msg.replace(target, f"/{parts[0]}/…")
        except Exception:
            pass
        log.info("%s %s", self.address_string(), redact_link_tokens(msg))

    # ---- helpers -----------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, headers: Optional[Dict[str, str]] = None,
              cookies: Optional[List[str]] = None) -> None:
        if self.close_connection:
            self._discard_body()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if "Referrer-Policy" not in (headers or {}):
            self.send_header("Referrer-Policy", "no-referrer")
        if not (headers and any(k.lower() == "strict-transport-security" for k in headers)):
            if auth.https(self._req()):   # only behind a trusted proxy that reports HTTPS, never on plain HTTP
                self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        for c in self._with_renewal(cookies or []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str, headers: Dict[str, str]) -> None:
        """One round file, answering a single byte range and advertising ranges on every response.

        A media element only gets a duration and a scrubbable timeline from a range-capable loader (`Accept-Ranges` plus
        a 206), and Safari may refuse a video served from a round without one. Only the requested slice is read, so a
        100-byte range on a 50 MB asset never loads the asset.
        """
        size = path.stat().st_size
        base = dict(headers, **{"Accept-Ranges": "bytes"})
        wanted = parse_range_header(self.headers.get("Range") or "", size)
        if wanted == RANGE_UNSATISFIABLE:
            return self._send(416, b"", ctype, dict(base, **{"Content-Range": f"bytes */{size}"}))
        if wanted is None:
            return self._send(200, path.read_bytes(), ctype, base)
        start, end = wanted
        with path.open("rb") as fh:
            fh.seek(start)
            body = fh.read(end - start + 1)
        return self._send(206, body, ctype, dict(base, **{
            "Content-Range": f"bytes {start}-{end}/{size}"}))

    def _with_renewal(self, cookies: List[str]) -> List[str]:
        """Any response to a request whose session just slid re-issues mr_session (and mr_csrf) with a fresh Max-Age,
        unless the response sets mr_session itself (sign-out, a used link, a new guest)."""
        who = getattr(self, "_who_cache", None)
        if who is None or any(c.startswith(auth.SESSION_COOKIE + "=") for c in cookies):
            return cookies
        renewed = auth.renewed_cookies(self._req(), who)
        if not renewed:
            return cookies
        return [c for c in cookies if not c.startswith(auth.CSRF_COOKIE + "=")] + renewed

    def _json(self, code: int, obj: Any, cookies: Optional[List[str]] = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"}, cookies)

    def _page(self, code: int, body: bytes, cookies: Optional[List[str]] = None, csp: str = pages.PAGE_CSP,
              referrer: str = "no-referrer") -> None:
        self._send(code, body, "text/html; charset=utf-8", {"Content-Security-Policy": csp, "Cache-Control": "no-store",
                                                             "Referrer-Policy": referrer}, cookies)

    def _api_path(self) -> bool:
        path = urllib.parse.urlsplit(self.path).path
        return ("/api/" in path or path.startswith("/api/")
                or (path.startswith("/r/") and path.rstrip("/").endswith("/events")))

    def _dead(self) -> None:
        """Missing and forbidden look the same, byte for byte."""
        if self._api_path() or self.path.endswith("state.json"):
            return self._json(404, DEAD_JSON)
        parts = self._parts()
        if len(parts) >= 5 and parts[0] == "r" and parts[2] == "rounds":
            # A dead end under a rounds/<n>/<v>/ path always carries the artifact-grade header: the choice must never
            # depend on the review, or a forbidden review would answer differently from a missing one. The frame that
            # reached it stays sandboxed and without a network, exactly like the round files it can navigate to.
            return self._page(404, pages.DEAD, csp=ARTIFACT_ROUND_CSP)
        return self._page(404, pages.DEAD)

    def _signin(self, who: "auth.Who") -> None:
        if self._api_path() or self.path.endswith("state.json"):
            return self._json(401, {"ok": False, "error": "sign in first", "signin": "/signin"})
        return self._page(401, pages.signin(who.identity))

    def _error(self, e: ReviewError) -> None:
        if self._api_path():
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

    def _peer(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _local(self) -> bool:
        """This computer's owner, unless the Host is one of the public names: a proxy that forwards without setting
        X-Forwarded-For must still not hand a stranger the owner account."""
        if hostname_of(self.headers.get("Host")) in self._allowed().public_names:
            return False
        return local_request(self._peer(), self.headers)

    def _req(self) -> "auth.Request":
        return auth.Request(self._peer(), self.headers, local=self._local(),
                            public_host=hostname_of(self.headers.get("Host")) in self._allowed().public_names)

    def _who(self) -> "auth.Who":
        who = getattr(self, "_who_cache", None)
        if who is None:
            who = self._who_cache = auth.identify(self._req())
        return who

    def _cookie_session(self, who: "auth.Who") -> Optional[Dict[str, Any]]:
        """The stored session this request's mr_session cookie names. It is not always who.session: a linked proxy
        identity signs the request in before the cookie is read, and sign-out must still end the cookie's session."""
        if who.session is not None:
            return who.session
        return users.session_row(self._req().cookies.get(auth.SESSION_COOKIE, ""))

    def _current_session_hash(self, who: "auth.Who") -> str:
        """This device's session, when it belongs to the account making the request."""
        sess = self._cookie_session(who)
        if sess is None or who.user is None or sess.get("user_id") != who.user["id"]:
            return ""
        return str(sess["id_hash"])

    def _csrf_cookie(self, who: "auth.Who") -> List[str]:
        c = auth.csrf_cookie_for(self._req(), who)
        return [c] if c else []

    # ---- GET ---------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self._who_cache = None
        if not self._host_ok():
            return
        try:
            if auth.bearer_token(self._req()) is not None:
                # A personal access token is a write credential for an agent. Reads — pages, assets, the API and
                # /api/owner — never authenticate by it, and it never falls back to the session or the owner.
                self.close_connection = True
                raise ReviewError("a personal access token is only accepted for a thread reply or status write", 403)
            self._route_get(self._parts())
        except ReviewError as e:
            self._error(e)
        except Exception:
            log.exception("GET %s failed", urllib.parse.urlsplit(self.path).path.split("/")[1:2])
            self._send(500, b"internal error\n", "text/plain")

    def _route_get(self, parts) -> None:
        if parts == ["healthz"]:
            public = hostname_of(self.headers.get("Host")) in self._allowed().public_names
            return self._json(200, {"ok": True} if public else {"ok": True, "service": "mock-review"})
        if parts == ["api", "owner"]:
            return self._owner()
        if parts == ["api", "owner", "challenge"]:
            return self._owner_challenge()
        if parts and parts[-1] == "favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if len(parts) == 2 and parts[0] == "_" and parts[1] in UI_STATIC:
            return self._send(200, (config.BOARD_DIR / parts[1]).read_bytes(), UI_STATIC[parts[1]],
                              {"Cache-Control": "no-cache"})
        if len(parts) == 2 and parts[0] in ("login", "invite"):
            return self._link_page(parts[0], parts[1])
        if len(parts) == 2 and parts[0] == "s":
            return self._share_join(parts[1])
        who = self._who()
        if parts == ["signin"]:
            if who.principal.signed_in:
                return self._send(303, b"", "text/plain", {"Location": "/"})
            return self._page(200, pages.signin(who.identity))
        if parts in ([""], []):
            return self._index(who)
        if parts == ["settings"]:
            if not who.principal.signed_in:
                return self._signin(who)
            if who.user is None:
                return self._dead()
            return self._page(200, pages.SETTINGS, self._csrf_cookie(who), csp=pages.SETTINGS_CSP)
        if parts and parts[0] == "api":
            return self._api_get(who, parts[1:])
        if len(parts) < 2 or parts[0] != "r":
            raise ReviewError("not found", 404)
        try:
            rid = store.validate_id(parts[1])
        except ReviewError:
            return self._dead() if who.principal.signed_in else self._signin(who)
        exists = store.exists(rid)
        decision = auth.decide(who, rid, exists)
        frame_round: Optional[int] = None
        frame_token: Optional[str] = None
        scope_who = who                      # the request's own identity, unless a token stands in for it
        if len(parts) > 4 and parts[2] == "rounds" and parts[4].startswith("~"):
            # One signed token per (viewer, review, round), in the path so every relative URL a round page derives
            # inherits it. Only a valid token authorises the request; an invalid one leaves the request's own
            # identity in charge, with the path normalised so the file routes exactly as it does today.
            frame_token = parts[4][1:]
            grant = frames.validate(frame_token, rid, parts[3], who, self._local(), exists)
            if grant is not None:
                frame_round = _ascii_id(parts[3])
                if decision.result == access.SIGNIN:
                    decision = grant.decision
                    scope_who = grant.who    # cookieless frame: the token's identity, not anonymous
            parts = parts[:4] + parts[5:]
        if decision.result == access.SIGNIN:
            return self._signin(who)
        if not access.can(decision, "read"):
            return self._dead()
        rest = parts[2:]
        if not rest:
            return self._send(301, b"", "text/plain", {"Location": f"/r/{rid}/"})
        if rest == [""] or rest == ["index.html"]:
            body = (config.BOARD_DIR / "index.html").read_bytes()
            return self._send(200, body, "text/html; charset=utf-8",
                              {"Content-Security-Policy": BOARD_CSP, "Cache-Control": "no-cache"},
                              self._csrf_cookie(who))
        static = "/".join(rest)
        if static in BOARD_STATIC:
            return self._send(200, (config.BOARD_DIR / static).read_bytes(), BOARD_STATIC[static],
                              {"Cache-Control": "no-cache"})
        if rest == ["state.json"]:
            data = store.load(rid)
            scope = self._view_scope(rid, who, decision, data=data)
            state = store.public_state(data)
            state["me"] = auth.describe(who, decision)
            state["threads"] = threads.summary(rid, scope)
            if who.principal.kind == "guest" and decision.role == "viewer":
                state.pop("token", None)
                for rnd in state["rounds"]:
                    rnd["feedback"] = None
                kept = [rnd for rnd in state["rounds"] if threads.sees_round(scope, rnd["n"])]
                if scope.pinned is not None:
                    # A pin is reported even when it hides nothing (a one-round review pinned to that round): the
                    # board shows "This link shows round N." from it, and the link is still pinned.
                    state["rounds"] = kept
                    state["current_round"] = scope.pinned
                    state["pinned_round"] = scope.pinned
            # A round's frame needs its own credential (see frames.py). Mint only for the rounds this viewer can see,
            # so a pinned guest never receives a token for a round the state itself hides. Only who.session counts:
            # a cookie that is not this request's own session may be foreign or expired (session_row ignores both).
            sess = who.session
            if sess is not None:
                binding = str(sess["id_hash"])
            elif who.source == "loopback":
                binding = frames.LOCAL
            elif who.user is not None:
                binding = frames.account_binding(int(who.user["id"]))
            else:
                binding = ""
            state["frame_tokens"] = {str(rnd["n"]): frames.mint(rid, int(rnd["n"]), binding)
                                     for rnd in state["rounds"]}
            state["now"] = time.time()
            return self._json(200, state, self._csrf_cookie(who))
        if rest == ["api", "threads"]:
            return self._threads_get(rid, who, decision)
        if rest == ["events"]:
            return self._events(rid, who, decision)
        if rest == ["api", "share"]:
            if not access.can(decision, "manage"):
                raise ReviewError("only the review's owner manages its share link", 403)
            return self._json(200, self._share_view(rid))
        if rest == ["export.html"] or (len(rest) == 2 and rest[0] == "export"
                                        and rest[1] in ("print", "capture")):
            return self._export(who, rid, "html" if rest == ["export.html"] else rest[1], decision)
        if len(rest) >= 3 and rest[0] == "rounds":
            # Enforce the share link's round pin before the path is resolved, so no status, body or timing can reveal
            # whether an unpinned round's file exists. A frame token authorises identity only; when it stands in for a
            # missing session the scope is built from the token's own identity, so the share link's pin still applies.
            # The path round must pass the same gate state.json, threads, SSE and the exports use, whatever variant,
            # asset, media Range or token the request carries.
            if not threads.sees_round(self._view_scope(rid, scope_who, decision), rest[1]):
                return self._dead()
            # A round file with no token for exactly this review and round is only for a document of the board's own
            # origin. A sandboxed frame - the only thing that renders a prototype or an artifact - has an opaque
            # origin, so every request it makes is cross-site, and every URL it derives relatively carries its own
            # round's token. So a prototype in review A cannot read review B's scripts or styles, and an outside site
            # cannot embed a round file at all. A navigation is not restricted this way: a tokenless page still opens
            # (sandboxed, with no network) because that is the existing no-token view; what it must not do is pull in
            # another review's files. An expired-but-signed token still counts as same-review for this decision only,
            # so the page's own derived scripts and styles keep loading, while it grants no read and no network.
            # `Sec-Fetch-Site` and `Sec-Fetch-Dest` are forbidden header names, so page script cannot forge either one.
            tokened = (frame_round is not None and _is_ascii_digits(rest[1]) and frame_round == int(rest[1]))
            same_review = tokened or (frame_token is not None and frames.signed_for(frame_token, rid, rest[1]))
            dest = (self.headers.get("Sec-Fetch-Dest") or "").strip().lower()
            navigation = dest in ("document", "iframe", "frame", "embed", "object")
            # A page served with the `sandbox` CSP has an opaque origin even as a top-level document, so its own
            # images, audio and video arrive cross-site too: those stay loadable (they render, they cannot be read),
            # while anything that could execute, style or fetch another review's content is a dead end.
            render = dest in ("image", "audio", "video", "track", "manifest")
            if (not same_review and not navigation and not render
                    and (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site"):
                return self._dead()
            try:
                f = store.safe_variant_file(rid, rest[1], rest[2], "/".join(rest[3:]))
            except ReviewError:
                return self._dead()   # missing and forbidden look the same here too
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            artifact = store.variant_has_artifact(rid, rest[1], rest[2])
            if artifact:
                # An artifact's own round keeps the artifact policy, token or not.
                csp = ARTIFACT_ROUND_CSP
            elif tokened:
                # A valid token for exactly this review and round is today's normal prototype policy: its page is
                # the reviewer's own, sandboxed mock and may load its own files and network.
                csp = VARIANT_CSP
            else:
                # A round document with no token has no network: no frames, no form posts, no fetch. Its own
                # round's subresources may load (a header on an image or a video is ignored, so mock media loads).
                own = f"/r/{rid}/rounds/{rest[1]}/{rest[2]}/"
                src = f"http://{self.headers.get('Host', '')}{own} https://{self.headers.get('Host', '')}{own}"
                csp = ARTIFACT_ROUND_CSP
                for directive in ("img-src", "media-src", "font-src", "style-src", "script-src"):
                    csp = csp.replace(directive + " ", directive + " " + src + " ", 1)
            # A file that is same-review must stay loadable from the frame's opaque origin (media, fonts,
            # `rounds/<n>/a/assets/`); an untokened one is a same-origin resource only, which is what keeps one
            # review's files out of another review's prototype without breaking the board's own document. An expired
            # token still counts here, but it stays on the no-network branch above.
            corp = "cross-origin" if (same_review or render) else "same-origin"
            headers = {"Content-Security-Policy": csp, "Cache-Control": "no-cache",
                       "Cross-Origin-Resource-Policy": corp}
            if (artifact and tokened and dest == "iframe" and len(rest) == 5 and rest[3] == "blocks"
                    and ctype.startswith("text/html;") and not self.headers.get("Range")):
                return self._send(200, store.artifact_board_page(f.read_bytes()), ctype, headers)
            return self._send_file(f, ctype, headers)
        raise ReviewError("not found", 404)

    def _query_int(self, name: str, default: Optional[int] = None) -> Optional[int]:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get(name) or []
        parsed = _ascii_int(values[-1]) if values else None
        return default if parsed is None else parsed

    def _view_scope(self, rid: str, who: "auth.Who", decision: "access.Decision",
                    round_n: Optional[int] = None, data: Optional[Dict[str, Any]] = None) -> "threads.Scope":
        return threads.view_scope(rid, guest=who.principal.kind == "guest", role=decision.role,
                                  round_n=round_n, data=data)

    def _threads_get(self, rid: str, who: "auth.Who", decision: "access.Decision") -> None:
        round_n = self._query_int("round")
        if round_n is not None and round_n < 1:
            raise ReviewError("round not found", 404)
        scope = self._view_scope(rid, who, decision, round_n)
        res = threads.listing(rid, principal=who.actor if who.principal.signed_in else "", round_n=round_n,
                              scope=scope)
        res["ok"] = True
        res["me"] = auth.describe(who, decision)
        return self._json(200, res, self._csrf_cookie(who))

    def _events(self, rid: str, who: "auth.Who", decision: "access.Decision") -> None:
        """One SSE stream. Seats are checked before the headers go out; every failure is JSON."""
        live.install()
        last = 0
        raw = (self.headers.get("Last-Event-ID") or "").strip()
        parsed = _ascii_id(raw)
        if parsed is not None:
            last = parsed
        # Admission snapshot, taken *before* authorization is read again and before the scope. A sign-out, a
        # regenerated or stopped share link, or a changed guest policy that commits after this point bumps one of
        # these, and subscribe() then refuses this request (a clean EOF; EventSource reconnects) instead of
        # registering the principal it was holding.
        epoch = live.REGISTRY.epoch(rid)
        revision = live.REGISTRY.revision()

        def admit() -> "Tuple[Optional[auth.Who], Optional[access.Decision]]":
            """Re-read this request's authorization from its own cookies, now.

            `who` and `decision` were computed when the request arrived; a sign-out or a link change can land while
            the response is being opened. The caller runs this before it computes the read scope and subscribe()
            runs it once more immediately before registration, so a revoked principal never takes a seat.
            """
            self._who_cache = None
            fresh = self._who()
            fresh_decision = auth.decide(fresh, rid, store.exists(rid))
            return (fresh, fresh_decision) if access.can(fresh_decision, "read") else (None, None)

        fresh, fresh_decision = admit()
        if fresh is not None and fresh_decision is not None:
            who, decision = fresh, fresh_decision
        scope = self._view_scope(rid, who, decision)
        owner = decision.role == "owner"
        session_hash = str(who.session["id_hash"]) if who.session is not None else ""
        generation = int(who.guest["generation"]) if who.guest is not None else None
        name = ((who.user or {}).get("name") or (who.guest or {}).get("display_name") or "Guest")
        kind = "guest" if who.guest is not None else "user"
        try:
            sub = live.REGISTRY.subscribe(rid, who.actor, name, kind, session_hash=session_hash,
                                          guest_generation=generation, owner=owner,
                                          # only a pinned view filters: an unrestricted stream keeps today's frames,
                                          # so a round that appears while the page is open still reaches it
                                          allow_round=(lambda n: threads.sees_round(scope, n))
                                          if scope.restricted else None,
                                          epoch=epoch, revision=revision,
                                          admit=lambda: admit()[0] is not None,
                                          # the tick that catches an expiry with no request in between; admit()
                                          # re-reads this request's own cookies and runs on this handler thread
                                          recheck=lambda: admit()[0] is not None)
        except live.TooManyStreams:
            self.close_connection = True
            return self._json(429, {"ok": False, "error": "too many live connections"})
        reset, rows, tip = live.replay(rid, last)
        rev = threads.rev(rid, scope)
        hello = {"actor": who.actor, "me": auth.describe(who, decision), "rev": rev,
                 "presence": live.presence(rid)}
        try:
            sse.respond(self)
        except Exception:
            sub.close()
            live.REGISTRY.unsubscribe(sub)
            raise
        live.REGISTRY.stream(self, sub, hello, rows, reset=reset, tip=tip, announce=rev, last=last)
        return None

    def _owner(self) -> None:
        """Who owns storage and wake-ups. For the local tools only: the data root and wake-route status must not be
        readable over the tailnet proxy (tailscale serve adds forwarding headers; a proxied request carries one)."""
        if not self._local():
            raise ReviewError("not found", 404)
        self._json(200, owner.describe())

    def _owner_challenge(self) -> None:
        """Prove to a same-account process that this is the server that wrote the descriptor, without putting the
        descriptor's secret on the wire: answer HMAC(secret, nonce). Local requests only, like /api/owner, and a
        descriptor that was never published makes it a 404."""
        if not self._local():
            raise ReviewError("not found", 404)
        secret = getattr(self.server, "runtime_secret", None)
        if not secret:
            raise ReviewError("not found", 404)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        nonce = (query.get("nonce") or [""])[0]
        if not 16 <= len(nonce) <= 128 or any(c not in "0123456789abcdefABCDEF" for c in nonce):
            raise ReviewError("nonce must be 16 to 128 hexadecimal characters", 400)
        self._json(200, {"ok": True, "service": runtime.SERVICE, "api": runtime.API, "pid": os.getpid(),
                         "data_root": str(config.own_data_root()), "nonce": nonce,
                         "mac": runtime.mac(secret, nonce)})

    def _index(self, who: "auth.Who") -> None:
        """The reviews this person may open: every review for an admin, granted ones for a member, the one review of
        a guest. Grouped by what needs the reader: needs you / waiting on Hermes / done."""
        if not who.principal.signed_in:
            return self._signin(who)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        q = (query.get("q") or [""])[0].strip().casefold()
        groups = [{"label": "Needs you", "key": "needs", "rows": []},
                  {"label": "Waiting on Hermes", "key": "waiting", "rows": []},
                  {"label": "Done", "key": "done", "rows": []}]
        by_key = {g["key"]: g for g in groups}
        for rid in reversed(store.list_ids()):
            if not access.can(auth.decide(who, rid, True), "read"):
                continue
            try:
                d = store.load(rid)
            except Exception:
                continue
            if q and q not in str(d.get("title") or "").casefold():
                continue
            rounds = d.get("rounds") or []
            current = rounds[-1] if rounds else {}
            sub = current.get("submission") or {}
            if d.get("status") != "open":
                key, badge, tone = "done", "Closed", ""
            elif sub.get("status") in ("pending_notify", "notified"):
                key, badge, tone = "waiting", "Hermes is building", "wn"
            else:
                key, badge, tone = "needs", "Needs you", "ac"
            n = current.get("n", 0)
            by_key[key]["rows"].append({
                "id": rid, "title": d.get("title") or rid, "updated_at": d.get("updated_at") or d.get("created_at"),
                "meta": "Round %s · updated %s" % (n, _age(d.get("updated_at") or d.get("created_at"))),
                "badge": badge, "tone": tone,
            })
        for g in groups:
            g["rows"].sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        name = (who.user or {}).get("name") or (who.guest or {}).get("display_name") or "guest"
        line = f"Signed in as {name}" + (" (this computer)" if who.source == "loopback" else "")
        self._page(200, pages.index(groups, line, who.user is not None, query=q), self._csrf_cookie(who))

    def _confirm_page(self, purpose: str, token: str, code: int = 200) -> None:
        """The link's confirmation form, with its link-bound token and no cookies at all: the GET must never replace
        the shared `mr_csrf` a signed-in board tab is using."""
        csrf = _link_csrf(self.server, purpose, token)
        return self._page(code, pages.confirm(purpose, f"/{purpose}/{token}", csrf, purpose == "invite"), [],
                          referrer=pages.CONFIRM_REFERRER)

    def _link_page(self, purpose: str, token: str) -> None:
        """GET of an invite or sign-in link: the confirmation form (a GET never uses the link)."""
        req = self._req()
        if users.throttled(auth.client_ip(req)):
            return self._page(429, pages.THROTTLED)
        try:
            users.check_link(purpose, token)
        except users.AuthError:
            users.record_failure(auth.client_ip(req))
            return self._page(404, pages.DEAD)
        return self._confirm_page(purpose, token)

    def _share_join(self, token: str) -> None:
        """A share link: a guest session bound to the link's generation, then the board. A signed-in person who can
        already open the review just goes there. A members-only link answers everyone else with the dead page."""
        req = self._req()
        ip = auth.client_ip(req)
        if users.throttled(ip):
            return self._page(429, pages.THROTTLED)
        link = users.resolve_share_token(token)
        if link is None or not store.exists(link["review_id"]):
            users.record_failure(ip)
            return self._page(404, pages.DEAD)
        rid = link["review_id"]
        who = self._who()
        if link["audience"] == "members":
            readable = who.principal.kind in ("owner", "member") and access.can(auth.decide(who, rid, True), "read")
            if not readable:
                users.record_failure(ip)
                return self._page(404, pages.DEAD)
        if who.principal.kind in ("owner", "member"):
            if access.can(auth.decide(who, rid, True), "read"):
                return self._send(303, b"", "text/plain", {"Location": f"/r/{rid}/"})
            return self._page(404, pages.DEAD)
        if (who.guest is not None and who.guest["review_id"] == rid
                and int(who.guest["generation"]) == int(link["generation"])):
            return self._send(303, b"", "text/plain", {"Location": f"/r/{rid}/"})
        try:
            g = users.join_as_guest(link, self.headers.get("User-Agent") or "")
        except users.AuthError as e:
            if e.status == 409:
                return self._page(409, pages.LINK_FULL)
            raise
        age = int(users.GUEST_SECONDS if link["expires_at"] is None else max(60, link["expires_at"] - users.now()))
        self._send(303, b"", "text/plain", {"Location": f"/r/{rid}/"},
                   auth.session_cookies(req, g["session"], g["csrf"], max_age=age))

    def _share_view(self, rid: str) -> Dict[str, Any]:
        """The one share view GET and POST answer: the live link, live seats, people and the review's rounds."""
        data = store.load(rid)
        return {"ok": True, "review_id": rid, "current_round": data["rounds"][-1]["n"],
                "rounds": [r["n"] for r in data["rounds"]], "base": users.share_base_url(rid),
                "link": users.share_link(rid), "seats": users.seats(rid), "people": users.review_people(rid)}

    def _export(self, who: "auth.Who", rid: str, kind: str, decision: "access.Decision") -> None:
        """One export document, derived into a download, a print page or a PNG capture page. All are viewer+,
        audited server-side, and never touch the live board or any external URL."""
        data = store.load(rid)
        visible = visible_rounds(rid, data, who, decision)
        if not visible:
            raise ReviewError("this round is not available", 403)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        raw_round = (query.get("round") or ["latest"])[0]
        asked = _ascii_id(raw_round)
        if raw_round == "latest":
            round_n = visible[-1]
        elif asked is not None and asked in visible:
            round_n = asked
        elif raw_round.isdecimal():
            restricted = who.principal.kind == "guest" and decision.role == "viewer"
            raise ReviewError("this round is not available" if restricted
                              else "round must be latest or one of this review's rounds", 404 if restricted else 400)
        else:
            raise ReviewError("round must be latest or one of this review's rounds", 400)
        raw_comments = (query.get("comments") or ["1"])[0]
        if raw_comments not in ("0", "1"):
            raise ReviewError("comments must be 0 or 1", 400)
        include_comments = raw_comments == "1"
        fmt = (query.get("format") or ["html"])[0]
        if fmt not in ("html", "md", "pdf"):
            raise ReviewError("format must be html, md or pdf", 400)
        width = 1280
        if kind == "capture":
            include_comments = False
            raw_width = (query.get("width") or ["1280"])[0]
            asked_width = _ascii_id(raw_width)
            width = asked_width if asked_width is not None else 1280
            width = max(320, min(1920, width))
        csp = export.EXPORT_CSP if kind == "html" else export.EXPORT_SCRIPT_CSP
        doc = export.build_export_html(data, round_n, include_comments,
                                       threads=export.export_threads(rid, rounds=visible) if include_comments else [],
                                       csp=csp)
        if kind == "print":
            doc = doc.replace("</body>", '<script src="/r/' + rid + '/export-print.js"></script></body>')
        elif kind == "capture":
            doc = doc.replace("<body ", '<body style="width:%dpx;max-width:%dpx;margin:0 auto" ' % (width, width), 1)
            doc = doc.replace("</body>", '<script src="/r/' + rid + '/vendor/snapdom.min.js"></script>'
                              '<script src="/r/' + rid + '/export-capture.js"></script></body>')
        if kind == "capture":
            audit_fmt, ext, disposition = "png", "png", "inline"
        elif kind == "print":
            audit_fmt, ext, disposition = "pdf", "pdf", "inline"
        else:
            audit_fmt, ext, disposition = fmt, "html", "attachment"
        users.record_export(who.actor, rid, audit_fmt, round_n, include_comments)
        self._send(200, doc.encode("utf-8"), "text/html; charset=utf-8",
                   {"Content-Security-Policy": csp, "Cache-Control": "no-store",
                    "Content-Disposition": 'attachment; filename="%s"' % export.export_filename(data, round_n, ext)
                    if disposition == "attachment" else 'inline; filename="%s"' % export.export_filename(data, round_n, ext)})

    def _api_get(self, who: "auth.Who", parts: List[str]) -> None:
        if not who.principal.signed_in:
            return self._signin(who)
        if parts == ["me"]:
            return self._json(200, {"ok": True, "me": auth.describe(who)}, self._csrf_cookie(who))
        if who.user is None:
            return self._dead()
        uid = int(who.user["id"])
        if parts == ["sessions"]:
            current = self._current_session_hash(who)
            return self._json(200, {"ok": True, "sessions": users.list_sessions(uid, current),
                                    "this_device": {"source": who.source}})
        if parts == ["tokens"]:
            return self._json(200, {"ok": True, "tokens": users.list_tokens(uid)})
        if who.user["role"] != "admin":
            raise ReviewError("admins only", 403)
        if parts == ["users"]:
            rows = users.list_users()
            admins = sum(1 for u in rows if u["role"] == "admin")
            for u in rows:
                u["you"] = u["id"] == uid
                u["locked"] = u["role"] == "admin" and admins <= 1
            return self._json(200, {"ok": True, "users": rows})
        if parts == ["invites"]:
            return self._json(200, {"ok": True, "invites": users.list_invites()})
        raise ReviewError("not found", 404)

    # ---- POST --------------------------------------------------------
    def _discard_body(self) -> None:
        """Consume the rest of the declared request body before writing a response that closes the connection.

        Closing a socket while receive data is still queued makes the kernel send RST instead of FIN; on Windows the
        client then reports WinError 10053 and loses the 4xx we just wrote. Reading exactly the declared bytes keeps
        the close clean. The cap bounds the work for a refused request, and a client that lies about Content-Length
        can hold the handler only until its own socket timeout."""
        left = min(max(getattr(self, "_unread_body", 0), 0), BODY_DRAIN_MAX)
        try:
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass
        self._unread_body = 0

    def _drain(self, cap: int = 1024 * 1024) -> None:
        """Read and drop what the client has already sent, without waiting for more. Closing a socket that still has
        unread bytes queued makes the kernel send RST instead of FIN, and the client then loses the 4xx we just wrote;
        a client that announced a huge body and stopped sending must still get its 413 at once, so this is
        non-blocking and capped."""
        try:
            timeout = self.connection.gettimeout()
            self.connection.setblocking(False)
        except OSError:
            return
        try:
            left = cap
            while left > 0:
                chunk = self.connection.recv(min(left, 65536))
                if not chunk:
                    return
                left -= len(chunk)
        except OSError:
            return
        finally:
            try:
                self.connection.settimeout(timeout)
            except OSError:
                pass

    def _read_body(self, limit: int) -> Dict[str, Any]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("application/json", "application/x-www-form-urlencoded"):
            raise ReviewError("Content-Type must be application/json", 415)
        length = _ascii_id(self.headers.get("Content-Length"))
        if length is None:
            raise ReviewError("Content-Length required", 411)
        if length > limit:
            self.close_connection = True
            self._unread_body = 0
            self._drain()
            raise ReviewError("Feedback is too large (2 MB max)." if limit == config.MAX_BODY
                              else "Request body is too large.", 413)
        raw = self.rfile.read(length)
        self._unread_body = 0
        try:
            text = raw.decode("utf-8")
            body = (json.loads(text) if ctype == "application/json"
                    else {k: v[-1] for k, v in urllib.parse.parse_qs(text, max_num_fields=20).items()})
        except (UnicodeDecodeError, ValueError):
            raise ReviewError("body is not valid JSON")
        if not isinstance(body, dict):
            raise ReviewError("body must be a JSON object")
        return body

    def _origin_ok(self) -> None:
        origin = self.headers.get("Origin")
        if origin is not None:   # browsers send it on every POST; curl and the CLI don't
            if origin.strip().lower() == "null":
                raise ReviewError("requests from sandboxed frames are not accepted", 403)
            if not origin_allowed(origin, self._allowed(), self.headers.get("Host") or "",
                                  auth.https(self._req())):
                raise ReviewError("cross-origin requests are not accepted", 403)
        if not auth.fetch_site_ok(self._req()):
            raise ReviewError("cross-site requests are not accepted", 403)

    def _csrf(self, who: "auth.Who", body: Optional[Dict[str, Any]] = None) -> None:
        if who.source == "token":
            # Explicit authority, not an ambient cookie: the token is the credential the caller chose to send, and the
            # server accepts it on the two MCP write routes only (see token_route). An invalid or revoked token never
            # reaches here as a user, and every other route refuses a bearer outright.
            return
        if not auth.csrf_ok(self._req(), who, str((body or {}).get("csrf") or "")):
            raise ReviewError("missing or wrong CSRF token: reload the page and try again", 403)

    def do_POST(self) -> None:
        self._who_cache = None
        self._unread_body = _ascii_id(self.headers.get("Content-Length")) or 0
        if not self._host_ok():
            return
        try:
            parts = self._parts()
            self._origin_ok()
            if auth.bearer_token(self._req()) is not None and not token_route(parts):
                # A bearer is an agent's write credential, accepted on the two routes it needs and nowhere else. It is
                # refused before any identity lookup, so it can never fall back to the session or the loopback owner.
                self.close_connection = True
                raise ReviewError("a personal access token is only accepted for a thread reply or status write", 403)
            if len(parts) == 2 and parts[0] in ("login", "invite"):
                return self._use_link(parts[0], parts[1])
            if parts and parts[0] == "api":
                return self._api_post(parts[1:])
            if len(parts) < 4 or parts[0] != "r" or parts[2] != "api":
                raise ReviewError("not found", 404)
            who = self._who()
            try:
                rid = store.validate_id(parts[1])
            except ReviewError:
                return self._dead() if who.principal.signed_in else self._signin(who)
            decision = auth.decide(who, rid, store.exists(rid))
            if decision.result == access.SIGNIN:
                self.close_connection = True
                return self._signin(who)
            if decision.result != access.OK:
                self.close_connection = True
                return self._dead()
            rest = parts[3:]
            cap = REVIEW_POSTS.get(rest[0]) if len(rest) == 1 else None
            thread_cap = thread_post_cap(rest)
            if cap is None and thread_cap is None:
                raise ReviewError("not found", 404)
            body = self._read_body(config.MAX_BODY if rest[0] == "submit" and len(rest) == 1 else API_MAX_BODY)
            self._csrf(who)
            if not access.can(decision, cap or thread_cap):
                raise ReviewError("your role on this review cannot do that", 403)
            if thread_cap is not None:
                return self._thread_post(rid, who, decision, rest, body)
            action = rest[0]
            if action == "submit":
                # Send, amend and Build are the owner's alone (wakeguard.request_gate); the owner's own Send from the
                # Funnel URL or another public path is saved and not woken.
                if decision.role != "owner":
                    raise ReviewError(wakeguard.SUBMIT_OWNER_ONLY, 403)
                res = service.submit(rid, body.get("round"), body.get("feedback"), body.get("token"),
                                     amend=body.get("amend") is True, source="local",
                                     no_wake_reason=self._wake_gate(wakeguard.SUBMIT, decision),
                                     author=who.actor, guest=who.guest is not None)
                return self._json(200, res)
            if action == "pin-fix":
                res = service.request_pin_fix(rid, body.get("round"), body.get("pin"), body.get("token"),
                                              idem=body.get("idem"), requester=who.actor,
                                              no_wake_reason=self._wake_gate(wakeguard.PIN_FIX, decision))
                return self._json(200, res)
            if action == "share":
                round_value = body.get("round")
                if round_value is not None:
                    text = str(round_value)
                    rounds = [r["n"] for r in store.load(rid)["rounds"]]
                    asked_round = _ascii_id(text)
                    if text != "latest" and (asked_round is None or asked_round not in rounds):
                        raise ReviewError("round must be latest or one of this review's rounds", 400)
                    round_value = text
                expires = body["expires"] if "expires" in body else users.UNSET
                users.share(who.actor, rid, str(body.get("action") or "create"),
                            guest_role=body.get("guest_role"), expires=expires,
                            audience=body.get("audience"), round=round_value)
                return self._json(200, self._share_view(rid))
            if action == "people":
                kind = str(body.get("action") or "")
                if kind == "invite":
                    name = users.clean_name(body.get("name"))
                    if not name:
                        raise ReviewError("a name or email is required", 400)
                    role = str(body.get("role") or "commenter")
                    if role not in users.REVIEW_ROLES:
                        raise ReviewError("role must be reviewer, commenter or viewer", 400)
                    inv = users.create_invite(who.actor, role="member", review_id=rid, review_role=role,
                                              grant_expires=body.get("expires"), label=name)
                    path = inv.pop("path")
                    return self._json(200, {"ok": True, "invite": {
                        "id": inv["id"], "path": path, "url": users.share_url(rid, path),
                        "review_role": inv["review_role"], "expires_at": inv["expires_at"]}})
                if kind in ("grant", "revoke"):
                    uid = body.get("user_id")
                    if not isinstance(uid, int) or isinstance(uid, bool):
                        raise ReviewError("user_id is required", 400)
                    if kind == "grant":
                        role = str(body.get("role") or "commenter")
                        if role not in users.REVIEW_ROLES:
                            raise ReviewError("role must be reviewer, commenter or viewer", 400)
                        expires = body["expires"] if "expires" in body else users.UNSET
                        users.grant(who.actor, uid, role, review_id=rid, expires=expires)
                    else:
                        users.revoke_grant(who.actor, uid, review_id=rid)
                    return self._json(200, {"ok": True, "people": users.review_people(rid)})
                raise ReviewError("action must be invite, grant or revoke", 400)
            if who.guest is None:     # guest-name
                raise ReviewError("only a guest has a display name here", 403)
            return self._json(200, users.set_guest_name(who.guest["id"], body.get("name")))
        except ReviewError as e:
            self.close_connection = True  # the body may be unread; don't reuse the socket
            self._json(e.status, {"ok": False, "error": e.message})
        except Exception:
            self.close_connection = True
            log.exception("POST %s failed", urllib.parse.urlsplit(self.path).path.split("/")[1:2])
            self._json(500, {"ok": False, "error": "internal error"})

    def _wake_gate(self, kind: str, decision: "access.Decision", origin: Any = None) -> str:
        """wakeguard.request_gate for this request: "" when it may wake Hermes, else why it is saved instead."""
        req = self._req()
        return wakeguard.request_gate(kind, decision.role, auth.private_ingress(req), origin,
                                      funnel=auth.via_funnel(req))

    def _mention_gate(self, rid: str, who: "auth.Who", decision: "access.Decision") -> Tuple[bool, str]:
        """(can_activate, why a mention is saved instead). An agent's token writes comments and thread status; it
        never activates @Hermes, so two agents cannot wake each other (the MCP tool already drops a hermes mention, and
        this is the server-side half of that guard), and no post on a board created with wake-up off does: both are
        plain comments with no reason to report. Everyone else asks the request gate."""
        data = store.load(rid)
        if who.source == "token" or data.get("wake") is False:
            return False, ""
        reason = self._wake_gate(wakeguard.MENTION, decision, data.get("origin"))
        return not reason, reason

    def _mention_wake(self, rid: str, thread: Dict[str, Any], comment: Optional[Dict[str, Any]],
                      decision: "access.Decision", can_activate: bool, reason: str) -> Optional[Dict[str, Any]]:
        """The `wake` member of a thread POST answer, or None when the comment does not mention Hermes."""
        if not comment or not _mentions_hermes(comment):
            return None
        if can_activate:
            # One wake per activating comment: the comment id is the idempotency key, so an idem replay is a
            # duplicate and a plain reply on an already-activated thread wakes nobody.
            return service.wake_thread_mention(rid, thread, comment["id"], by_owner=decision.role == "owner")
        if reason:
            return {"status": "stored", "notified": False, "detail": reason}
        return None

    def _thread_post(self, rid: str, who: "auth.Who", decision: "access.Decision", rest: List[str],
                     body: Dict[str, Any]) -> None:
        if rest == ["threads"]:
            can_activate, reason = self._mention_gate(rid, who, decision)
            round_n = body.get("round")
            if round_n is not None:
                try:
                    round_n = int(round_n)
                except (TypeError, ValueError):
                    raise ReviewError("round must be a number", 400)
            thread = threads.create(
                rid, who.actor, ((who.user or {}).get("name") or (who.guest or {}).get("display_name") or "Guest"),
                decision.role, body.get("text"), anchor=body.get("anchor"), mentions=body.get("mentions"),
                round_n=round_n, idem=body.get("idem"), can_activate=can_activate, principal=who.actor)
            res = {"ok": True, "thread": thread}
            wake = self._mention_wake(rid, thread, (thread.get("comments") or [None])[0], decision, can_activate,
                                      reason)
            if wake is not None:
                res["wake"] = wake
            return self._json(201, res)
        if len(rest) == 3 and rest[0] == "threads" and rest[2] == "reply":
            can_activate, reason = self._mention_gate(rid, who, decision)
            thread, comment_id = threads.reply_comment(
                rid, rest[1], who.actor,
                ((who.user or {}).get("name") or (who.guest or {}).get("display_name") or "Guest"),
                decision.role, body.get("text"), mentions=body.get("mentions"), idem=body.get("idem"),
                can_activate=can_activate, principal=who.actor)
            comment = next((c for c in thread.get("comments") or [] if c["id"] == comment_id), None)
            res = {"ok": True, "thread": thread, "comment": comment}
            wake = self._mention_wake(rid, thread, comment, decision, can_activate, reason)
            if wake is not None:
                res["wake"] = wake
            return self._json(200, res)
        if len(rest) == 3 and rest[0] == "threads" and rest[2] == "status":
            # The board offers Resolve to anyone who may comment, but an agent's token is narrower: the card and the MCP
            # tool require the reviewer role, so this route enforces the same rule instead of trusting the client.
            if who.source == "token" and decision.role not in ("owner", "reviewer"):
                raise ReviewError("a personal access token needs the reviewer role to resolve or reopen a thread", 403)
            thread = threads.set_status(rid, rest[1], str(body.get("status") or ""), who.actor, principal=who.actor)
            return self._json(200, {"ok": True, "thread": thread})
        if len(rest) == 3 and rest[0] == "threads" and rest[2] == "read":
            threads.mark_read(rid, who.actor, rest[1])
            return self._json(200, {"ok": True})
        if len(rest) == 2 and rest[0] == "comments":
            thread = threads.edit_comment(rid, rest[1], who.actor, body.get("text"), principal=who.actor)
            comment = next((c for c in thread["comments"] if c["id"] == rest[1]), None)
            return self._json(200, {"ok": True, "thread": thread, "comment": comment})
        if len(rest) == 3 and rest[0] == "comments" and rest[2] == "delete":
            thread = threads.delete_comment(rid, rest[1], who.actor, principal=who.actor)
            return self._json(200, {"ok": True, "thread": thread} if thread else {"ok": True})
        if rest == ["typing"]:
            tid = str(body.get("thread") or "")
            if not tid:
                raise ReviewError("thread is required", 400)
            name = ((who.user or {}).get("name") or (who.guest or {}).get("display_name") or "Guest")
            threads.type_lease(rid, tid, who.actor, name)
            return self._json(200, {"ok": True})
        if rest == ["activity", "read"]:
            threads.mark_read(rid, who.actor, "")
            return self._json(200, {"ok": True})
        raise ReviewError("not found", 404)

    def _link_token_ok(self, purpose: str, token: str, form_token: str) -> bool:
        """The form's token is the link's own, or the older double-submit pair (form value == `mr_csrf` cookie).

        The link token is what the form always renders, so a real browser never needs the fallback: the confirmation
        page sets no `mr_csrf`, and a board tab's cookie stays whatever its session issued. The fallback exists only
        for a client that already carries the pair (the base tests post `csrf=form-csrf` with `mr_csrf=form-csrf`);
        it is never required, and a cross-site POST can produce neither.
        """
        if not form_token:
            return False
        expected = _link_csrf(self.server, purpose, token)
        if hmac.compare_digest(form_token.encode(), expected.encode()):
            return True
        return auth.csrf_ok(self._req(), auth.Who(access.ANONYMOUS), form_token)

    def _use_link(self, purpose: str, token: str) -> None:
        """POST of an invite or sign-in link: use it once, open a session, go to the board.

        Every failure past the throttle answers with HTML. A wrong or missing token re-renders this link's own
        confirmation form (403) and uses nothing: the link stays open and no throttle failure is counted, so a
        browser whose `mr_csrf` cookie moved on can simply press the button again.
        """
        req = self._req()
        ip = auth.client_ip(req)
        if users.throttled(ip):
            self.close_connection = True
            return self._page(429, pages.THROTTLED)
        body = self._read_body(API_MAX_BODY)
        if not self._link_token_ok(purpose, token, str(body.get("csrf") or "")):
            try:
                users.check_link(purpose, token)
            except users.AuthError:
                users.record_failure(ip)        # a POST at a dead link is a failed use, as it always was
                return self._page(404, pages.DEAD)
            return self._confirm_page(purpose, token, 403)
        try:
            res = users.redeem(purpose, token, name=str(body.get("name") or ""), identity=auth.proxy_identity(req),
                               user_agent=self.headers.get("User-Agent") or "")
        except users.AuthError:
            users.record_failure(ip)
            return self._page(404, pages.DEAD)
        where = f"/r/{res['review_id']}/" if res.get("review_id") else "/"
        self._send(303, b"", "text/plain", {"Location": where}, auth.session_cookies(req, res["session"], res["csrf"]))

    def _api_post(self, parts: List[str]) -> None:
        who = self._who()
        if not who.principal.signed_in:
            self.close_connection = True
            return self._signin(who)
        body = self._read_body(API_MAX_BODY)
        self._csrf(who, body)
        if parts == ["logout"]:
            sess = self._cookie_session(who)
            if sess is not None:
                users.end_session(sess["id_hash"])
            return self._json(200, {"ok": True}, auth.clear_cookies(self._req()))
        if who.user is None:
            raise ReviewError("guests have no account settings", 403)
        uid = int(who.user["id"])
        if parts == ["sessions", "sign-out-others"]:
            return self._json(200, users.sign_out_others(uid, self._current_session_hash(who)))
        if parts == ["tokens"]:
            return self._json(200, users.create_token(uid, str(body.get("name") or "")))
        if len(parts) == 3 and parts[0] == "tokens" and parts[2] == "revoke":
            token_id = _ascii_id(parts[1])
            if token_id is not None:
                return self._json(200, users.revoke_token(uid, token_id))
        if who.user["role"] != "admin":
            raise ReviewError("admins only", 403)
        actor = who.actor
        if parts == ["invites"]:
            if body.get("review_id") and not store.exists(str(body["review_id"])):
                raise ReviewError("review not found", 404)
            return self._json(200, users.create_invite(
                actor, role=str(body.get("role") or "member"), review_id=body.get("review_id") or None,
                review_role=body.get("review_role") or None, all_reviews=body.get("all_reviews") is True,
                expires=body.get("expires"), label=str(body.get("label") or "")))
        if len(parts) == 3 and parts[0] == "invites" and parts[2] == "revoke":
            invite_id = _ascii_id(parts[1])
            if invite_id is not None:
                return self._json(200, users.revoke_invite(actor, invite_id))
        if len(parts) == 3 and parts[0] == "users" and parts[2] in ("role", "delete"):
            target = _ascii_id(parts[1])
            if target is not None:
                if parts[2] == "role":
                    return self._json(200, {"ok": True,
                                            "user": users.set_role(actor, target, str(body.get("role")))})
                return self._json(200, users.delete_user(actor, target))
        raise ReviewError("not found", 404)


class Worker(threading.Thread):
    """Retries pending notifications and runs the housekeeping pass (auto_close_idle_days / keep_closed_days) every
    housekeeping_seconds. Each tick runs in the owning profile's secret scope (secrets.owner_scope; owner_home is read
    once, here)."""

    def __init__(self, tick: float = 5.0):
        super().__init__(name="mockreview-worker", daemon=True)
        self._context = copy_context()
        self.owner_home = config.hermes_home()
        self.stop_event = threading.Event()
        self.tick = tick
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
            if time.time() - self.last_stalled >= service.STALLED_SCAN_SECONDS:
                self.last_stalled = time.time()
                try:
                    # Stalled pings off: still expire pin requests, but never run the ping watchdog.
                    if settings.get("stale_after_minutes"):
                        service.check_stalled()
                    else:
                        service.expire_pin_requests()
                except Exception:
                    log.exception("stalled-notice pass failed")
            try:
                service.retry_pending()
            except Exception:
                log.exception("retry pass failed")
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
        # The two lines ThreadingMixIn.process_request runs, so an interrupt after start() but before it returns can
        # tell whether the thread really began: only then will process_request_thread's finally release the slot.
        t = threading.Thread(target=self.process_request_thread, args=(request, client_address))
        t.daemon = self.daemon_threads
        try:
            t.start()
        except BaseException:
            if t.ident is None:   # never started: its finally will not run either
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

    def server_close(self) -> None:
        runtime.withdraw(self)
        super().server_close()


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
    auth.check_startup()
    if host == "0.0.0.0":
        log.warning("listening on every network interface: any device on this network can reach the board (people "
                    "still sign in; loopback_owner only trusts this computer)")
    cls = BoardHTTPServer6 if ":" in host else BoardHTTPServer
    live.install()
    srv = cls((host, port), Handler)
    runtime.publish(srv)
    return srv


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
