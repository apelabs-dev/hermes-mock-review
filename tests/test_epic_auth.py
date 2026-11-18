"""AUTH: forwarded identity on a public host, public_base is not a public name, the Origin must match
the request's scheme and port, and every percent-encoded spelling of a link route is redacted from the log."""

import json
import logging
import threading
import urllib.parse

import pytest

import authkit
from authkit import account, signed_in, token_of
from mockreview import config, public, server as srv, users
from mockreview.store import ReviewError
from test_auth_proxy import TS_HEADERS, link_tailscale
from test_host_guard import CSRF, raw
from test_public_hosting import FakeTailscale, status_json

PUBLIC_URL_HOST = "public.example.test"
SERVE_HOST = "serve.example.test"
FUNNEL_HOST = "funnel.example.test"
FORWARDED = {"X-Forwarded-Email": "mo@example.test"}
MO = {"source": "forwarded", "subject": "mo@example.test", "name": "Mo", "email": "mo@example.test"}


def _board(monkeypatch, public_url=None, public_base=None):
    """A real board on 127.0.0.1:0, with the allowlist built after these settings are in place."""
    if public_url is None:
        monkeypatch.delenv("MOCKREVIEW_PUBLIC_URL", raising=False)
    else:
        monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", public_url)
    if public_base is not None:
        monkeypatch.setenv("MOCKREVIEW_PUBLIC_BASE", public_base)
    monkeypatch.setattr(config, "funnel_url", lambda: "")
    httpd = srv.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd


@pytest.fixture
def forwarded_public_board(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    httpd = _board(monkeypatch, public_url=f"https://{PUBLIC_URL_HOST}")
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def serve_board(env, monkeypatch):
    httpd = _board(monkeypatch, public_url=f"https://{PUBLIC_URL_HOST}",
                   public_base=f"https://{SERVE_HOST}:8790")
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- 1. forwarded identity on a public host

def test_forwarded_identity_is_refused_on_a_public_host(forwarded_public_board):
    port = forwarded_public_board
    account(instance="admin", name="Mo", identity=MO)
    headers = "".join(f"{k}: {v}\r\n" for k, v in FORWARDED.items())

    code, body = raw(port, "GET", "/api/me", PUBLIC_URL_HOST, extra=headers)
    assert code == 401, body
    before = users.list_invites()
    code, _ = raw(port, "POST", "/api/invites", PUBLIC_URL_HOST, extra=headers + CSRF, body=b"{}")
    assert code in (401, 403)
    assert users.list_invites() == before

    code, body = raw(port, "GET", "/api/me", f"127.0.0.1:{port}", extra=headers)
    assert code == 200 and b'"source": "forwarded"' in body


def test_forwarded_identity_is_refused_with_the_funnel_marker(forwarded_public_board):
    port = forwarded_public_board
    account(instance="admin", name="Mo", identity=MO)
    headers = "".join(f"{k}: {v}\r\n" for k, v in FORWARDED.items()) + "Tailscale-Funnel-Request: ?1\r\n"
    assert raw(port, "GET", "/api/me", PUBLIC_URL_HOST, extra=headers)[0] == 401


# ---------------------------------------------------------------- 2. public_base is not a public name

def test_public_on_refuses_while_forwarded_identity_is_on(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    fake = FakeTailscale(status_json({}, []))
    monkeypatch.setattr(config, "run_tailscale", fake)
    with pytest.raises(ReviewError) as e:
        public.enable()
    assert "forwarded_identity" in e.value.message
    assert "hermes config set plugins.entries.mock-review.settings.forwarded_identity false" in e.value.message
    assert fake.calls == []


def test_public_base_is_not_a_public_name(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_BASE", f"https://{SERVE_HOST}:8790")
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", f"https://{PUBLIC_URL_HOST}")
    monkeypatch.setattr(config, "funnel_url", lambda: f"https://{FUNNEL_HOST}:8443")
    allowed = srv.build_allowed_hosts("127.0.0.1")
    assert SERVE_HOST in allowed.names
    assert SERVE_HOST not in allowed.public_names
    assert PUBLIC_URL_HOST in allowed.public_names
    assert FUNNEL_HOST in allowed.public_names


def test_tailnet_sign_in_works_on_the_serve_address(serve_board):
    port = serve_board
    dana = link_tailscale()
    headers = "".join(f"{k}: {v}\r\n" for k, v in TS_HEADERS.items())
    code, body = raw(port, "GET", "/api/me", f"{SERVE_HOST}:8790", extra=headers)
    assert code == 200 and b'"user_id": %d' % dana["user_id"] in body
    assert raw(port, "GET", "/api/me", f"{SERVE_HOST}:8790",
               extra=headers + "Tailscale-Funnel-Request: ?1\r\n")[0] == 401
    assert raw(port, "GET", "/api/me", PUBLIC_URL_HOST, extra=headers)[0] == 401


# ---------------------------------------------------------------- 3. Origin scheme + host + port

def test_origin_must_match_the_requests_scheme_and_port(server):
    admin = account(instance="admin", name="Ada")
    signed = signed_in(admin)
    port = urllib.parse.urlsplit(server).port
    body = json.dumps({"role": "member"}).encode()
    before = users.list_invites()

    code, _, _ = authkit.raw("POST", server + "/api/invites", body,
                             dict(signed, **{"Content-Type": "application/json",
                                             "Origin": "http://localhost:3000"}))
    assert code == 403
    assert users.list_invites() == before

    code, _, _ = authkit.raw("POST", server + "/api/invites", body,
                             dict(signed, **{"Content-Type": "application/json",
                                             "Origin": f"http://127.0.0.1:{port}"}))
    assert code == 200
    assert len(users.list_invites()) == len(before) + 1


# ---------------------------------------------------------------- 4. log redaction

def test_percent_encoded_link_routes_are_redacted_in_the_log(server, review, caplog):
    invite = users.create_invite("test")
    login = users.create_login_link("test")
    share = users.share("test", review["id"], "create", "viewer")
    tokens = [token_of(invite["path"]), token_of(login["path"]), token_of(share["path"])]
    paths = ["/%69nvite/" + tokens[0], "/%6Cogin/" + tokens[1], "/%73/" + tokens[2]]
    with caplog.at_level(logging.INFO, logger="mockreview.server"):
        statuses = [authkit.raw("GET", server + path)[0] for path in paths]
    assert statuses == [200, 200, 303]
    text = caplog.text
    for token in tokens:
        assert token not in text
    for line in ('"GET /invite/… HTTP/1.1" 200', '"GET /login/… HTTP/1.1" 200', '"GET /s/… HTTP/1.1" 303'):
        assert line in text


@pytest.mark.parametrize("spelling", ["//invite/{token}", "///%69nvite/{token}",
                                      "http://127.0.0.1:{port}/invite/{token}"])
def test_every_request_target_spelling_the_router_accepts_is_redacted(server, caplog, spelling):
    port = int(server.rsplit(":", 1)[1])
    token = token_of(users.create_invite("test")["path"])
    target = spelling.format(token=token, port=port)
    with caplog.at_level(logging.INFO, logger="mockreview.server"):
        status, _ = raw(port, "GET", target, f"127.0.0.1:{port}")
    assert status == 200
    assert token not in caplog.text
    assert '"GET /invite/… HTTP/1.1" 200' in caplog.text


def test_redact_link_tokens_keeps_the_rest_of_the_line():
    assert srv.redact_link_tokens('"GET /%69nvite/abc HTTP/1.1" 404 -') == '"GET /invite/… HTTP/1.1" 404 -'
    assert srv.redact_link_tokens('"GET /%6Cogin/abc HTTP/1.1" 200 -') == '"GET /login/… HTTP/1.1" 200 -'
    assert srv.redact_link_tokens('"GET /%73/abc HTTP/1.1" 303 -') == '"GET /s/… HTTP/1.1" 303 -'
    assert srv.redact_link_tokens('"GET /r/abc/state.json HTTP/1.1" 200 42') == \
        '"GET /r/abc/state.json HTTP/1.1" 200 42'
