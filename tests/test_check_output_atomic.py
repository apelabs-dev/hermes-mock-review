"""The scheduler prints from one thread per browser job: a line must never land inside another job's line.

print() writes its text and its newline as separate calls, so two jobs printing at the same moment could put one
job's line inside another's newline. On the Windows CI job that merged a summary line with the next job's
replacement line, and the merged line no longer started with either job's name.
"""

from __future__ import annotations

import threading

from test_check_runner import check


class WriteRecorder:
    """A stream stand-in that keeps every write() call, so a line can be checked for being whole."""

    def __init__(self):
        self.writes: list[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        pass


def _whole_lines(writes: list[str]) -> list[str]:
    """Every write carries one or more complete lines: no write may end in the middle of a line."""
    for write in writes:
        assert write.endswith("\n"), ("a write that does not end its line", writes)
    return "".join(writes).splitlines()


def test_each_line_is_one_write_even_when_jobs_print_at_once():
    recorder = WriteRecorder()
    threads = [threading.Thread(target=check._emit,
                                args=("[job%d] 1 tests, 0 failed, 0 skipped in 0.5s" % i,
                                      "[job%d] browserkit: replacing a browser" % i),
                                kwargs={"stream": recorder})
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = _whole_lines(recorder.writes)
    assert len(lines) == 16, lines
    assert sorted(lines) == sorted(
        "[job%d] %s" % (i, tail) for i in range(8)
        for tail in ("1 tests, 0 failed, 0 skipped in 0.5s", "browserkit: replacing a browser"))


def test_a_job_reports_its_summary_and_its_replacements_in_one_write(tmp_path):
    log = tmp_path / "01-test_mixed.log"
    log.write_text("collected\nbrowserkit: replacing a browser whose page bring-up failed (1/2): the plain job\n",
                   encoding="utf-8")
    recorder = WriteRecorder()
    res = check.Result(name="test_mixed", cmd=[], log=log, xml=tmp_path / "x.xml", wall=0.5, tests=1)
    check._report(res, stream=recorder)
    assert len(recorder.writes) == 1, recorder.writes
    assert _whole_lines(recorder.writes) == [
        "[test_mixed] 1 tests, 0 failed, 0 skipped in 0.5s",
        "[test_mixed] browserkit: replacing a browser whose page bring-up failed (1/2): the plain job",
    ]


def test_a_job_with_no_replacements_still_writes_its_summary_alone(tmp_path):
    log = tmp_path / "01-test_x.log"
    log.write_text("collected\n1 passed\n", encoding="utf-8")
    recorder = WriteRecorder()
    check._report(check.Result(name="test_x", cmd=[], log=log, xml=tmp_path / "x.xml", wall=0.4, tests=1),
                  stream=recorder)
    assert _whole_lines(recorder.writes) == ["[test_x] 1 tests, 0 failed, 0 skipped in 0.4s"]
