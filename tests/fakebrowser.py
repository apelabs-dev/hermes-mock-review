"""Offline doubles for the gated-site browser check.

GatedSite is a faithful stand-in for a password-gated here.now site: every path answers 401 (with a password form)
until the unlock cookie `__hn_unlock_<slug>` (SameSite=Lax) comes back; POSTing the right password answers 303 plus
that cookie.

FakeBrowser is a DevTools-protocol server that behaves like Chrome would for such a site, over real sockets and real
HTTP: it keeps a cookie jar, types into the password form, follows the 303, loads the board's state.json with the
cookie (first-party), builds the board's prototype iframe from the production board.js (its real sandbox attribute),
loads the iframe document with the cookie (same-site navigation) and then loads that document's own subresources
WITHOUT the cookie: the frame is sandboxed without allow-same-origin, its origin is opaque, so those requests are
cross-site and SameSite=Lax cookies are not sent. It reports everything as Network.* events, exactly what the
production checker consumes. Like Chrome it puts that sandboxed frame in its own process (an out-of-process frame): a
separate session (Target.attachedToTarget, paused until resumed), the frame missing from the page session's frame
tree, its navigation, context and responses reported on the frame's session (its document request starts on the page's).
"""

import base64
import hashlib
import http.client
import json
import mimetypes
import re
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mockreview import browsercheck as bc

ROOT = Path(__file__).resolve().parent.parent
BOARD_JS = (ROOT / "mockreview" / "board" / "board.js").read_text(encoding="utf-8")
PROTO_SANDBOX = re.search(r"src: v\.file, title: R\(\)\.title \+ ' prototype', sandbox: '([^']*)'", BOARD_JS).group(1)


class GatedSite:
    def __init__(self, root_getter, password, slug="fake-slug-ab12"):
        self.root_getter, self.password, self.slug = root_getter, password, slug
        self.cookie = f"__hn_unlock_{slug}=unlocked"
        self.log = []                      # (method, path, sent the cookie?)
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _unlocked(self):
                return site.cookie in (self.headers.get("Cookie") or "")

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                has = self._unlocked()
                site.log.append(("GET", path, has))
                if not has:
                    return self._send(401, b'<form method="post"><input type="password" name="password"></form>')
                rel = path.lstrip("/") or "index.html"
                f = (Path(site.root_getter()) / rel)
                if not f.is_file():
                    return self._send(404, b"not found")
                self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                given = urllib.parse.parse_qs(body).get("password", [""])[0]
                site.log.append(("POST", urllib.parse.urlsplit(self.path).path, self._unlocked()))
                if given != site.password:
                    return self._send(401, b"wrong password")
                self._send(303, b"", extra=[("Location", "/"),
                                            ("Set-Cookie", site.cookie + "; Path=/; SameSite=Lax; HttpOnly")])

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def subresource_log(self):
        """Requests the sandboxed frame's document made for its own files: (path, sent the cookie?)."""
        return [(p, c) for m, p, c in self.log if m == "GET" and "/assets/" in p]


class FakeBrowser:
    """A DevTools server for one connection. `site` is a GatedSite; `commands` records every method called."""

    def __init__(self, site, board_sandbox=None):
        self.site, self.commands, self.typed = site, [], None
        self.sessions = []                 # (session id or None, method) for every command received
        self.board_sandbox = PROTO_SANDBOX if board_sandbox is None else board_sandbox
        self.jar, self.page, self.iframes, self.frame_html, self.frame_status = "", "blank", [], "", {}
        self.sock = None
        self._n = 0
        self.srv = None
        import socket
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(2)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    # ------------------------------------------------------------- launcher protocol
    def __enter__(self):
        return "127.0.0.1", self.port, "/devtools/browser/fake"

    def __exit__(self, *exc):
        try:
            self.listener.close()
        except OSError:
            pass

    # ------------------------------------------------------------- websocket server
    def _accept(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        self.sock = conn
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(4096)
        head, _, rest = buf.partition(b"\r\n\r\n")
        key = re.search(rb"Sec-WebSocket-Key: (\S+)", head, re.I).group(1).decode()
        acc = base64.b64encode(hashlib.sha1((key + bc.WS_GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
        self._buf = rest
        try:
            while True:
                msg = self._read()
                if msg is None:
                    return
                self._handle(json.loads(msg))
        except (OSError, ValueError):
            return

    def _need(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("closed")
            self._buf += chunk

    def _read(self):
        try:
            self._need(2)
            ln = self._buf[1] & 0x7F
            off = 2
            if ln == 126:
                self._need(4)
                ln, off = struct.unpack(">H", self._buf[2:4])[0], 4
            elif ln == 127:
                self._need(10)
                ln, off = struct.unpack(">Q", self._buf[2:10])[0], 10
            self._need(off + 4 + ln)
        except OSError:
            return None
        mask = self._buf[off:off + 4]
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._buf[off + 4:off + 4 + ln]))
        op = self._buf[0] & 0x0F
        self._buf = self._buf[off + 4 + ln:]
        if op == 8:
            return None
        return data.decode()

    def _send(self, obj):
        data = json.dumps(obj).encode()
        head = bytearray([0x81])
        if len(data) < 126:
            head.append(len(data))
        elif len(data) < 65536:
            head += bytes([126]) + struct.pack(">H", len(data))
        else:
            head += bytes([127]) + struct.pack(">Q", len(data))
        self.sock.sendall(bytes(head) + data)

    def emit(self, method, session="S1", **params):
        self._send({"method": method, "params": params, "sessionId": session})

    # ------------------------------------------------------------- the browser model
    def _req(self, method, path, body=None, cookie=True, rtype="Document", frame="F0", session="S1", start="S1"):
        """One HTTP request, reported as Network.* events. Returns (status, body). `start` is the session that sees
        requestWillBeSent, `session` the one that sees the response: for a sandboxed frame's own document they differ,
        as in Chrome (the navigation starts in the page's process, the frame's process receives it)."""
        self._n += 1
        rid = f"R{self._n}"
        url = self.site.url.rstrip("/") + path
        self.emit("Network.requestWillBeSent", session=start, requestId=rid, request={"url": url}, type=rtype,
                  frameId=frame)
        conn = http.client.HTTPConnection("127.0.0.1", self.site.port, timeout=10)
        headers = {}
        if cookie and self.jar:
            headers["Cookie"] = self.jar
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie:
            self.jar = set_cookie.split(";", 1)[0]
        self.emit("Network.responseReceived", session=session, requestId=rid, type=rtype, frameId=frame,
                  response={"url": url, "status": resp.status})
        self.emit("Network.loadingFinished", session=session, requestId=rid)
        conn.close()
        return resp.status, data

    def _handle(self, msg):
        method, params, mid = msg["method"], msg.get("params") or {}, msg["id"]
        self.commands.append(method)
        self.sessions.append((msg.get("sessionId"), method))
        result, after = {}, None
        if method == "Target.createTarget":
            result = {"targetId": "F0"}          # a page's main frame has its target's id
        elif method == "Target.attachToTarget":
            result = {"sessionId": "S1"}
        elif method == "Page.navigate":
            result = {"frameId": "F0"}
            after = lambda: self._navigate(params["url"])
        elif method == "Input.insertText":
            self.typed = params["text"]
        elif method == "Page.getFrameTree":
            # the sandboxed frame is an out-of-process frame: it is NOT in the page session's tree
            result = {"frameTree": {"frame": {"id": "F0", "url": self.site.url}, "childFrames": []}}
        elif method == "Runtime.evaluate":
            expr = params["expression"]
            val, after = self._evaluate(expr, params.get("contextId"), msg.get("sessionId"))
            result = {"result": {"type": "string", "value": val}}
        self._send({"id": mid, "result": result, "sessionId": msg.get("sessionId")})
        if after:
            after()

    def _evaluate(self, expr, ctx, session=None):
        if expr == bc.JS_HAS_PASSWORD:
            return self.page == "gate", None
        if expr == bc.JS_FOCUS_PASSWORD:
            return True, None
        if expr == bc.JS_SUBMIT:
            return True, self._submit
        if expr == bc.JS_FRAMES:
            return json.dumps(self.iframes), None
        if expr == bc.JS_STYLES and ctx == 7 and session == "S2":      # only the frame's own session can see it
            return json.dumps(self._styles()), None
        raise AssertionError("unexpected page script: " + expr[:60])

    def _navigate(self, url):
        status, _ = self._req("GET", urllib.parse.urlsplit(url).path or "/")
        self.page = "gate" if status == 401 else "board"
        self.emit("Page.loadEventFired", timestamp=1)

    def _submit(self):
        status, _ = self._req("POST", "/", body="password=" + urllib.parse.quote(self.typed or ""))
        if status == 401:
            self.page = "gate"
            self.emit("Page.loadEventFired", timestamp=2)
            return
        self._req("GET", "/")
        _, raw = self._req("GET", "/state.json", rtype="Fetch")
        self.page = "board"
        self.emit("Page.loadEventFired", timestamp=2)
        state = json.loads(raw)
        rnd = state["rounds"][-1]
        if rnd["kind"] != "prototype":
            return
        file = rnd["variants"][0]["file"]
        self.iframes = [{"src": file, "sandbox": self.board_sandbox}]
        # the sandboxed frame gets its own process: a new session, paused until the checker resumes it
        self.emit("Target.attachedToTarget", sessionId="S2", waitingForDebugger=True,
                  targetInfo={"targetId": "F1", "type": "iframe", "url": self.site.url + file})
        self.emit("Page.frameNavigated", session="S2",
                  frame={"id": "F1", "parentId": "F0", "url": self.site.url + file})
        # the frame's own document: a same-site navigation from the board, so the cookie goes along
        _, doc = self._req("GET", "/" + file, rtype="Document", frame="F1", session="S2")
        self.frame_html = doc.decode("utf-8", "replace")
        base = "/" + file.rsplit("/", 1)[0] + "/"
        for m in re.finditer(r"""<(link|script|img)\b[^>]*?\b(?:href|src)=["']([^"']+)["']""", self.frame_html, re.I):
            ref = m.group(2)
            if re.match(r"^(data:|https?:|//|#)", ref):
                continue
            kind = {"link": "Stylesheet", "script": "Script", "img": "Image"}[m.group(1).lower()]
            # opaque origin: cross-site, so no SameSite=Lax cookie
            st, _ = self._req("GET", urllib.parse.urljoin(base, ref), cookie=False, rtype=kind, frame="F1",
                              session="S2", start="S2")
            self.frame_status[ref] = st
        self.emit("Runtime.executionContextCreated", session="S2",
                  context={"id": 7, "auxData": {"frameId": "F1", "isDefault": True}, "origin": "://"})

    def _styles(self):
        html = self.frame_html
        styles = re.findall(r"<style\b[^>]*>(.*?)</style>", html, re.S | re.I)
        links = re.findall(r"<link\b[^>]*rel=[\"']?stylesheet[^>]*href=[\"']([^\"']+)[\"']", html, re.I)
        failed = [ln for ln in links if self.frame_status.get(ln, 200) >= 400]
        rules = sum(s.count("{") for s in styles)
        return {"sheets": len(styles) + len(links) - len(failed), "rules": rules, "styleEls": len(styles),
                "links": len(links), "linksFailed": len(failed), "ready": "complete", "body": True}
