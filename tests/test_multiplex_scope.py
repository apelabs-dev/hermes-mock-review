"""The board inside a multiplexing gateway reads the owning profile's secrets.

Hermes installs no secret scope around the webhook adapter's connect, which is where the plugin's factory starts the
board, so every context the board captures there carries none. Each scenario runs against a small fake
`agent.secret_scope` (default suite) and against the real module (skipped without Hermes; set HERMES_AGENT_SRC).
"""

import argparse
import contextlib
import contextvars
import importlib
import json
import logging
import os
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import FEEDBACK, http, load_plugin
from mockreview import autostart, config, herenow, notify, owner, secrets, server, service, store
from test_plugin import FakeCtx


REAL_GET_SECRET = secrets.get_secret   # the production wrapper, before the suite's autouse stub replaces it

OWN = {"MOCKREVIEW_WEBHOOK_SECRET": "synthetic-owner-signing", "HERENOW_API_KEY": "synthetic-owner-herenow"}
OTHER = {"MOCKREVIEW_WEBHOOK_SECRET": "synthetic-other-signing", "HERENOW_API_KEY": "synthetic-other-herenow"}
PROCESS = {"MOCKREVIEW_WEBHOOK_SECRET": "synthetic-process-signing", "HERENOW_API_KEY": "synthetic-process-herenow"}
ORIGIN = {"platform": "telegram", "chat_id": "test-chat", "thread_id": "topic:alpha"}


# ---------------------------------------------------------------- fake agent.secret_scope

def fake_secret_scope() -> types.ModuleType:
    """The semantics the plugin relies on, mirrored from agent/secret_scope.py: a process-wide multiplex flag, a
    context-local scope, get_secret that fails closed under multiplexing with no scope and never reads os.environ
    under multiplexing with one."""
    mod = types.ModuleType("agent.secret_scope")
    state = {"active": False}
    bound = contextvars.ContextVar("fake_secret_scope", default=None)

    class UnscopedSecretError(RuntimeError):
        pass

    def set_multiplex_active(active):
        state["active"] = bool(active)

    def is_multiplex_active():
        return state["active"]

    def set_secret_scope(mapping, *, profile_home=None):
        return bound.set(None if mapping is None else (mapping, str(profile_home) if profile_home else None))

    def reset_secret_scope(token):
        bound.reset(token)

    def current_secret_scope():
        b = bound.get()
        return b[0] if b is not None else None

    def current_secret_scope_home():
        b = bound.get()
        return b[1] if b is not None else None

    def build_profile_secret_scope(home):
        out = {}
        try:
            text = (Path(home) / ".env").read_text(encoding="utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                out[key.strip()] = value.strip()
        return out

    def get_secret(name, default=None):
        b = bound.get()
        if b is not None:
            value = b[0].get(name)
            if value is not None:
                return value
            return default if state["active"] else os.environ.get(name, default)
        if state["active"]:
            raise UnscopedSecretError(f"get_secret({name!r}) called with no profile secret scope active")
        return os.environ.get(name, default)

    for fn in (set_multiplex_active, is_multiplex_active, set_secret_scope, reset_secret_scope, current_secret_scope,
               current_secret_scope_home, build_profile_secret_scope, get_secret):
        setattr(mod, fn.__name__, fn)
    mod.UnscopedSecretError = UnscopedSecretError
    return mod


@pytest.fixture(params=["fake", "real"])
def scope(request, monkeypatch):
    """agent.secret_scope (fake or real), with the production secrets.get_secret restored."""
    if request.param == "fake":
        mod = fake_secret_scope()
        pkg = types.ModuleType("agent")
        pkg.__path__ = []
        pkg.secret_scope = mod
        monkeypatch.setitem(sys.modules, "agent", pkg)
        monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    else:
        source = os.environ.get("HERMES_AGENT_SRC")
        if source:
            monkeypatch.syspath_prepend(source)
        try:
            mod = importlib.import_module("agent.secret_scope")
        except ImportError:
            pytest.skip("real agent.secret_scope unavailable; run under Hermes or with HERMES_AGENT_SRC")
    monkeypatch.setattr(secrets, "get_secret", REAL_GET_SECRET)
    return mod


@contextlib.contextmanager
def multiplexing(scope):
    """The gateway's set_multiplex_active(True) (load_gateway_config_for_runner). The real one also pins the
    process home; False releases that pin."""
    scope.set_multiplex_active(True)
    try:
        yield
    finally:
        scope.set_multiplex_active(False)


def write_env(home: Path, values) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def wait_for(predicate, what="condition"):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------- the board, started like the gateway does

class Recorder:
    """notify.post and the here.now Site Data poll, without any network: who was called, with which secret, from
    which thread, and which secret scope that thread had installed."""

    def __init__(self, scope, monkeypatch):
        self.scope = scope
        self.posted = []
        self.polls = []
        monkeypatch.setattr(notify, "post", self.post)
        self._urlopen = herenow.urllib.request.urlopen
        monkeypatch.setattr(herenow.urllib.request, "urlopen", self.urlopen)

    def _installed(self):
        return self.scope.current_secret_scope() if self.scope is not None else None

    def post(self, url, payload, key, delivery_id, **kwargs):
        self.posted.append({"review": payload["review_id"], "key": key, "thread": threading.current_thread().name,
                            "scope": self._installed()})
        return True, "synthetic transport"

    def urlopen(self, request, **kwargs):
        if getattr(request, "full_url", "").startswith("https://here.now/api/"):
            self.polls.append({"auth": request.get_header("Authorization"),
                               "thread": threading.current_thread().name, "scope": self._installed()})

            class Reply:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def read(self, *args):
                    return b'{"records": []}'

            return Reply()
        return self._urlopen(request, **kwargs)


@pytest.fixture
def board(monkeypatch, variants, request):
    """Start the board through the plugin's registered webhook factory (autostart on, port 0) in the caller's
    current context, exactly as the webhook adapter's connect does. Returns a namespace with helpers."""
    home = config.hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("MOCKREVIEW_HOME", raising=False)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_URL", "https://hooks.example.test/webhooks/mock-review")
    monkeypatch.setenv("MOCKREVIEW_RETRY_SECONDS", "0")
    for k, v in PROCESS.items():   # process environment values that must never reach a multiplexed board
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(autostart.config, "port", lambda: 0)
    specs = [store.parse_variant_spec(v) for v in variants]
    plugin = load_plugin()
    autostart.stop_running()

    def cleanup():
        autostart.stop_running()
        owner.mark_owner_process(False)

    request.addfinalizer(cleanup)
    ns = types.SimpleNamespace(home=home, base="")

    def start(worker_tick=None):
        if worker_tick is not None:
            base_worker = server.Worker

            class FastWorker(base_worker):
                def __init__(self):
                    super().__init__(tick=worker_tick)

            monkeypatch.setattr(server, "Worker", FastWorker)
        ctx = FakeCtx({"autostart": True})
        plugin.register(ctx)
        [(platform, factory)] = ctx.platform_handlers
        assert platform == "webhook"
        assert factory(None, object()) == autostart.STARTED
        ns.base = "http://127.0.0.1:" + str(autostart.running().port)
        return ns.base

    def review(mode="local"):
        return store.create_review("Scope review", "", ORIGIN, mode, specs)

    def owner_info():
        code, _, body = http("GET", ns.base + "/api/owner")
        return code, (json.loads(body) if code == 200 else body)

    def submit(r):
        code, _, body = http("POST", ns.base + "/r/" + r["id"] + "/api/submit",
                             {"round": 1, "token": r["token"], "feedback": FEEDBACK})
        assert code == 200, body
        return json.loads(body)

    ns.start, ns.review, ns.owner_info, ns.submit = start, review, owner_info, submit
    ns.data_root = str(home / "workspace" / "mock-reviews")
    return ns


# ---------------------------------------------------------------- reproduction

def test_owner_api_under_multiplexing_without_ambient_scope(scope, board):
    """The release blocker: /api/owner answered 500 (UnscopedSecretError) on a multiplexing gateway."""
    write_env(board.home, OWN)
    with multiplexing(scope):
        board.start()
        code, info = board.owner_info()
        assert code == 200, info
        assert info["wake"]["ready"] is True and info["wake"]["secret"] == "set"
        assert info["data_root"] == board.data_root


# ---------------------------------------------------------------- request threads and the Worker

def test_request_threads_sign_with_the_owning_secret(scope, board, monkeypatch):
    write_env(board.home, OWN)
    rec = Recorder(scope, monkeypatch)
    with multiplexing(scope):
        one = board.review()
        many = [board.review() for _ in range(6)]
        board.start()
        assert board.submit(one)["status"] == "notified"
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(board.submit, many))
            owners = list(pool.map(lambda _: board.owner_info(), range(8)))
    assert all(r["status"] == "notified" for r in results)
    assert all(code == 200 and info["wake"]["secret"] == "set" for code, info in owners)
    assert len(rec.posted) == 7 and {p["key"] for p in rec.posted} == {OWN["MOCKREVIEW_WEBHOOK_SECRET"]}
    assert all(p["scope"] is not None for p in rec.posted)


def test_worker_retries_and_polls_with_the_owning_secrets(scope, board, monkeypatch):
    write_env(board.home, OWN)
    rec = Recorder(scope, monkeypatch)
    with multiplexing(scope):
        pending = board.review()
        service.submit(pending["id"], 1, FEEDBACK, pending["token"], do_notify=False)
        public = board.review("herenow")
        public["herenow"]["slug"] = "synthetic-scope-site"
        store.save(public["id"], public)
        board.start(worker_tick=0.02)
        wait_for(lambda: rec.polls and any(p["review"] == pending["id"] for p in rec.posted), "retry and poll")
    sent = [p for p in rec.posted if p["review"] == pending["id"]]
    assert sent and all(p["thread"] == "mockreview-worker" for p in sent)
    assert {p["key"] for p in sent} == {OWN["MOCKREVIEW_WEBHOOK_SECRET"]}
    assert all(p["thread"] == "mockreview-worker" for p in rec.polls)
    assert {p["auth"] for p in rec.polls} == {"Bearer " + OWN["HERENOW_API_KEY"]}


# ---------------------------------------------------------------- rotation without a restart

def test_rotated_secret_is_used_on_the_next_request_without_restart(scope, board, monkeypatch):
    write_env(board.home, {"HERENOW_API_KEY": OWN["HERENOW_API_KEY"]})   # signing secret absent at start
    rec = Recorder(scope, monkeypatch)
    with multiplexing(scope):
        first, second = board.review(), board.review()
        board.start()
        code, info = board.owner_info()
        assert code == 200 and info["wake"]["secret"] == "not-set" and info["wake"]["ready"] is False
        # absent -> present (what `mockreview setup` does), no restart
        write_env(board.home, {**OWN, "MOCKREVIEW_WEBHOOK_SECRET": "synthetic-rotated-one"})
        code, info = board.owner_info()
        assert code == 200 and info["wake"]["secret"] == "set" and info["wake"]["ready"] is True
        board.submit(first)
        # rotated to a new value (a different length keeps the real .env memo's fstat key distinct on any fs)
        write_env(board.home, {**OWN, "MOCKREVIEW_WEBHOOK_SECRET": "synthetic-rotated-second-value"})
        board.submit(second)
    assert [p["key"] for p in rec.posted] == ["synthetic-rotated-one", "synthetic-rotated-second-value"]


# ---------------------------------------------------------------- never another profile, never os.environ

def test_a_routed_profile_scope_in_the_factory_caller_is_never_used(scope, board, monkeypatch):
    """A factory fired during another profile's turn (rewire_plugin_handlers) captures that profile's scope."""
    write_env(board.home, OWN)
    other_home = board.home / "profiles" / "other"
    write_env(other_home, OTHER)
    rec = Recorder(scope, monkeypatch)
    with multiplexing(scope):
        pending = board.review()
        service.submit(pending["id"], 1, FEEDBACK, pending["token"], do_notify=False)
        live = board.review()
        token = scope.set_secret_scope(scope.build_profile_secret_scope(other_home), profile_home=str(other_home))
        home_override = None
        try:
            try:   # the real gateway also routes HERMES_HOME for the turn; the board must not follow it
                import hermes_constants
                home_override = (hermes_constants, hermes_constants.set_hermes_home_override(str(other_home)))
            except ImportError:
                pass
            board.start(worker_tick=0.02)
        finally:
            if home_override is not None:
                home_override[0].reset_hermes_home_override(home_override[1])
            scope.reset_secret_scope(token)
        code, info = board.owner_info()
        assert code == 200 and info["wake"]["secret"] == "set" and info["data_root"] == board.data_root
        board.submit(live)
        wait_for(lambda: any(p["review"] == pending["id"] for p in rec.posted), "worker retry")
    assert {p["key"] for p in rec.posted} == {OWN["MOCKREVIEW_WEBHOOK_SECRET"]}
    assert {p["thread"] for p in rec.posted} >= {"mockreview-worker"}


def test_owning_env_without_the_key_is_not_set_and_ignores_os_environ(scope, board, monkeypatch):
    write_env(board.home, {"HERENOW_API_KEY": OWN["HERENOW_API_KEY"]})
    rec = Recorder(scope, monkeypatch)
    assert os.environ["MOCKREVIEW_WEBHOOK_SECRET"] == PROCESS["MOCKREVIEW_WEBHOOK_SECRET"]
    with multiplexing(scope):
        r = board.review()
        board.start()
        code, info = board.owner_info()
        assert code == 200, info
        assert info["wake"]["ready"] is False and info["wake"]["secret"] == "not-set"
        assert board.submit(r)["status"] == "pending_notify"
    assert rec.posted == []


def test_scope_build_failure_installs_nothing_and_logs_no_value(scope, board, monkeypatch, caplog):
    """Static routes keep working; the secret read fails closed as Hermes intends (no fallback of our own)."""
    write_env(board.home, OWN)

    def broken(home):
        raise RuntimeError("synthetic-owner-signing leaked into an error message")

    monkeypatch.setattr(scope, "build_profile_secret_scope", broken)
    caplog.set_level(logging.WARNING)
    with multiplexing(scope):
        r = board.review()
        board.start()
        code, _, body = http("GET", board.base + "/r/" + r["id"] + "/state.json")
        assert code == 200 and json.loads(body)["id"] == r["id"]
        code, _, _ = http("GET", board.base + "/r/" + r["id"] + "/board.js")
        assert code == 200
        code, _, _ = http("GET", board.base + "/api/owner")
        assert code == 500
        code, _, _ = http("GET", board.base + "/api/owner")
        assert code == 500
    assert "synthetic-owner-signing" not in caplog.text
    scoped = [rec for rec in caplog.records if rec.name == "mockreview.secrets"]
    assert len(scoped) == 1 and "RuntimeError" in scoped[0].getMessage()


# ---------------------------------------------------------------- unchanged without multiplexing

def test_multiplex_off_installs_no_scope_and_reads_os_environ(scope, board, monkeypatch):
    write_env(board.home, OWN)   # present, but a non-multiplexed Hermes reads os.environ exactly as before
    rec = Recorder(scope, monkeypatch)
    pending = board.review()
    service.submit(pending["id"], 1, FEEDBACK, pending["token"], do_notify=False)
    live = board.review()
    seen = []
    handle = server.Handler.do_GET

    def probe(self):
        seen.append(scope.current_secret_scope())
        handle(self)

    monkeypatch.setattr(server.Handler, "do_GET", probe)
    board.start(worker_tick=0.02)
    code, info = board.owner_info()
    assert code == 200 and info["wake"]["secret"] == "set"
    board.submit(live)
    wait_for(lambda: any(p["review"] == pending["id"] for p in rec.posted), "worker retry")
    assert seen == [None]
    assert {p["key"] for p in rec.posted} == {PROCESS["MOCKREVIEW_WEBHOOK_SECRET"]}
    assert all(p["scope"] is None for p in rec.posted)
    assert {p["thread"] for p in rec.posted} >= {"mockreview-worker"}


def test_foreground_serve_cli_is_unchanged(scope, board, monkeypatch):
    rec = Recorder(scope, monkeypatch)
    plugin = load_plugin()
    ctx = FakeCtx({})
    plugin.register(ctx)
    servers, bound = [], threading.Event()
    make_server = server.make_server

    def capture(*args, **kwargs):
        srv = make_server(*args, **kwargs)
        servers.append(srv)
        bound.set()
        return srv

    monkeypatch.setattr(server, "make_server", capture)
    r = board.review()
    parser = argparse.ArgumentParser()
    ctx.cli["mockreview"]["setup_fn"](parser)
    args = parser.parse_args(["serve", "--port", "0"])
    codes = []
    fg = threading.Thread(target=contextvars.copy_context().run,
                          args=(lambda: codes.append(ctx.cli["mockreview"]["handler_fn"](args)),), daemon=True)
    fg.start()
    try:
        assert bound.wait(5)
        board.base = "http://127.0.0.1:" + str(servers[0].server_address[1])
        code, info = board.owner_info()
        assert code == 200 and info["wake"]["secret"] == "set"
        board.submit(r)
    finally:
        if servers:
            servers[0].shutdown()
        fg.join(8)
    assert codes == [0]
    assert [p["key"] for p in rec.posted] == [PROCESS["MOCKREVIEW_WEBHOOK_SECRET"]]
    assert rec.posted[0]["scope"] is None


def test_without_hermes_there_is_no_scope_and_no_secret(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setattr(secrets, "get_secret", REAL_GET_SECRET)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", PROCESS["MOCKREVIEW_WEBHOOK_SECRET"])
    write_env(config.hermes_home(), OWN)
    assert secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET") == ""
    srv = server.make_server("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        code, _, body = http("GET", "http://127.0.0.1:" + str(srv.server_address[1]) + "/api/owner")
        assert code == 200 and json.loads(body)["wake"]["secret"] == "not-set"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
