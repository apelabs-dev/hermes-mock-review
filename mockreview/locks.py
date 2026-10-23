"""A cross-platform exclusive file lock: fcntl.flock on POSIX, msvcrt.locking on Windows.

Both are advisory between processes that use this module. On Windows the lock is byte 0 of the lock file (locking
past the end of an empty file is allowed), retried with a short sleep until it is free.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import IO, Iterator, Union

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

RETRY_SECONDS = 0.05


def acquire(fh: IO) -> None:
    """Block until this process holds the exclusive lock on the open file."""
    if fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_EX)
        return
    while True:
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            time.sleep(RETRY_SECONDS)


def release(fh: IO) -> None:
    if fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_UN)
        return
    fh.seek(0)
    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def exclusive(path: Union[str, Path]) -> Iterator[None]:
    """Hold an exclusive lock on `path` (created if missing) for the block."""
    with open(path, "a+", encoding="utf-8") as fh:
        acquire(fh)
        try:
            yield
        finally:
            release(fh)
