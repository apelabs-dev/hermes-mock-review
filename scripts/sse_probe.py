#!/usr/bin/env python3
"""Reproduce the SSE-through-a-TLS-proxy evidence quoted in docs/public-hosting.md. Standard library only.

    python3 scripts/sse_probe.py serve --port 18902 [--seconds 600] [--heartbeat 15]
    python3 scripts/sse_probe.py client --url https://board.example.ts.net:10000/events --seconds 300

`serve` runs an HTTP server whose `/events` is a real SSE stream using `mockreview.sse` (an initial data frame, then
comment heartbeats from `Stream.tick()`), so a mapping through Funnel or any TLS proxy exercises the same code the
board uses. `client` prints one line per frame received with the monotonic delta since the previous frame, then

    RESULT ok=<bool> frames=<n> max_gap=<s> seconds=<s>

and exits non-zero when a read raises, no frame arrives within 3x the heartbeat interval, a gap exceeds that, or the
stream closes before `--seconds` (`--seconds 0` reads until the stream closes). `https://` uses
`ssl.create_default_context()` with default certificate verification.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mockreview import settings, sse  # noqa: E402


class _Events(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # noqa: A002 - keep the probe's output clean
        pass

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/events":
            body = b'{"ok": true}\n'
            self.send_response(200 if self.path.split("?", 1)[0] == "/healthz" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        sse.respond(self)
        stream = sse.Stream(self, self.server.heartbeat)   # type: ignore[attr-defined]
        if not stream.write(json.dumps({"event": "probe", "at": time.time()}), event="hello"):
            return
        deadline = None if self.server.seconds <= 0 else time.monotonic() + self.server.seconds   # type: ignore[attr-defined]
        while deadline is None or time.monotonic() < deadline:
            time.sleep(min(0.25, stream.interval))
            if not stream.tick():
                return


def serve(port: int, seconds: float, heartbeat: float) -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Events)
    httpd.daemon_threads = True
    httpd.heartbeat = heartbeat          # type: ignore[attr-defined]
    httpd.seconds = seconds              # type: ignore[attr-defined]
    print(f"listening on http://127.0.0.1:{port}/events (heartbeat {heartbeat:g}s)", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


def _opener():
    # Default verification: a Funnel certificate is a public Let's Encrypt one.
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def client(url: str, seconds: float, heartbeat: float) -> int:
    start = time.monotonic()
    last = start
    frames = 0
    max_gap = 0.0
    pending = []
    error = ""
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"})
        with _opener().open(req, timeout=max(10.0, heartbeat * 3 + 5)) as resp:
            for raw in resp:
                now = time.monotonic()
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.endswith("\r"):
                    line = line[:-1]
                if line:
                    pending.append(line)
                    continue
                frames += 1
                gap = now - last
                max_gap = max(max_gap, gap)
                last = now
                print(f"frame {frames} +{gap:.3f}s {' | '.join(pending) or '(empty frame)'}", flush=True)
                pending = []
                if seconds > 0 and now - start >= seconds:
                    break
    except Exception as e:  # a closed or refused stream is a failed probe, not a traceback
        error = f"{type(e).__name__}: {e}"
    took = time.monotonic() - start
    ok = not error
    if frames == 0:
        ok = False
        error = error or "no frame arrived"
    if max_gap > heartbeat * 3:
        ok = False
        error = error or f"gap {max_gap:.1f}s exceeds 3x the {heartbeat:g}s heartbeat"
    if ok and took + 1.0 < seconds:
        ok = False
        error = error or f"stream closed after {took:.1f}s"
    if error:
        print(f"RESULT detail: {error}", flush=True)
    print(f"RESULT ok={str(ok).lower()} frames={frames} max_gap={max_gap:.3f} seconds={took:.1f}", flush=True)
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sse_probe", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the SSE probe server")
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--seconds", type=float, default=0, help="stop after this long (0: until killed)")
    s.add_argument("--heartbeat", type=float, default=None, help="default: sse_heartbeat_seconds")
    c = sub.add_parser("client", help="read an SSE stream and measure its gaps")
    c.add_argument("--url", required=True)
    c.add_argument("--seconds", type=float, default=300)
    c.add_argument("--heartbeat", type=float, default=None, help="default: sse_heartbeat_seconds")
    a = p.parse_args(argv)
    heartbeat = float(settings.get("sse_heartbeat_seconds") if a.heartbeat is None else a.heartbeat)
    if heartbeat <= 0:
        p.error("--heartbeat must be positive")
    if a.cmd == "serve":
        return serve(a.port, a.seconds, heartbeat)
    return client(a.url, a.seconds, heartbeat)


if __name__ == "__main__":
    raise SystemExit(main())
