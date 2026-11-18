"""The SSE probe's exit code: a read that raises is a failed probe, even after frames arrived."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sse_probe  # noqa: E402


class _Response:
    def __init__(self, lines, error=None):
        self.lines = lines
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield from self.lines
        if self.error is not None:
            raise self.error


class _Opener:
    def __init__(self, response):
        self.response = response

    def open(self, req, timeout=None):   # noqa: A002 - mirrors urllib's signature
        return self.response


def _probe(monkeypatch, response, *args):
    monkeypatch.setattr(sse_probe, "_opener", lambda: _Opener(response))
    return sse_probe.main(["client", "--url", "https://board.example.test/events", "--heartbeat", "15", *args])


FRAMES = [b"data: one\n", b"\n", b"data: two\n", b"\n"]


def test_a_caught_read_error_fails_the_probe(monkeypatch, capsys):
    assert _probe(monkeypatch, _Response(FRAMES, TimeoutError("read timed out")), "--seconds", "0") == 1
    out = capsys.readouterr().out
    assert "RESULT ok=false" in out
    assert "RESULT detail: TimeoutError" in out


def test_a_healthy_stream_that_ended_after_its_frames_passes(monkeypatch, capsys):
    assert _probe(monkeypatch, _Response(FRAMES), "--seconds", "0") == 0
    out = capsys.readouterr().out
    assert "RESULT ok=true frames=2" in out


def test_a_stream_that_closes_early_fails(monkeypatch, capsys):
    assert _probe(monkeypatch, _Response(FRAMES[:2]), "--seconds", "30") == 1
    out = capsys.readouterr().out
    assert "RESULT ok=false" in out
    assert "stream closed after" in out
