"""A test-only browser harness: headless by default and headed on request (MOCKREVIEW_TEST_HEADED).

The generic launcher, DevTools WebSocket client and Cdp helper used by the board tests. Standard library only. The
browser runs in its own process group with a throwaway profile, cleaned up in a finally block and from atexit.

The Chrome cap is machine-wide for this OS user: the default slot registry lives in /tmp/mockreview-chrome-slots-<uid>
on POSIX (not under TMPDIR, which each scratch process may point elsewhere) and in %LOCALAPPDATA%\\mockreview\\chrome-slots
on Windows. It covers every process of this user on this machine. It does not cover other OS users, other machines, or a
host/sandbox that maps /tmp away per process; MOCKREVIEW_CHROME_SLOTS moves the registry on purpose (tests isolate
themselves with it).
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
import tempguard  # noqa: E402

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 32 * 1024 * 1024
BROWSER_ENV = "MOCKREVIEW_BROWSER"
HEADED_ENV = "MOCKREVIEW_TEST_HEADED"
CHROMES_ENV = "MOCKREVIEW_CHROMES"
CHROME_RUN_ENV = "MOCKREVIEW_CHROME_RUN"
CHROME_SLOTS_ENV = "MOCKREVIEW_CHROME_SLOTS"
CHROME_WAIT_ENV = "MOCKREVIEW_CHROME_WAIT"
DEFAULT_CHROMES = 3
MAX_CHROMES = 4           # the machine's hard limit, whatever MOCKREVIEW_CHROMES says
STALE_SLOT_SECONDS = 12 * 3600
BROWSER_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "microsoft-edge",
                 "microsoft-edge-stable", "msedge")
BROWSER_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


class BrowserCheckError(Exception):
    """The check ran and the page failed it, or it could not complete. The message is safe to show."""

    def __init__(self, message: str, report: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.report = report or {}


class BrowserUnavailable(BrowserCheckError):
    """No usable browser: the check cannot run (never treated as a pass)."""


class PageScriptError(BrowserCheckError):
    """The page's own script threw during the check (often a document mid-navigation), never a transport failure."""


def find_browser() -> str:
    explicit = os.environ.get(BROWSER_ENV, "").strip()
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        raise BrowserUnavailable(f"{BROWSER_ENV} is set but is not an executable file")
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in BROWSER_PATHS:
        if os.path.isfile(path):
            return path
    raise BrowserUnavailable(
        "no Chrome, Chromium or Edge was found, so the page could not be loaded the way a reviewer loads it. "
        f"Install one, or point {BROWSER_ENV} at its executable.")


# ---------------------------------------------------------------- the machine-wide Chrome budget

def chrome_budget() -> int:
    """How many Chromes this run may hold: MOCKREVIEW_CHROMES (default 3), never more than the machine's 4."""
    raw = os.environ.get(CHROMES_ENV, "").strip()
    try:
        wanted = int(raw) if raw else DEFAULT_CHROMES
    except ValueError:
        wanted = DEFAULT_CHROMES
    return max(1, min(MAX_CHROMES, wanted))


_pid_alive = tempguard.pid_alive      # kept under its old name: the slot registry and the tests use it


def _machine_slot_dir() -> str:
    """The OS-user-wide slot directory: the same for every process of this user, whatever TMPDIR or HOME says.

    POSIX: /tmp/mockreview-chrome-slots-<uid>. /tmp is used deliberately because gettempdir() follows TMPDIR, and
    independent TMPDIRs would then get independent registries and no shared ceiling. Only when /tmp cannot be
    created or written (a locked-down or sandboxed host), or the path is not a directory this uid owns with no
    group/other write bit (a pre-created registry another user could fill), does this fall back to the temp
    directory, still keyed by uid; that fallback is not machine-wide across different TMPDIRs. Windows:
    %LOCALAPPDATA%\\mockreview\\chrome-slots. The CHROME_SLOTS_ENV override comes first and is intentional (tests
    isolate with it).
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "mockreview", "chrome-slots")
    wanted = "/tmp/mockreview-chrome-slots-%d" % os.getuid()
    try:
        os.makedirs(wanted, mode=0o700, exist_ok=True)
        st = os.lstat(wanted)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
                or st.st_mode & 0o022):
            raise OSError("the slot registry is not a private directory owned by this user")
        fd, probe = tempfile.mkstemp(dir=wanted, prefix=".probe-")
        os.close(fd)
        os.unlink(probe)
        return wanted
    except OSError:
        return os.path.join(tempfile.gettempdir(), "mockreview-chrome-slots-%d" % os.getuid())


def _slot_dir() -> str:
    return os.environ.get(CHROME_SLOTS_ENV) or _machine_slot_dir()


def _slot_wait() -> float:
    try:
        return max(1.0, float(os.environ.get(CHROME_WAIT_ENV, "300")))
    except ValueError:
        return 300.0


def _lock(root: str) -> str:
    lock = os.path.join(root, ".lock")
    deadline = time.monotonic() + 30
    while True:
        try:
            os.mkdir(lock)
            return lock
        except FileExistsError:
            try:
                if time.time() - os.stat(lock).st_mtime > 30:
                    shutil.rmtree(lock, ignore_errors=True)
                    continue
            except OSError:
                continue
            if time.monotonic() >= deadline:
                raise BrowserCheckError("the Chrome slot registry stayed locked for 30s")
            time.sleep(0.02)


def _unlock(lock: str) -> None:
    try:
        os.rmdir(lock)
    except OSError:
        pass


def _live_slots(root: str) -> List[Tuple[str, str]]:
    """The slot files whose owner still runs; stale ones (dead pid, or older than the day) are removed first."""
    live = []
    for name in os.listdir(root):
        if not name.endswith(".slot"):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, encoding="utf-8") as fh:
                parts = fh.read(200).split()
            pid, run = int(parts[0]), parts[1]
            age = time.time() - os.stat(path).st_mtime
        except (OSError, ValueError, IndexError):
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        if not _pid_alive(pid) or age > STALE_SLOT_SECONDS:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        live.append((path, run))
    return live


def _acquire_chrome_slot(timeout: Optional[float] = None) -> str:
    """Take one of the machine's 4 Chrome slots, or fail when this run's budget and the machine's are full.

    Slots are files in one OS-user-wide directory (see _machine_slot_dir), shared by every process of this user, so a
    second pytest run respects them even when its TMPDIR or HOME differs. A run holds its own count
    (MOCKREVIEW_CHROMES, default 3) and the machine never exceeds 4.
    """
    budget = chrome_budget()
    run = os.environ.get(CHROME_RUN_ENV) or "pid-%d" % os.getppid()
    root = _slot_dir()
    os.makedirs(root, mode=0o700, exist_ok=True)
    deadline = time.monotonic() + (timeout if timeout is not None else _slot_wait())
    while True:
        lock = _lock(root)
        try:
            live = _live_slots(root)
            mine = sum(1 for _, owner in live if owner == run)
            if len(live) < MAX_CHROMES and mine < budget:
                fd, path = tempfile.mkstemp(dir=root, prefix="%d-%d-" % (os.getpid(), time.time_ns()),
                                            suffix=".slot")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write("%d %s %.3f\n" % (os.getpid(), run, time.time()))
                return path
        finally:
            _unlock(lock)
        if time.monotonic() >= deadline:
            raise BrowserCheckError(
                "no free Chrome slot: this run may hold %d (%s) and the machine at most %d; waited %.0fs"
                % (budget, CHROMES_ENV, MAX_CHROMES, _slot_wait()))
        time.sleep(0.1)


def _release_chrome_slot(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


class WebSocket:
    """Client side of RFC 6455 for text messages: handshake, masked frames out, fragments/ping/close in."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode("ascii"))
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self.buf:
            try:
                got = self._recv(deadline)
            except BrowserCheckError:
                self.close()
                raise
            if not got:
                self.close()
                raise BrowserCheckError("the browser's debugging port did not complete the connection in time")
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        got = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        if " 101" not in lines[0] or got.get("sec-websocket-accept") != want:
            self.close()
            raise BrowserCheckError("the browser's debugging port did not accept the connection")
        self._parts: List[bytes] = []

    def _recv(self, deadline: float) -> bool:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        self.sock.settimeout(left)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not chunk:
            raise BrowserCheckError("the browser closed its debugging connection")
        self.buf += chunk
        return True

    def send(self, text: str) -> None:
        data = text.encode("utf-8")
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.settimeout(10.0)
        self.sock.sendall(bytes(head) + mask + masked)

    def _frame(self) -> Optional[Tuple[int, bool, bytes]]:
        b = self.buf
        if len(b) < 2:
            return None
        fin, op, ln, off = bool(b[0] & 0x80), b[0] & 0x0F, b[1] & 0x7F, 2
        if ln == 126:
            if len(b) < 4:
                return None
            ln, off = struct.unpack(">H", b[2:4])[0], 4
        elif ln == 127:
            if len(b) < 10:
                return None
            ln, off = struct.unpack(">Q", b[2:10])[0], 10
        if ln > MAX_MESSAGE:
            raise BrowserCheckError("the browser sent a message that is too large")
        masked = bool(b[1] & 0x80)
        if masked:
            off += 4
        if len(b) < off + ln:
            return None
        payload = b[off:off + ln]
        if masked:
            m = b[off - 4:off]
            payload = bytes(x ^ m[i % 4] for i, x in enumerate(payload))
        self.buf = b[off + ln:]
        return op, fin, payload

    def recv(self, timeout: float) -> Optional[str]:
        """The next text message, or None when none arrives within `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while True:
            fr = self._frame()
            if fr is None:
                if not self._recv(deadline):
                    return None
                continue
            op, fin, payload = fr
            if op == 0x9:                          # ping -> pong
                self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + b"\0\0\0\0" + payload if len(payload) < 126
                                  else b"")
            elif op == 0x8:
                raise BrowserCheckError("the browser closed its debugging connection")
            elif op in (0x1, 0x2, 0x0):
                self._parts.append(payload)
                if fin:
                    msg, self._parts = b"".join(self._parts), []
                    return msg.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------- DevTools protocol

class Observer:
    """What the page did on the network after the unlock. Events only; no page content is kept."""

    def __init__(self) -> None:
        self.counting = False
        self.requests: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.total = 0
        self.failures: List[Dict[str, Any]] = []
        self.inflight: set = set()
        self.last_activity = time.monotonic()
        self.loads = 0
        self.contexts: Dict[str, Tuple[Optional[str], int]] = {}
        self.attach: List[str] = []
        # Child frames by id -> (url, session). Chrome puts a sandboxed frame in its own process (an OOPIF), which then
        # is not part of the page session's Page.getFrameTree: Page.frameNavigated on its own session is how it shows up.
        self.frames: Dict[str, Tuple[str, Optional[str]]] = {}
        self.top_frame = ""

    def reset(self) -> None:
        self.counting = True
        self.total = 0
        self.failures = []

    def sessions(self) -> List[str]:
        """Every session id this page's events have carried, in first-seen order.

        The page session appears through its top frame's own default context, so a resolver can ask every session
        that could own a frame's context without the caller having to remember the page session id."""
        seen: List[str] = []
        for sid, _ in self.contexts.values():
            if sid and sid not in seen:
                seen.append(sid)
        for _, sid in self.frames.values():
            if sid and sid not in seen:
                seen.append(sid)
        for sid in self.attach:
            if sid and sid not in seen:
                seen.append(sid)
        return seen

    @staticmethod
    def _path(url: str) -> str:
        p = urllib.parse.urlsplit(url)
        return (p.path or "/")[:160] if p.scheme in ("http", "https") else p.scheme + ":"

    def _browser_favicon(self, req: Dict[str, Any]) -> bool:
        """The browser's own favicon probe for the board page (top frame) is not the prototype's business. A favicon
        the prototype asks for is a request of its frame and counts like any other."""
        return req.get("url", "").endswith("/favicon.ico") and bool(self.top_frame) and req.get("frame") == self.top_frame

    def pending(self) -> List[str]:
        """Paths of requests that never resolved (no response finished or failed)."""
        out = []
        for key in sorted(self.inflight):
            url = (self.requests.get(key) or {}).get("url", "")
            if url and not url.startswith(("data:", "blob:", "about:")):
                out.append(self._path(url))
        return out

    def _request(self, sid: Optional[str], rid: str) -> Dict[str, Any]:
        req = self.requests.get((sid or "", rid))
        if req is None:
            req = next((v for (s, r), v in self.requests.items() if r == rid), {})
        return req

    def _settle(self, sid: Optional[str], rid: str) -> None:
        """A request is over. A sandboxed frame's own document request starts in the page session (requestWillBeSent)
        and ends in the frame's session (loadingFinished) under the same request id, so session does not match."""
        self.inflight.discard((sid or "", rid))
        for key in [k for k in self.inflight if k[1] == rid]:
            self.inflight.discard(key)

    def handle(self, msg: Dict[str, Any]) -> None:
        method, p, sid = msg.get("method"), msg.get("params") or {}, msg.get("sessionId")
        if method == "Network.requestWillBeSent":
            url = (p.get("request") or {}).get("url", "")
            self.requests[(sid or "", p.get("requestId", ""))] = {"url": url, "type": p.get("type", ""),
                                                                  "frame": p.get("frameId", "")}
            self.inflight.add((sid or "", p.get("requestId", "")))
            self.last_activity = time.monotonic()
            if self.counting and not url.startswith(("data:", "blob:", "about:")):
                self.total += 1
        elif method == "Network.responseReceived":
            resp = p.get("response") or {}
            status, url = int(resp.get("status") or 0), resp.get("url", "")
            req = self._request(sid, p.get("requestId", "")) or {"url": url, "frame": p.get("frameId", "")}
            if self.counting and status >= 400 and not url.startswith("data:") and not self._browser_favicon(req):
                self.failures.append({"kind": "http", "status": status, "type": p.get("type", ""),
                                      "path": self._path(url)})
            self.last_activity = time.monotonic()
        elif method == "Network.loadingFinished":
            self._settle(sid, p.get("requestId", ""))
            self.last_activity = time.monotonic()
        elif method == "Network.loadingFailed":
            req = self._request(sid, p.get("requestId", ""))
            self._settle(sid, p.get("requestId", ""))
            self.last_activity = time.monotonic()
            url = req.get("url", "")
            if (self.counting and not (p.get("canceled") and p.get("errorText") == "net::ERR_ABORTED")
                    and not self._browser_favicon(req) and not url.startswith(("data:", "blob:"))):
                self.failures.append({"kind": "network", "status": 0, "type": p.get("type", ""),
                                      "path": self._path(url), "error": str(p.get("errorText", ""))[:80]})
        elif method == "Page.loadEventFired":
            self.loads += 1
        elif method == "Page.frameNavigated":
            fr = p.get("frame") or {}
            if fr.get("parentId") and fr.get("id"):
                self.frames[fr["id"]] = (str(fr.get("url", "")), sid)
        elif method == "Page.frameDetached":
            if p.get("reason") == "remove":                  # "swap" is the same frame moving to its own process
                self.frames.pop(p.get("frameId", ""), None)
        elif method == "Runtime.executionContextCreated":
            ctx = p.get("context") or {}
            aux = ctx.get("auxData") or {}
            if aux.get("isDefault", True) and aux.get("frameId"):
                self.contexts[aux["frameId"]] = (sid, ctx.get("id"))
        elif method == "Runtime.executionContextsCleared":
            self.contexts = {k: v for k, v in self.contexts.items() if v[0] != sid}
        elif method == "Target.attachedToTarget":
            if p.get("sessionId"):
                self.attach.append(p["sessionId"])


class Cdp:
    def __init__(self, ws: WebSocket, observer: Observer):
        self.ws, self.obs, self._id = ws, observer, 0
        self._context_ask: Dict[Tuple[str, ...], float] = {}

    def pump(self, timeout: float) -> bool:
        raw = self.ws.recv(timeout)
        if raw is None:
            return False
        try:
            msg = json.loads(raw)
        except ValueError:
            return True
        if "id" not in msg:
            self.obs.handle(msg)
        return True

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, session: Optional[str] = None,
             timeout: float = 20.0) -> Dict[str, Any]:
        self._id += 1
        mid = self._id
        msg: Dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + timeout
        while True:
            raw = self.ws.recv(max(0.0, deadline - time.monotonic()))
            if raw is None:
                raise BrowserCheckError(f"the browser did not answer {method} in time")
            try:
                got = json.loads(raw)
            except ValueError:
                continue
            if got.get("id") == mid:
                if "error" in got:
                    raise BrowserCheckError(f"the browser refused {method}: {str(got['error'].get('message'))[:120]}")
                return got.get("result") or {}
            if "id" not in got:
                self.obs.handle(got)

    def flush(self) -> None:
        """Set up sessions the browser attached (frames in their own process) before they run.

        One refused domain must not leave the target paused at start: a timeout on Page.enable would otherwise skip
        Runtime.runIfWaitingForDebugger and the frame never loads, never creates a context and never answers again.
        That is a wedge, not a slow page, so every enable is attempted and the target is resumed in every case."""
        while self.obs.attach:
            sid = self.obs.attach.pop(0)
            for method in ("Network.enable", "Runtime.enable", "Page.enable"):
                try:
                    self.call(method, session=sid, timeout=5.0)
                except BrowserCheckError:
                    pass
            try:
                self.call("Runtime.runIfWaitingForDebugger", session=sid, timeout=5.0)
            except BrowserCheckError:
                pass                        # the target went away between attach and enable: nothing left to resume

    def reannounce_contexts(self, sessions: Optional[Sequence[Optional[str]]] = None, timeout: float = 5.0,
                            min_interval: float = 0.5) -> List[str]:
        """Make the browser report every live execution context on `sessions` again.

        A plain Runtime.enable only reports contexts on the first enable. Disable then enable does report every live
        default context (executionContextsCleared and one executionContextCreated per context), so an event map that
        lost a frame's entry (or holds an id a process swap made stale) is rebuilt without reloading the page. A
        resolver calls this when its frame has no usable context and reads Observer.contexts again; per-session-set
        throttling keeps a waiting loop from flooding the browser."""
        wanted = tuple(dict.fromkeys(sessions if sessions is not None else self.obs.sessions()))
        now = time.monotonic()
        if now - self._context_ask.get(wanted, 0.0) < min_interval:
            return []
        self._context_ask[wanted] = now
        asked: List[str] = []
        for sid in wanted:
            if not sid:
                continue
            try:
                self.call("Runtime.disable", session=sid, timeout=timeout)
                self.call("Runtime.enable", session=sid, timeout=timeout)
            except BrowserCheckError:
                continue                    # the target went away between events; its next attach reports its own
            asked.append(sid)
        return asked

    def wait(self, predicate: Callable[[], Any], deadline: float, step: float = 0.2) -> Any:
        while True:
            self.flush()
            got = predicate()
            if got:
                return got
            if time.monotonic() >= deadline:
                return None
            self.pump(min(step, max(0.01, deadline - time.monotonic())))

    def alive(self, timeout: float = 5.0) -> bool:
        """True while this connection still answers a cheap browser-level command promptly."""
        try:
            self.call("Browser.getVersion", timeout=timeout)
            return True
        except (BrowserCheckError, OSError):
            return False

    @staticmethod
    def _page_error(details: Dict[str, Any]) -> str:
        """A bounded one-line reason from Runtime.evaluate's exceptionDetails, so a failure names the throw."""
        text = " ".join(str(details.get("text") or "").split())
        exc = details.get("exception") or {}
        desc = " ".join(str(exc.get("description") or "").splitlines()[0].split())
        reason = ": ".join(part for part in (text, desc) if part)
        return (reason or "no details")[:280]

    def evaluate(self, expression: str, session: Optional[str] = None, context: Optional[int] = None) -> Any:
        params: Dict[str, Any] = {"expression": expression, "returnByValue": True}
        if context is not None:
            params["contextId"] = context
        res = self.call("Runtime.evaluate", params, session=session)
        if res.get("exceptionDetails"):
            raise PageScriptError("a page script check failed in the browser: "
                                  + self._page_error(res["exceptionDetails"]))
        return (res.get("result") or {}).get("value")


# ---------------------------------------------------------------------- launching

def headed_requested() -> bool:
    """True when MOCKREVIEW_TEST_HEADED is 1 or true, case-insensitive."""
    return os.environ.get(HEADED_ENV, "").strip().lower() in ("1", "true")


def browser_args(exe: str, profile: str, headed: bool) -> List[str]:
    """The browser argv. Pure: the caller decides headed, so a unit test can inspect both forms without a launch."""
    args = [exe]
    if headed:
        args.append("--window-position=0,25")
    else:
        args.append("--headless=new")
    args += ["--disable-gpu", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions", "--disable-dev-shm-usage", "--mute-audio", "--use-mock-keychain",
             "--password-store=basic", "--remote-debugging-port=0",
             f"--user-data-dir={profile}", "about:blank"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")
    return args


def _child_env() -> Dict[str, str]:
    bad = re.compile(r"key|token|secret|password|credential", re.I)
    return {k: v for k, v in os.environ.items() if not bad.search(k)}


def _kill_group(proc: "subprocess.Popen[Any]") -> None:
    if proc.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    elif os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)       # helpers that outlived the parent
        except OSError:
            pass


PROFILE_PREFIX = "mockreview-browser-"
CHROME_SINGLETON = "com.google.Chrome."


def _singleton_leftover(profile: str) -> Optional[str]:
    """The per-user temp dir this launch's Chrome made for its singleton, or None.

    macOS Chrome ignores TMPDIR: it puts SingletonSocket/SingletonCookie in a `com.google.Chrome.XXXXXX` dir there and
    links `<user-data-dir>/SingletonSocket` at that socket, and a browser killed at teardown never removes the dir (so
    test runs leave one behind per launch, ~1k/hour). The link names exactly the dir this launch created, so teardown
    can remove it without guessing at names or touching any other run's dir.
    """
    try:
        target = os.readlink(os.path.join(profile, "SingletonSocket"))
    except OSError:
        return None                                          # not macOS Chrome: no link, nothing to do
    if not os.path.isabs(target):
        target = os.path.join(profile, target)
    made = os.path.dirname(os.path.normpath(target))
    if not os.path.basename(made).startswith(CHROME_SINGLETON) or os.path.realpath(made) == os.path.realpath(profile):
        return None
    return made


def _listening(socket_path: str) -> bool:
    """True while some browser still accepts connections on a singleton socket (a live Chrome owns that dir)."""
    if os.name == "nt":
        return True                                          # no unix sockets: never guess, leave the dir alone
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(socket_path)
    except OSError:
        return False
    else:
        return True
    finally:
        s.close()


class Chrome:
    """Context manager: a browser with a throwaway profile, headless by default, yielding (host, port, websocket path)."""

    def __init__(self, executable: Optional[str] = None, startup: float = 20.0):
        self.executable, self.startup = executable, startup
        self.proc: Optional["subprocess.Popen[Any]"] = None
        self.profile: Optional[str] = None
        self.slot: Optional[str] = None
        self._unregister: Optional[Any] = None

    def __enter__(self) -> Tuple[str, int, str]:
        exe = self.executable or find_browser()
        self.slot = _acquire_chrome_slot()
        try:
            tempguard.sweep(PROFILE_PREFIX)           # profiles of earlier runs killed before they could clean up
            self.profile = tempguard.make_dir(PROFILE_PREFIX)
        except OSError:
            self._cleanup()
            raise
        atexit.register(self._cleanup)
        self._unregister = tempguard.on_termination(self._cleanup)
        tempguard.watchdog().track_dir(self.profile)
        args = browser_args(exe, self.profile, headed_requested())
        kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                              "env": _child_env()}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kw["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(args, **kw)
            tempguard.watchdog().track_group(self.proc.pid)     # a SIGKILLed run must not leave Chrome behind
        except OSError:
            self._cleanup()
            raise BrowserUnavailable("the browser could not be started") from None
        port_file = os.path.join(self.profile, "DevToolsActivePort")
        deadline = time.monotonic() + self.startup
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._cleanup()
                raise BrowserCheckError("the browser exited before it opened its debugging port")
            try:
                with open(port_file, encoding="utf-8") as fh:
                    lines = fh.read().split("\n")
                if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].strip():
                    return "127.0.0.1", int(lines[0]), lines[1].strip()
            except OSError:
                pass
            time.sleep(0.1)
        self._cleanup()
        raise BrowserCheckError("the browser did not open its debugging port in time")

    def _cleanup(self) -> None:
        # Read the link while the profile still exists: it names the singleton dir this launch created.
        leftover = _singleton_leftover(self.profile) if self.profile else None
        if self.proc is not None:
            _kill_group(self.proc)
            tempguard.watchdog().forget_group(self.proc.pid)
            self.proc = None
        if leftover and not _listening(os.path.join(leftover, "SingletonSocket")):
            for _ in range(3):
                shutil.rmtree(leftover, ignore_errors=True)
                if not os.path.exists(leftover):
                    break
                time.sleep(0.2)
        _release_chrome_slot(self.slot)
        self.slot = None
        if self.profile:
            for _ in range(5):
                shutil.rmtree(self.profile, ignore_errors=True)
                if not os.path.exists(self.profile):
                    break
                time.sleep(0.3)
            tempguard.watchdog().forget_dir(self.profile)
            self.profile = None
        if self._unregister is not None:
            self._unregister()
            self._unregister = None
        try:
            atexit.unregister(self._cleanup)
        except Exception:
            pass

    def __exit__(self, *exc: Any) -> None:
        self._cleanup()


class LiveBrowser:
    """A browser, its DevTools socket and the observer that handles its events: what `with_live_browser` hands bring_up()."""

    def __init__(self, chrome: Chrome, ws: WebSocket, cdp: Cdp, observer: Observer):
        self.chrome, self.ws, self.cdp, self.observer = chrome, ws, cdp, observer

    def alive(self) -> bool:
        """True while the browser process runs and its debugging connection answers a cheap command."""
        if self.chrome.proc is None or self.chrome.proc.poll() is not None:
            return False
        return self.cdp.alive()

    def kill(self) -> None:
        """Kill this launch now; teardown still reaps it. Used to prove a dead browser is replaced."""
        if self.chrome.proc is not None:
            _kill_group(self.chrome.proc)


TRANSPORT_ERRORS = ("closed its debugging connection", "exited before it opened its debugging port",
                    "did not open its debugging port in time",
                    "did not complete the connection in time", "did not accept the connection")

NEVER_REPLACED = ("the browser refused ", "no free Chrome slot ", "message that is too large")


def _never_replaced(error: BaseException) -> bool:
    """Failures a second launch cannot fix: the browser answered and refused, the run holds every slot, the message
    is over the cap, no browser is installed, or the page itself threw."""
    if isinstance(error, (PageScriptError, AssertionError, BrowserUnavailable)):
        return True
    text = str(error)
    return any(marker in text for marker in NEVER_REPLACED)


def _transport_failure(error: BaseException) -> bool:
    """A dead or unresponsive debug transport during bring-up (a wedged renderer answers Browser.getVersion but not
    Page.enable). A Runtime.evaluate timeout is the page hanging, not the transport, so the body raises it as is."""
    if _never_replaced(error):
        return False
    if isinstance(error, OSError):
        return True
    text = str(error)
    if "did not answer Runtime.evaluate in time" in text:
        return False
    if "did not answer " in text and " in time" in text:
        return True
    return any(marker in text for marker in TRANSPORT_ERRORS)


def with_live_browser(bring_up: Any, body: Any, observer_factory: Any = Observer, executable: Optional[str] = None,
                      startup: float = 20.0, attempts: int = 2) -> Any:
    """Launch a browser, run `bring_up(LiveBrowser)` under a replacement guard, then run `body(LiveBrowser, ctx)` once.

    A transport-class failure during bring-up (the launch, the connection, the alive probe or a bring-up CDP call)
    replaces the launch, at most `attempts` launches total, and prints one warning per replacement to stderr. `ctx`
    is whatever bring_up returned and may be None. The body runs outside the guard: every exception it raises -- a
    page script error, a transport timeout, an assertion -- is raised as is and the scenario is never rerun. A page
    script error, an assertion, a CDP refusal, a full slot registry, an oversize message and BrowserUnavailable are
    never replaced. `observer_factory` is called per launch.
    """
    failure: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        chrome = Chrome(executable=executable, startup=startup)
        ws: Optional[WebSocket] = None
        failure = None
        try:
            host, port, path = chrome.__enter__()
            ws = WebSocket(host, port, path)
            observer = observer_factory() if callable(observer_factory) else observer_factory
            live = LiveBrowser(chrome, ws, Cdp(ws, observer), observer)
            if not live.alive():
                failure = BrowserCheckError("the browser's debugging connection did not answer before setup")
        except (BrowserCheckError, OSError) as e:
            if not _transport_failure(e):
                raise
            failure = e
        else:
            try:
                ctx = bring_up(live)
            except (BrowserCheckError, OSError) as e:
                if _never_replaced(e) or (not _transport_failure(e) and live.alive()):
                    raise
                failure = e
            else:
                return body(live, ctx)                  # outside the guard: the scenario is never rerun
        finally:
            if ws is not None:
                ws.close()
            chrome.__exit__(None, None, None)
        if attempt == attempts:
            raise failure
        print(f"browserkit: replacing a browser that failed during bring-up ({attempt}/{attempts}): {failure}",
              file=sys.stderr, flush=True)
    raise AssertionError("with_live_browser ended without a result")       # unreachable


# ---------------------------------------------------------------------- the check
