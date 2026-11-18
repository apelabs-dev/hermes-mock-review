"""Who a request is: trusted proxies only, never a Tailscale identity through Funnel, Cloudflare Access JWTs, and
loopback = owner. Peers are simulated: no test binds anything but 127.0.0.1."""

import sys
import time
from types import SimpleNamespace

import pytest

from authkit import account, loopback_owner_off, peer, raw  # noqa: F401 - fixtures
from conftest import http
from mockreview import auth, server as srv, users

TAILNET_HOP = "100.101.102.103"     # what Tailscale Serve puts in X-Forwarded-For: the tailnet source address
TS_HEADERS = {"Tailscale-User-Login": "dana@example.test", "Tailscale-User-Name": "Dana",
              "X-Forwarded-For": TAILNET_HOP}


def req(peer_addr, headers=None, local=False, public_host=False):
    return auth.Request(peer_addr, auth.as_mapping(headers or {}), local=local, public_host=public_host)


def link_tailscale(login="dana@example.test"):
    return account(name="Dana", identity={"source": "tailscale", "subject": login, "name": "Dana", "email": login})


# ---------------------------------------------------------------- trusted proxies

@pytest.mark.parametrize("headers", [
    TS_HEADERS,
    {"X-Forwarded-User": "dana", "X-Forwarded-Email": "dana@example.test"},
])
def test_proxy_headers_from_a_peer_that_is_not_a_trusted_proxy_are_ignored(env, monkeypatch, headers):
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    assert auth.proxy_identity(req("192.0.2.10", headers)) is None
    assert auth.proxy_identity(req("10.1.2.3", headers)) is None
    assert auth.proxy_identity(req("127.0.0.1", headers)) is not None


def test_a_linked_tailnet_person_is_signed_in_only_through_the_trusted_proxy(env, server, peer):
    dana = link_tailscale()
    peer["addr"] = "192.0.2.10"
    status, _, body = http("GET", server + "/api/me", headers=TS_HEADERS)
    assert status == 401
    peer["addr"] = "127.0.0.1"
    status, _, body = http("GET", server + "/api/me", headers=TS_HEADERS)
    assert status == 200 and b'"source": "tailscale"' in body and b'"user_id": %d' % dana["user_id"] in body


def test_trusted_proxies_takes_networks(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_TRUSTED_PROXIES", "10.0.0.0/8, fd7a:115c:a1e0::/48")
    assert auth.trusted_peer("10.1.2.3") and auth.trusted_peer("fd7a:115c:a1e0::5")
    assert not auth.trusted_peer("127.0.0.1") and not auth.trusted_peer("192.0.2.10")
    assert not auth.trusted_peer("not-an-address")


def test_forwarded_headers_are_off_unless_turned_on(env, monkeypatch):
    headers = {"X-Forwarded-User": "dana", "X-Forwarded-Email": "Dana@Example.test"}
    assert auth.forwarded_identity(req("127.0.0.1", headers)) is None
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    assert auth.forwarded_identity(req("127.0.0.1", headers))["subject"] == "dana@example.test"


def test_an_unlinked_identity_gets_the_sign_in_page_naming_it(env, server, peer):
    peer["addr"] = "127.0.0.1"
    status, _, page = http("GET", server + "/", headers=TS_HEADERS)
    assert status == 401 and b"dana@example.test" in page and b"no account" in page


def test_the_throttle_counts_the_client_behind_a_trusted_proxy(env):
    assert auth.client_ip(req("127.0.0.1", {"X-Forwarded-For": "198.51.100.1, 198.51.100.7"})) == "198.51.100.7"
    assert auth.client_ip(req("192.0.2.10", {"X-Forwarded-For": "198.51.100.7"})) == "192.0.2.10"


def test_cookies_are_secure_when_the_trusted_proxy_terminated_https(env):
    assert "Secure" in auth.cookie("a", "b", req("127.0.0.1", {"X-Forwarded-Proto": "https"}))
    assert "Secure" not in auth.cookie("a", "b", req("192.0.2.10", {"X-Forwarded-Proto": "https"}))
    assert "Secure" not in auth.cookie("a", "b", req("127.0.0.1"))


# ---------------------------------------------------------------- Tailscale Funnel

@pytest.mark.parametrize("marker", ["Tailscale-Funnel-Request", "tailscale-funnel-request", "Tailscale_Funnel_Request"])
def test_a_funnel_request_gets_no_tailscale_identity(env, marker):
    assert auth.tailscale_identity(req("127.0.0.1", {**TS_HEADERS, marker: "?1"})) is None
    assert auth.tailscale_identity(req("127.0.0.1", TS_HEADERS))["subject"] == "dana@example.test"


def test_a_public_host_gets_no_tailscale_identity(env):
    """A public URL (Funnel or public_url) never signs anyone in with a Tailscale identity."""
    assert auth.tailscale_identity(req("127.0.0.1", TS_HEADERS, public_host=True)) is None
    assert auth.tailscale_identity(req("127.0.0.1", TS_HEADERS, public_host=False))["subject"] == "dana@example.test"


def test_a_funnel_request_is_not_signed_in_over_http(env, server, peer):
    link_tailscale()
    peer["addr"] = "127.0.0.1"
    assert http("GET", server + "/api/me", headers={**TS_HEADERS, "Tailscale-Funnel-Request": "?1"})[0] == 401
    assert http("GET", server + "/api/me", headers=TS_HEADERS)[0] == 200


def test_tailscale_identity_can_be_turned_off(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_TAILSCALE_IDENTITY", "false")
    assert auth.tailscale_identity(req("127.0.0.1", TS_HEADERS)) is None


def test_rfc2047_names_are_decoded(env):
    ident = auth.tailscale_identity(req("127.0.0.1", {"Tailscale-User-Login": "jo@example.test",
                                                      "Tailscale-User-Name": "=?utf-8?q?Jos=C3=A9?=",
                                                      "X-Forwarded-For": TAILNET_HOP}))
    assert ident["name"] == "José"


# ---------------------------------------------------------------- Tailscale identity is bound to Serve's own hop

@pytest.mark.parametrize("xff, signed_in", [
    (TAILNET_HOP, True),
    ("fd7a:115c:a1e0::7", True),
    (None, False),                               # no X-Forwarded-For: not Serve
    ("198.51.100.7", False),                     # a public proxy in front put the real client address there
    ("100.1.1.1, 100.2.2.2", False),             # a chain is not Serve's single hop
    ("100.101.102.103, 100.101.102.104", False),
    ("10.1.2.3", False),
    ("not-an-address", False),
])
def test_a_tailscale_identity_needs_the_single_tailnet_address_serve_forwards(env, server, peer, xff, signed_in):
    dana = link_tailscale()
    peer["addr"] = "127.0.0.1"
    headers = {"Tailscale-User-Login": "dana@example.test"}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    status, _, body = http("GET", server + "/api/me", headers=headers)
    if signed_in:
        assert status == 200 and b'"user_id": %d' % dana["user_id"] in body
    else:
        assert status == 401


def test_two_forwarded_for_headers_are_not_serves_single_hop(env):
    from email.message import Message
    m = Message()
    m["Tailscale-User-Login"] = "dana@example.test"
    m["X-Forwarded-For"] = TAILNET_HOP
    m["X-Forwarded-For"] = "100.101.102.104"
    assert auth.tailscale_identity(auth.Request("127.0.0.1", m)) is None


# ---------------------------------------------------------------- one identity source per request

def _two_accounts():
    """An admin linked to a Tailscale login and a member linked to a forwarded identity."""
    admin = account(instance="admin", name="Ada", identity={"source": "tailscale", "subject": "ada@example.test",
                                                            "name": "Ada", "email": "ada@example.test"})
    member = account(name="Mo", identity={"source": "forwarded", "subject": "mo@example.test", "name": "Mo",
                                          "email": "mo@example.test"})
    return admin, member


ADA = {"Tailscale-User-Login": "ada@example.test", "X-Forwarded-For": TAILNET_HOP}
MO = {"X-Forwarded-User": "mo", "X-Forwarded-Email": "mo@example.test"}


@pytest.mark.parametrize("tailscale", [ADA, {"tailscale_user_login": "ada@example.test"},
                                       {"TAILSCALE-USER-NAME": "Ada"}, {"Tailscale-User-Profile-Pic": "x"}])
def test_headers_of_two_sources_give_no_proxy_identity(env, server, peer, monkeypatch, tailscale):
    """A loopback proxy that passes its client's headers through must not let the client add a Tailscale login beside
    the identity the proxy vouches for: neither account answers, in any spelling of a Tailscale-User-* header."""
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    admin, member = _two_accounts()
    peer["addr"] = "127.0.0.1"
    assert auth.proxy_identity(req("127.0.0.1", {**tailscale, **MO})) is None
    status, _, body = http("GET", server + "/api/me", headers={**tailscale, **MO})
    assert status == 401, body
    status, _, body = http("GET", server + "/api/me", headers=MO)
    assert status == 200 and b'"user_id": %d' % member["user_id"] in body
    status, _, body = http("GET", server + "/api/me", headers=ADA)
    assert status == 200 and b'"user_id": %d' % admin["user_id"] in body


def test_only_enabled_sources_count(env, monkeypatch):
    """Headers of a source that is switched off are not an identity, and do not block the one that is on."""
    assert auth.proxy_identity(req("127.0.0.1", {**ADA, **MO, "Cf-Access-Jwt-Assertion": "a.b.c"}))["subject"] == \
        "ada@example.test"
    monkeypatch.setenv("MOCKREVIEW_TAILSCALE_IDENTITY", "false")
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    assert auth.proxy_identity(req("127.0.0.1", {**ADA, **MO}))["subject"] == "mo@example.test"


# ---------------------------------------------------------------- Cloudflare Access

TEAM, AUD = "example-team", "aud-0123456789"


@pytest.fixture
def cloudflare(env, monkeypatch):
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives.asymmetric import rsa
    monkeypatch.setenv("MOCKREVIEW_CLOUDFLARE_TEAM", TEAM)
    monkeypatch.setenv("MOCKREVIEW_CLOUDFLARE_AUDIENCE", AUD)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    asked = []

    class LocalJwks:
        """What PyJWKClient would return, from a local key set instead of the team's certs URL."""

        def get_signing_key_from_jwt(self, token):
            if jwt.get_unverified_header(token).get("kid") != "k1":
                raise jwt.PyJWKClientError("no matching key")
            return SimpleNamespace(key=key.public_key())

    def factory(url):
        asked.append(url)
        return LocalJwks()

    monkeypatch.setattr(auth, "jwks_factory", factory)

    def mint(signer=key, **claims):
        now = int(time.time())
        body = {"aud": [AUD], "iss": "https://{}.cloudflareaccess.com".format(TEAM), "sub": "user-123",
                "email": "dana@example.test", "iat": now, "exp": now + 300}
        body.update(claims)
        return jwt.encode({k: v for k, v in body.items() if v is not None}, signer, algorithm="RS256",
                          headers={"kid": "k1"})

    return SimpleNamespace(mint=mint, other=other, asked=asked)


def _cf(token, peer_addr="127.0.0.1"):
    return auth.cloudflare_identity(req(peer_addr, {"Cf-Access-Jwt-Assertion": token}))


def test_a_good_cloudflare_token_names_the_person(cloudflare):
    ident = _cf(cloudflare.mint())
    assert ident == {"source": "cloudflare", "subject": "user-123", "name": "dana", "email": "dana@example.test"}
    assert cloudflare.asked == ["https://{}.cloudflareaccess.com/cdn-cgi/access/certs".format(TEAM)]


@pytest.mark.parametrize("case", ["wrong-audience", "bad-signature", "wrong-issuer", "expired", "no-subject",
                                  "untrusted-peer", "garbage"])
def test_a_bad_cloudflare_token_is_refused(cloudflare, case):
    token = {
        "wrong-audience": lambda: cloudflare.mint(aud=["another-app"]),
        "bad-signature": lambda: cloudflare.mint(signer=cloudflare.other),
        "wrong-issuer": lambda: cloudflare.mint(iss="https://other-team.cloudflareaccess.com"),
        "expired": lambda: cloudflare.mint(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600),
        "no-subject": lambda: cloudflare.mint(sub=None),
        "untrusted-peer": lambda: cloudflare.mint(),
        "garbage": lambda: "a.b.c",
    }[case]()
    assert _cf(token, "192.0.2.10" if case == "untrusted-peer" else "127.0.0.1") is None


def test_a_linked_cloudflare_person_is_signed_in_over_http(cloudflare, server, peer):
    dana = account(name="Dana", identity={"source": "cloudflare", "subject": "user-123", "name": "Dana",
                                          "email": "dana@example.test"})
    peer["addr"] = "127.0.0.1"
    status, _, body = http("GET", server + "/api/me", headers={"Cf-Access-Jwt-Assertion": cloudflare.mint()})
    assert status == 200 and b'"user_id": %d' % dana["user_id"] in body
    bad = cloudflare.mint(aud=["another-app"])
    assert http("GET", server + "/api/me", headers={"Cf-Access-Jwt-Assertion": bad})[0] == 401


@pytest.mark.parametrize("valid", [True, False])
def test_a_cloudflare_token_beside_a_tailscale_login_gives_no_proxy_identity(cloudflare, valid):
    """Present counts, valid or not: a bad JWT beside a Tailscale login must not fall through to the Tailscale one."""
    token = cloudflare.mint() if valid else cloudflare.mint(signer=cloudflare.other)
    assert auth.proxy_identity(req("127.0.0.1", {"Cf-Access-Jwt-Assertion": token, **TS_HEADERS})) is None
    assert auth.proxy_identity(req("127.0.0.1", TS_HEADERS))["source"] == "tailscale"
    if valid:
        assert auth.proxy_identity(req("127.0.0.1", {"Cf-Access-Jwt-Assertion": token}))["source"] == "cloudflare"


def test_cloudflare_mode_refuses_to_start_without_pyjwt(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_CLOUDFLARE_TEAM", TEAM)
    monkeypatch.setenv("MOCKREVIEW_CLOUDFLARE_AUDIENCE", AUD)
    monkeypatch.setitem(sys.modules, "jwt", None)          # `import jwt` now raises ImportError
    with pytest.raises(auth.AuthConfigError) as e:
        auth.check_startup()
    assert "PyJWT" in str(e.value) and "cloudflare_team" in str(e.value)
    with pytest.raises(auth.AuthConfigError):
        srv.make_server("127.0.0.1", 0)


def test_cloudflare_settings_must_come_as_a_pair(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_CLOUDFLARE_TEAM", TEAM)
    with pytest.raises(auth.AuthConfigError) as e:
        auth.check_startup()
    assert "set both" in str(e.value)


def test_without_cloudflare_settings_pyjwt_is_not_needed(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "jwt", None)
    auth.check_startup()


def test_the_server_refuses_to_start_without_itsdangerous(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "itsdangerous", None)
    with pytest.raises(auth.AuthConfigError) as e:
        auth.check_startup()
    assert "itsdangerous" in str(e.value)
    with pytest.raises(users.AuthError):
        users.create_invite("test")


# ---------------------------------------------------------------- loopback = owner

def test_a_fresh_install_opens_on_this_computer_without_signing_in(env, server, review):
    status, _, page = http("GET", server + "/")
    assert status == 200 and b"Signed in as Owner (this computer)" in page and review["id"].encode() in page
    assert http("GET", server + "/r/{}/state.json".format(review["id"]))[0] == 200
    assert [u["role"] for u in users.list_users()] == ["admin"]


def test_with_loopback_owner_off_a_peer_without_headers_gets_the_sign_in_page(env, server, review, peer,
                                                                             loopback_owner_off):
    for addr in ("192.0.2.10", "127.0.0.1"):
        peer["addr"] = addr
        status, _, page = http("GET", server + "/")
        assert status == 401 and b"Sign in to mock-review" in page
        status, _, page = http("GET", server + "/r/{}/".format(review["id"]))
        assert status == 401 and review["id"].encode() not in page


def test_a_remote_peer_is_never_the_loopback_owner(env, server, review, peer):
    peer["addr"] = "192.0.2.10"
    assert http("GET", server + "/")[0] == 401
    assert http("GET", server + "/r/{}/".format(review["id"]))[0] == 401


def test_a_proxied_request_from_loopback_is_not_the_owner(env, server, review):
    status, _, _ = http("GET", server + "/", headers={"X-Forwarded-For": "198.51.100.7"})
    assert status == 401


def test_the_sign_in_page_is_the_same_for_a_missing_and_an_existing_review(env, server, review, peer):
    peer["addr"] = "192.0.2.10"
    a = raw("GET", server + "/r/{}/".format(review["id"]))
    b = raw("GET", server + "/r/no-such-review-0000/")
    assert a[0] == b[0] == 401 and a[2] == b[2]
