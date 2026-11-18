"""Many threads opening a fresh data root's database at once: the switch to WAL and the migration must not
race into "database is locked"."""

import threading

from mockreview import db

ROOTS = 60
THREADS = 16


def test_threads_racing_to_open_a_fresh_data_root_all_get_a_working_connection(env, monkeypatch):
    errors = []
    for i in range(ROOTS):
        monkeypatch.setenv("MOCKREVIEW_HOME", str(env["tmp"] / "root{}".format(i)))
        barrier = threading.Barrier(THREADS)

        def work():
            try:
                barrier.wait(10)
                conn = db.connect()
                conn.execute("SELECT COUNT(*) FROM users").fetchone()
                assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            except Exception as e:
                errors.append("{}: {!r}".format(i, e))
            finally:
                db.close_all()

        threads = [threading.Thread(target=work) for _ in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not any(t.is_alive() for t in threads)
    assert errors == []
