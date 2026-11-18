"""Public hosting: the `public` mode, the Funnel CLI guard rails, and the hardening for an open internet."""

import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import authkit
from mockreview import api, cli, config, public, server as srv, settings, store, wakeguard
from mockreview.store import ReviewError
from test_host_guard import raw
from test_store import _decisions

ORIGIN = {"platform": "telegram", "chat_id": "test-chat"}
NODE = "board.example.ts.net"
OURS = "https://board.example.test"


def status_json(mappings, funnel, handlers=None, tcp=None):
    """A `tailscale funnel status --json` body: Web entries from root proxies and/or explicit handlers, the AllowFunnel
    keys, and optional TCP entries."""
    handlers = handlers or {}
    web = {}
    for port in sorted(set(mappings) | set(handlers)):
        web[f"{NODE}:{port}"] = {"Handlers": handlers.get(port) or {"/": {"Proxy": mappings[port]}}}
    body = {"Web": web, "AllowFunnel": {f"{NODE}:{p}": True for p in funnel}}
    if tcp is not None:
        body["TCP"] = {str(p): entry for p, entry in tcp.items()}
    return body


class FakeTailscale:
    """run_tailscale double: status before and after the mutation, recording every argv."""

    def __init__(self, before, after=None):
        self.before, self.after = before, after or before
        self.calls, self.served = [], "before"

    def __call__(self, args, timeout=5):
        args = list(args)
        self.calls.append(args)
        if args == ["funnel", "status", "--json"]:
            body = self.before if self.served == "before" else self.after
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        if args[0] == "funnel":
            self.served = "after"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def mutations(self):
        return [c for c in self.calls if c != ["funnel", "status", "--json"]]


@pytest.fixture
def no_funnel(monkeypatch):
    monkeypatch.setattr(config, "funnel_url", lambda: "")


@pytest.fixture
def public_board(env, monkeypatch, no_funnel):
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", OURS)
    httpd = srv.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------- public mode and links

def test_public_mode_is_accepted_and_uses_public_url(env, tmp_path, monkeypatch):
    assert store.MODES == ("tailscale", "local", "public")
    assert api.resolve_mode("public") == "public"
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", OURS)
    monkeypatch.setattr(config, "funnel_url", lambda: "")
    res = api.create("Public", ORIGIN, mode="public", decisions=str(_decisions(tmp_path)))
    assert res["mode"] == "public" and res["url"].startswith(OURS + "/r/")
    assert "note" not in res
    assert store.load(res["review_id"])["mode"] == "public"


def test_public_link_falls_back_to_funnel_then_public_base(env, tmp_path, monkeypatch):
    decisions = str(_decisions(tmp_path))
    monkeypatch.setattr(api, "server_up", lambda: True)
    monkeypatch.delenv("MOCKREVIEW_PUBLIC_URL", raising=False)
    monkeypatch.setattr(config, "funnel_url", lambda: "https://board.example.ts.net:8443")
    res = api.create("Funnel", ORIGIN, mode="public", decisions=decisions)
    assert res["url"].startswith("https://board.example.ts.net:8443/r/") and "note" not in res
    monkeypatch.setattr(config, "funnel_url", lambda: "")
    res = api.create("Base", ORIGIN, mode="public", decisions=decisions)
    assert res["url"].startswith("https://board.example.test:8790/r/")
    assert "public mode has no public URL yet" in res["note"]
    assert "hermes mockreview public on" in res["note"]


def test_funnel_url_reads_only_a_mapping_to_the_board_port(env, monkeypatch):
    board = config.port()
    body = status_json({443: "http://127.0.0.1:9000", 8443: f"http://127.0.0.1:{board}"}, [443, 8443])
    monkeypatch.setattr(config, "run_tailscale", lambda args, timeout=5: SimpleNamespace(
        returncode=0, stdout=json.dumps(body), stderr=""))
    assert config.funnel_url() == f"https://{NODE}:8443"
    body = status_json({443: "http://127.0.0.1:9000"}, [443])       # nothing serves the board
    monkeypatch.setattr(config, "run_tailscale", lambda args, timeout=5: SimpleNamespace(
        returncode=0, stdout=json.dumps(body), stderr=""))
    assert config.funnel_url() == ""
    body = status_json({8443: f"http://127.0.0.1:{board}"}, [])     # a Serve route, not Funnel
    monkeypatch.setattr(config, "run_tailscale", lambda args, timeout=5: SimpleNamespace(
        returncode=0, stdout=json.dumps(body), stderr=""))
    assert config.funnel_url() == ""

    def missing(args, timeout=5):
        raise FileNotFoundError("tailscale")
    monkeypatch.setattr(config, "run_tailscale", missing)
    assert config.funnel_url() == ""


@pytest.mark.parametrize("value", ["", "https://reviews.example.com", "http://127.0.0.1:8080"])
def test_public_url_accepts_empty_and_host_only(value):
    assert settings.coerce(settings.BY_NAME["public_url"], value) == value


@pytest.mark.parametrize("value", ["https://reviews.example.com/", "https://reviews.example.com/path",
                                   "https://reviews.example.com?x=1", "https://reviews.example.com#top",
                                   "https://user:pw@reviews.example.com", "reviews.example.com", "ftp://example.test",
                                   "https://"])
def test_public_url_rejects_paths_queries_and_credentials(value):
    with pytest.raises(settings.SettingsError, match="public_url"):
        settings.coerce(settings.BY_NAME["public_url"], value)


# ---------------------------------------------------------------- mockreview public on|off|status

def test_enable_maps_only_the_chosen_port_and_leaves_443_alone(env, monkeypatch):
    board = config.port()
    before = status_json({443: "http://127.0.0.1:9000"}, [443])
    after = status_json({443: "http://127.0.0.1:9000", 8443: f"http://127.0.0.1:{board}"}, [443, 8443])
    fake = FakeTailscale(before, after)
    monkeypatch.setattr(config, "run_tailscale", fake)
    res = public.enable()
    assert fake.mutations() == [["funnel", "--bg", "--yes", "--https=8443",
                                 f"http://127.0.0.1:{board}"]]
    for call in fake.calls:
        assert "reset" not in call and "off" not in call and "--https=443" not in call
    assert res["enabled"] is True and res["url"] == f"https://{NODE}:8443"
    assert "restart" not in res["note"]
    assert "a Funnel request carries no proxy identity at all" in res["note"]
    assert "public public_url" not in res["note"]                 # notes are separated, not run together
    assert res["note"].endswith("(docs/public-hosting.md)")
    assert res["mapping"] == {"host": NODE, "https_port": 8443, "proxy": f"http://127.0.0.1:{board}"}
    assert res["status"]["Web"][f"{NODE}:443"] == before["Web"][f"{NODE}:443"]
    assert res["status"]["AllowFunnel"][f"{NODE}:443"] == before["AllowFunnel"][f"{NODE}:443"]


def test_enable_refuses_a_port_that_serves_another_app(env, monkeypatch):
    before = status_json({8443: "http://127.0.0.1:9000"}, [8443])
    fake = FakeTailscale(before)
    monkeypatch.setattr(config, "run_tailscale", fake)
    with pytest.raises(ReviewError, match=r"port 8443 already serves http://127\.0\.0\.1:9000; refusing"):
        public.enable()
    assert fake.mutations() == []


@pytest.mark.parametrize("handler", [{"Text": "hello from another app"}, {"Path": "/files/"}])
def test_enable_refuses_foreign_file_or_text_handler(env, monkeypatch, handler):
    before = status_json({}, [], handlers={8443: {"/": handler}})
    fake = FakeTailscale(before)
    monkeypatch.setattr(config, "run_tailscale", fake)
    kind = next(iter(handler))
    with pytest.raises(ReviewError, match=rf"port 8443 already serves / \(a {kind} handler\); "
                                          r"refusing to make a port that is not only this board public"):
        public.enable()
    assert fake.mutations() == []
    assert not any("--bg" in call for call in fake.calls)


@pytest.mark.parametrize("handler, expected", [
    ({"Proxy": "http://127.0.0.1:9000"},
     r"/private/ -> http://127\.0\.0\.1:9000; refusing to replace another app's route"),
    ({"Redirect": "/new/"},
     r"/old/ \(a Redirect handler\); refusing to make a port that is not only this board public"),
])
def test_enable_refuses_foreign_nonroot_handler(env, monkeypatch, handler, expected):
    board = config.port()
    before = status_json({}, [], handlers={8443: {
        "/": {"Proxy": f"http://127.0.0.1:{board}"},
        "/private/" if "Proxy" in handler else "/old/": handler,
    }})
    fake = FakeTailscale(before)
    monkeypatch.setattr(config, "run_tailscale", fake)
    with pytest.raises(ReviewError, match=rf"port 8443 already serves {expected}"):
        public.enable()
    assert fake.mutations() == []


def test_enable_refuses_a_tcp_forward_on_the_chosen_port(env, monkeypatch):
    before = status_json({}, [], tcp={8443: {"TCPForward": "127.0.0.1:9000"}})
    fake = FakeTailscale(before)
    monkeypatch.setattr(config, "run_tailscale", fake)
    with pytest.raises(ReviewError, match=r"port 8443 already serves TCP 8443 \(a TCPForward to "
                                          r"127\.0\.0\.1:9000\); refusing to make a port that is not only this board "
                                          r"public"):
        public.enable()
    assert fake.mutations() == []


def test_enable_accepts_a_port_that_is_only_the_board(env, monkeypatch):
    board = config.port()
    before = status_json({}, [], handlers={8443: {
        "/": {"Proxy": f"http://127.0.0.1:{board}"},
        "/board/": {"Proxy": f"http://localhost:{board}"},
    }}, tcp={8443: {"HTTPS": True}})
    after = status_json({8443: f"http://127.0.0.1:{board}"}, [8443])
    fake = FakeTailscale(before, after)
    monkeypatch.setattr(config, "run_tailscale", fake)
    res = public.enable()
    assert fake.mutations() == [["funnel", "--bg", "--yes", "--https=8443",
                                 f"http://127.0.0.1:{board}"]]
    assert res["enabled"] is True and res["url"] == f"https://{NODE}:8443"


def test_enable_validates_the_allowed_ports(env):
    with pytest.raises(ReviewError, match="443, 8443 and 10000"):
        public.enable(8790)


def test_disable_refuses_a_foreign_mapping(env, monkeypatch):
    before = status_json({8443: "http://127.0.0.1:9000"}, [8443])
    fake = FakeTailscale(before)
    monkeypatch.setattr(config, "run_tailscale", fake)
    with pytest.raises(ReviewError, match="not this board"):
        public.disable()
    assert fake.mutations() == []


def test_disable_removes_only_this_boards_mapping(env, monkeypatch):
    board = config.port()
    before = status_json({8443: f"http://127.0.0.1:{board}"}, [8443])
    fake = FakeTailscale(before)
    monkeypatch.setattr(config, "run_tailscale", fake)
    res = public.disable()
    assert fake.mutations() == [["funnel", "--https=8443", "off"]]
    for call in fake.calls:
        assert "--https=443" not in call and "reset" not in call
    assert res["ok"] is True and res["enabled"] is False


@pytest.mark.parametrize("foreign", [
    {"/private/": {"Proxy": "http://127.0.0.1:9000"}},
    {"/files/": {"Path": "/files/"}},
    {"/about": {"Text": "hello from another app"}},
    {"/old/": {"Redirect": "/new/"}},
])
def test_disable_refuses_mixed_foreign_handlers(env, monkeypatch, foreign):
    board = config.port()
    path, handler = next(iter(foreign.items()))
    kind, target = next(iter(handler.items()))
    before = status_json({443: "http://127.0.0.1:9000"}, [443, 8443],
                         handlers={8443: {"/": {"Proxy": f"http://127.0.0.1:{board}"}, **foreign}})
    web_443 = json.dumps(before["Web"][f"{NODE}:443"], sort_keys=True)
    allow_443 = json.dumps(before["AllowFunnel"][f"{NODE}:443"], sort_keys=True)
    fake = FakeTailscale(before)
    monkeypatch.setattr(config, "run_tailscale", fake)
    what = target if path == "/" else (f"{path} -> {target}" if kind == "Proxy" else f"{path} (a {kind} handler)")
    with pytest.raises(ReviewError) as err:
        public.disable()
    assert err.value.message == f"port 8443 already serves {what}; {public.REMOVE_TAIL}"
    assert fake.mutations() == []
    assert json.dumps(before["Web"][f"{NODE}:443"], sort_keys=True) == web_443
    assert json.dumps(before["AllowFunnel"][f"{NODE}:443"], sort_keys=True) == allow_443


def test_disable_refuses_a_tcp_forward_next_to_the_board(env, monkeypatch):
    board = config.port()
    before = status_json({8443: f"http://127.0.0.1:{board}"}, [8443],
                         tcp={8443: {"TCPForward": "127.0.0.1:9000"}})
    fake = FakeTailscale(before)
    monkeypatch.setattr(config, "run_tailscale", fake)
    with pytest.raises(ReviewError) as err:
        public.disable()
    assert err.value.message == (f"port 8443 already serves TCP 8443 (a TCPForward to 127.0.0.1:9000); "
                                 f"{public.REMOVE_TAIL}")
    assert fake.mutations() == []


def test_disable_leaves_443_alone(env, monkeypatch):
    board = config.port()
    before = status_json({443: "http://127.0.0.1:9000", 8443: f"http://127.0.0.1:{board}"}, [443, 8443])
    after = status_json({443: "http://127.0.0.1:9000"}, [443])
    fake = FakeTailscale(before, after)
    monkeypatch.setattr(config, "run_tailscale", fake)
    res = public.disable()
    assert fake.mutations() == [["funnel", "--https=8443", "off"]]
    for call in fake.calls:
        assert "--https=443" not in call
        assert "reset" not in call
        assert "--bg" not in call
    assert res["status"]["Web"][f"{NODE}:443"] == before["Web"][f"{NODE}:443"]
    assert res["status"]["AllowFunnel"][f"{NODE}:443"] == before["AllowFunnel"][f"{NODE}:443"]


def test_disable_accepts_a_board_only_port_with_a_duplicate_proxy(env, monkeypatch):
    board = config.port()
    before = status_json({}, [], handlers={8443: {
        "/": {"Proxy": f"http://127.0.0.1:{board}"},
        "/board/": {"Proxy": f"http://localhost:{board}"},
    }}, tcp={8443: {"HTTPS": True}})
    after = status_json({}, [])
    fake = FakeTailscale(before, after)
    monkeypatch.setattr(config, "run_tailscale", fake)
    res = public.disable()
    assert fake.mutations() == [["funnel", "--https=8443", "off"]]
    assert res["ok"] is True and res["enabled"] is False


def test_disable_with_nothing_mapped_runs_no_command(env, monkeypatch):
    fake = FakeTailscale(status_json({}, []))
    monkeypatch.setattr(config, "run_tailscale", fake)
    res = public.disable()
    assert res["ok"] is True and res["enabled"] is False
    assert fake.mutations() == []


def test_status_without_tailscale_is_ok_and_says_why(env, monkeypatch, capsys):
    def missing(args, timeout=5):
        raise FileNotFoundError("tailscale")
    monkeypatch.setattr(config, "run_tailscale", missing)
    res = public.status()
    assert res["ok"] is True and res["available"] is False and "not installed" in res["detail"]
    assert res["enabled"] is False and res["status"] is None
    assert cli.main(["public", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    for key in ("ok", "provider", "available", "port", "board_port", "enabled", "url", "mapping", "public_url",
                "status"):
        assert key in out
    assert out["available"] is False


def test_status_cli_reports_the_mapping(env, monkeypatch, capsys):
    board = config.port()
    fake = FakeTailscale(status_json({8443: f"http://127.0.0.1:{board}"}, [8443]))
    monkeypatch.setattr(config, "run_tailscale", fake)
    assert cli.main(["public", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["available"] is True and out["enabled"] is True
    assert out["url"] == f"https://{NODE}:8443" and out["port"] == 8443
    assert out["commands"][0]["argv"] == ["funnel", "status", "--json"]


# ---------------------------------------------------------------- Host allowlist and public hardening

def test_public_url_host_is_allowed_and_a_foreign_one_is_421(public_board, review):
    code, body = raw(public_board, "GET", f"/r/{review['id']}/state.json", "board.example.test")
    assert code == 401   # allowed and served (the public Host is not the loopback owner), never 421
    code, body = raw(public_board, "GET", "/healthz", "attacker.example")
    assert code == 421 and b"allow" not in body.lower()


def test_healthz_is_minimal_on_the_public_host(public_board):
    code, body = raw(public_board, "GET", "/healthz", "board.example.test")
    assert code == 200 and json.loads(body.split(b"\r\n\r\n", 1)[1]) == {"ok": True}
    code, body = raw(public_board, "GET", "/healthz", "127.0.0.1")
    assert code == 200 and json.loads(body.split(b"\r\n\r\n", 1)[1]) == {"ok": True, "service": "mock-review"}


def test_public_host_from_loopback_is_never_the_owner(public_board, review):
    code, body = raw(public_board, "GET", "/api/owner", "board.example.test")   # from 127.0.0.1, no proxy header
    assert code == 404 and b"workspace" not in body
    code, body = raw(public_board, "GET", "/", "board.example.test")
    assert code == 401 and b"Sign in to mock-review" in body and b"Mock reviews" not in body


def test_a_public_host_refuses_a_tailscale_identity(env, monkeypatch):
    from test_auth_proxy import TS_HEADERS, link_tailscale
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", OURS)
    monkeypatch.setattr(config, "funnel_url", lambda: "")
    httpd = srv.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]
    try:
        link_tailscale()
        identity = "".join(f"{k}: {v}\r\n" for k, v in TS_HEADERS.items())
        code, body = raw(port, "GET", "/", "board.example.test", extra=identity)
        assert code == 401 and b"Sign in to mock-review" in body and b"proxy says" not in body
        code, body = raw(port, "GET", "/", f"127.0.0.1:{port}", extra=identity)
        assert code == 200 and b"Mock reviews" in body and b"Sign in" not in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_hsts_only_when_a_trusted_proxy_reports_https(public_board):
    code, body = raw(public_board, "GET", "/healthz", "127.0.0.1",
                     extra="X-Forwarded-Proto: https\r\n")
    assert code == 200 and b"Strict-Transport-Security: max-age=31536000" in body
    code, body = raw(public_board, "GET", "/healthz", "127.0.0.1")
    assert code == 200 and b"Strict-Transport-Security" not in body
    code, body = raw(public_board, "GET", "/healthz", "board.example.test",
                     extra="X-Forwarded-Proto: https\r\n")
    assert code == 200 and b"Strict-Transport-Security: max-age=31536000" in body


def test_wake_toolsets_refuses_a_public_board(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    reason = wakeguard.exposure("public", True)
    assert reason and "public URL" in reason and "press Send" in reason
    assert wakeguard.review_exposure({"mode": "public", "wake": True}) == reason
    assert wakeguard.exposure("public", False) is None
    with pytest.raises(ReviewError) as e:
        api.create("Public wake", ORIGIN, mode="public", decisions=str(_decisions(tmp_path)))
    assert e.value.status == 409 and "wake_toolsets" in e.value.message and "public" in e.value.message
    assert not store.list_ids()


def test_the_server_holds_a_public_board_wake(env, monkeypatch, tmp_path):
    from mockreview import service
    from test_notice import DEC_FB
    spec = store.parse_round_spec(decisions=str(_decisions(tmp_path)))
    data = store.create_review("Public hold", "", ORIGIN, "public", spec)
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    res = service.submit(data["id"], 1, DEC_FB, data["token"])
    assert res["notified"] is False and "public URL" in res["detail"]
    assert env["receiver"].wakes == []


# ---------------------------------------------------------------- the other limits still apply on a public Host

def test_oversized_body_is_413_with_a_public_host(public_board, review):
    host, port = "board.example.test", public_board
    reviewer = authkit.account(review["id"], role="reviewer")
    signed = authkit.signed_in(reviewer)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall((f"POST /r/{review['id']}/api/submit HTTP/1.1\r\nHost: {host}\r\n"
                   f"Cookie: {signed['Cookie']}\r\nX-CSRF-Token: {signed['X-CSRF-Token']}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {config.MAX_BODY + 1}\r\n\r\n").encode()
                  + b"{")
        resp = b""
        while chunk := s.recv(4096):
            resp += chunk
    assert resp.startswith(b"HTTP/1.1 413") and b"2 MB" in resp


def test_slow_read_times_out_with_a_public_host(public_board, review, monkeypatch):
    monkeypatch.setattr(srv.Handler, "timeout", 1)
    with socket.create_connection(("127.0.0.1", public_board), timeout=5) as s:
        s.settimeout(5)
        s.sendall(b"GET /healthz HTTP/1.1\r\nHost: board.example.test\r\n")   # no blank line: the handler waits
        started = time.monotonic()
        assert s.recv(4096) == b""
        assert time.monotonic() - started < 4
