"""The foreground server (`mockreview serve` / `hermes mockreview serve`), run as a real subprocess."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from conftest import ROOT
from mockreview import server

BIN = ROOT / "bin" / "mockreview"
POSIX = os.name == "posix"


def free_port(host="127.0.0.1"):
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def ipv6_loopback():
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


def child_env(tmp_path, port):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MOCKREVIEW_", "HERMES_"))}
    env.update(HERMES_HOME=str(tmp_path / "hermes"), HERMES_KANBAN_HOME=str(tmp_path / "hermes"),
               MOCKREVIEW_HOME=str(tmp_path / "data"), MOCKREVIEW_CONFIG=str(tmp_path / "no-config.yaml"),
               MOCKREVIEW_OWNER="self", MOCKREVIEW_PORT=str(port), PYTHONUNBUFFERED="1")
    return env


@pytest.fixture
def spawn():
    """Start a child in its own process group; whatever is still running at teardown is killed (whole group)."""
    procs = []

    def run(argv, env):
        kw = {"start_new_session": True} if POSIX else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        p = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kw)
        procs.append(p)
        return p

    yield run
    for p in procs:
        if p.poll() is None:
            if POSIX:
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
        p.communicate(timeout=10)


def wait_healthy(p, url, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if p.poll() is not None:
            raise AssertionError(f"server exited early ({p.returncode}): {p.communicate()[1][-2000:]}")
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=1) as r:
                return r.status, r.read()
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server never answered /healthz")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to a child process is POSIX-only")
@pytest.mark.parametrize("sig", ["SIGINT", "SIGTERM"])
def test_foreground_serve_answers_and_stops_cleanly(spawn, tmp_path, sig):
    port = free_port()
    p = spawn([sys.executable, str(BIN), "serve", "--host", "127.0.0.1"], child_env(tmp_path, port))
    status, body = wait_healthy(p, f"http://127.0.0.1:{port}")
    assert status == 200 and b'"mock-review"' in body
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
        assert r.status == 200 and b"Mock reviews" in r.read()
    t0 = time.monotonic()
    p.send_signal(getattr(signal, sig))
    _, err = p.communicate(timeout=5)
    assert p.returncode == 0, err[-2000:]
    assert time.monotonic() - t0 < 5
    assert "Traceback" not in err and "stopped" in err
    with socket.socket() as s:                                   # nothing listens there any more
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # (as the server binds: TIME_WAIT is not a listener)
        s.bind(("127.0.0.1", port))
        s.listen(1)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to a child process is POSIX-only")
def test_ctrl_c_works_even_when_started_with_sigint_ignored(spawn, tmp_path):
    """`cmd &` in a non-interactive shell starts children with SIGINT ignored; Ctrl+C must still stop serve."""
    port = free_port()
    code = ("import runpy, signal, sys; signal.signal(signal.SIGINT, signal.SIG_IGN); "
            f"sys.argv = [{str(BIN)!r}, 'serve', '--no-worker']; runpy.run_path({str(BIN)!r}, run_name='__main__')")
    p = spawn([sys.executable, "-c", code], child_env(tmp_path, port))
    wait_healthy(p, f"http://127.0.0.1:{port}")
    p.send_signal(signal.SIGINT)
    _, err = p.communicate(timeout=5)
    assert p.returncode == 0 and "Traceback" not in err, err[-2000:]


def test_port_in_use_exits_2_with_one_clear_line(spawn, tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        p = spawn([sys.executable, str(BIN), "serve", "--host", "127.0.0.1", "--port", str(port)],
                  child_env(tmp_path, port))
        _, err = p.communicate(timeout=30)
    assert p.returncode == 2
    assert "Traceback" not in err
    msg = [ln for ln in err.splitlines() if "already in use" in ln]
    assert len(msg) == 1 and str(port) in msg[0] and "hermes mockreview owner" in msg[0]


def test_bad_host_exits_2_without_traceback(spawn, tmp_path):
    p = spawn([sys.executable, str(BIN), "serve", "--host", "10.1.2.3"], child_env(tmp_path, free_port()))
    _, err = p.communicate(timeout=30)
    assert p.returncode == 2 and "Traceback" not in err and "loopback" in err


@pytest.mark.skipif(not ipv6_loopback(), reason="no IPv6 loopback here")
def test_ipv6_loopback_really_binds(env):
    srv = server.make_server("::1", 0)
    assert srv.address_family == socket.AF_INET6
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        assert server.display_url("::1", port) == f"http://[::1]:{port}"
        with urllib.request.urlopen(f"http://[::1]:{port}/healthz", timeout=5) as r:
            assert r.status == 200
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def test_addr_in_use_covers_every_os():
    for code in (48, 98, 10048):
        assert server.addr_in_use(OSError(code, "in use"))
    assert not server.addr_in_use(OSError(13, "permission denied"))
    win = OSError(None, "in use")
    win.winerror = 10048
    assert server.addr_in_use(win)


# ---------------------------------------------------------------- signal setup on platforms without some signals

@pytest.fixture
def recorded_signals(monkeypatch):
    calls = []

    def fake_signal(sig, handler):
        calls.append(sig)
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(signal, "getsignal", lambda sig: signal.default_int_handler)
    return calls


def test_signal_setup_skips_signals_that_do_not_exist(monkeypatch, recorded_signals):
    monkeypatch.delattr(signal, "SIGTERM", raising=False)
    monkeypatch.delattr(signal, "SIGBREAK", raising=False)
    assert server.install_stop_signals(lambda *a: None) == []
    assert recorded_signals == []


def test_signal_setup_windows_handles_sigbreak(monkeypatch, recorded_signals):
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)      # Windows' Ctrl+Break
    monkeypatch.setattr(signal, "SIGTERM", 15, raising=False)
    restore = server.install_stop_signals(lambda *a: None)
    assert recorded_signals == [15, 21] and [s for s, _ in restore] == [15, 21]


def test_signal_setup_never_touches_signals_off_the_main_thread(recorded_signals):
    out = []
    t = threading.Thread(target=lambda: out.append(server.install_stop_signals(lambda *a: None)))
    t.start()
    t.join(5)
    assert out == [[]] and recorded_signals == []
