"""The Chrome slot registry is machine-wide for this OS user, not local to one TMPDIR or HOME.

Non-browser and fast: these tests exercise tests/browserkit.py's file registry with short-lived processes, never a
real browser. The env override keeps its meaning (an explicit registry directory), and the defaults are checked
from subprocesses with *different* TMPDIR and HOME so an implementation that follows gettempdir() is caught.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import browserkit as bk

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

CHILD_ENV_TO_DROP = (bk.CHROME_SLOTS_ENV, bk.CHROMES_ENV, bk.CHROME_RUN_ENV, bk.CHROME_WAIT_ENV)


def child_env(tmpdir: Path, home: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_TO_DROP}
    env.update(TMPDIR=str(tmpdir), HOME=str(home), PYTHONPATH=str(TESTS), PYTHONUNBUFFERED="1")
    env.update(extra)
    return env


def run_child(code: str, tmpdir: Path, home: Path, timeout: float = 30.0, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=child_env(tmpdir, home, **extra),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


DIR_CODE = "import browserkit; print(browserkit._machine_slot_dir()); print(browserkit._slot_dir())"


def test_the_default_registry_is_the_same_under_different_tmpdir_and_home(tmp_path):
    one, two = tmp_path / "tmp-one", tmp_path / "tmp-two"
    home_one, home_two = tmp_path / "home-one", tmp_path / "home-two"
    for path in (one, two, home_one, home_two):
        path.mkdir()
    first = run_child(DIR_CODE, one, home_one)
    second = run_child(DIR_CODE, two, home_two)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    machine_one, resolved_one = first.stdout.splitlines()
    machine_two, resolved_two = second.stdout.splitlines()
    assert machine_one == machine_two == resolved_one == resolved_two
    assert not Path(resolved_one).is_relative_to(one)
    assert not Path(resolved_one).is_relative_to(two)


HOLD_CODE = """\
import time
import browserkit as bk
print("DIR " + bk._slot_dir(), flush=True)
try:
    slot = bk._acquire_chrome_slot(timeout=3.0)
except bk.BrowserCheckError:
    print("NONE", flush=True)
else:
    print("SLOT " + slot, flush=True)
    time.sleep(4.0)
    bk._release_chrome_slot(slot)
    print("DONE", flush=True)
"""


def test_five_concurrent_processes_with_distinct_tmpdirs_get_at_most_four_slots(tmp_path):
    """Five independent scratch environments race for one slot each; the shared registry admits at most four."""
    children = []
    for index in range(5):
        tmpdir, home = tmp_path / ("tmp-%d" % index), tmp_path / ("home-%d" % index)
        tmpdir.mkdir()
        home.mkdir()
        env = child_env(tmpdir, home, **{
            bk.CHROMES_ENV: "1", bk.CHROME_RUN_ENV: "child-%d" % index, bk.CHROME_WAIT_ENV: "3"})
        children.append(subprocess.Popen([sys.executable, "-c", HOLD_CODE], cwd=str(ROOT), env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         encoding="utf-8", errors="replace"))
    slots, dirs = [], set()
    try:
        for child in children:
            out, err = child.communicate(timeout=60)
            assert child.returncode == 0, err
            for line in out.splitlines():
                if line.startswith("DIR "):
                    dirs.add(line[4:])
                elif line.startswith("SLOT "):
                    slots.append(line[5:])
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
        for slot in slots:
            try:
                os.unlink(slot)
            except OSError:
                pass
    assert len(dirs) == 1, dirs
    assert len(slots) <= bk.MAX_CHROMES, slots
    assert all(Path(slot).parent == Path(next(iter(dirs))) for slot in slots)


def test_the_env_override_still_yields_independent_registries(monkeypatch, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv(bk.CHROME_SLOTS_ENV, str(first))
    monkeypatch.setenv(bk.CHROMES_ENV, "4")
    held = []
    try:
        for index in range(bk.MAX_CHROMES):
            monkeypatch.setenv(bk.CHROME_RUN_ENV, "run-%d" % index)
            held.append(bk._acquire_chrome_slot(timeout=1))
        monkeypatch.setenv(bk.CHROME_RUN_ENV, "overflow")
        with pytest.raises(bk.BrowserCheckError, match="no free Chrome slot"):
            bk._acquire_chrome_slot(timeout=0.2)
        monkeypatch.setenv(bk.CHROME_SLOTS_ENV, str(second))
        monkeypatch.setenv(bk.CHROME_RUN_ENV, "other")
        held.append(bk._acquire_chrome_slot(timeout=1))
        assert Path(held[-1]).parent == second
    finally:
        for slot in held:
            bk._release_chrome_slot(slot)
    assert list(first.iterdir()) == [] and list(second.iterdir()) == []


def _slot_path(uid: int) -> str:
    return "/tmp/mockreview-chrome-slots-%d" % uid


posix_registry = pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "getuid"),
    reason="the /tmp slot registry is POSIX-only; Windows uses %LOCALAPPDATA%",
)


def _fresh_uid(offset: int) -> int:
    return 9_200_000 + (os.getpid() * 10 + offset) % 100_000


def _make_slot_dir(uid: int) -> str:
    path = _slot_path(uid)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _fake_stat_for(monkeypatch, path: str, *, uid: int, mode: int) -> None:
    """Make `path` report this owner and mode without chown/root or touching another user's files."""
    real_lstat, real_stat = os.lstat, os.stat
    fake = type("Stat", (), {"st_mode": mode, "st_uid": uid})()

    def matches(p) -> bool:
        try:
            return os.fspath(p) == path
        except TypeError:
            return False

    def lstat(p, *a, **k):
        return fake if matches(p) else real_lstat(p, *a, **k)

    def stat_(p, *a, **k):
        return fake if matches(p) else real_stat(p, *a, **k)

    monkeypatch.setattr(bk.os, "lstat", lstat)
    monkeypatch.setattr(bk.os, "stat", stat_)


@posix_registry
def test_a_slot_directory_owned_by_another_uid_is_rejected(monkeypatch):
    uid = _fresh_uid(1)
    path = _make_slot_dir(uid)
    try:
        monkeypatch.setattr(bk.os, "getuid", lambda: uid)
        _fake_stat_for(monkeypatch, path, uid=uid + 1, mode=stat.S_IFDIR | 0o700)
        assert bk._machine_slot_dir() == os.path.join(tempfile.gettempdir(), "mockreview-chrome-slots-%d" % uid)
    finally:
        os.chmod(path, 0o700)
        os.rmdir(path)


@posix_registry
@pytest.mark.parametrize("mode", [0o777, 0o775])
def test_a_group_or_other_writable_slot_directory_is_rejected(monkeypatch, mode):
    uid = _fresh_uid(2 + mode % 2)
    path = _make_slot_dir(uid)
    try:
        monkeypatch.setattr(bk.os, "getuid", lambda: uid)
        _fake_stat_for(monkeypatch, path, uid=uid, mode=stat.S_IFDIR | mode)
        assert bk._machine_slot_dir() == os.path.join(tempfile.gettempdir(), "mockreview-chrome-slots-%d" % uid)
    finally:
        os.chmod(path, 0o700)
        os.rmdir(path)


@posix_registry
def test_a_private_slot_directory_owned_by_this_uid_is_used(monkeypatch):
    uid = _fresh_uid(4)
    path = _make_slot_dir(uid)
    try:
        monkeypatch.setattr(bk.os, "getuid", lambda: uid)
        _fake_stat_for(monkeypatch, path, uid=uid, mode=stat.S_IFDIR | 0o700)
        assert bk._machine_slot_dir() == path
    finally:
        os.chmod(path, 0o700)
        os.rmdir(path)


@posix_registry
def test_a_symlinked_slot_directory_is_rejected(monkeypatch, tmp_path):
    """A symlink is not the directory the lstat check wants, even when it points at a private directory."""
    uid = _fresh_uid(5)
    target = tmp_path / "real-registry"
    target.mkdir(mode=0o700)
    path = _slot_path(uid)
    os.symlink(str(target), path)
    try:
        monkeypatch.setattr(bk.os, "getuid", lambda: uid)
        assert bk._machine_slot_dir() == os.path.join(tempfile.gettempdir(), "mockreview-chrome-slots-%d" % uid)
        assert list(target.iterdir()) == []
    finally:
        os.unlink(path)


def denied_mkdir(times: int):
    """os.mkdir that refuses `times` calls with WinError 5's PermissionError, then creates the directory."""
    real, calls = os.mkdir, []

    def mkdir(path, *args, **kwargs):
        calls.append(path)
        if len(calls) <= times:
            raise PermissionError(13, "Access is denied", path)
        return real(path, *args, **kwargs)

    return mkdir, calls


def test_a_lock_mid_removal_on_windows_is_waited_for(monkeypatch, tmp_path):
    mkdir, calls = denied_mkdir(2)
    monkeypatch.setattr(bk, "_MKDIR_DENIED_MEANS_BUSY", True)
    monkeypatch.setattr(bk.os, "mkdir", mkdir)
    lock = bk._lock(str(tmp_path))
    assert lock == str(tmp_path / ".lock") and len(calls) == 3
    bk._unlock(lock)


def test_a_denied_lock_elsewhere_fails_at_once(monkeypatch, tmp_path):
    mkdir, calls = denied_mkdir(1)
    monkeypatch.setattr(bk, "_MKDIR_DENIED_MEANS_BUSY", False)
    monkeypatch.setattr(bk.os, "mkdir", mkdir)
    with pytest.raises(PermissionError):
        bk._lock(str(tmp_path))
    assert len(calls) == 1
