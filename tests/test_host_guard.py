"""Host allowlist (421), Origin check (403) and the loopback-only index: the DNS-rebinding guards."""

import json
import socket
import threading

import pytest

from conftest import FEEDBACK, http
from mockreview import config, server as srv

PUBLIC = "board.tailnet-example.test"


@pytest.fixture
def board(env, monkeypatch):
    """A real server on 127.0.0.1:0 whose public_base host is PUBLIC (set before the allowlist is computed)."""
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_BASE", f"https://{PUBLIC}:8790")
    httpd = srv.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def raw(port, method, path, host, extra="", body=b""):
    """One request with exactly the given Host header (None: no Host at all); returns (status, raw response)."""
    lines = [f"{method} {path} HTTP/1.1"]
    if host is not None:
        lines.append(f"Host: {host}")
    if body:
        lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    req = ("\r\n".join(lines) + "\r\n" + extra + "Connection: close\r\n\r\n").encode() + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.settimeout(5)
        s.sendall(req)
        resp = b""
        while chunk := s.recv(65536):
            resp += chunk
    return int(resp.split(b" ", 2)[1]), resp


def submit_body(review):
    return json.dumps({"round": 1, "token": review["token"], "feedback": FEEDBACK}).encode()


def feedback_file(rid):
    return config.data_root() / rid / "rounds" / "1" / "feedback.md"


# ---- host_allowed / origin_allowed (unit) ----------------------------------------------------------------------

def test_hostname_parsing():
    assert srv.hostname_of("Example.COM.:8790") == "example.com"
    assert srv.hostname_of("[::1]:8000") == "::1"
    assert srv.hostname_of("[::1]") == "::1"
    assert srv.hostname_of("localhost") == "localhost"
    for bad in ("", None, "::1", "a:b:c", "[::1]x", "[::1]:x", "host:port", "a b", "user@host", "h/x"):
        assert srv.hostname_of(bad) == "", bad


def test_allowlist_specific_bind(monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_BASE", f"https://{PUBLIC.upper()}.:8790/")
    allowed = srv.build_allowed_hosts("127.0.0.1")
    assert not allowed.ip_literals
    for ok in ("localhost", "LOCALHOST:8790", "localhost.:1", "127.0.0.1:8790", "[::1]:8790", PUBLIC,
               f"{PUBLIC}:443", f"{PUBLIC.upper()}."):
        assert srv.host_allowed(ok, allowed), ok
    for bad in ("attacker.example", "attacker.example:8790", "192.168.1.20:8790", "[fe80::1]:8790", "", None,
                f"{PUBLIC}.attacker.example", "127.0.0.1.attacker.example", "localhost:x"):
        assert not srv.host_allowed(bad, allowed), bad


def test_allowlist_wildcard_bind_accepts_ip_literals_only():
    allowed = srv.build_allowed_hosts("0.0.0.0")   # computed only: no socket is bound
    assert allowed.ip_literals
    for ok in ("192.168.1.20:8790", "10.0.0.5", "[fe80::1]:8790", "[2001:db8::2]", "localhost:8790"):
        assert srv.host_allowed(ok, allowed), ok
    for bad in ("attacker.example", "rebind.attacker.example:8790", "192.168.1.20.nip.io"):
        assert not srv.host_allowed(bad, allowed), bad


def test_allowlist_bind_host_and_lan_address(monkeypatch):
    assert srv.host_allowed("[::1]:9", srv.build_allowed_hosts("::1"))
    monkeypatch.setenv("MOCKREVIEW_LAN_ADDRESS", "mac.lan-example.test")
    allowed = srv.build_allowed_hosts("127.0.0.1")
    assert srv.host_allowed("mac.lan-example.test:8790", allowed)
    assert not srv.host_allowed("192.168.1.20", allowed)


def test_origin_allowed():
    allowed = srv.build_allowed_hosts("127.0.0.1")
    for ok in ("http://127.0.0.1:8790", "http://localhost:8790", "https://localhost", "http://[::1]:8790"):
        assert srv.origin_allowed(ok, allowed), ok
    for bad in ("http://attacker.example", "http://attacker.example:8790", "null", "file://", "ftp://localhost",
                "http://localhost:8790/x", "http://user@localhost:8790", "localhost:8790", ""):
        assert not srv.origin_allowed(bad, allowed), bad


def test_local_request():
    assert srv.local_request("127.0.0.1", {})
    assert srv.local_request("::1", {})
    assert not srv.local_request("192.168.1.20", {})
    assert not srv.local_request("127.0.0.1", {"X-Forwarded-For": "100.64.0.9"})
    assert not srv.local_request("127.0.0.1", {"Tailscale-User-Login": "someone"})


# ---- a real server ----------------------------------------------------------------------------------------------

def test_foreign_host_is_421_everywhere(board, review):
    rid = review["id"]
    for path in ("/", "/healthz", f"/r/{rid}/", f"/r/{rid}/state.json", f"/r/{rid}/board.js", "/api/owner"):
        for method in ("GET", "HEAD"):
            code, resp = raw(board, method, path, "attacker.example")
            assert code == 421, (method, path)
            assert b"text/plain" in resp and b"allow" not in resp.lower() and PUBLIC.encode() not in resp
    code, _ = raw(board, "POST", f"/r/{rid}/api/submit", f"attacker.example:{board}", body=submit_body(review))
    assert code == 421
    assert raw(board, "GET", "/healthz", None)[0] == 421               # no Host at all
    assert raw(board, "GET", "/healthz", "localhost", extra="Host: attacker.example\r\n")[0] == 421   # two
    assert not feedback_file(rid).exists() and review_untouched(review)


def review_untouched(review):
    from mockreview import store
    return store.load(review["id"])["status"] == "open"


def test_allowed_hosts_are_served(board, review):
    for host in (f"localhost:{board}", f"[::1]:{board}", f"127.0.0.1:{board}", PUBLIC, f"{PUBLIC}:8790"):
        code, resp = raw(board, "GET", f"/r/{review['id']}/state.json", host)
        assert code == 200, host
        assert raw(board, "GET", "/healthz", host)[0] == 200, host


def test_cross_origin_post_refused_without_side_effects(board, review, env):
    rid = review["id"]
    code, resp = raw(board, "POST", f"/r/{rid}/api/submit", f"127.0.0.1:{board}",
                     extra="Origin: http://attacker.example\r\n", body=submit_body(review))
    assert code == 403 and b"cross-origin" in resp
    code, _ = raw(board, "POST", f"/r/{rid}/api/submit", f"127.0.0.1:{board}",
                  extra=f"Origin: http://attacker.example:{board}\r\n", body=submit_body(review))
    assert code == 403
    assert env["receiver"].requests == []
    assert not feedback_file(rid).exists()


def test_same_origin_post_works(board, review, env):
    code, _, body = http("POST", f"http://127.0.0.1:{board}/r/{review['id']}/api/submit",
                         {"round": 1, "token": review["token"], "feedback": FEEDBACK},
                         {"Origin": f"http://127.0.0.1:{board}"})
    assert code == 200 and json.loads(body)["notified"] is True
    assert len(env["receiver"].requests) == 2 and feedback_file(review["id"]).is_file()


def test_public_origin_post_works(board, review):
    code, _ = raw(board, "POST", f"/r/{review['id']}/api/submit", PUBLIC,
                  extra=f"Origin: https://{PUBLIC}:8790\r\n", body=submit_body(review))
    assert code == 200


def test_index_local_only(board, review, monkeypatch):
    host = f"127.0.0.1:{board}"
    code, resp = raw(board, "GET", "/", host)
    assert code == 200 and b"Mock reviews" in resp
    code, resp = raw(board, "GET", "/", host, extra="X-Forwarded-For: 100.64.0.9\r\n")
    assert code == 404 and b"Mock reviews" not in resp
    assert raw(board, "HEAD", "/", host, extra="Forwarded: for=100.64.0.9\r\n")[0] == 404
    assert raw(board, "GET", "/", PUBLIC, extra="Tailscale-User-Login: someone\r\n")[0] == 404
    monkeypatch.setattr(srv, "LOCAL_PEERS", ())     # as seen from a non-loopback peer
    code, resp = raw(board, "GET", "/", host)
    assert code == 404 and b"Mock reviews" not in resp
    assert raw(board, "HEAD", "/", host)[0] == 404
    assert raw(board, "GET", f"/r/{review['id']}/state.json", host)[0] == 200   # the review URL still works
