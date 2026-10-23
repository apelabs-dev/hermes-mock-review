"""`hermes mockreview setup`, first-run friction: origin platform, dead test link, port identity, next step.

The `hermes` CLI is the recording shim of test_setup.py; the board is a real HTTP server on a loopback port 0 started
in the test (urllib is never mocked).
"""

import json
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import yaml

from conftest import ROOT, load_plugin
from mockreview import api, cli, config, setup
from test_setup import FakeTTY, fake_create, find, make_shim, read_log, run_setup, shim_secret

RID = "review-setuptest-000000"
CHAT_PLATFORMS = {"telegram": {"enabled": True}, "discord": {"enabled": True}}

RUNNING = "✓ Gateway is running (PID: 4242)\n  (Running manually, not as a system service)\n"
STOPPED = "✗ Gateway is not running\n\nTo start:\n  hermes gateway run      # Run in foreground\n"


# ---------------------------------------------------------------- helpers

class FakeBoard:
    """A real loopback HTTP server. `routes` maps a path to (status, body[, delay seconds])."""

    def __init__(self, routes):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                status, body, *delay = outer.routes.get(self.path, (404, "not found"))
                if delay:
                    time.sleep(delay[0])
                data = body.encode("utf-8") if isinstance(body, str) else body
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.routes, self.hits = routes, []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def boards():
    made = []

    def start(routes):
        b = FakeBoard(routes)
        made.append(b)
        return b

    yield start
    for b in made:
        b.close()


def owner_json(home=None, data_root=None, pid=4242, service="mock-review"):
    return json.dumps({"ok": True, "service": service, "api": 1, "pid": pid,
                       "data_root": str(data_root or config.own_data_root()),
                       "hermes_home": str(home or config.hermes_home()), "wake": {"ready": True}})


def ours_routes(rid=RID):
    return {"/api/owner": (200, owner_json()), f"/r/{rid}/state.json": (200, json.dumps({"id": rid}))}


@pytest.fixture(autouse=True)
def posix_next_steps(monkeypatch):
    """The next: wording differs on Windows (the board runs in the foreground there); tests that want it opt in."""
    monkeypatch.setattr(setup, "_is_windows", lambda: False)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tty(monkeypatch, *answers):
    monkeypatch.setattr(sys, "stdin", FakeTTY(list(answers)))
    monkeypatch.setattr(setup, "tailnet_base", lambda: (None, "tailscale is not installed"))


def mutating(log):
    return [e for e in log if e[:2] in (["config", "set"], ["config", "unset"], ["webhook", "subscribe"],
                                         ["webhook", "remove"], ["config", "migrate"])]


def go(monkeypatch, tmp_path, capsys, scenario=None, args=("--yes",), create=None):
    shim = make_shim(tmp_path, {"webhook_enabled": True, **(scenario or {})})
    calls = fake_create(monkeypatch, result=create)
    rc, out, err = run_setup(monkeypatch, shim, capsys, args=args)
    return SimpleNamespace(rc=rc, out=out, err=err, log=read_log(shim), shim=shim, calls=calls)


def next_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("next:")]


def summary_row(out, label):
    for ln in out.splitlines():
        if ln.strip().startswith(label + " "):
            return " ".join(ln.split())
    return ""


# ================================================================ 1. origin platform

def test_origin_detect_platforms_skips_the_non_chat_ones():
    platforms = {"webhook": {"enabled": True}, "msgraph_webhook": {"enabled": True}, "api_server": {"enabled": True},
                 "local": {"enabled": True}, "telegram": {"enabled": True}, "discord": {"enabled": False},
                 "slack": {"token": "x"}, "Bad Name": {"enabled": True}, "signal": "not a dict"}
    run = lambda argv: SimpleNamespace(returncode=0, stdout=json.dumps(platforms), stderr="")
    names, _ = setup.detect_platforms(run)
    assert names == ["slack", "telegram"]
    assert setup.NON_CHAT_PLATFORMS == ("webhook", "msgraph_webhook", "api_server", "local")


def test_origin_the_only_chat_platform_is_used_and_said(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"platforms": {"webhook": {"enabled": True}, "telegram": {"enabled": True}}})
    assert r.rc == 0
    assert ("origin platform: telegram (the only chat platform configured; pass --origin-platform to choose "
            "another)") in r.out
    assert [c[2] for c in find(r.log, "webhook", "subscribe")] == ["mock-review-telegram", "mock-review-telegram-notice"]


def test_origin_explicit_webhook_is_honoured(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"platforms": {"webhook": {"enabled": True}}},
           args=("--yes", "--origin-platform", "webhook"))
    assert r.rc == 0
    assert [c[2] for c in find(r.log, "webhook", "subscribe")] == ["mock-review-webhook", "mock-review-webhook-notice"]
    assert not find(r.log, "webhook", "list")          # webhook is chosen: there is no stale route to look for


def test_origin_explicit_wins_over_several_detected(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"platforms": CHAT_PLATFORMS}, args=("--yes", "--origin-platform", "discord"))
    assert r.rc == 0
    assert [c[2] for c in find(r.log, "webhook", "subscribe")] == ["mock-review-discord", "mock-review-discord-notice"]


@pytest.mark.parametrize("flag", ["--yes", "--dry-run"])
def test_origin_several_platforms_non_interactive_stops_before_any_change(monkeypatch, tmp_path, capsys, flag):
    r = go(monkeypatch, tmp_path, capsys, {"platforms": CHAT_PLATFORMS, "webhook_enabled": False}, args=(flag,))
    assert r.rc == 2
    assert "discord, telegram" in r.out and "--origin-platform" in r.out
    assert mutating(r.log) == []


@pytest.mark.parametrize("mode", ["--yes", "--dry-run", "tty"])
def test_origin_no_chat_platform_stops_before_any_change(monkeypatch, tmp_path, capsys, mode):
    args = (mode,) if mode != "tty" else ()
    if mode == "tty":
        tty(monkeypatch)
    r = go(monkeypatch, tmp_path, capsys, {"platforms": {"webhook": {"enabled": True}}, "webhook_enabled": False},
           args=args)
    assert r.rc == 2
    assert ("no chat platform found in the Hermes config (platforms.*), so setup cannot tell where to wake you. "
            "Re-run with the platform you review on: hermes mockreview setup --origin-platform telegram "
            "(repeatable)") in r.out
    assert "which platforms do you review on" not in r.out
    assert mutating(r.log) == []
    assert r.calls == {}
    assert "setup summary" not in r.out


def test_origin_platforms_config_unreadable_is_the_same_stop(monkeypatch, tmp_path, capsys):
    # `hermes config get --json platforms` fails when nothing is configured
    r = go(monkeypatch, tmp_path, capsys, {"platforms": None})
    assert r.rc == 2 and "--origin-platform" in r.out and mutating(r.log) == []


def test_origin_several_interactive_asks_once_and_honours_the_answer(monkeypatch, tmp_path, capsys):
    tty(monkeypatch, "telegram")
    r = go(monkeypatch, tmp_path, capsys, {"platforms": CHAT_PLATFORMS}, args=("--skip-validate",))
    assert r.rc == 0
    assert "discord, telegram" in r.out                     # the question lists them
    assert [c[2] for c in find(r.log, "webhook", "subscribe")] == ["mock-review-telegram", "mock-review-telegram-notice"]


def test_origin_several_interactive_enter_means_all(monkeypatch, tmp_path, capsys):
    tty(monkeypatch, "")
    r = go(monkeypatch, tmp_path, capsys, {"platforms": CHAT_PLATFORMS}, args=("--skip-validate",))
    assert r.rc == 0
    assert sorted(c[2] for c in find(r.log, "webhook", "subscribe")) == [
        "mock-review-discord", "mock-review-discord-notice", "mock-review-telegram", "mock-review-telegram-notice"]


def test_origin_interactive_unlisted_name_asks_again_once_then_accepts(monkeypatch, tmp_path, capsys):
    tty(monkeypatch, "slack", " Discord ,telegram ")
    r = go(monkeypatch, tmp_path, capsys, {"platforms": CHAT_PLATFORMS}, args=("--skip-validate",))
    assert r.rc == 0
    assert "slack" in r.out                                  # named as not listed
    assert len(find(r.log, "webhook", "subscribe")) == 4


def test_origin_interactive_two_bad_answers_stop_with_no_change(monkeypatch, tmp_path, capsys):
    tty(monkeypatch, "slack", "matrix")
    r = go(monkeypatch, tmp_path, capsys, {"platforms": CHAT_PLATFORMS, "webhook_enabled": False},
           args=("--skip-validate",))
    assert r.rc == 2
    assert "--origin-platform" in r.out
    assert mutating(r.log) == []


def test_origin_an_explicit_name_that_cannot_be_a_platform_stops(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--origin-platform", "Not A Platform"))
    assert r.rc == 2 and "Not A Platform" in r.out and mutating(r.log) == []


# ---------------------------------------------------------------- the stale self-delivering route

STALE_LIST = (
    "\n  Webhook subscriptions:\n\n"
    "  ◆ mock-review-webhook\n    Wakes the origin chat when a mock-review round is submitted.\n"
    "    URL:     http://localhost:8644/webhooks/mock-review-webhook\n    Profile: default\n    Events:  (all)\n"
    "    Deliver: webhook\n\n"
    "  ◆ mock-review-webhook-notice\n    Instant ack and stalled-ping messages.\n"
    "    URL:     http://localhost:8644/webhooks/mock-review-webhook-notice\n    Profile: default\n"
    "    Events:  (all)\n    Deliver: webhook (direct — no agent)\n\n"
    "  ◆ mock-review-telegram\n    URL:     http://localhost:8644/webhooks/mock-review-telegram\n"
    "    Deliver: telegram\n\n"
    "  ◆ other-webhook\n    Deliver: webhook\n")
TELEGRAM = {"platforms": {"webhook": {"enabled": True}, "telegram": {"enabled": True}}}


def test_route_stale_self_delivering_routes_are_removed_after_a_yes(monkeypatch, tmp_path, capsys):
    tty(monkeypatch, "y")
    r = go(monkeypatch, tmp_path, capsys, {**TELEGRAM, "webhook_list": STALE_LIST}, args=("--skip-validate",))
    assert r.rc == 0
    assert ("remove the self-delivering route(s) mock-review-webhook, mock-review-webhook-notice left by an earlier "
            "setup? [Y/n]") in r.out
    assert [c[2] for c in find(r.log, "webhook", "remove")] == ["mock-review-webhook", "mock-review-webhook-notice"]
    assert find(r.log, "webhook", "list")


def test_route_stale_routes_stay_after_a_no(monkeypatch, tmp_path, capsys):
    tty(monkeypatch, "n")
    r = go(monkeypatch, tmp_path, capsys, {**TELEGRAM, "webhook_list": STALE_LIST}, args=("--skip-validate",))
    assert r.rc == 0 and not find(r.log, "webhook", "remove")


@pytest.mark.parametrize("flag", ["--yes", "--dry-run"])
def test_route_stale_routes_are_only_printed_when_unattended(monkeypatch, tmp_path, capsys, flag):
    r = go(monkeypatch, tmp_path, capsys, {**TELEGRAM, "webhook_list": STALE_LIST}, args=(flag,))
    assert r.rc == 0
    assert not find(r.log, "webhook", "remove")
    assert "hermes webhook remove mock-review-webhook\n" in r.out
    assert "hermes webhook remove mock-review-webhook-notice\n" in r.out
    assert "other-webhook" not in r.out and "hermes webhook remove mock-review-telegram" not in r.out


def test_route_stale_lookup_uses_the_route_prefix(monkeypatch, tmp_path, capsys):
    listing = "  ◆ custom-webhook\n    Deliver: webhook\n  ◆ mock-review-webhook\n    Deliver: webhook\n"
    r = go(monkeypatch, tmp_path, capsys, {**TELEGRAM, "webhook_list": listing},
           args=("--yes", "--webhook-route", "custom"))
    assert "hermes webhook remove custom-webhook" in r.out
    assert "hermes webhook remove mock-review-webhook" not in r.out


@pytest.mark.parametrize("listing", ["", "garbage\x00\nDeliver: webhook", "  ◆ \n    Deliver:", "Error: boom"])
def test_route_stale_unexpected_output_is_skipped_silently(monkeypatch, tmp_path, capsys, listing):
    r = go(monkeypatch, tmp_path, capsys, {**TELEGRAM, "webhook_list": listing})
    assert r.rc == 0 and "self-delivering" not in r.out and not find(r.log, "webhook", "remove")


# ================================================================ 2. the test review and its link

def test_testreview_ok_needs_a_board_that_is_ours_and_loads_the_review(monkeypatch, tmp_path, capsys, boards):
    b = boards(ours_routes())
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--port", str(b.port)),
           create={"review_id": RID, "url": f"http://127.0.0.1:{b.port}/r/{RID}"})
    assert r.rc == 0
    assert f"test review created: {RID}" in r.out
    assert f"board url: http://127.0.0.1:{b.port}/r/{RID}" in r.out
    assert summary_row(r.out, "test-review") == "test-review ok"
    assert "/api/owner" in b.hits and f"/r/{RID}/state.json" in b.hits
    assert "notify-test" not in r.out and "wake verification" not in r.out


def test_testreview_pending_when_no_board_answers(monkeypatch, tmp_path, capsys):
    port = free_port()
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--port", str(port)))
    assert r.rc == 0
    assert summary_row(r.out, "test-review") == "test-review pending (board not running)"
    assert "board url:" not in r.out and "test review created" not in r.out
    assert "notify-test" not in r.out and "wake verification" not in r.out
    assert RID in r.out and "once the board runs" in r.out        # saved, loads later
    assert next_lines(r.out)


@pytest.mark.parametrize("routes", [
    {"/api/owner": (200, owner_json(service="something-else"))},
    {"/api/owner": (200, "<html>hello</html>")},
    {"/api/owner": (200, owner_json(home="/elsewhere/.hermes", data_root="/elsewhere/store"))},
    {"/api/owner": (200, owner_json()), f"/r/{RID}/state.json": (404, "no")},
    {"/api/owner": (200, owner_json()), f"/r/{RID}/state.json": (200, json.dumps({"id": "review-other"}))},
    {"/api/owner": (200, owner_json()), f"/r/{RID}/state.json": (200, "not json")},
], ids=["other-service", "html", "other-home", "state-404", "state-other-id", "state-not-json"])
def test_testreview_pending_when_the_board_is_not_ours_or_cannot_load_it(monkeypatch, tmp_path, capsys, boards, routes):
    b = boards(routes)
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--port", str(b.port)))
    assert r.rc == 0
    assert summary_row(r.out, "test-review") == "test-review pending (board not running)"
    assert "board url:" not in r.out


def _make_setup_review(title, brief, mode="local"):
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    f = d / "decisions.json"
    f.write_text(json.dumps({"blocks": [{"type": "text", "title": "t", "text": "x"}]}), encoding="utf-8")
    return api.create(title=title, origin=None, brief=brief, mode=mode, decisions=str(f), wake=False)["review_id"]


def _statuses():
    return {r["review_id"]: r["status"] for r in api.list_reviews(include_closed=True)}


def test_testreview_earlier_setup_test_reviews_are_closed_and_nothing_else(monkeypatch, tmp_path, capsys):
    mine = _make_setup_review(setup.TEST_TITLE, "my own notes")
    other = _make_setup_review("another review", setup.SETUP_BRIEF)
    earlier_a = _make_setup_review(setup.TEST_TITLE, setup.SETUP_BRIEF)
    earlier_b = _make_setup_review(setup.TEST_TITLE, setup.SETUP_BRIEF)
    done = _make_setup_review(setup.TEST_TITLE, setup.SETUP_BRIEF)
    api.close(done)
    r = go(monkeypatch, tmp_path, capsys)
    assert r.rc == 0
    assert "will change: close 2 earlier setup test review(s)" in r.out
    st = _statuses()
    assert st[earlier_a] == st[earlier_b] == "closed"
    assert st[mine] == "open" and st[other] == "open"
    assert r.out.index("will change: close 2 earlier") < r.out.index("test review")


def test_testreview_dry_run_announces_the_close_only(monkeypatch, tmp_path, capsys):
    earlier = _make_setup_review(setup.TEST_TITLE, setup.SETUP_BRIEF)
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--dry-run", "--origin-platform", "telegram"))
    assert r.rc == 0
    assert "will change: close 1 earlier setup test review(s)" in r.out
    assert _statuses()[earlier] == "open"


def test_testreview_skip_leaves_earlier_reviews_alone(monkeypatch, tmp_path, capsys):
    earlier = _make_setup_review(setup.TEST_TITLE, setup.SETUP_BRIEF)
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--skip-test-review"))
    assert r.rc == 0 and "earlier setup test review" not in r.out
    assert _statuses()[earlier] == "open"


def test_testreview_no_earlier_reviews_means_no_close_line(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys)
    assert "earlier setup test review" not in r.out


# ================================================================ 3. port identity

def test_port_board_identity_names_the_four_cases(boards):
    ours = boards({"/api/owner": (200, owner_json())})
    assert setup.board_identity(ours.port)[0] == "ours"
    kind, info = setup.board_identity(ours.port)
    assert info["pid"] == 4242

    by_data_root = boards({"/api/owner": (200, owner_json(home="/elsewhere/.hermes"))})
    assert setup.board_identity(by_data_root.port)[0] == "ours"        # the store matches even if the home spelling does not

    other = boards({"/api/owner": (200, owner_json(home="/elsewhere/.hermes", data_root="/elsewhere/store"))})
    kind, info = setup.board_identity(other.port)
    assert kind == "other-mock-review" and info["hermes_home"] == "/elsewhere/.hermes"

    for routes in ({"/api/owner": (200, "<html>")}, {}, {"/api/owner": (500, "boom")},
                   {"/api/owner": (200, owner_json(service="x"))}, {"/api/owner": (200, "[1, 2]")},
                   {"/api/owner": (200, json.dumps({"service": "mock-review"}))}):
        assert setup.board_identity(boards(routes).port)[0] == "foreign", routes

    assert setup.board_identity(free_port())[0] == "none"


def test_port_board_identity_never_raises_and_is_quick(boards):
    slow = boards({"/api/owner": (200, owner_json(), 3.0)})
    t0 = time.time()
    kind, _ = setup.board_identity(slow.port, timeout=0.3)
    assert kind == "foreign" and time.time() - t0 < 2.5               # something is listening, it just does not answer
    assert setup.board_identity(0)[0] == "none"
    assert setup.board_identity(70000)[0] == "none"


def test_port_slow_refusal_is_none_not_foreign(monkeypatch):
    # Windows refuses a closed loopback port only after ~2 s; the request timeout alone would call that "hung".
    def slow_refuse(addr, timeout=None):
        assert timeout >= 5.0
        raise ConnectionRefusedError(10061, "refused after SYN retries")
    monkeypatch.setattr(setup.socket, "create_connection", slow_refuse)
    monkeypatch.setattr(setup, "_get_json", lambda *a, **k: ("hung", None))
    assert setup.board_identity(free_port(), timeout=0.3)[0] == "none"


def test_port_ours_is_announced_with_no_warning(monkeypatch, tmp_path, capsys, boards):
    b = boards({"/api/owner": (200, owner_json(pid=31337))})
    monkeypatch.setattr(setup, "port_free", lambda port: (False, "Address already in use"))
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--port", str(b.port)))
    assert f"board: already running on http://127.0.0.1:{b.port} (this plugin's board, pid 31337)" in r.out
    assert "warning: port" not in r.out


def test_port_held_by_a_board_of_another_home_names_it_and_suggests_port(monkeypatch, tmp_path, capsys, boards):
    b = boards({"/api/owner": (200, owner_json(home="/elsewhere/.hermes", data_root="/elsewhere/store"))})
    monkeypatch.setattr(setup, "port_free", lambda port: (False, "Address already in use"))
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--port", str(b.port)))
    warn = [ln for ln in r.out.splitlines() if ln.startswith("warning:") and str(b.port) in ln]
    assert warn and "/elsewhere/.hermes" in warn[0] and "another Hermes home" in warn[0] and "--port" in warn[0]
    assert "already running" not in r.out


def test_port_foreign_listener_keeps_the_busy_warning(monkeypatch, tmp_path, capsys, boards):
    b = boards({})
    monkeypatch.setattr(setup, "port_free", lambda port: (False, "Address already in use"))
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--port", str(b.port)))
    assert f"warning: port {b.port} is busy (Address already in use)" in r.out
    assert "already running" not in r.out


def test_port_nothing_listening_and_not_free_keeps_the_busy_warning(monkeypatch, tmp_path, capsys):
    port = free_port()
    monkeypatch.setattr(setup, "port_free", lambda p: (False, "Address already in use"))
    r = go(monkeypatch, tmp_path, capsys, args=("--yes", "--port", str(port)))
    assert f"warning: port {port} is busy" in r.out


# ================================================================ 4. the next step

@pytest.mark.parametrize("text,state,manual", [
    ("✓ Gateway is running (PID: 4242)\n  (Running manually, not as a system service)\n", "running", True),
    ("✓ Gateway is running via the default-profile multiplexer\n  Manage it from the default profile\n", "running", False),
    ("✓ Gateway is running (supervised by launchd)\n", "running", False),
    ("✓ Gateway is supervised by launchd (PID 123)\n  Auto-start at login\n", "running", False),
    ("● svc\n✓ User gateway service is running\n", "running", False),
    ("⚠ registered\n✓ Detached fallback process is running (PID 9)\n", "running", False),
    ("✗ Gateway is not running\n\nTo start:\n  hermes gateway run\n", "stopped", False),
    ("✗ User gateway service is stopped\n  Run: hermes gateway start\n", "stopped", False),
    ("✗ Gateway service is not loaded\n  Run: hermes gateway start\n", "stopped", False),
    ("✗ No fallback process is running\n", "stopped", False),
    ("✗ Gateway is not running\n✓ Gateway is running (PID: 5)\n", "running", False),
    ("✗ Gateway service is not loaded\n  Note: a detached gateway process is running (PID 7)\n", "running", False),
    ("✓ Gateway service is registered with launchd\n  Detached gateway process is running (PID 7)\n", "running", False),
    ("✗ Gateway service is not loaded\n  Run: hermes gateway start\n"
     "⚠ Gateway process is running for this profile, but the service is not active\n", "running", True),
    ("✗ User gateway service is stopped\n"
     "⚠ Gateway process is running for this profile, but the service is not active\n", "running", True),
    ("", "unknown", False),
    ("something new and unexpected\n", "unknown", False),
])
def test_gateway_state_classifies_hermes_status_lines(text, state, manual):
    run = lambda argv, timeout=None: SimpleNamespace(returncode=0, stdout=text, stderr="")
    assert setup.gateway_state(run) == (state, manual)


def test_gateway_state_runs_status_with_a_timeout_and_never_raises():
    seen = []

    def run(argv, timeout=None):
        seen.append((argv, timeout))
        return SimpleNamespace(returncode=0, stdout=RUNNING, stderr="")
    setup.gateway_state(run)
    assert seen == [(["gateway", "status"], 20)]

    def nonzero(argv, timeout=None):
        return SimpleNamespace(returncode=1, stdout=RUNNING, stderr="")

    def timeout(argv, timeout=None):
        raise subprocess.TimeoutExpired(argv, 20)

    def missing(argv, timeout=None):
        raise FileNotFoundError("hermes")

    def oserror(argv, timeout=None):
        raise OSError("boom")
    for fn in (nonzero, timeout, missing, oserror):
        assert setup.gateway_state(fn) == ("unknown", False)


RESTART_RUNNING = ("the board starts with the gateway: restart it (hermes gateway restart; a gateway you started "
                   "with hermes gateway run: stop it and run it again), or run hermes mockreview serve")
START_STOPPED = ("the board starts with the gateway: start it (hermes gateway run, or hermes gateway start for the "
                 "installed service), or run hermes mockreview serve")


def test_next_board_not_running_gateway_running_says_restart(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": RUNNING}, args=("--yes", "--port", str(free_port())))
    text = "\n".join(next_lines(r.out))
    assert RESTART_RUNNING in text
    assert r.out.index("setup summary:") < r.out.index("next:")


def test_next_board_not_running_gateway_stopped_says_start_never_restart(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": STOPPED}, args=("--yes", "--port", str(free_port())))
    text = "\n".join(next_lines(r.out))
    assert START_STOPPED in text
    assert "wake-ups need the gateway" in text
    assert "restart" not in text


def test_next_board_not_running_gateway_unknown_offers_both(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": "", "gateway_rc": 1}, args=("--yes", "--port", str(free_port())))
    text = "\n".join(next_lines(r.out))
    assert "start or restart the gateway" in text and "or run hermes mockreview serve" in text


def test_next_autostart_off_board_not_running_says_serve(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": STOPPED},
           args=("--yes", "--no-autostart", "--port", str(free_port())))
    text = "\n".join(next_lines(r.out))
    assert "run hermes mockreview serve" in text and "starts with the gateway" not in text
    assert "wake-ups need the gateway" in text and "hermes gateway run" in text


def test_next_board_already_running_has_no_board_advice(monkeypatch, tmp_path, capsys, boards):
    b = boards(ours_routes())
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": RUNNING}, args=("--yes", "--port", str(b.port)),
           create={"review_id": RID, "url": f"http://127.0.0.1:{b.port}/r/{RID}"})
    text = "\n".join(next_lines(r.out))
    assert "mockreview serve" not in text and "restart it" not in text and "starts with the gateway" not in text
    assert "wake-ups need the gateway" not in text


def test_next_board_already_running_gateway_stopped_adds_the_gateway_line(monkeypatch, tmp_path, capsys, boards):
    b = boards(ours_routes())
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": STOPPED}, args=("--yes", "--port", str(b.port)),
           create={"review_id": RID, "url": f"http://127.0.0.1:{b.port}/r/{RID}"})
    text = "\n".join(next_lines(r.out))
    assert ("wake-ups need the gateway: start it with: hermes gateway run (or hermes gateway start for the "
            "installed service)") in text
    assert "mockreview serve" not in text and "restart" not in text


def test_next_running_board_needs_no_restart_for_the_port_it_answers_on(monkeypatch, tmp_path, capsys, boards):
    b = boards(ours_routes())
    # --port differs from the saved port, so setup saves it; the board already listens there
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": RUNNING}, args=("--yes", "--port", str(b.port)),
           create={"review_id": RID, "url": f"http://127.0.0.1:{b.port}/r/{RID}"})
    assert find(r.log, "config", "set", "plugins.entries.mock-review.settings.port", str(b.port))
    assert "restart the process that runs the board" not in "\n".join(next_lines(r.out))


def test_next_running_board_with_no_start_only_change_has_no_restart_note(monkeypatch, tmp_path, capsys, boards):
    b = boards(ours_routes())
    monkeypatch.setenv("MOCKREVIEW_PORT", str(b.port))          # the saved port already is the board's port
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": RUNNING}, args=("--yes",),
           create={"review_id": RID, "url": f"http://127.0.0.1:{b.port}/r/{RID}"})
    assert not find(r.log, "config", "set", "plugins.entries.mock-review.settings.port")
    assert "restart the process that runs the board" not in "\n".join(next_lines(r.out))


def test_next_lan_hosting_on_a_running_board_names_host(monkeypatch, tmp_path, capsys, boards):
    b = boards(ours_routes())
    monkeypatch.setenv("MOCKREVIEW_PORT", str(b.port))
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": RUNNING}, args=("--yes", "--hosting", "lan"),
           create={"review_id": RID, "url": f"http://127.0.0.1:{b.port}/r/{RID}"})
    text = "\n".join(next_lines(r.out))
    assert "restart the process that runs the board" in text and "host" in text


def test_next_is_printed_once_after_the_summary_and_the_old_lines_are_gone(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": RUNNING}, args=("--yes", "--port", str(free_port())))
    assert "restart your gateway to start the board" not in r.out
    assert "foreground alternative" not in r.out
    assert r.out.count("the board starts with the gateway") == 1
    assert r.out.index("setup summary:") < r.out.index("next:")
    assert len(find(r.log, "gateway", "status")) == 1


def test_next_dry_run_never_calls_gateway_status(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": RUNNING}, args=("--yes", "--dry-run"))
    assert r.rc == 0 and not find(r.log, "gateway")


def test_next_windows_runs_the_board_in_the_foreground(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(setup, "_is_windows", lambda: True)
    r = go(monkeypatch, tmp_path, capsys, {"gateway_status": RUNNING}, args=("--yes", "--port", str(free_port())))
    text = "\n".join(next_lines(r.out))
    assert "hermes mockreview serve" in text and "Windows" in text
    assert "starts with the gateway" not in text and "restart" not in text


# ================================================================ 5. folded findings

SETTING = "plugins.entries.mock-review.settings.webhook_url"
WEBHOOK_PORT_PLATFORMS = {"webhook": {"enabled": True, "extra": {"port": 18644, "host": "127.0.0.1",
                                                                  "secret": "SENTINEL-PLATFORM-SECRET"}},
                          "telegram": {"enabled": True, "token": "SENTINEL-TOKEN"}}


def test_webhookport_default_webhook_url_follows_the_configured_port(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"platforms": WEBHOOK_PORT_PLATFORMS})
    assert r.rc == 0
    assert find(r.log, "config", "set", SETTING, "http://localhost:18644/webhooks/mock-review")
    assert "will change: set webhook_url to http://localhost:18644/webhooks/mock-review" in r.out
    assert "SENTINEL" not in r.out and "SENTINEL" not in r.err
    assert re.search(r"^\s*webhook_url\s+ok", r.out, re.M)


def test_webhookport_follows_the_route_name_and_a_specific_host(monkeypatch, tmp_path, capsys):
    platforms = {"webhook": {"enabled": True, "extra": {"port": "18700", "host": "192.0.2.10"}},
                 "telegram": {"enabled": True}}
    r = go(monkeypatch, tmp_path, capsys, {"platforms": platforms}, args=("--yes", "--webhook-route", "reviews"))
    assert find(r.log, "config", "set", SETTING, "http://192.0.2.10:18700/webhooks/reviews")


def test_webhookport_dry_run_announces_only(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"platforms": WEBHOOK_PORT_PLATFORMS}, args=("--yes", "--dry-run"))
    assert "will change: set webhook_url to http://localhost:18644/webhooks/mock-review" in r.out
    assert not find(r.log, "config", "set")


def test_webhookport_no_configured_port_changes_nothing(monkeypatch, tmp_path, capsys):
    r = go(monkeypatch, tmp_path, capsys, {"platforms": {"webhook": {"enabled": True}, "telegram": {"enabled": True}}})
    assert not find(r.log, "config", "set", SETTING)
    r = go(monkeypatch, tmp_path, capsys, {"platforms": {"webhook": {"enabled": True, "extra": {"port": "nope"}},
                                                          "telegram": {"enabled": True}}})
    assert not find(r.log, "config", "set", SETTING)


def test_webhookport_explicit_webhook_url_is_kept_and_a_port_mismatch_is_warned(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_URL", "http://localhost:8644/webhooks/mock-review")
    r = go(monkeypatch, tmp_path, capsys, {"platforms": WEBHOOK_PORT_PLATFORMS})
    assert not find(r.log, "config", "set", SETTING)
    warn = [ln for ln in r.out.splitlines() if ln.startswith("warning:") and "webhook_url" in ln]
    assert warn and "8644" in warn[0] and "18644" in warn[0]
    assert "SENTINEL" not in r.out


def test_webhookport_explicit_webhook_url_on_the_right_port_is_quiet(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_URL", "http://localhost:18644/webhooks/mock-review")
    r = go(monkeypatch, tmp_path, capsys, {"platforms": WEBHOOK_PORT_PLATFORMS})
    assert not find(r.log, "config", "set", SETTING)
    assert "warning: webhook_url" not in r.out


def test_tailnet_stderr_keeps_only_the_first_line_of_at_most_120_chars():
    crash = "\n\nFatal error: The current bundleIdentifier is unknown to the registry\nstack line 1\nstack line 2\n"
    base, why = setup.tailnet_base(runner=lambda: SimpleNamespace(returncode=1, stdout="", stderr=crash))
    assert base is None
    assert why == "tailscale exited 1: Fatal error: The current bundleIdentifier is unknown to the registry"
    long = "x" * 500
    _, why = setup.tailnet_base(runner=lambda: SimpleNamespace(returncode=2, stdout="", stderr=long))
    assert why == "tailscale exited 2: " + "x" * 120


def test_manifest_install_prompts_say_to_press_enter():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    env = {e["name"]: e for e in manifest["requires_env"]}
    assert env["MOCKREVIEW_WEBHOOK_SECRET"]["description"] == "Leave empty: hermes mockreview setup generates and stores it."
    assert env["HERENOW_API_KEY"]["description"] == "Leave empty unless you use here.now hosting."
    assert all(e["optional"] is True and e["secret"] is True for e in env.values())


def test_help_lists_setup_first_and_says_who_should_run_it():
    import argparse
    p = argparse.ArgumentParser(prog="mockreview")
    cli.add_commands(p)
    names = list(p._subparsers._group_actions[0].choices)
    assert names[0] == "setup" and len(names) == 15

    class Ctx:
        cli = {}

        def get_config(self, key, default=None):
            return default

        def register_cli_command(self, **kw):
            self.cli = kw
        register_tool = register_skill = register_command = lambda self, *a, **k: None
    ctx = Ctx()
    load_plugin().register(ctx)
    assert "First time? Run: hermes mockreview setup" in ctx.cli["description"]


def test_readme_try_a_board_example_runs(tmp_path):
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Try a board without an agent" in text and "examples/demo-variants" in text
    page = ROOT / "examples" / "demo-variants" / "a-calm-editorial" / "index.html"
    assert page.is_file()
    res = api.create(title="Pricing page", origin={"platform": "local", "chat_id": "demo"}, wake=False,
                     prototype=str(page), allow_live_demo=True)
    assert res["review_id"] and res["url"]


# ================================================================ a slow or missing hermes never ends in a traceback

def hermes_fails(monkeypatch, words, exc_for, seen=None):
    """Make the real subprocess call raise `exc_for(cmd, timeout)` whenever the hermes argv starts with `words`."""
    real = subprocess.run

    def fake(cmd, *a, **kw):
        argv = list(cmd)
        if seen is not None:
            seen.append((argv, kw.get("timeout")))
        if any(argv[i:i + len(words)] == words for i in range(len(argv))):
            raise exc_for(cmd, kw.get("timeout"))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", fake)


def test_timeout_version_check_waits_120_s_then_says_so_and_changes_nothing(monkeypatch, tmp_path, capsys):
    seen = []
    hermes_fails(monkeypatch, ["--version"], lambda cmd, t: subprocess.TimeoutExpired(cmd, t), seen)
    r = go(monkeypatch, tmp_path, capsys)
    assert r.rc == 2
    assert ("hermes --version did not answer within 120 s (on the first run in a Hermes home it checks for updates); "
            "re-run setup, it is safe to repeat") in r.out
    assert [t for argv, t in seen if "--version" in argv] == [120]
    assert mutating(r.log) == [] and "Traceback" not in r.out + r.err


def test_timeout_missing_hermes_binary_is_one_line_and_changes_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_HERMES_BIN", "/nowhere/at/all/hermes-x")
    hermes_fails(monkeypatch, ["--version"], lambda cmd, t: FileNotFoundError(2, "No such file or directory", cmd[0]))
    r = go(monkeypatch, tmp_path, capsys)
    assert r.rc == 2
    assert "cannot run hermes (hermes-x): install Hermes or set MOCKREVIEW_HERMES_BIN" in r.out
    assert "/nowhere" not in r.out + r.err
    assert mutating(r.log) == [] and "Traceback" not in r.out + r.err


def test_timeout_mid_run_names_two_words_and_never_the_secret(monkeypatch, tmp_path, capsys):
    hermes_fails(monkeypatch, ["webhook", "subscribe"], lambda cmd, t: subprocess.TimeoutExpired(cmd, t))
    r = go(monkeypatch, tmp_path, capsys)
    secret = shim_secret(r.shim)
    assert r.rc == 1 and secret
    assert "hermes webhook subscribe did not answer within 30 s" in r.out
    assert "steps before it were applied" in r.out and "safe to re-run" in r.out
    assert secret not in r.out + r.err and "--secret" not in r.out + r.err
    assert "Traceback" not in r.out + r.err


def test_timeout_mid_run_hermes_that_cannot_start_is_one_line_too(monkeypatch, tmp_path, capsys):
    hermes_fails(monkeypatch, ["webhook", "subscribe"], lambda cmd, t: PermissionError(13, "Permission denied", cmd[0]))
    r = go(monkeypatch, tmp_path, capsys)
    secret = shim_secret(r.shim)
    assert r.rc == 1 and secret
    assert "cannot run hermes webhook subscribe" in r.out and "safe to re-run" in r.out
    assert secret not in r.out + r.err and "Traceback" not in r.out + r.err


def test_timeout_read_only_lookups_still_degrade_quietly(monkeypatch, tmp_path, capsys):
    hermes_fails(monkeypatch, ["webhook", "list"], lambda cmd, t: subprocess.TimeoutExpired(cmd, t))
    r = go(monkeypatch, tmp_path, capsys, {"platforms": {"telegram": {"enabled": True}}})
    assert r.rc == 0 and "Traceback" not in r.out + r.err
