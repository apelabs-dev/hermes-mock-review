"""mockreview.locks: fcntl on POSIX, msvcrt on Windows (simulated: no real Windows needed)."""

import importlib
import sys
import threading
import time
import types

import pytest

from mockreview import locks


@pytest.fixture
def fake_windows(monkeypatch):
    """Reload locks.py as Windows would see it: no fcntl, an msvcrt that records locking() calls."""
    calls = []
    busy = {"n": 2}                 # the first two LK_NBLCK attempts find the lock taken

    def locking(fd, mode, nbytes):
        calls.append((fd, mode, nbytes))
        if mode == fake.LK_NBLCK and busy["n"]:
            busy["n"] -= 1
            raise OSError(36, "Resource deadlock avoided")

    fake = types.ModuleType("msvcrt")
    fake.LK_UNLCK, fake.LK_LOCK, fake.LK_NBLCK, fake.LK_RLCK, fake.LK_NBRLCK = 0, 1, 2, 3, 4
    fake.locking = locking
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setitem(sys.modules, "fcntl", None)       # `import fcntl` -> ImportError
    mod = importlib.reload(locks)
    mod.RETRY_SECONDS = 0.001
    yield mod, fake, calls
    monkeypatch.undo()
    importlib.reload(locks)


def test_windows_uses_msvcrt_locking(fake_windows, tmp_path):
    mod, fake, calls = fake_windows
    assert mod.fcntl is None and mod.msvcrt is fake
    with mod.exclusive(tmp_path / ".lock"):
        held = list(calls)
    assert [c[1] for c in held] == [fake.LK_NBLCK] * 3          # retried until acquired, never the blocking LK_LOCK
    assert all(c[2] == 1 for c in calls)                        # one byte
    assert calls[-1][1] == fake.LK_UNLCK and len(calls) == 4
    assert len({c[0] for c in calls}) == 1                      # same fd for lock and unlock


def test_windows_unlock_seeks_to_the_locked_byte(fake_windows, tmp_path):
    mod, fake, calls = fake_windows
    p = tmp_path / ".lock"
    p.write_text("xyz", encoding="utf-8")                                          # "a+" opens at EOF: the lock must still be byte 0
    positions = []
    orig = fake.locking

    def spy(fd, mode, n):
        import os
        positions.append(os.lseek(fd, 0, 1))
        return orig(fd, mode, n)

    fake.locking = spy
    with mod.exclusive(p):
        pass
    assert positions and set(positions) == {0}


def test_posix_lock_excludes_another_holder(tmp_path):
    if locks.fcntl is None:
        pytest.skip("POSIX only")
    p = tmp_path / ".lock"
    order = []

    def other():
        with locks.exclusive(p):
            order.append("other")

    with locks.exclusive(p):
        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.2)
        order.append("first")
    t.join(5)
    assert order == ["first", "other"]
