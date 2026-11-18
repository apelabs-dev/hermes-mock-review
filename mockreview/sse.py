"""Server-sent events plumbing, with no board dependency.

The threads and live-update layer is the consumer: it calls `respond()` once for a new stream, then writes frames
through a `Stream` from its own loop. `Stream.tick()` emits a comment heartbeat after `sse_heartbeat_seconds` of
silence, so a proxy (Tailscale Funnel, Caddy, nginx, Cloudflare) never sees an idle stream and drops it.

Frames go through `Output`, the one write path of a response. Its `close()` fences the socket, so a stream closed from
another thread (a sign-out, a revoked share link) writes nothing more, not even a frame that was already assembled or
queued: a plain "is it closed?" check before the write leaves exactly that window open.

Wire format (https://html.spec.whatwg.org/multipage/server-sent-events.html): one or more `field: value` lines, one
blank line to end the frame. A line starting with `:` is a comment; this module uses it for heartbeats.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import settings

STREAM_TYPE = "text/event-stream; charset=utf-8"
# Any write failure means this stream is over: a dead peer (BrokenPipeError, ConnectionResetError,
# ConnectionAbortedError), a stalled one (socket.timeout) or a file object that is already closed.
WRITE_ERRORS = (OSError, ValueError)
# How long `Output.close()` waits for a frame that is already inside the write path. The socket is shut down and its
# descriptor closed first, so a blocked write returns at once instead of holding the close for its own timeout.
CLOSE_WAIT = 2.0


def respond(handler: Any) -> None:
    """Start an SSE response: 200, the streaming headers, no Content-Length, and the connection closes at the end."""
    handler.send_response(200)
    handler.send_header("Content-Type", STREAM_TYPE)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    handler.wfile.flush()


class Output:
    """The actual socket write of one SSE response, fenced against closure.

    A stream is closed from another thread (a sign-out, a regenerated share link, a policy change), while the writing
    thread may be anywhere between "this frame is ready" and "the bytes are out". A plain `closed` flag checked before
    the write cannot close that window: the frame can pass the check, wait, and still be written after the close
    returned. So every frame goes through `emit()`, which holds this output's own lock across the check and the write,
    and `close()` sets the flag, shuts the socket down, closes its descriptor (which wakes a write blocked on a
    stalled peer) and only then waits for that lock. Once `close()` returns, no further frame can begin to go out, and
    a frame that was queued or held writes nothing. The lock is per connection and held for one frame, never across a
    queue or a database call, so a slow client cannot hold up a publisher or another stream.

    `close()` is also safe to call concurrently, and every caller returns only once the fence is done: the first
    caller through `_close_lock` performs it, the others wait there for `fenced`. A closer that finds a close already
    in progress therefore cannot skip the transport fence.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self.closed = False
        self.fenced = False
        self._handler: Any = None

    def bind(self, handler: Any) -> None:
        """Attach this output to the response handler carrying it, so `_emit` finds it (idempotent)."""
        self._handler = handler
        handler.sse_output = self
        if self.closed:
            _shutdown(handler)

    def close(self) -> None:
        """End this output. After this returns, no frame can start writing on it, and any write in flight is done.

        One caller performs the fence (flag, shutdown, bounded wait for the write lock); concurrent callers wait on
        the per-connection close lock until it has completed. No registry or publisher lock is taken here.
        """
        with self._close_lock:
            if self.fenced:
                return
            self.closed = True
            handler = self._handler
            if handler is not None:
                _shutdown(handler)
            if self._lock.acquire(timeout=CLOSE_WAIT):
                self._lock.release()
            self.fenced = True

    def emit(self, frame: str) -> bool:
        with self._lock:
            if self.closed or self._handler is None:
                return False
            try:
                self._handler.wfile.write(frame.encode("utf-8"))
                self._handler.wfile.flush()
            except WRITE_ERRORS:
                self.closed = True
                return False
            return True


def _shutdown(handler: Any) -> None:
    """Fence the connection: wake a write parked on a stalled peer, then close the descriptor under it.

    `shutdown()` alone wakes a blocked send on POSIX but not on Windows, where a send already inside the Winsock
    provider keeps going until `closesocket` cancels it, and it never releases the descriptor. A plain `socket.close()`
    does not close the descriptor either while `http.server`'s `rfile` still holds a `makefile()` reference
    (`socket._io_refs`): CPython only marks the socket closed and defers the real close. So detach the descriptor and
    close it through a throwaway wrapper. `detach()` first invalidates this socket object, so a later write or
    shutdown on it fails locally with EBADF instead of touching a number the system may already have reused; a later
    `close()` is only a no-op, and the wrapper is the one remaining owner that releases the descriptor.
    """
    sock = getattr(handler, "connection", None)
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        fd = sock.detach()
    except (AttributeError, OSError, ValueError):
        return
    if fd < 0:
        return
    try:
        socket.socket(fileno=fd).close()
    except OSError:
        pass


def _emit(handler: Any, frame: str) -> bool:
    """Write one assembled frame; False when the peer is gone. A handler carrying an `Output` is written through it,
    so a closed stream refuses the frame even when it was built (or dequeued, or held) before the close."""
    output = getattr(handler, "sse_output", None)
    if isinstance(output, Output):
        return output.emit(frame)
    try:
        handler.wfile.write(frame.encode("utf-8"))
        handler.wfile.flush()
    except WRITE_ERRORS:
        return False
    return True


def write(handler: Any, data: Any, event: Optional[str] = None, id: Optional[Any] = None) -> bool:
    """One SSE frame (`id:`, `event:`, then one `data:` per line). False when the peer is gone."""
    lines = []
    if id is not None:
        lines.append(f"id: {id}")
    if event is not None:
        lines.append(f"event: {event}")
    text = str(data).replace("\r\n", "\n").replace("\r", "\n")
    lines.extend(f"data: {line}" for line in text.split("\n"))
    return _emit(handler, "\n".join(lines) + "\n\n")


def heartbeat(handler: Any) -> bool:
    """A comment frame, so intermediaries see traffic on a quiet stream. False when the peer is gone."""
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return _emit(handler, f": ping {stamp}\n\n")


def heartbeat_seconds() -> int:
    return int(settings.get("sse_heartbeat_seconds"))


class Stream:
    """One open SSE response; the live-update layer keeps one per client and calls `tick()` from its loop.

    `write()` and `heartbeat()` update `last_write`; `tick()` writes a heartbeat once the stream has been quiet for
    `interval` seconds and returns False once the socket is gone, so the caller can stop its loop. The interval is
    read from the setting when the stream opens; pass `interval` to override it (tests, the probe script). Pass
    `output` to write through an existing `Output` (a live subscription's), whose `close()` then stops this stream's
    writes at the socket even when they are raised from another thread; without one the stream makes its own.
    """

    def __init__(self, handler: Any, interval: Optional[float] = None, output: Optional[Output] = None):
        self.handler = handler
        self.output = output if output is not None else Output()
        self.output.bind(handler)
        self.interval = float(heartbeat_seconds() if interval is None else interval)
        self.last_write = time.monotonic()
        self.closed = False

    def write(self, data: Any, event: Optional[str] = None, id: Optional[Any] = None) -> bool:
        ok = write(self.handler, data, event=event, id=id)
        if ok:
            self.last_write = time.monotonic()
        else:
            self.closed = True
        return ok

    def heartbeat(self) -> bool:
        ok = heartbeat(self.handler)
        if ok:
            self.last_write = time.monotonic()
        else:
            self.closed = True
        return ok

    def idle_for(self) -> float:
        return time.monotonic() - self.last_write

    def tick(self) -> bool:
        """Send a heartbeat when the stream has been idle; True while it is still open."""
        if self.closed:
            return False
        if self.idle_for() < self.interval:
            return True
        return self.heartbeat()
