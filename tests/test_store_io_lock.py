"""review.json reads and replaces share a lock, so a replace never meets an open reader.

Windows refuses to replace a file another handle has open for reading (PermissionError). These tests reproduce that
rule on any OS: while a read of review.json is in progress, os.replace onto it raises PermissionError, and the
Windows retry is off, so only the lock can keep a save from failing.
"""

import os
import threading
from pathlib import Path

import pytest

from mockreview import store


@pytest.fixture
def windows_semantics(monkeypatch):
    """While a review.json read is in progress, replacing it fails the way Windows does. Returns two events: set
    `inside` when a reader is in its read, and the reader waits for `release` before it finishes."""
    readers = {"open": 0}
    guard = threading.Lock()
    inside, release = threading.Event(), threading.Event()
    release.set()
    real_read, real_replace = Path.read_text, os.replace

    def read_text(self, *args, **kwargs):
        if self.name != "review.json":
            return real_read(self, *args, **kwargs)
        with guard:
            readers["open"] += 1
        try:
            inside.set()
            release.wait(5)
            return real_read(self, *args, **kwargs)
        finally:
            with guard:
                readers["open"] -= 1

    def replace(src, dst):
        if Path(dst).name == "review.json":
            with guard:
                if readers["open"]:
                    raise PermissionError(13, "The process cannot access the file because it is being used")
        return real_replace(src, dst)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(store, "_WINDOWS", False)
    return inside, release


def _hold_a_reader(review_id, inside, release):
    release.clear()
    inside.clear()
    t = threading.Thread(target=store.load, args=(review_id,), daemon=True)
    t.start()
    assert inside.wait(5), "the reader never started"
    return t


def test_a_save_waits_for_an_open_reader_instead_of_failing(review, windows_semantics):
    inside, release = windows_semantics
    rid = review["id"]
    data = store.load(rid)
    data["title"] = "Saved while read"
    reader = _hold_a_reader(rid, inside, release)
    errors, done = [], threading.Event()

    def save():
        try:
            store.save(rid, data)
        except Exception as e:  # noqa: BLE001 - the test reports whatever escaped
            errors.append(e)
        finally:
            done.set()

    writer = threading.Thread(target=save, daemon=True)
    writer.start()
    assert not done.wait(0.3), "the save went ahead while a reader had review.json open"
    release.set()
    reader.join(5)
    writer.join(5)
    assert done.is_set() and errors == []
    assert store.load(rid)["title"] == "Saved while read"


def test_without_the_lock_the_same_save_fails(review, windows_semantics, monkeypatch):
    # Control: proves the simulation bites, so the test above passes because of the lock and not by luck.
    inside, release = windows_semantics
    rid = review["id"]
    data = store.load(rid)
    monkeypatch.setattr(store, "io_lock", lambda d: _NoLock())
    reader = _hold_a_reader(rid, inside, release)
    try:
        with pytest.raises(PermissionError):
            store.save(rid, data)
    finally:
        release.set()
        reader.join(5)


def test_concurrent_loads_and_saves_never_fail(review, windows_semantics):
    rid = review["id"]
    errors = []

    def reader():
        for _ in range(60):
            try:
                store.load(rid)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    def writer():
        for i in range(30):
            try:
                with store.locked(rid):
                    data = store.load(rid)
                    data["title"] = "round {}".format(i)
                    store.save(rid, data)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(3)] + [threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert store.load(rid)["title"] == "round 29"


class _NoLock:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
