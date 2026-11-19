"""Leak-proof temp dirs and child processes for the test harnesses (standard library only).

A harness that starts a server or a browser and makes a scratch dir for it cleans up in a `finally`, but a `finally`
does not run when a CI job or a worker timeout kills the harness with SIGTERM, SIGHUP or SIGKILL. Left behind: a server
that keeps running and scratch dirs that pile up. This module closes each of those holes:

  * `on_termination(callback)` runs the callback on SIGTERM, SIGHUP and SIGINT, restores the previous handler and
    re-delivers the signal, so the process still ends with the conventional status (128 + signal number to a shell).
    Keep the `finally`; the callback is the same cleanup and must be safe to run twice.
  * `watchdog()` is a small helper process that holds the read end of a pipe whose write end only the harness has. When
    the harness dies by any cause, SIGKILL included, the pipe closes and the helper kills the process groups and removes
    the dirs it was told about. macOS has no parent-death signal; this works the same on Linux, macOS and Windows.
  * `make_dir(prefix)` makes a scratch dir under the system temp dir with an owner file naming this process, and
    `sweep(prefix)` removes old dirs of that prefix whose owner is gone. That covers a harness that was killed before
    its watchdog started, or on a host where the watchdog could not run.

A dir whose owner pid is alive is never swept, and a dir without an owner file (left by a version that wrote none) is
swept only after a full day without a change, so the sweep errs toward leaving a dir behind rather than removing one in
use.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

OWNER_FILE = ".owner-pid"
SWEEP_MIN_AGE = 3 * 3600          # a dir younger than this is never swept, whatever its owner
SWEEP_OWNERLESS_AGE = 24 * 3600   # a dir with no owner file (made before this module existed) is swept past this age
WATCH_PREFIX = "mockreview-"      # the watchdog removes only dirs named like this, whatever it is told
TERM_GRACE = 3.0


def pid_alive(pid: int) -> bool:
    """Whether process `pid` still exists, without signalling it (Windows included: os.kill would terminate it)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x00100000, False, pid)      # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5                    # ERROR_ACCESS_DENIED: someone's live process
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102   # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- scratch dirs

def make_dir(prefix: str, base: Optional[str] = None) -> str:
    """A new scratch dir under `base` (default: the system temp dir) holding an owner file with this process's pid."""
    path = tempfile.mkdtemp(prefix=prefix, dir=base or tempfile.gettempdir())
    try:
        with open(os.path.join(path, OWNER_FILE), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def _owner(path: str) -> Optional[int]:
    try:
        with open(os.path.join(path, OWNER_FILE), encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def sweep(prefix: str, base: Optional[str] = None, min_age: float = SWEEP_MIN_AGE,
          now: Optional[float] = None, ownerless_age: float = SWEEP_OWNERLESS_AGE) -> List[str]:
    """Remove the dirs `prefix*` under `base` that are older than `min_age` seconds and whose owner process is gone.

    A dir with no readable owner file is removed only when it was last changed more than `ownerless_age` ago.

    Returns the removed paths. Best effort: anything unreadable or already removed is skipped, never raised.
    """
    base = base or tempfile.gettempdir()
    now = time.time() if now is None else now
    removed: List[str] = []
    try:
        names = os.listdir(base)
    except OSError:
        return removed
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(base, name)
        try:
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            owner = _owner(path)
            if owner is None:
                if now - os.stat(path).st_mtime < ownerless_age:
                    continue
            elif pid_alive(owner) or now - os.stat(os.path.join(path, OWNER_FILE)).st_mtime < min_age:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed.append(path)
    return removed


# ---------------------------------------------------------------- termination signals

_lock = threading.Lock()
_callbacks: List[Callable[[], None]] = []
_previous: Dict[int, Any] = {}
_firing = False


def _signals() -> List[int]:
    return [getattr(signal, n) for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n)]


def _handle(signum: int, frame: Any) -> None:
    global _firing
    if _firing:
        return                                    # a second signal while cleaning up must not interrupt the cleanup
    _firing = True
    try:
        for cb in list(_callbacks):
            try:
                cb()
            except Exception:                     # one failing cleanup must not skip the others
                pass
    finally:
        _firing = False
    prev = _previous.get(signum, signal.SIG_DFL)
    _restore()
    if callable(prev):
        prev(signum, frame)                       # e.g. SIGINT -> KeyboardInterrupt, as if we had never been here
    elif os.name == "nt":
        raise SystemExit(128 + signum)
    else:
        os.kill(os.getpid(), signum)              # default action: end by the signal, the conventional status


def _install() -> None:
    if _previous or threading.current_thread() is not threading.main_thread():
        return
    for sig in _signals():
        try:
            prev = signal.getsignal(sig)
            if prev == signal.SIG_IGN:
                continue                          # the launcher chose to ignore it (nohup): keep that
            signal.signal(sig, _handle)
            _previous[sig] = prev
        except (OSError, ValueError):
            pass


def _restore() -> None:
    for sig, prev in list(_previous.items()):
        try:
            signal.signal(sig, prev if prev is not None else signal.SIG_DFL)
        except (OSError, ValueError):
            pass
    _previous.clear()


def on_termination(callback: Callable[[], None]) -> Callable[[], None]:
    """Run `callback` when this process gets SIGTERM, SIGHUP or SIGINT, then end the way the signal would have.

    Returns a function that unregisters it (call it from the `finally`). Outside the main thread nothing is installed.
    """
    with _lock:
        _callbacks.append(callback)
        _install()

    def unregister() -> None:
        with _lock:
            if callback in _callbacks:
                _callbacks.remove(callback)
            if not _callbacks:
                _restore()
    return unregister


# ---------------------------------------------------------------- the watchdog

def _group_gone(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def kill_group(pid: int, grace: float = TERM_GRACE) -> None:
    """Terminate process group `pid` (a group this harness started with start_new_session), then force it."""
    if pid <= 1:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        return
    try:
        if os.getpgid(pid) != pid:
            return                                # pid was reused by something that is not a group leader of its own
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and not _group_gone(pid):
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


class Watchdog:
    """Handle to the helper process. Every method is best effort: if the helper cannot start, the harness still runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional["subprocess.Popen[bytes]"] = None
        self._failed = False

    def _send(self, line: str) -> None:
        if "\n" in line or self._failed:
            return
        with self._lock:
            try:
                if self._proc is None:
                    kw: Dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL,
                                          "stderr": subprocess.DEVNULL}
                    if os.name == "nt":
                        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    else:
                        kw["start_new_session"] = True      # a terminal's Ctrl-C or hangup must not reach it first
                    self._proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--watch"], **kw)
                assert self._proc.stdin is not None
                self._proc.stdin.write((line + "\n").encode("utf-8"))
                self._proc.stdin.flush()
            except (OSError, ValueError, AssertionError):
                self._failed = True

    def track_dir(self, path: str) -> None:
        self._send("dir\t" + os.path.abspath(path))

    def forget_dir(self, path: str) -> None:
        self._send("forget-dir\t" + os.path.abspath(path))

    def track_group(self, pid: int) -> None:
        self._send("group\t%d" % pid)

    def forget_group(self, pid: int) -> None:
        self._send("forget-group\t%d" % pid)

    def close(self) -> None:
        """Release the helper; it then cleans up whatever is still tracked and exits."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=TERM_GRACE + 5)
            except subprocess.TimeoutExpired:
                pass


_watchdog: Optional[Watchdog] = None


def watchdog() -> Watchdog:
    """This process's shared watchdog, started on first use."""
    global _watchdog
    with _lock:
        if _watchdog is None:
            _watchdog = Watchdog()
        return _watchdog


def _watch() -> int:
    """The helper's main loop: read tracking lines until the harness's end of the pipe closes, then clean up."""
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, signal.SIG_IGN)
    dirs: List[str] = []
    groups: List[int] = []
    for raw in sys.stdin.buffer:
        verb, _, arg = raw.decode("utf-8", "replace").rstrip("\n").partition("\t")
        try:
            if verb == "dir" and arg not in dirs:
                dirs.append(arg)
            elif verb == "forget-dir" and arg in dirs:
                dirs.remove(arg)
            elif verb == "group" and int(arg) not in groups:
                groups.append(int(arg))
            elif verb == "forget-group" and int(arg) in groups:
                groups.remove(int(arg))
        except ValueError:
            pass
    for pid in groups:
        try:
            kill_group(pid)
        except (OSError, subprocess.SubprocessError):
            pass
    for path in dirs:
        if os.path.basename(path.rstrip("/\\")).startswith(WATCH_PREFIX) and os.path.isabs(path):
            shutil.rmtree(path, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(_watch() if sys.argv[1:] == ["--watch"] else 2)
