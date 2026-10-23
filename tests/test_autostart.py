"""In-process board autostart: the gateway's webhook-connect factory (mockreview/autostart.py)."""

import logging
import socket
import threading
import time
import urllib.request

import pytest

from conftest import load_plugin
from mockreview import autostart, owner, server
from test_plugin import FakeCtx, OldFakeCtx
from test_serve import free_port


@pytest.fixture(autouse=True)
def no_board_left():
    autostart.stop_running(2)
    yield
    autostart.stop_running(2)
    owner.mark_owner_process(False)


@pytest.fixture
def plugin():
    return load_plugin()


def board_threads():
    return [t for t in threading.enumerate() if t.name == autostart.THREAD_NAME and t.is_alive()]


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # TIME_WAIT from our own requests is not a listener
        try:
            s.bind(("127.0.0.1", port))
            s.listen(1)
            return True
        except OSError:
            return False


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode()


def factory_of(ctx):
    [(platform, factory)] = ctx.platform_handlers
    assert platform == "webhook"
    return factory


@pytest.mark.parametrize("on", [True, False])
def test_register_starts_nothing(env, plugin, on):
    port = free_port()
    ctx = FakeCtx({"autostart": on, "port": port})
    plugin.register(ctx)
    assert board_threads() == [] and autostart.running() is None
    assert port_free(port)
    assert not owner.is_owner_process()


def test_register_registers_one_webhook_handler(env, plugin):
    ctx = FakeCtx()
    plugin.register(ctx)
    factory = factory_of(ctx)
    assert callable(factory) and factory.__qualname__ == "MockReview.gateway_factory"   # stable dedup key


def test_older_hermes_without_platform_handlers_still_registers(env, plugin):
    ctx = OldFakeCtx({"autostart": True})
    assert not hasattr(ctx, "register_platform_handler")
    plugin.register(ctx)
    assert len(ctx.tools) == 4 and ctx.commands and ctx.cli and ctx.skills
    assert ctx.platform_handlers == [] and board_threads() == []


def test_factory_with_autostart_off_starts_nothing(env, plugin):
    port = free_port()
    ctx = FakeCtx({"autostart": False, "port": port})
    plugin.register(ctx)
    assert factory_of(ctx)(None, object()) == autostart.DISABLED
    assert board_threads() == [] and port_free(port) and not owner.is_owner_process()


def test_factory_with_autostart_on_serves_and_is_idempotent(env, plugin, monkeypatch):
    port = free_port()
    ctx = FakeCtx({"autostart": True, "port": port})
    plugin.register(ctx)
    factory = factory_of(ctx)
    assert factory(None, object()) == autostart.STARTED
    board = autostart.running()
    assert board is not None and board.url == f"http://127.0.0.1:{port}"
    status, body = get(f"http://127.0.0.1:{port}/")
    assert status == 200 and "Mock reviews" in body
    status, body = get(f"http://127.0.0.1:{port}/healthz")
    assert status == 200 and '"ok": true' in body
    assert owner.is_owner_process()
    assert factory(None, object()) == autostart.ALREADY_RUNNING            # reconnect / second call
    ctx2 = FakeCtx({"autostart": True, "port": port})                       # forced rescan: fresh register(), new ctx
    plugin.register(ctx2)
    assert factory_of(ctx2)(None, object()) == autostart.ALREADY_RUNNING
    assert autostart.running() is board and len(board_threads()) == 1


def test_port_in_use_warns_once_and_leaves_nothing_behind(env, plugin, caplog):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        ctx = FakeCtx({"autostart": True, "port": port})
        plugin.register(ctx)
        caplog.set_level(logging.DEBUG, logger="mockreview")
        assert factory_of(ctx)(None, object()) == autostart.PORT_IN_USE        # returns, never raises
    warnings = [r for r in caplog.records if r.name == "mockreview.autostart" and r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    msg = warnings[0].getMessage()
    assert f"127.0.0.1:{port}" in msg and "hermes mockreview owner" in msg and "port" in msg
    assert board_threads() == [] and autostart.running() is None and not owner.is_owner_process()


def test_kanban_worker_is_skipped(env, plugin, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    port = free_port()
    ctx = FakeCtx({"autostart": True, "port": port})
    plugin.register(ctx)
    assert factory_of(ctx)(None, None) == autostart.SKIPPED_KANBAN
    assert board_threads() == [] and port_free(port)


def test_a_board_thread_from_another_module_copy_counts_as_running(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_AUTOSTART", "true")
    monkeypatch.setenv("MOCKREVIEW_PORT", str(free_port()))
    gate = threading.Event()
    t = threading.Thread(target=gate.wait, name=autostart.THREAD_NAME, daemon=True)
    t.start()
    try:
        assert autostart.ensure_started("test") == autostart.ALREADY_RUNNING
        assert autostart.running() is None
    finally:
        gate.set()
        t.join(5)


def test_in_process_start_touches_no_signals_and_no_logging_config(env, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("in-process code must not call this")

    monkeypatch.setattr("signal.signal", forbidden)
    monkeypatch.setattr(logging, "basicConfig", forbidden)
    monkeypatch.setenv("MOCKREVIEW_AUTOSTART", "1")
    monkeypatch.setenv("MOCKREVIEW_PORT", str(free_port()))
    assert autostart.ensure_started("test") == autostart.STARTED


def test_unexpected_error_is_logged_not_raised(env, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "make_server", boom)
    monkeypatch.setenv("MOCKREVIEW_AUTOSTART", "1")
    with caplog.at_level(logging.ERROR, logger="mockreview.autostart"):
        assert autostart.gateway_factory(None, None) == autostart.ERROR
    assert any("kaboom" in (r.exc_text or "") or r.exc_info for r in caplog.records)
    assert board_threads() == []


def test_stop_releases_the_port(env, monkeypatch):
    port = free_port()
    monkeypatch.setenv("MOCKREVIEW_AUTOSTART", "1")
    monkeypatch.setenv("MOCKREVIEW_PORT", str(port))
    assert autostart.ensure_started("test") == autostart.STARTED
    assert get(f"http://127.0.0.1:{port}/healthz")[0] == 200
    assert not port_free(port)
    t0 = time.monotonic()
    autostart.stop_running()
    assert time.monotonic() - t0 < 5
    assert port_free(port) and board_threads() == [] and not owner.is_owner_process()
    autostart.stop_running()                                            # twice is fine
    assert autostart.ensure_started("again") == autostart.STARTED      # and it can start again


def test_stop_on_a_never_started_runner_returns_promptly():
    b = autostart.BoardServer()
    t0 = time.monotonic()
    b.stop()
    b.stop()
    assert time.monotonic() - t0 < 2 and not b.running and b.url == ""


def test_board_server_stop_twice(env):
    b = autostart.BoardServer()
    b.start("127.0.0.1", 0, worker=True)
    port = b.port
    assert b.running and get(f"http://127.0.0.1:{port}/healthz")[0] == 200
    b.stop(5)
    b.stop(5)
    assert not b.running and port_free(port)
