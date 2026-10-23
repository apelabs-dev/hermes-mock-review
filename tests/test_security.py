import json

import pytest

from conftest import FEEDBACK, http
from mockreview import config, store
from mockreview.store import ReviewError


@pytest.mark.parametrize("bad", ["..", "../etc", "a/b", "A", "", "-x", "x-", "a" * 80, "x\x00y", "%2e%2e", "r.json"])
def test_review_id_validation(env, bad):
    with pytest.raises(ReviewError):
        store.review_dir(bad)


@pytest.mark.parametrize("rel", ["../../review.json", "../b/index.html", "/etc/passwd", ".lock", "a/../../x",
                                 "assets/../../../review.json", "x\x00.html", "..\\..\\review.json"])
def test_variant_file_traversal_rejected(review, rel):
    with pytest.raises(ReviewError):
        store.safe_variant_file(review["id"], "1", "a", rel)


def test_variant_file_symlink_escape_rejected(review):
    d = config.data_root() / review["id"] / "rounds/1/a"
    (d / "evil.html").symlink_to(config.data_root() / review["id"] / "review.json")
    with pytest.raises(ReviewError):
        store.safe_variant_file(review["id"], "1", "a", "evil.html")


@pytest.mark.parametrize("path", [
    "/r/../etc/passwd",
    "/r/%2e%2e/review.json",
    "/r/{id}/rounds/1/a/..%2f..%2f..%2freview.json",
    "/r/{id}/rounds/1/a/%2e%2e/%2e%2e/%2e%2e/review.json",
    "/r/{id}/rounds/1/../../review.json",
    "/r/{id}/rounds/1/e/index.html",
    "/r/{id}/rounds/x/a/index.html",
    "/r/{id}/review.json",
    "/r/{id}/.lock",
])
def test_http_traversal_returns_404(server, review, path):
    code, _, body = http("GET", server + path.format(id=review["id"]))
    assert code in (400, 404), (path, code)
    assert b"token" not in body and b"thread_id" not in body


def test_board_and_variant_headers(server, review):
    code, h, body = http("GET", f"{server}/r/{review['id']}/")
    assert code == 200 and "frame-ancestors 'none'" in h["Content-Security-Policy"]
    code, h, body = http("GET", f"{server}/r/{review['id']}/rounds/1/a/index.html")
    assert code == 200 and h["Content-Security-Policy"] == "sandbox allow-scripts"
    assert b"<h1>alpha</h1>" in body
    code, h, _ = http("GET", f"{server}/r/{review['id']}/rounds/1/a/assets/logo.svg")
    assert code == 200 and h["Content-Type"].startswith("image/svg+xml")
    code, h, _ = http("GET", f"{server}/r/{review['id']}")
    assert code in (200, 301)


def test_state_json_has_no_thread_id(server, review):
    code, _, body = http("GET", f"{server}/r/{review['id']}/state.json")
    st = json.loads(body)
    assert code == 200 and st["id"] == review["id"] and "origin" not in st
    assert b"1490000000000000001" not in body


def test_body_size_capped(server, review):
    """A huge Content-Length is refused with 413 before any body is read."""
    import socket
    host, port = server.replace("http://", "").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.sendall((f"POST /r/{review['id']}/api/submit HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {config.MAX_BODY + 1}\r\n\r\n").encode() + b"{")
        resp = b""
        while chunk := s.recv(4096):
            resp += chunk
    assert resp.startswith(b"HTTP/1.1 413") and b"2 MB" in resp
    # just under the cap is read and parsed normally (then rejected as bad feedback, not as too large)
    body = b'{"round":1,"token":"x","feedback":{"guidelines":"' + b"a" * (config.MAX_BODY - 200) + b'"}}'
    code, _, _ = http("POST", f"{server}/r/{review['id']}/api/submit", body)
    assert code == 403


def test_content_type_and_null_origin(server, review):
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _, _ = http("POST", f"{server}/r/{review['id']}/api/submit", json.dumps(body).encode(),
                      {"Content-Type": "text/plain"})
    assert code == 415
    code, _, _ = http("POST", f"{server}/r/{review['id']}/api/submit", body, {"Origin": "null"})
    assert code == 403


def test_index_escapes_titles(env, server, variants):
    specs = [store.parse_variant_spec(variants[0])]
    store.create_review("<script>alert(1)</script>", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, "tailscale", specs)
    code, _, body = http("GET", server + "/")
    assert code == 200 and b"<script>alert(1)" not in body and b"&lt;script&gt;" in body


def test_server_refuses_public_bind():
    """Loopback by default; 0.0.0.0 only as the explicit local-network opt-in; any other address refused."""
    from mockreview import server as srv
    for bad in ("192.168.1.5", "100.64.0.1", "example.com", "::"):
        with pytest.raises(SystemExit):
            srv.make_server(bad, 0)
    s = srv.make_server("0.0.0.0", 0)  # opt-in: share local links with the network
    try:
        assert s.server_address[0] == "0.0.0.0"
    finally:
        s.server_close()


def test_local_mode_links(env, tmp_path, monkeypatch):
    from mockreview import api, config, store
    assert api.resolve_mode("local") == "local" and api.resolve_mode("lan") == "local"
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "local")
    assert api.resolve_mode(None) == "local"
    data = {"id": "x-123abc", "mode": "local"}
    # loopback bind: a local link can only mean this computer
    monkeypatch.setenv("MOCKREVIEW_HOST", "127.0.0.1")
    assert store.review_url(data) == f"http://127.0.0.1:{config.port()}/r/x-123abc"
    # network bind: the computer's own detected address
    monkeypatch.setenv("MOCKREVIEW_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "detect_lan_ip", lambda: "10.0.0.42")
    assert store.review_url(data) == f"http://10.0.0.42:{config.port()}/r/x-123abc"
    assert config.local_base().startswith("http://127.0.0.1:")  # the CLI still talks to it over loopback
    # explicit address wins
    monkeypatch.setenv("MOCKREVIEW_LAN_ADDRESS", "review-box.example.test")
    assert store.review_url(data) == f"http://review-box.example.test:{config.port()}/r/x-123abc"
    # tailnet links unchanged
    assert store.review_url({"id": "x-123abc", "mode": "tailscale"}).startswith(config.public_base())
