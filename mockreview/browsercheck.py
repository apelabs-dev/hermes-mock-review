"""Load a published, password-gated board in a real browser and count what fails.

Why a browser: the here.now unlock cookie is SameSite=Lax. The board shows a prototype in an iframe with
sandbox="allow-scripts", an opaque origin, so every request that frame makes for its own CSS, scripts, images and fonts
is cross-site and carries no cookie: the gate answers 401. A urllib or curl request that sends the cookie by hand sees
200 for the same URLs and proves nothing. This check unlocks the site the way a reviewer does (type the password, submit
the form), lets the board build its production iframe and counts every 4xx/5xx response and failed request after the
unlock. It refuses a frame that is missing, that is not exactly sandbox="allow-scripts", or that has stylesheets but
none applied. It never adds allow-same-origin.

How: it launches the installed Chrome, Chromium or Edge headless with a throwaway profile and drives it over the
DevTools protocol on a loopback port, with a minimal WebSocket client written here (standard library only). Without a
browser the check cannot run and says so (BrowserUnavailable); it never passes silently.

Secrets: the password is typed through Input.insertText, never put in argv, a URL, the environment, a log or an error;
every message is scrubbed of it. The browser runs in its own process group, killed (and its profile deleted) in a
finally block and from atexit; at most one browser runs at a time.
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
import struct
import subprocess
import tempfile
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_TIMEOUT = 60.0
MAX_MESSAGE = 32 * 1024 * 1024
BROWSER_ENV = "MOCKREVIEW_BROWSER"
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

# Page-side helpers. Constants so a test double can recognise exactly what production asks.
JS_HAS_PASSWORD = "!!document.querySelector('input[type=password]')"
JS_FOCUS_PASSWORD = ("(function(){var i=document.querySelector('input[type=password]');if(!i)return false;"
                     "i.focus();i.value='';return true})()")
JS_SUBMIT = ("(function(){var i=document.querySelector('input[type=password]');var f=i&&i.form;"
             "if(!f)return false;f.submit();return true})()")
JS_FRAMES = ("JSON.stringify(Array.prototype.map.call(document.querySelectorAll('iframe'),function(f){"
             "return {src:f.getAttribute('src')||'',sandbox:f.getAttribute('sandbox')}}))")
JS_STYLES = ("(function(){var r=0,s=0;try{var ss=document.styleSheets;s=ss.length;for(var i=0;i<ss.length;i++){"
             "try{r+=ss[i].cssRules.length}catch(e){}}}catch(e){}"
             "var l=document.querySelectorAll('link[rel~=stylesheet]'),lf=0;"
             "for(var j=0;j<l.length;j++){if(!l[j].sheet)lf++}"
             "return JSON.stringify({sheets:s,rules:r,styleEls:document.querySelectorAll('style').length,"
             "links:l.length,linksFailed:lf,ready:document.readyState,body:!!document.body})})()")


class BrowserCheckError(Exception):
    """The check ran and the board failed it, or it could not complete. The message is safe to show."""

    def __init__(self, message: str, report: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.report = report or {}


class BrowserUnavailable(BrowserCheckError):
    """No usable browser: the check cannot run (never treated as a pass)."""


# ---------------------------------------------------------------------- finding a browser

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
        "no Chrome, Chromium or Edge was found, so the board could not be loaded the way a reviewer loads it. "
        f"Install one, or point {BROWSER_ENV} at its executable.")


# ---------------------------------------------------------------------- a minimal WebSocket client

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
        """Set up sessions the browser attached (frames in their own process) before they run."""
        while self.obs.attach:
            sid = self.obs.attach.pop(0)
            for method in ("Network.enable", "Runtime.enable", "Page.enable", "Runtime.runIfWaitingForDebugger"):
                try:
                    self.call(method, session=sid, timeout=5.0)
                except BrowserCheckError:
                    break

    def wait(self, predicate: Callable[[], Any], deadline: float, step: float = 0.2) -> Any:
        while True:
            self.flush()
            got = predicate()
            if got:
                return got
            if time.monotonic() >= deadline:
                return None
            self.pump(min(step, max(0.01, deadline - time.monotonic())))

    def evaluate(self, expression: str, session: Optional[str] = None, context: Optional[int] = None) -> Any:
        params: Dict[str, Any] = {"expression": expression, "returnByValue": True}
        if context is not None:
            params["contextId"] = context
        res = self.call("Runtime.evaluate", params, session=session)
        if res.get("exceptionDetails"):
            raise BrowserCheckError("a page script check failed in the browser")
        return (res.get("result") or {}).get("value")


# ---------------------------------------------------------------------- launching

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


class Chrome:
    """Context manager: a headless browser with a throwaway profile, yielding (host, port, websocket path)."""

    def __init__(self, executable: Optional[str] = None, startup: float = 20.0):
        self.executable, self.startup = executable, startup
        self.proc: Optional["subprocess.Popen[Any]"] = None
        self.profile: Optional[str] = None

    def __enter__(self) -> Tuple[str, int, str]:
        exe = self.executable or find_browser()
        self.profile = tempfile.mkdtemp(prefix="mockreview-browser-")
        atexit.register(self._cleanup)
        args = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-dev-shm-usage", "--mute-audio", "--use-mock-keychain",
                "--password-store=basic", "--remote-debugging-port=0",
                f"--user-data-dir={self.profile}", "about:blank"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")
        kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                              "env": _child_env()}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kw["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(args, **kw)
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
        if self.proc is not None:
            _kill_group(self.proc)
            self.proc = None
        if self.profile:
            for _ in range(5):
                shutil.rmtree(self.profile, ignore_errors=True)
                if not os.path.exists(self.profile):
                    break
                time.sleep(0.3)
            self.profile = None
        try:
            atexit.unregister(self._cleanup)
        except Exception:
            pass

    def __exit__(self, *exc: Any) -> None:
        self._cleanup()


# ---------------------------------------------------------------------- the check

def _scrub(text: str, password: str) -> str:
    for s in sorted({password, *[w for w in re.split(r"\s+", password) if len(w) >= 4]}, key=len, reverse=True):
        if s:
            text = text.replace(s, "***")
    return text


def judge(report: Dict[str, Any]) -> List[str]:
    """What is wrong with a finished load, one line each; empty means the board passed."""
    out: List[str] = []
    frame = report.get("frame")
    if not frame:
        out.append("the board never showed the prototype frame")
    else:
        tokens = set((frame.get("sandbox") or "").split())
        if tokens != {"allow-scripts"}:
            out.append("the prototype frame is not sandbox=\"allow-scripts\" exactly "
                       f"(found {sorted(tokens) or 'no sandbox'})")
    failed = report.get("failed") or []
    if failed:
        shown = ", ".join(f"{f['status'] or f.get('error') or 'failed'} {f['type'] or 'request'} {f['path']}"
                          for f in failed[:6])
        out.append(f"{len(failed)} subresource request(s) failed (HTTP 4xx/5xx or network error): {shown}"
                   + (", ..." if len(failed) > 6 else ""))
    pending = report.get("pending") or []
    if pending:
        out.append(f"{len(pending)} request(s) had not finished when the wait ended: " + ", ".join(pending[:6])
                   + (", ..." if len(pending) > 6 else ""))
    st = report.get("styles")
    if st:
        if st.get("linksFailed"):
            out.append(f"{st['linksFailed']} stylesheet link(s) in the prototype never loaded")
        if (st.get("links", 0) + st.get("styleEls", 0)) > 0 and st.get("rules", 0) == 0:
            out.append("the prototype has stylesheets but none applied: it rendered unstyled")
    elif frame:
        out.append("the prototype frame could not be inspected")
    return out


def verify_board(site_url: str, password: str, round_n: int, variant: str = "a", timeout: float = DEFAULT_TIMEOUT,
                 launcher: Optional[Any] = None) -> Dict[str, Any]:
    """Unlock `site_url` with `password` in a browser, let the board render round `round_n`'s prototype frame and
    judge the network result. Returns the report on success; raises BrowserCheckError (BrowserUnavailable when no
    browser exists) with a scrubbed message otherwise."""
    started = time.monotonic()
    deadline = started + timeout
    try:
        with (launcher if launcher is not None else Chrome()) as (host, port, path):
            ws = WebSocket(host, port, path)
            try:
                return _run(ws, site_url, password, round_n, variant, deadline)
            finally:
                ws.close()
    except BrowserCheckError as e:
        raise type(e)(_scrub(e.message, password), e.report) from None
    except Exception as e:      # never let a secret ride out in an unexpected error
        raise BrowserCheckError(_scrub(f"the browser check failed ({type(e).__name__})", password)) from None


def _run(ws: WebSocket, site_url: str, password: str, round_n: int, variant: str, deadline: float) -> Dict[str, Any]:
    obs = Observer()
    cdp = Cdp(ws, obs)
    tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
    obs.top_frame = tid                                # a page's main frame has its target's id
    sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
    for method, params in (("Page.enable", {}), ("Network.enable", {}), ("Runtime.enable", {}),
                           ("Network.setCacheDisabled", {"cacheDisabled": True}),
                           ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True,
                                                     "flatten": True})):
        cdp.call(method, params, session=sid)

    def ev(expr: str, **kw: Any) -> Any:
        return cdp.evaluate(expr, session=kw.pop("session", sid), **kw)

    loads = obs.loads
    nav = cdp.call("Page.navigate", {"url": site_url}, session=sid)
    if nav.get("errorText"):
        raise BrowserCheckError(f"the browser could not load the site ({nav['errorText']})")
    if not cdp.wait(lambda: obs.loads > loads, deadline):
        raise BrowserCheckError("the site did not finish loading in time")
    if not ev(JS_HAS_PASSWORD):
        raise BrowserCheckError("the site showed no password form: it is not behind its password gate")
    if not ev(JS_FOCUS_PASSWORD):
        raise BrowserCheckError("the password field could not be focused")
    cdp.call("Input.insertText", {"text": password}, session=sid)
    loads = obs.loads
    obs.reset()                                    # count from the unlock on
    if not ev(JS_SUBMIT):
        raise BrowserCheckError("the password form could not be submitted")
    if not cdp.wait(lambda: obs.loads > loads, deadline):
        raise BrowserCheckError("the board did not load after the password was submitted"
                                + _failure_hint(obs))

    want = f"rounds/{round_n}/{variant}/index.html"
    frame: Dict[str, Any] = {}

    def frame_present() -> bool:
        try:
            raw = ev(JS_FRAMES)
            for f in json.loads(raw or "[]"):
                if str(f.get("src", "")).split("?", 1)[0].endswith(want):
                    frame.update(f)
                    return True
        except (BrowserCheckError, ValueError):
            pass
        return False

    report: Dict[str, Any] = {"round": round_n, "frame": None, "styles": None}
    if not cdp.wait(frame_present, deadline, 0.3):
        report.update(requests=obs.total, failed=list(obs.failures))
        raise BrowserCheckError("the board did not render the prototype frame after unlock"
                                + _failure_hint(obs), report)
    report["frame"] = dict(frame)

    def frame_id() -> Optional[str]:
        def walk(node: Dict[str, Any]) -> Optional[str]:
            fr = node.get("frame") or {}
            if str(fr.get("url", "")).split("?", 1)[0].endswith(want) and node.get("frame") is not None \
                    and fr.get("parentId"):
                return fr.get("id")
            for child in node.get("childFrames") or []:
                got = walk(child)
                if got:
                    return got
            return None
        try:
            got = walk(cdp.call("Page.getFrameTree", session=sid).get("frameTree") or {})
        except BrowserCheckError:
            got = None
        if got:
            return got
        # an out-of-process frame is missing from the tree; its own session reported where it navigated
        for fid, (url, _) in obs.frames.items():
            if url.split("?", 1)[0].endswith(want):
                return fid
        return None

    styles: Dict[str, Any] = {}

    def frame_ready() -> bool:
        fid = frame_id()
        ctx = obs.contexts.get(fid or "")
        if not ctx:
            return False
        try:
            got = json.loads(ev(JS_STYLES, session=ctx[0] or sid, context=ctx[1]) or "{}")
        except (BrowserCheckError, ValueError):
            return False
        if got.get("ready") == "complete":
            styles.update(got)
            return True
        return False

    if not cdp.wait(frame_ready, deadline, 0.3):
        report.update(requests=obs.total, failed=list(obs.failures))
        raise BrowserCheckError("the prototype frame did not finish loading" + _failure_hint(obs), report)
    # let script-driven loads (lazy images, fonts, dynamic imports) land before counting
    settle = min(deadline, time.monotonic() + 8.0)
    cdp.wait(lambda: not obs.inflight and time.monotonic() - obs.last_activity > 1.0, settle)
    fid = frame_id()
    ctx = obs.contexts.get(fid or "")
    if ctx:
        try:
            styles = json.loads(ev(JS_STYLES, session=ctx[0] or sid, context=ctx[1]) or "{}") or styles
        except (BrowserCheckError, ValueError):
            pass
    report.update(styles=styles, requests=obs.total, failed=list(obs.failures), pending=obs.pending())
    problems = judge(report)
    report["problems"] = problems
    if problems:
        raise BrowserCheckError("; ".join(problems), report)
    return report


def _failure_hint(obs: Observer) -> str:
    if not obs.failures:
        return ""
    f = obs.failures[0]
    return f" (first failure: {f['status'] or f.get('error') or 'failed'} {f['path']})"
