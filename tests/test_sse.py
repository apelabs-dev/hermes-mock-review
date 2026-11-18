"""The reusable SSE plumbing the live-update layer drives: headers, frame format, and heartbeats on a quiet stream."""

import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mockreview import sse


class Wfile:
    def __init__(self):
        self.chunks = []

    def write(self, data):
        self.chunks.append(data)

    def flush(self):
        pass


class FakeHandler:
    def __init__(self):
        self.headers = []
        self.wfile = Wfile()
        self.close_connection = False
        self.code = None

    def send_response(self, code):
        self.code = code

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass


def body(handler):
    return b"".join(handler.wfile.chunks)


def test_respond_sends_the_streaming_headers():
    h = FakeHandler()
    sse.respond(h)
    got = {k.lower(): v for k, v in h.headers}
    assert h.code == 200 and h.close_connection is True
    assert got["content-type"] == "text/event-stream; charset=utf-8"
    assert got["cache-control"] == "no-store" and got["x-accel-buffering"] == "no"
    assert got["connection"] == "close" and "content-length" not in got


def test_write_frame_format():
    h = FakeHandler()
    assert sse.write(h, "one\ntwo", event="round", id=7) is True
    assert body(h) == b"id: 7\nevent: round\ndata: one\ndata: two\n\n"
    assert sse.write(h, "plain") is True and body(h).endswith(b"data: plain\n\n")


def test_heartbeat_is_a_comment_frame():
    h = FakeHandler()
    assert sse.heartbeat(h) is True
    frame = body(h).decode()
    assert frame.startswith(": ping ") and frame.endswith("\n\n") and frame.count("\n") == 2


class BrokenWfile:
    def write(self, data):
        raise BrokenPipeError("peer gone")

    def flush(self):
        pass


def test_a_closed_peer_makes_write_and_heartbeat_return_false():
    h = FakeHandler()
    h.wfile = BrokenWfile()
    assert sse.write(h, "x") is False
    assert sse.heartbeat(h) is False
    stream = sse.Stream(h, interval=0)
    assert stream.write("x") is False and stream.closed is True
    assert stream.tick() is False


def test_heartbeat_seconds_comes_from_the_setting(monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_SSE_HEARTBEAT_SECONDS", "1")
    assert sse.heartbeat_seconds() == 1


def test_a_closed_output_refuses_a_frame_that_waited_for_the_socket():
    """A closer runs on another thread: a frame assembled before it must not reach the socket afterwards."""
    h = FakeHandler()
    output = sse.Output()
    stream = sse.Stream(h, interval=0, output=output)
    assert stream.write("one") is True
    output.close()                       # what a sign-out or a re-pinned link does, from its own thread
    assert stream.write("two") is False and stream.tick() is False
    assert b"data: one" in body(h) and b"two" not in body(h)


class Events(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        sse.respond(self)
        stream = sse.Stream(self)
        for _ in range(3):
            time.sleep(1.05)
            if not stream.tick():
                return


def test_heartbeats_arrive_and_keep_the_stream_open(monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_SSE_HEARTBEAT_SECONDS", "1")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Events)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    stamps = []
    start = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/events", timeout=10) as r:
            for raw in r:
                if raw.strip() == b"":
                    stamps.append(time.monotonic())
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert len(stamps) == 3, stamps
    assert stamps[0] - start < 2.5 and stamps[-1] - start > 2.0   # past two heartbeats, inside the interval
    gaps = [b - a for a, b in zip([start] + stamps, stamps)]
    assert max(gaps) < 2.0
