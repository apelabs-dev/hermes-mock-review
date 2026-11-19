"""scripts/tempguard.py: scratch dirs with an owner file, the startup sweep, termination-signal cleanup and the
watchdog that cleans up after a harness killed with SIGKILL. The process tests use short-lived Python children and
`sleep`, never a browser or a server, and every child is reaped before the test ends.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tempguard  # noqa: E402

posix_only = pytest.mark.skipif(os.name == "nt", reason="process groups and POSIX signals")
HOURS = 3600


def _wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _old(path: Path, age: float) -> None:
    """Make the owner file look `age` seconds old."""
    stamp = time.time() - age
    os.utime(path / tempguard.OWNER_FILE, (stamp, stamp))


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _run_child(code: str, tmp_path: Path, **kw) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(code)], cwd=str(ROOT),
                            env=dict(os.environ, TMPDIR=str(tmp_path), TMP=str(tmp_path), TEMP=str(tmp_path)), **kw)


# ---------------------------------------------------------------- make_dir and sweep

def test_make_dir_lives_under_the_system_temp_dir_and_names_its_owner(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    made = Path(tempguard.make_dir("mockreview-unit-"))
    assert made.parent == tmp_path and made.name.startswith("mockreview-unit-")
    assert (made / tempguard.OWNER_FILE).read_text() == str(os.getpid())


def test_sweep_removes_an_old_dir_whose_owner_is_dead(tmp_path):
    dead = Path(tempguard.make_dir("mockreview-unit-", str(tmp_path)))
    (dead / tempguard.OWNER_FILE).write_text(str(_dead_pid()))
    (dead / "profile" / "deep").mkdir(parents=True)
    _old(dead, 5 * HOURS)
    assert tempguard.sweep("mockreview-unit-", str(tmp_path)) == [str(dead)]
    assert not dead.exists()


def test_sweep_keeps_everything_that_may_still_be_in_use(tmp_path):
    base = str(tmp_path)
    young = Path(tempguard.make_dir("mockreview-unit-", base))
    (young / tempguard.OWNER_FILE).write_text(str(_dead_pid()))
    live = Path(tempguard.make_dir("mockreview-unit-", base))        # owner is this process, which is alive
    _old(live, 50 * HOURS)
    ownerless = tmp_path / "mockreview-unit-ownerless"      # no owner file, changed an hour ago: maybe in use
    ownerless.mkdir()
    stamp = time.time() - HOURS
    os.utime(ownerless, (stamp, stamp))
    other = Path(tempguard.make_dir("mockreview-other-", base))
    (other / tempguard.OWNER_FILE).write_text(str(_dead_pid()))
    _old(other, 50 * HOURS)
    garbled = tmp_path / "mockreview-unit-garbled"
    garbled.mkdir()
    (garbled / tempguard.OWNER_FILE).write_text("not a pid")
    os.utime(garbled, (stamp, stamp))
    (tmp_path / "mockreview-unit-file").write_text("x")
    assert tempguard.sweep("mockreview-unit-", base) == []
    for kept in (young, live, ownerless, other, garbled, tmp_path / "mockreview-unit-file"):
        assert kept.exists(), kept


def test_sweep_removes_a_dir_with_no_owner_file_once_it_is_a_day_stale(tmp_path):
    old = tmp_path / "mockreview-unit-legacy"
    old.mkdir()
    (old / "profile").mkdir()
    stamp = time.time() - 30 * HOURS
    os.utime(old, (stamp, stamp))
    assert tempguard.sweep("mockreview-unit-", str(tmp_path)) == [str(old)]
    assert not old.exists()


@posix_only
def test_sweep_never_follows_a_symlink(tmp_path):
    target = Path(tempguard.make_dir("mockreview-unit-target-", str(tmp_path)))
    (target / tempguard.OWNER_FILE).write_text(str(_dead_pid()))
    _old(target, 50 * HOURS)
    link = tmp_path / "mockreview-unit-link"
    link.symlink_to(target, target_is_directory=True)
    tempguard.sweep("mockreview-unit-link", str(tmp_path))
    assert link.is_symlink() and target.exists()


def test_sweep_of_a_missing_base_is_quiet(tmp_path):
    assert tempguard.sweep("mockreview-unit-", str(tmp_path / "absent")) == []


# ---------------------------------------------------------------- termination signals

SIGNAL_CHILD = """
    import os, signal, sys
    sys.path.insert(0, "scripts")
    import tempguard
    marker = sys.argv[1]
    def first():
        raise RuntimeError("a failing cleanup must not skip the next one")
    def second():
        open(marker, "w").write("cleaned")
    tempguard.on_termination(first)
    tempguard.on_termination(second)
    print("ready", flush=True)
    os.kill(os.getpid(), getattr(signal, sys.argv[2]))
    import time; time.sleep(30)
    print("survived the signal")
"""


@posix_only
@pytest.mark.parametrize("name,codes", [("SIGTERM", (-signal.SIGTERM,)), ("SIGHUP", (-signal.SIGHUP,)),
                                        ("SIGINT", (-signal.SIGINT, 130, 1))])
def test_a_termination_signal_runs_the_cleanup_then_ends_the_process_the_usual_way(tmp_path, name, codes):
    marker = tmp_path / "marker"
    proc = _run_child(SIGNAL_CHILD.replace("sys.argv[1]", repr(str(marker))).replace("sys.argv[2]", repr(name)),
                      tmp_path, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        out, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert marker.read_text() == "cleaned"
    assert proc.returncode in codes
    assert "survived" not in out


@posix_only
def test_an_ignored_signal_stays_ignored(tmp_path):
    marker = tmp_path / "marker"
    code = SIGNAL_CHILD.replace("sys.argv[1]", repr(str(marker))).replace("sys.argv[2]", repr("SIGHUP")).replace(
        "tempguard.on_termination(first)", "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n    tempguard.on_termination(first)")
    proc = _run_child(code.replace("time.sleep(30)", "time.sleep(0.5)"), tmp_path, stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, text=True)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0 and "survived" in out and not marker.exists()


def test_unregistering_restores_the_previous_handler():
    before = signal.getsignal(signal.SIGTERM)
    unregister = tempguard.on_termination(lambda: None)
    assert signal.getsignal(signal.SIGTERM) is not before
    unregister()
    assert signal.getsignal(signal.SIGTERM) == before
    unregister()                                               # twice is fine


# ---------------------------------------------------------------- the watchdog

WATCH_CHILD = """
    import os, subprocess, sys, time
    sys.path.insert(0, "scripts")
    import tempguard
    forgotten = subprocess.Popen(["sleep", "300"], start_new_session=True)
    server = subprocess.Popen(["sleep", "300"], start_new_session=True)
    helper = subprocess.Popen(["sleep", "300"], start_new_session=False)   # same group as this process: not tracked
    scratch = tempguard.make_dir("mockreview-unit-watch-")
    kept_dir = tempguard.make_dir("mockreview-unit-watch-")
    guard = tempguard.watchdog()
    guard.track_dir(scratch)
    guard.track_dir(kept_dir)
    guard.forget_dir(kept_dir)
    guard.track_group(server.pid)
    guard.track_group(forgotten.pid)
    guard.forget_group(forgotten.pid)
    print(server.pid, forgotten.pid, scratch, kept_dir, helper.pid, flush=True)
    time.sleep(300)
"""


def _alive(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)          # reap it if it is our child
    except ChildProcessError:
        pass
    return tempguard.pid_alive(pid)


@posix_only
def test_the_watchdog_cleans_up_after_a_harness_killed_with_sigkill(tmp_path):
    proc = _run_child(WATCH_CHILD, tmp_path, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    pids = []
    try:
        server, forgotten, scratch, kept_dir, helper = proc.stdout.readline().split()
        pids = [int(server), int(forgotten), int(helper)]
        assert all(tempguard.pid_alive(p) for p in pids) and os.path.isdir(scratch)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        assert _wait_for(lambda: not os.path.exists(scratch)), "the tracked dir was not removed"
        assert _wait_for(lambda: not _alive(int(server))), "the tracked server group was not stopped"
        assert os.path.isdir(kept_dir), "a forgotten dir must be left alone"
        assert tempguard.pid_alive(int(forgotten)), "a forgotten group must be left alone"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGKILL) if pid != pids[2] else os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@posix_only
def test_the_watchdog_removes_only_dirs_named_like_a_harness_scratch_dir(tmp_path):
    mine = tmp_path / "mockreview-unit-mine"
    other = tmp_path / "precious"
    for d in (mine, other):
        d.mkdir()
        (d / "f").write_text("x")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "tempguard.py"), "--watch"], timeout=30,
                          input=("dir\t%s\ndir\t%s\ngroup\t1\ngroup\tnonsense\nbogus\n" % (mine, other)).encode())
    assert proc.returncode == 0
    assert not mine.exists() and (other / "f").exists()


@posix_only
def test_kill_group_leaves_a_pid_that_is_not_a_group_leader_alone():
    child = subprocess.Popen(["sleep", "300"])                 # shares this process's group
    try:
        tempguard.kill_group(child.pid, grace=0.2)
        time.sleep(0.2)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


@posix_only
def test_kill_group_stops_the_leader_and_its_helpers():
    child = subprocess.Popen(["sh", "-c", "sleep 300 & sleep 300 & wait"], start_new_session=True)
    time.sleep(0.3)
    tempguard.kill_group(child.pid, grace=2)
    child.wait(timeout=10)
    assert _wait_for(lambda: tempguard._group_gone(child.pid))
