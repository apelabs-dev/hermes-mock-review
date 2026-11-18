"""Who is making a request: sessions, trusted proxy identity, loopback = owner, CSRF and cookies.

Identity sources, each switched on in settings and read only from a peer listed in `trusted_proxies`:
- Tailscale Serve: Tailscale-User-Login / -Name / -Profile-Pic (`tailscale_identity`). At tag v1.98.1 (commit
  `2f4c02ee68c459aaea92d8b2dd81a38816823bef`), `ipn/ipnlocal/serve.go:1046-1052` deletes every incoming
  Tailscale-User-*, Tailscale-Funnel-Request and Tailscale-Headers-Info header, so a client cannot forge them; then
  `:1058-1060` marks Funnel ingress with `Tailscale-Funnel-Request: ?1` and returns before any identity header is set,
  while only private tailnet Serve sets them from `WhoIs` (`:1062-1074`). A Tailscale identity is therefore refused on
  a request that carries the marker (`via_funnel`) and on any public host (`Request.public_host`: a Funnel URL or
  `public_url` name): a public URL never signs anyone in with it. Round 1's claim that 1.98.1 sends a real identity on
  Funnel measured private Serve, not Funnel, and is retracted. Serve also sets X-Forwarded-For to the
  one tailnet address the request came from (`addProxyForwardedHeaders`, `ipn/ipnlocal/serve.go:1036-1044` at the same
  tag), so a Tailscale identity also needs X-Forwarded-For to be exactly one address in 100.64.0.0/10 or
  fd7a:115c:a1e0::/48 (another proxy in front, which puts the real client address there, gives none).
- Cloudflare Access: the `Cf-Access-Jwt-Assertion` JWT, verified with PyJWT (RS256) against the team's JWKS, with the
  configured audience and the team issuer (`cloudflare_team`, `cloudflare_audience`). Needs the optional `crypto` extra;
  the server refuses to start in this mode without it.
- oauth2-proxy / Authelia style X-Forwarded-User / X-Forwarded-Email (`forwarded_identity`), for a board on a private
  host only: Funnel and Serve connect from a trusted loopback proxy and strip only `Tailscale-*` headers, so this source
  is refused on a public host and with the Funnel marker.
A request answers to one source at most: when it carries the headers of two enabled sources (valid or not), it gets no
proxy identity at all. A proxy identity is only a name for a person: it signs someone in only when it is linked to an
account (an invite or sign-in link redeemed through that proxy links it).

Then the session cookie (invite, sign-in link or share link), then loopback = owner (`loopback_owner`, on by
default): a request from this computer that did not come through a proxy is the install's owner account.

A personal access token is the one identity that arrives as a credential rather than a cookie: an
`Authorization: Bearer mrp_...` header names its user, but only on a loopback request. A bearer token is never
ambient (a browser does not attach it by itself), so it is exempt from CSRF; and a bearer header that is invalid,
revoked or sent from anywhere but this computer resolves to anonymous and never falls through to the session or
loopback-owner path, so a dead token can never become the owner.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import secrets
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from http.cookies import CookieError, SimpleCookie
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import access, settings, users

log = logging.getLogger("mockreview.auth")

SESSION_COOKIE = "mr_session"
CSRF_COOKIE = "mr_csrf"
CSRF_HEADER = "X-CSRF-Token"
LOCAL_PEERS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
FUNNEL_HEADER = "tailscale-funnel-request"
TAILSCALE_HEADERS = ("tailscale-user-login", "tailscale-user-name", "tailscale-user-profile-pic")
CLOUDFLARE_HEADERS = ("cf-access-jwt-assertion",)
FORWARDED_HEADERS = ("x-forwarded-user", "x-forwarded-email")
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
TEAM_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
CRYPTO_MISSING = ("Cloudflare Access sign-in (cloudflare_team / cloudflare_audience) needs PyJWT with cryptography, "
                  "which is not installed. Install the plugin's crypto extra (PyJWT[crypto]) or clear those two "
                  "settings.")


class AuthConfigError(SystemExit):
    pass


# ---------------------------------------------------------------- request view

@dataclass
class Request:
    """What the access layer needs from an HTTP request."""
    peer: str
    headers: Any                      # an email.message.Message (http.server) or a plain mapping
    local: bool = False               # from this computer and not through a proxy (server.local_request)
    public_host: bool = False         # the Host is a public name (server.build_allowed_hosts)

    def header(self, name: str) -> str:
        return (self.headers.get(name) or "").strip()

    def header_names(self) -> List[str]:
        keys = self.headers.keys() if hasattr(self.headers, "keys") else []
        return [str(k) for k in keys]

    def header_values(self, name: str) -> List[str]:
        """Every copy of one header (a client may send it twice)."""
        get_all = getattr(self.headers, "get_all", None)
        values = get_all(name) if get_all is not None else [self.headers.get(name)]
        return [str(v) for v in values or [] if v is not None]

    def has_header(self, names: Any) -> bool:
        """One of these lowercase names is present, in any spelling (case, '_' for '-'), with any value."""
        return any(n.replace("_", "-").lower() in names for n in self.header_names())

    @property
    def cookies(self) -> Dict[str, str]:
        raw = self.headers.get("Cookie") or ""
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            return {}
        return {k: m.value for k, m in jar.items()}


@dataclass
class Who:
    principal: access.Principal
    user: Optional[Dict[str, Any]] = None
    session: Optional[Dict[str, Any]] = None
    guest: Optional[Dict[str, Any]] = None
    source: str = ""                         # loopback | session | tailscale | cloudflare | forwarded
    identity: Optional[Dict[str, str]] = None  # a trusted proxy identity, linked to an account or not
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def actor(self) -> str:
        if self.user is not None:
            return f"user:{self.user['id']}"
        if self.guest is not None:
            return f"guest:{self.guest['id']}"
        return "anonymous"


# ---------------------------------------------------------------- proxy trust

def _networks() -> List[Any]:
    nets = []
    for item in settings.get("trusted_proxies") or []:
        try:
            nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            log.warning("trusted_proxies: %r is not an address or network, ignored", item)
    return nets


def _ip(peer: str) -> Optional[Any]:
    try:
        ip = ipaddress.ip_address((peer or "").split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped or ip


def trusted_peer(peer: str) -> bool:
    ip = _ip(peer)
    return ip is not None and any(ip.version == n.version and ip in n for n in _networks())


def client_ip(req: Request) -> str:
    """The address throttling counts: the proxy's X-Forwarded-For (its last hop) from a trusted proxy, else the peer."""
    if trusted_peer(req.peer):
        fwd = [p.strip() for p in req.header("X-Forwarded-For").split(",") if p.strip()]
        if fwd and _ip(fwd[-1]) is not None:
            return str(_ip(fwd[-1]))
    return str(_ip(req.peer) or req.peer or "unknown")


def via_funnel(req: Request) -> bool:
    """True when Tailscale marks the request as public Funnel ingress. v1.98.1 sets Tailscale-Funnel-Request to `?1`
    on the Funnel path and returns before setting identity headers (`ipn/ipnlocal/serve.go:1058-1060`), and it deletes
    every incoming copy of the header first (`:1048-1052`), so this is the primary signal, not a fallback. Any spelling
    (case, '_' for '-') counts."""
    return req.has_header((FUNNEL_HEADER,))


def from_tailnet(req: Request) -> bool:
    """Tailscale Serve sets X-Forwarded-For to the single tailnet address the request came from (ipn/ipnlocal/serve.go,
    addProxyForwardedHeaders). Anything else (missing, a chain, a public address) did not come straight from Serve."""
    hops = [h.strip() for v in req.header_values("X-Forwarded-For") for h in v.split(",") if h.strip()]
    ip = _ip(hops[0]) if len(hops) == 1 else None
    return ip is not None and any(ip.version == n.version and ip in n for n in TAILNET)


def _decode(value: str) -> str:
    """Tailscale may RFC 2047 "Q"-encode non-ASCII values."""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def tailscale_identity(req: Request) -> Optional[Dict[str, str]]:
    if (not settings.get("tailscale_identity") or not trusted_peer(req.peer) or req.public_host
            or via_funnel(req) or not from_tailnet(req)):
        return None
    login = _decode(req.header("Tailscale-User-Login")).strip().lower()
    if not login or len(login) > 254:
        return None
    return {"source": "tailscale", "subject": login, "name": users.clean_name(_decode(req.header("Tailscale-User-Name")))
            or login.split("@")[0], "email": login if "@" in login else ""}


def forwarded_identity(req: Request) -> Optional[Dict[str, str]]:
    # Funnel and Serve connect from loopback, a trusted proxy by default, and Tailscale strips only Tailscale-* headers:
    # without this guard a client-sent X-Forwarded-Email on a public request would be believed.
    if (not settings.get("forwarded_identity") or not trusted_peer(req.peer) or req.public_host
            or via_funnel(req)):
        return None
    user = req.header("X-Forwarded-User")
    email = req.header("X-Forwarded-Email").lower()
    subject = (email or user).strip()
    if not subject or len(subject) > 254:
        return None
    return {"source": "forwarded", "subject": subject, "name": users.clean_name(user) or subject.split("@")[0],
            "email": email}


# ---------------------------------------------------------------- Cloudflare Access

def cloudflare_config() -> Optional[Dict[str, str]]:
    team = (settings.get("cloudflare_team") or "").strip().lower()
    aud = (settings.get("cloudflare_audience") or "").strip()
    if not team and not aud:
        return None
    if not TEAM_RE.match(team) or not aud:
        raise AuthConfigError("cloudflare_team must be your Cloudflare Access team name (the <team> in "
                              "<team>.cloudflareaccess.com) and cloudflare_audience its application AUD tag; set both "
                              "or neither.")
    return {"team": team, "audience": aud, "issuer": f"https://{team}.cloudflareaccess.com",
            "certs": f"https://{team}.cloudflareaccess.com/cdn-cgi/access/certs"}


def _jwt() -> Any:
    """PyJWT with its cryptography backend, or AuthConfigError(CRYPTO_MISSING)."""
    try:
        import jwt  # type: ignore[import-not-found]
        from jwt import algorithms  # type: ignore[import-not-found]
    except ImportError:
        raise AuthConfigError(CRYPTO_MISSING) from None
    if not getattr(algorithms, "has_crypto", False):
        raise AuthConfigError(CRYPTO_MISSING)
    return jwt


def check_startup() -> None:
    """Refuse to serve with a sign-in mode that cannot work (raises AuthConfigError, a SystemExit, with the reason)."""
    try:
        users.itsdangerous()
    except users.AuthError as e:
        raise AuthConfigError(str(e)) from None
    if cloudflare_config() is not None:
        _jwt()


_jwks_clients: Dict[str, Any] = {}
JwksFactory = Callable[[str], Any]
jwks_factory: Optional[JwksFactory] = None      # tests install a local JWKS here; production fetches the team's certs


def _jwks(url: str) -> Any:
    if jwks_factory is not None:
        return jwks_factory(url)
    client = _jwks_clients.get(url)
    if client is None:
        client = _jwks_clients[url] = _jwt().PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=5)
    return client


def cloudflare_identity(req: Request) -> Optional[Dict[str, str]]:
    cfg = cloudflare_config()
    token = req.header("Cf-Access-Jwt-Assertion")
    if cfg is None or not token or len(token) > 16384 or not trusted_peer(req.peer):
        return None
    jwt = _jwt()
    try:
        key = _jwks(cfg["certs"]).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=cfg["audience"], issuer=cfg["issuer"],
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]}, leeway=30)
    except Exception as e:      # any failure is no identity: bad signature, wrong audience, expired, JWKS down
        log.info("Cloudflare Access token refused: %s", type(e).__name__)
        return None
    sub = str(claims.get("sub") or "").strip()
    if not sub:
        return None
    email = str(claims.get("email") or "").strip().lower()
    return {"source": "cloudflare", "subject": sub, "name": email.split("@")[0] if email else sub[:12], "email": email}


def _sources_present(req: Request) -> List[str]:
    """The enabled identity sources whose headers this request carries, valid or not."""
    present = []
    if cloudflare_config() is not None and req.has_header(CLOUDFLARE_HEADERS):
        present.append("cloudflare")
    if settings.get("tailscale_identity") and req.has_header(TAILSCALE_HEADERS):
        present.append("tailscale")
    if settings.get("forwarded_identity") and req.has_header(FORWARDED_HEADERS):
        present.append("forwarded")
    return present


READERS = {"cloudflare": cloudflare_identity, "tailscale": tailscale_identity, "forwarded": forwarded_identity}


def proxy_identity(req: Request) -> Optional[Dict[str, str]]:
    """The identity the one source on this request vouches for. Headers of two sources on one request mean a proxy
    passed a client's headers through: no proxy identity at all (fail closed)."""
    if not trusted_peer(req.peer):
        return None
    present = _sources_present(req)
    if len(present) > 1:
        log.info("identity headers of %s on one request: no proxy identity", " and ".join(present))
        return None
    return READERS[present[0]](req) if present else None


# ---------------------------------------------------------------- who is asking

def principal_for(user: Dict[str, Any]) -> access.Principal:
    return access.Principal("owner" if user["role"] == "admin" else "member", user_id=int(user["id"]))


def bearer_token(req: Request) -> Optional[str]:
    """The personal access token on this request, or None. Any other Authorization header is ignored."""
    value = req.header("Authorization")
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token if token.startswith(users.TOKEN_PREFIX) and len(token) <= 200 else None


def identify(req: Request) -> Who:
    """Read fresh on every request: a personal access token (loopback only), else a proxy identity linked to an
    account, else the session cookie, else loopback = owner, else anonymous."""
    token = bearer_token(req)
    if token is not None:
        if req.local:
            user = users.token_user(token)
            if user is not None:
                return Who(principal_for(user), user=user, source="token")
        return Who(access.ANONYMOUS, source="token")
    ident = proxy_identity(req)
    if ident:
        user = users.user_for_identity(ident["source"], ident["subject"])
        if user:
            return Who(principal_for(user), user=user, source=ident["source"], identity=ident)
    sess = users.session(req.cookies.get(SESSION_COOKIE, ""))
    if sess is not None:
        if sess["user_id"] is not None:
            user = users.get_user(sess["user_id"])
            if user:
                return Who(principal_for(user), user=user, session=sess, source="session", identity=ident)
        elif sess["guest_id"] is not None:
            g = users.guest(sess["guest_id"])
            if g:
                p = access.Principal("guest", guest_review=g["review_id"], guest_generation=int(g["generation"]))
                return Who(p, session=sess, guest=g, source="share", identity=ident)
    else:
        revoked = users.revoked_guest_session(req.cookies.get(SESSION_COOKIE, ""))
        if revoked is not None:
            return Who(access.Principal("guest", guest_review=revoked), source="share", identity=ident)
    if req.local and settings.get("loopback_owner"):
        user = users.owner_user()
        return Who(principal_for(user), user=user, source="loopback")
    return Who(access.ANONYMOUS, identity=ident)


def decide(who: Who, review_id: str, review_exists: bool) -> access.Decision:
    """access.resolve() with this principal's live grant and the review's live share link."""
    grant = None
    link = None
    if review_exists and who.principal.kind == "member" and who.user is not None:
        grant = users.grant_role(int(who.user["id"]), review_id)
    if review_exists and who.principal.kind == "guest":
        row = users.resolve_link_row(review_id)
        if row is not None and row["audience"] == "anyone":
            link = access.Link(int(row["generation"]), row["guest_role"])
    return access.resolve(who.principal, review_id, review_exists, grant, link)


# ---------------------------------------------------------------- CSRF and cookies

def csrf_ok(req: Request, who: Who, form_token: str = "") -> bool:
    """Double submit: the X-CSRF-Token header (or a form's csrf field) must equal the mr_csrf cookie, and, for a
    session, that session's own token. The Origin / Host checks run before this in the server."""
    sent = req.header(CSRF_HEADER) or form_token
    cookie = req.cookies.get(CSRF_COOKIE, "")
    if not sent or not cookie or not hmac.compare_digest(sent.encode(), cookie.encode()):
        return False
    if who.session is not None and not hmac.compare_digest(sent.encode(), str(who.session["csrf"]).encode()):
        return False
    return True


def fetch_site_ok(req: Request) -> bool:
    """Fetch Metadata, when the browser sends it: a cross-site POST is refused even before the token check."""
    site = req.header("Sec-Fetch-Site").lower()
    return site in ("", "same-origin", "none")


def https(req: Request) -> bool:
    """The request reached the proxy over HTTPS (a trusted proxy says so), so cookies get Secure."""
    return trusted_peer(req.peer) and req.header("X-Forwarded-Proto").lower() == "https"


def cookie(name: str, value: str, req: Request, http_only: bool = True, max_age: Optional[int] = None) -> str:
    parts = [f"{name}={value}", "Path=/", "SameSite=Lax"]
    if http_only:
        parts.append("HttpOnly")
    if https(req):
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    return "; ".join(parts)


def csrf_cookie_for(req: Request, who: Who) -> Optional[str]:
    """The Set-Cookie for mr_csrf when the browser's copy is missing or no longer matches its session."""
    have = req.cookies.get(CSRF_COOKIE, "")
    want = str(who.session["csrf"]) if who.session is not None else (have or secrets.token_urlsafe(24))
    if have == want:
        return None
    return cookie(CSRF_COOKIE, want, req, http_only=False, max_age=users.SESSION_SECONDS)


def session_cookies(req: Request, token: str, csrf: str, max_age: int = users.SESSION_SECONDS) -> List[str]:
    return [cookie(SESSION_COOKIE, token, req, max_age=max_age), cookie(CSRF_COOKIE, csrf, req, http_only=False,
                                                                       max_age=max_age)]


def renewed_cookies(req: Request, who: Who) -> List[str]:
    """mr_session and mr_csrf again, with a fresh Max-Age, when users.session() just pushed this account session's
    sliding expiry out, so the browser keeps the cookie as long as the server keeps the session. Guest sessions never
    slide: they end with their link generation."""
    if who.session is None or not who.session.get("renewed") or who.session.get("user_id") is None:
        return []
    token = req.cookies.get(SESSION_COOKIE, "")
    return session_cookies(req, token, str(who.session["csrf"])) if token else []


def clear_cookies(req: Request) -> List[str]:
    return [cookie(SESSION_COOKIE, "", req, max_age=0), cookie(CSRF_COOKIE, "", req, http_only=False, max_age=0)]


def describe(who: Who, decision: Optional[access.Decision] = None) -> Dict[str, Any]:
    """The `me` object the board and settings page read. No secrets: the CSRF token is in its own cookie."""
    p = who.principal
    out: Dict[str, Any] = {"kind": p.kind, "source": who.source, "actor": who.actor}
    if who.user is not None:
        out.update(user_id=who.user["id"], name=who.user["name"], role=who.user["role"])
    if who.guest is not None:
        out.update(guest_id=who.guest["id"], name=who.guest["display_name"])
    if decision is not None:
        out.update(review_role=decision.role, caps=sorted(decision.caps))
    return out


def as_mapping(headers: Mapping[str, str]) -> Any:
    """A case-insensitive header view for tests and callers holding a plain dict."""
    from email.message import Message
    m = Message()
    for k, v in headers.items():
        m[k] = v
    return m
