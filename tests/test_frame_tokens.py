"""Signed per-round frame tokens.

A round page runs in a sandboxed frame (an opaque origin): its subresource requests are cross-site and carry no
`SameSite=Lax` session cookie, so a round file needs its own credential. The board mints one signed token per visible
round into `state.json`; the board puts it in the path of every frame document URL, and every URL a prototype derives
relatively inherits it. The token names one review and one round, is bound to the viewer's live session (or to a
loopback owner), and dies with that session, share-link generation or grant.

The HTTP-level tests pin the shape and the rules; the browser tests load the real board in Chrome as a guest and as an
invited reviewer, and reproduce the ART-2 navigation from a hostile artifact.
"""

import base64
import json
import math
import re
import threading
import time
from pathlib import Path

import pytest

import browserkit as bc
from authkit import account, peer, signed_in, token_of
from conftest import http
from mockreview import db, server as board_server, store, users
from test_artifact_browser import Sink
from test_media_range import _pcm_wav

ORIGIN = {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PROTO_PAGE = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<img id="still" alt="still" src="assets/still.png">
<audio id="a" controls preload="metadata" src="assets/tone.wav"></audio>
<script>
var R = {fixture: 'proto', img: 0, duration: 0, state: 'loading'};
var a = document.getElementById('a');
function send() {
  R.img = document.getElementById('still').naturalWidth || 0;
  R.duration = isFinite(a.duration) ? a.duration : 0;
  if (R.duration > 0) R.state = 'metadata';
  parent.postMessage(R, '*');
}
a.addEventListener('loadedmetadata', send);
a.addEventListener('error', function () { R.state = 'error'; send(); });
document.getElementById('still').addEventListener('load', send);
document.getElementById('still').addEventListener('error', send);
setTimeout(send, 3000);
</script></body></html>"""
ART_PAGE = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<img id="still" alt="still" src="assets/still.png">
<audio id="a" controls preload="metadata" src="assets/tone.wav"></audio>
<script>
var R = {fixture: 'artifact', img: 0, duration: 0, state: 'loading'};
var a = document.getElementById('a');
function send() {
  R.img = document.getElementById('still').naturalWidth || 0;
  R.duration = isFinite(a.duration) ? a.duration : 0;
  if (R.duration > 0) R.state = 'metadata';
  parent.postMessage(R, '*');
}
a.addEventListener('loadedmetadata', send);
a.addEventListener('error', function () { R.state = 'error'; send(); });
document.getElementById('still').addEventListener('load', send);
document.getElementById('still').addEventListener('error', send);
setTimeout(send, 3000);
</script></body></html>"""


# ---------------------------------------------------------------- helpers

def _proto_tree(tmp_path, name="proto", page=PROTO_PAGE):
    d = tmp_path / name
    (d / "assets").mkdir(parents=True, exist_ok=True)
    (d / "assets" / "still.png").write_bytes(PNG)
    (d / "assets" / "tone.wav").write_bytes(_pcm_wav())
    (d / "index.html").write_text(page, encoding="utf-8")
    return d / "index.html"


def _proto_review(tmp_path, name="proto", page=PROTO_PAGE, mode="tailscale"):
    return store.create_review("Frame tokens", "", ORIGIN, mode,
                               store.parse_round_spec(prototype=str(_proto_tree(tmp_path, name, page))))


def _artifact_review(tmp_path, name="art", html="<p>art</p>", src=None):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    block = {"type": "artifact", "id": "film", "title": "Film"}
    if src is not None:
        block["src"] = str(src)
    else:
        block["html"] = html
    (d / "decisions.json").write_text(json.dumps({"blocks": [block]}), encoding="utf-8")
    return store.create_review("Artifact frame", "", ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(d / "decisions.json")))


def _guest(rid, role="reviewer"):
    made = users.share("test", rid, "create", role)
    return users.join_as_guest(users.resolve_share_token(token_of(made["path"])))


def _state(base, rid, sess):
    code, _, body = http("GET", f"{base}/r/{rid}/state.json", headers=signed_in(sess, False))
    assert code == 200, body
    return json.loads(body)


def _tokens(base, rid, sess):
    return _state(base, rid, sess).get("frame_tokens", {})


def _expire(sess):
    with db.tx() as conn:
        conn.execute("UPDATE sessions SET expires_at = ? WHERE id_hash = ?",
                     (users.now() - 1, users.digest(sess["session"])))


def _own_policy(base, rid, n=1, variant="a"):
    host = base.replace("http://", "")
    own = f"http://{host}/r/{rid}/rounds/{n}/{variant}/ https://{host}/r/{rid}/rounds/{n}/{variant}/"
    csp = board_server.ARTIFACT_ROUND_CSP
    for directive in ("img-src", "media-src", "font-src", "style-src", "script-src"):
        csp = csp.replace(directive + " ", directive + " " + own + " ", 1)
    return csp


# ---------------------------------------------------------------- HTTP: minting and the token path

def test_state_json_mints_one_token_per_round_and_the_token_opens_its_round_files(env, server, tmp_path, peer):
    data = _proto_review(tmp_path)
    page = tmp_path / "proto" / "index.html"
    store.add_round(data["id"], store.parse_round_spec(prototype=str(page)))
    member = account(review_id=data["id"], role="reviewer")

    st = _state(server, data["id"], member)
    assert set(st["frame_tokens"]) == {"1", "2"}
    assert users.digest(member["session"]) not in json.dumps(st)      # the session cookie value never goes in state
    tok = st["frame_tokens"]["1"]
    assert "/" not in tok and "+" not in tok                             # itsdangerous URL-safe, no extra encoding
    from mockreview import frames
    assert frames._serializer().loads(tok) == {"r": data["id"], "n": 1,
                                                "s": users.digest(member["session"]), "v": 1}   # exactly {r, n, s, v}

    code, headers, body = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")
    assert code == 200, (code, body[:200])
    assert headers["Content-Security-Policy"] == board_server.VARIANT_CSP
    assert headers["Referrer-Policy"] == "no-referrer"
    code, headers, body = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/assets/still.png")
    assert code == 200 and headers["Content-Security-Policy"] == board_server.VARIANT_CSP
    assert body == PNG

    guest = _guest(data["id"])
    gst = _state(server, data["id"], guest)
    assert set(gst["frame_tokens"]) == {"1", "2"}
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{gst['frame_tokens']['1']}/a/index.html")
    assert code == 200 and headers["Content-Security-Policy"] == board_server.VARIANT_CSP


def test_a_pinned_viewer_guest_only_gets_a_token_for_the_round_they_can_see(env, server, tmp_path, peer):
    data = _proto_review(tmp_path)
    store.add_round(data["id"], store.parse_round_spec(prototype=str(tmp_path / "proto" / "index.html")))
    made = users.share("test", data["id"], "create", "viewer")
    users.share("test", data["id"], "update", round="1")
    guest = users.join_as_guest(users.resolve_share_token(token_of(made["path"])))
    st = _state(server, data["id"], guest)
    assert [r["n"] for r in st["rounds"]] == [1]
    assert set(st["frame_tokens"]) == {"1"}


# ---------------------------------------------------------------- HTTP: scope and viewer binding

def test_a_token_is_scoped_to_its_own_review_and_round(env, server, tmp_path, peer):
    one = _proto_review(tmp_path, name="one")
    store.add_round(one["id"], store.parse_round_spec(prototype=str(tmp_path / "one" / "index.html")))
    other = _proto_review(tmp_path, name="other")
    member = account(review_id=one["id"], role="reviewer")
    other_member = account(review_id=other["id"], role="reviewer")
    tok = _tokens(server, one["id"], member)["1"]
    other_tok = _tokens(server, other["id"], other_member)["1"]

    assert http("GET", f"{server}/r/{one['id']}/rounds/1/~{tok}/a/index.html")[0] == 200
    wrong_round = http("GET", f"{server}/r/{one['id']}/rounds/2/~{tok}/a/index.html")
    assert wrong_round[0] == 401, wrong_round[:1]
    wrong_review = http("GET", f"{server}/r/{other['id']}/rounds/1/~{tok}/a/index.html")
    assert wrong_review[0] == 401, wrong_review[:1]
    assert http("GET", f"{server}/r/{other['id']}/rounds/1/~{other_tok}/a/index.html")[0] == 200


def test_a_token_for_one_viewer_never_grants_another_viewer_the_network(env, server, tmp_path, peer):
    data = _proto_review(tmp_path)
    _artifact_review(tmp_path)
    # A review with an artifact elsewhere is exactly where the two policies differ.
    store.add_round(data["id"], store.parse_round_spec(
        decisions=str(tmp_path / "art" / "decisions.json")))
    one, two = _guest(data["id"]), _guest(data["id"])
    tok = _tokens(server, data["id"], one)["1"]
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html",
                            headers=signed_in(two, False))
    assert code == 200
    csp = headers["Content-Security-Policy"]
    assert csp != board_server.VARIANT_CSP and "connect-src 'none'" in csp, csp


def test_a_signed_out_session_or_a_regenerated_link_stops_authorising(env, server, tmp_path, peer):
    data = _proto_review(tmp_path)
    member = account(review_id=data["id"], role="reviewer")
    mtok = _tokens(server, data["id"], member)["1"]
    assert http("GET", f"{server}/r/{data['id']}/rounds/1/~{mtok}/a/index.html")[0] == 200
    users.end_session(users.digest(member["session"]))
    assert http("GET", f"{server}/r/{data['id']}/rounds/1/~{mtok}/a/index.html")[0] == 401

    guest = _guest(data["id"])
    gtok = _tokens(server, data["id"], guest)["1"]
    assert http("GET", f"{server}/r/{data['id']}/rounds/1/~{gtok}/a/index.html")[0] == 200
    users.share("test", data["id"], "regenerate")
    assert http("GET", f"{server}/r/{data['id']}/rounds/1/~{gtok}/a/index.html")[0] == 401


def test_a_token_expires(env, server, tmp_path, peer, monkeypatch):
    from mockreview import frames
    data = _proto_review(tmp_path)
    peer["addr"] = "127.0.0.1"                        # a loopback state.json with no cookie mints a `local` token
    code, _, body = http("GET", f"{server}/r/{data['id']}/state.json")
    assert code == 200
    tok = json.loads(body)["frame_tokens"]["1"]
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")
    assert code == 200 and headers["Content-Security-Policy"] == board_server.VARIANT_CSP
    monkeypatch.setattr(frames, "FRAME_TOKEN_SECONDS", 0)
    time.sleep(1.2)                                  # itsdangerous' timestamp check has 1 s granularity
    peer["addr"] = "192.0.2.10"                       # off this computer a `local` token is refused outright
    assert http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")[0] == 401
    peer["addr"] = "127.0.0.1"                        # and here the expired token no longer grants the network
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")
    assert code == 200 and headers["Content-Security-Policy"] == _own_policy(server, data["id"])


def test_a_session_bound_token_lives_with_its_session_not_the_clock(env, server, tmp_path, peer, monkeypatch):
    from mockreview import frames
    data = _proto_review(tmp_path)
    guest = _guest(data["id"])
    tok = _tokens(server, data["id"], guest)["1"]
    monkeypatch.setattr(frames, "FRAME_TOKEN_SECONDS", 0)
    time.sleep(1.2)                                  # itsdangerous' timestamp check has 1 s granularity
    peer["addr"] = "192.0.2.10"                       # the frame has no cookie: only the token can open the round
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")
    assert code == 200 and headers["Content-Security-Policy"] == board_server.VARIANT_CSP, (code, headers)
    users.end_session(users.digest(guest["session"]))
    assert http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")[0] == 401


def test_a_proxy_signed_in_viewer_without_a_cookie_gets_a_live_account_token(env, server, tmp_path, peer, monkeypatch):
    """A token minted for a trusted-proxy identity (no session cookie) still names an account, not an empty string."""
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    data = _proto_review(tmp_path)
    dana_headers = {"X-Forwarded-User": "dana", "X-Forwarded-Email": "dana@example.test"}
    sam_headers = {"X-Forwarded-User": "sam", "X-Forwarded-Email": "sam@example.test"}
    kim_headers = {"X-Forwarded-User": "kim", "X-Forwarded-Email": "kim@example.test"}
    dana = account(review_id=data["id"], role="reviewer", name="Dana",
                   identity={"source": "forwarded", "subject": "dana@example.test", "name": "Dana",
                             "email": "dana@example.test"})
    account(review_id=data["id"], role="viewer", name="Sam",
            identity={"source": "forwarded", "subject": "sam@example.test", "name": "Sam",
                      "email": "sam@example.test"})
    kim = account(review_id=data["id"], role="reviewer", name="Kim",
                  identity={"source": "forwarded", "subject": "kim@example.test", "name": "Kim",
                            "email": "kim@example.test"})

    peer["addr"] = "127.0.0.1"
    code, _, body = http("GET", f"{server}/r/{data['id']}/state.json", headers=dana_headers)
    assert code == 200
    tok = json.loads(body)["frame_tokens"]["1"]

    # The frame itself has no cookie and no proxy identity: the token's account decides.
    peer["addr"] = "192.0.2.10"
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")
    assert code == 200 and headers["Content-Security-Policy"] == board_server.VARIANT_CSP, headers

    # The same signed-in identity, still with no cookie, is the same account.
    peer["addr"] = "127.0.0.1"
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html", headers=dana_headers)
    assert code == 200 and headers["Content-Security-Policy"] == board_server.VARIANT_CSP, headers

    # Another signed-in account may read the file through its own grant, but never inherits the token's CSP.
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html", headers=sam_headers)
    assert code == 200
    assert headers["Content-Security-Policy"] != board_server.VARIANT_CSP
    assert "connect-src 'none'" in headers["Content-Security-Policy"]

    # A revoked grant, and a deleted account, both stop the token loading anything.
    users.revoke_grant("test", dana["user_id"], review_id=data["id"])
    ktok = json.loads(http("GET", f"{server}/r/{data['id']}/state.json", headers=kim_headers)[2])["frame_tokens"]["1"]
    users.delete_user("test", kim["user_id"])
    peer["addr"] = "192.0.2.10"
    assert http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")[0] == 401
    assert http("GET", f"{server}/r/{data['id']}/rounds/1/~{ktok}/a/index.html")[0] == 401


def test_a_forwarded_viewer_with_a_foreign_guest_cookie_gets_an_account_token(env, server, tmp_path, peer, monkeypatch):
    """The proxy identity decides who the request is; a guest cookie from another review must not bind the token."""
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    data = _proto_review(tmp_path)
    other = _proto_review(tmp_path, name="other")
    foreign = _guest(other["id"])
    dana_headers = {"X-Forwarded-User": "dana", "X-Forwarded-Email": "dana@example.test"}
    account(review_id=data["id"], role="reviewer", name="Dana",
            identity={"source": "forwarded", "subject": "dana@example.test", "name": "Dana",
                      "email": "dana@example.test"})
    peer["addr"] = "127.0.0.1"                        # the trusted proxy
    code, _, body = http("GET", f"{server}/r/{data['id']}/state.json",
                         headers=dict(dana_headers, **signed_in(foreign, False)))
    assert code == 200
    st = json.loads(body)
    assert st["me"]["source"] == "forwarded"          # the foreign cookie did not decide who this is
    tok = st["frame_tokens"]["1"]
    peer["addr"] = "192.0.2.10"                       # the frame carries no cookie and no proxy identity
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")
    assert code == 200 and headers["Content-Security-Policy"] == board_server.VARIANT_CSP, (code, headers)


def test_a_forwarded_viewer_with_their_own_expired_cookie_gets_an_account_token(env, server, tmp_path, peer, monkeypatch):
    """A cookie whose session has expired must not bind the token; the live account does, and the frame still opens."""
    monkeypatch.setenv("MOCKREVIEW_FORWARDED_IDENTITY", "true")
    data = _proto_review(tmp_path)
    dana_headers = {"X-Forwarded-User": "dana", "X-Forwarded-Email": "dana@example.test"}
    dana = account(review_id=data["id"], role="reviewer", name="Dana",
                   identity={"source": "forwarded", "subject": "dana@example.test", "name": "Dana",
                             "email": "dana@example.test"})
    _expire(dana)
    peer["addr"] = "127.0.0.1"
    code, _, body = http("GET", f"{server}/r/{data['id']}/state.json",
                         headers=dict(dana_headers, **signed_in(dana, False)))
    assert code == 200
    st = json.loads(body)
    assert st["me"]["source"] == "forwarded"
    tok = st["frame_tokens"]["1"]
    peer["addr"] = "192.0.2.10"
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html")
    assert code == 200 and headers["Content-Security-Policy"] == board_server.VARIANT_CSP, (code, headers)


# ---------------------------------------------------------------- HTTP: CSP selection

def test_an_artifact_variant_keeps_the_artifact_policy_even_with_a_valid_token(env, server, tmp_path, peer):
    data = _artifact_review(tmp_path)
    member = account(review_id=data["id"], role="reviewer")
    tok = _tokens(server, data["id"], member)["1"]
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/blocks/film.html")
    assert code == 200
    assert headers["Content-Security-Policy"] == board_server.ARTIFACT_ROUND_CSP


def test_a_plain_round_document_without_a_token_has_no_network(env, server, tmp_path):
    data = _proto_review(tmp_path)
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/a/index.html")
    csp = headers["Content-Security-Policy"]
    assert code == 200 and csp == _own_policy(server, data["id"])
    assert csp != board_server.VARIANT_CSP and "connect-src 'none'" in csp

    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/a/assets/still.png")
    assert code == 200 and headers["Content-Security-Policy"] == _own_policy(server, data["id"])


def test_a_token_in_the_path_is_redacted_in_the_request_log():
    line = "192.0.2.10 GET /r/x-123abc/rounds/1/~eyJhbGciOi.abc.def/a/index.html HTTP/1.1"
    got = board_server.redact_link_tokens(line)
    assert "eyJhbGciOi" not in got and "/~…/" in got
    encoded = "GET /r/x-123abc/rounds/1/%7Eabcdef.abc.def/a/assets/x.png"
    assert board_server.redact_link_tokens(encoded) == "GET /r/x-123abc/rounds/1/~…/a/assets/x.png"
    assert board_server.redact_link_tokens("/s/secret.share.token") == "/s/…"


def test_every_state_replacement_goes_through_apply_state():
    """`S.frameTokens` must never outlive the state it came from: one helper owns the assignment, six sites call it."""
    src = (Path(__file__).resolve().parents[1] / "mockreview/board/board.js").read_text(encoding="utf-8")
    assert src.count("S.review =") == 1
    assert "S.review = st;" in src and "S.frameTokens = st.frame_tokens || S.frameTokens;" in src
    assert src.count("applyState(") >= 7                      # the definition plus at least the six replacements
    for call in ("applyState(res.state)", "applyState(st)", "applyState(fresh)"):
        assert call in src, call


# ---------------------------------------------------------------- real Chrome

class Recorder(bc.Observer):
    """The requests and responses one browser scenario saw, including each response's headers (for CSP and 206)."""

    def __init__(self):
        super().__init__()
        self.log = []
        self.responses = {}
        self.history = []                 # every response in arrival order, for "after this point" assertions

    def handle(self, msg):
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "Network.requestWillBeSent":
            req = params.get("request") or {}
            self.log.append({"method": req.get("method"), "url": req.get("url"),
                             "headers": {k.lower(): v for k, v in (req.get("headers") or {}).items()}})
        elif method == "Network.responseReceived":
            resp = params.get("response") or {}
            got = {
                "status": int(resp.get("status") or 0),
                "headers": {k.lower(): v for k, v in (resp.get("headers") or {}).items()},
            }
            self.responses[str(resp.get("url") or "")] = got
            self.history.append((str(resp.get("url") or ""), got))
        super().handle(msg)

    def response(self, suffix):
        for url, got in self.responses.items():
            if url.split("?")[0].endswith(suffix):
                return got
        return None

    def since(self, index, suffix):
        """The responses recorded at or after `index` whose URL ends with `suffix`."""
        return [got for url, got in self.history[index:] if url.split("?")[0].endswith(suffix)]


class HeaderSink:
    """A sink that also keeps each request's headers (the control needs the Referer header)."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append({"path": self.path,
                                   "headers": {k.lower(): v for k, v in self.headers.items()}})
                body = b"window.__cdn = 1;"
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


RECORD_MESSAGES = ("window.__reports=[];addEventListener('message',function(e){"
                   "if(e.data&&e.data.fixture)window.__reports.push(e.data);});")


class Tab:
    """One target: cookies, a page-init recorder, then navigate. Frames are found through the Recorder."""

    def __init__(self, cdp, base, url, cookies=(), clear_cookies=False, init="", width=1280, height=1000):
        self.cdp = cdp
        tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        for method, params in (
                ("Page.enable", {}), ("Runtime.enable", {}), ("Network.enable", {}),
                ("Network.setCacheDisabled", {"cacheDisabled": True}),
                ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}),
                ("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                        "deviceScaleFactor": 1, "mobile": False})):
            cdp.call(method, params, session=self.sid)
        if clear_cookies:
            cdp.call("Network.clearBrowserCookies", session=self.sid)
        if init:
            cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": init}, session=self.sid)
        for name, value in cookies:
            got = cdp.call("Network.setCookie", {"name": name, "value": value, "url": base, "path": "/"},
                           session=self.sid)
            assert got.get("success"), (name, got)
        cdp.call("Page.navigate", {"url": url}, session=self.sid)

    def js(self, expr):
        return self.cdp.evaluate(expr, session=self.sid)

    def until(self, expr, timeout=25):
        return self.cdp.wait(lambda: self.js(expr), time.monotonic() + timeout, 0.25)


def _live_frame_context(cdp, rec, fragment):
    """(frame id, session and context) of the document now at `fragment`, once it has finished parsing.

    A sandboxed frame is an OOPIF: the recorded context can be about:blank, belong to a document the frame has since
    left, or have been refused as stale while its replacement's creation event was consumed already. Verify the
    document this context really is before handing it out; when the map lost the frame, ask the browser to report
    its live execution contexts again (tests/browserkit.Cdp.reannounce_contexts) so the next poll can resolve it."""
    lost = False
    for fid, (url, _) in list(rec.frames.items()):
        if fragment not in url:
            continue
        ctx = rec.contexts.get(fid)
        if ctx is None:
            lost = True
            continue
        try:
            raw = cdp.evaluate("JSON.stringify([location.href, document.readyState])",
                               session=ctx[0], context=ctx[1])
            href, ready = json.loads(raw)
        except bc.BrowserCheckError as e:
            if "Cannot find context with specified id" not in str(e):
                raise
            rec.contexts.pop(fid, None)
            lost = True
            continue
        except (TypeError, ValueError):
            rec.contexts.pop(fid, None)
            lost = True
            continue
        if not (isinstance(href, str) and fragment in href):
            rec.contexts.pop(fid, None)          # this frame id no longer hosts the document we are after
            lost = True
            continue
        if ready == "loading":
            continue                             # the frame's own document is not ready for its script yet
        return fid, ctx
    if lost:
        cdp.reannounce_contexts()
    return None


def _in_frame_containing(cdp, rec, fragment, expr):
    """Evaluate in the document at `fragment`; a context that died between events is dropped, not fatal.

    The old lookup could run the expression in a stale about:blank context (the element then never exists) or lose
    the frame for good after one refusal; this resolves the live document, asks for the contexts again when the map
    lost one, and lets the caller's wait retry, so a process swap or a cold Chrome start cannot error the module."""
    got = _live_frame_context(cdp, rec, fragment)
    if got is None:
        return None
    fid, ctx = got
    try:
        return cdp.evaluate(expr, session=ctx[0], context=ctx[1])
    except bc.BrowserCheckError as e:
        if "Cannot find context with specified id" not in str(e):
            raise
        rec.contexts.pop(fid, None)
        cdp.reannounce_contexts()
        return None


def _ready_media(cdp, rec, fragment):
    media = _media_state(cdp, rec, fragment)
    return media if media and (media.get("img") or 0) > 0 and (media.get("duration") or 0) > 0 else None


def _media_state(cdp, rec, fragment):
    raw = _in_frame_containing(
        cdp, rec, fragment,
        "JSON.stringify((function(){var i=document.getElementById('still'), a=document.getElementById('a');"
        "return {img:i?i.naturalWidth:0, duration:a?Number(a.duration):0, seekable:a?a.seekable.length:0,"
        "ready:a?a.readyState:0};})())")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


@pytest.fixture(scope="module")
def frames_browser(tmp_path_factory):
    """One Chrome, deleted in `finally`; every (A)-(F) case runs here once and the tests judge their own evidence."""
    try:
        bc.find_browser()
    except bc.BrowserUnavailable as e:
        pytest.skip(e.message)
    tmp = tmp_path_factory.mktemp("frame-tokens")
    mp = pytest.MonkeyPatch()
    for key, value in (("HOME", tmp / "home"), ("HERMES_HOME", tmp / "hermes"),
                       ("MOCKREVIEW_HOME", tmp / "data"), ("MOCKREVIEW_CONFIG", tmp / "no-config.yaml")):
        mp.setenv(key, str(value))
    mp.setenv("MOCKREVIEW_OWNER", "self")
    mp.setenv("MOCKREVIEW_PUBLIC_BASE", "https://board.example.test:8790")
    state = {"peer": "192.0.2.10"}
    mp.setattr(board_server.Handler, "_peer", lambda self: state["peer"])

    leak = Sink()               # C: the plain page a hostile artifact navigates to
    round2 = Sink()             # E: round 2's page
    cdn = HeaderSink()          # D: the second local "CDN"
    out = {}
    try:
        # A: a guest's page-mode prototype, with one relative still and one relative audio file.
        data_a = _proto_review(tmp, name="a")
        r2 = tmp / "a2" / "round2.html"
        r2.parent.mkdir(parents=True)
        r2.write_text("<!doctype html><html><body><script>fetch(%s,{mode:'no-cors'});</script></body></html>"
                      % json.dumps(round2.url + "/round2"), encoding="utf-8")
        store.add_round(data_a["id"], store.parse_round_spec(prototype=str(r2)))
        made = users.share("test", data_a["id"], "create", "viewer")
        users.share("test", data_a["id"], "update", round="1")          # the guest opens on round 1
        guest = users.join_as_guest(users.resolve_share_token(token_of(made["path"])))

        # B: an invited reviewer's markable artifact, whose page references a still and a tone beside it.
        art_src = tmp / "b" / "art"
        (art_src / "assets").mkdir(parents=True)
        (art_src / "assets" / "still.png").write_bytes(PNG)
        (art_src / "assets" / "tone.wav").write_bytes(_pcm_wav())
        (art_src / "index.html").write_text(ART_PAGE, encoding="utf-8")
        data_b = _artifact_review(tmp, name="b", src=art_src / "index.html")
        reviewer = account(review_id=data_b["id"], role="reviewer", name="Reviewer")

        # C: a plain review whose prototype page wants the leak sink; the artifact navigates to it tokenless.
        plain = _proto_review(tmp, name="c", page=(
            "<!doctype html><html><body><script>fetch(%s,{mode:'no-cors'});</script></body></html>"
            % json.dumps(leak.url + "/escaped")))
        attack = _artifact_review(tmp, name="attack", html=(
            '<p>attack</p><script>location.href = %s;</script>'
            % json.dumps(f"/r/{plain['id']}/rounds/1/a/index.html")))

        # D: a plain review, page1 links relatively to page2, which loads a script from a second CDN sink.
        p2 = tmp / "d" / "assets" / "page2.html"
        p2.parent.mkdir(parents=True)
        p2.write_text('<!doctype html><html><body><p id="two">page two</p><script src="%s/cdn.js"></script></body></html>'
                      % cdn.url, encoding="utf-8")
        d_page = tmp / "d" / "index.html"
        d_page.write_text('<!doctype html><html><body><a id="go" href="assets/page2.html">next</a></body></html>',
                          encoding="utf-8")
        data_d = store.create_review("Plain link", "", ORIGIN, "tailscale",
                                     store.parse_round_spec(prototype=str(d_page)))
        guest_d = _guest(data_d["id"])

        # F: a guest reviewer opens round 1, then round 2 arrives while the board is open (defect 1).
        f1_page = _proto_tree(tmp, name="f1")
        data_f = store.create_review("Live round", "", ORIGIN, "tailscale",
                                     store.parse_round_spec(prototype=str(f1_page)), wake=False)
        made_f = users.share("test", data_f["id"], "create", "reviewer")
        guest_f = users.join_as_guest(users.resolve_share_token(token_of(made_f["path"])))
        f2_page = _proto_tree(tmp, name="f2")

        httpd = board_server.make_server("127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        board = f"http://127.0.0.1:{httpd.server_address[1]}"
        out["board"] = board
        tokens_a = _tokens(board, data_a["id"], guest)
        out["tokens_a"] = tokens_a

        from browserkit import Chrome, Cdp, WebSocket
        rec = Recorder()
        with Chrome() as (host, port, path):
            ws = WebSocket(host, port, path)
            try:
                cdp = Cdp(ws, rec)

                # (A) guest, page mode
                state["peer"] = "192.0.2.10"
                a = Tab(cdp, board, f"{board}/r/{data_a['id']}/",
                        cookies=(("mr_session", guest["session"]), ("mr_csrf", guest["csrf"])), init=RECORD_MESSAGES)
                a.until("!!document.querySelector('.proto iframe')", 60)
                a_fragment = f"/r/{data_a['id']}/rounds/1/"
                out["a_media"] = cdp.wait(lambda: _ready_media(cdp, rec, a_fragment),
                                           time.monotonic() + 30, 0.25)
                out["a_src"] = a.js("(document.querySelector('.proto iframe')||{}).src || ''")
                out["a_reports"] = json.loads(a.js("JSON.stringify(window.__reports||[])"))
                tone = f"/r/{data_a['id']}/rounds/1/"
                out["a_tone"] = [v for u, v in rec.responses.items() if u.endswith("/assets/tone.wav")
                                 and tone in u]

                # (E) the round-1 token used on round 2, in the same sandboxed frame and guest session
                got_tok = re.search(r"/rounds/1/~([^/]+)/a/", out.get("a_src", ""))
                tok1 = got_tok.group(1) if got_tok else ""
                if tok1:
                    _in_frame_containing(cdp, rec, f"/r/{data_a['id']}/rounds/1/",
                                         "location.href = %s; true"
                                         % json.dumps(f"/r/{data_a['id']}/rounds/2/~{tok1}/a/round2.html"))
                    wrong = f"/r/{data_a['id']}/rounds/2/~{tok1}/a/round2.html"
                    cdp.wait(lambda: rec.response(wrong) is not None, time.monotonic() + 15, 0.25)
                    out["e_status"] = (rec.response(wrong) or {}).get("status")
                else:
                    out["e_status"] = None
                cdp.wait(lambda: False, time.monotonic() + 1.5)
                out["e_sink"] = list(round2.requests)

                # (B) invited reviewer, markable artifact
                state["peer"] = "192.0.2.10"
                b = Tab(cdp, board, f"{board}/r/{data_b['id']}/",
                        cookies=(("mr_session", reviewer["session"]), ("mr_csrf", reviewer["csrf"])),
                        clear_cookies=True, init=RECORD_MESSAGES)
                b.until("!!document.querySelector('#dz-film iframe.art-frame')", 60)
                b_fragment = f"/r/{data_b['id']}/rounds/1/"
                out["b_media"] = cdp.wait(lambda: _ready_media(cdp, rec, b_fragment),
                                           time.monotonic() + 30, 0.25)
                src = b.js("(document.querySelector('#dz-film iframe.art-frame')||{}).getAttribute('src') || ''")
                out["b_src"] = src
                out["b_reports"] = json.loads(b.js("JSON.stringify(window.__reports||[])"))
                out["b_doc"] = rec.response("/" + src.lstrip("/")) if src else None

                # (C) ART-2: an artifact navigates its own frame to another review's tokenless prototype page
                state["peer"] = "127.0.0.1"
                c = Tab(cdp, board, f"{board}/r/{attack['id']}/", clear_cookies=True, width=1280, height=1000)
                c.until("!!document.querySelector('.art-frame')", 60)
                target = f"/r/{plain['id']}/rounds/1/a/index.html"
                cdp.wait(lambda: rec.response(target) is not None, time.monotonic() + 20, 0.25)
                cdp.wait(lambda: False, time.monotonic() + 1.5)
                out["c_response"] = rec.response(target)
                out["c_sink"] = list(leak.requests)

                # (D) a plain review keeps the network, and the token never reaches a third party in the Referer
                state["peer"] = "192.0.2.10"
                d = Tab(cdp, board, f"{board}/r/{data_d['id']}/",
                        cookies=(("mr_session", guest_d["session"]), ("mr_csrf", guest_d["csrf"])),
                        clear_cookies=True)
                d.until("!!document.querySelector('.proto iframe')", 60)
                clicked = cdp.wait(lambda: _in_frame_containing(cdp, rec, f"/r/{data_d['id']}/rounds/1/",
                                                                "(function(){var a=document.getElementById('go');"
                                                                "if(!a)return false;a.click();return true;})()"),
                                   time.monotonic() + 20, 0.25)
                out["d_frames"] = [u for u, _ in rec.frames.values()]
                cdp.wait(lambda: cdn.seen, time.monotonic() + 20, 0.25)
                cdp.wait(lambda: False, time.monotonic() + 1.0)
                out["d_clicked"] = bool(clicked)
                out["d_src"] = d.js("(document.querySelector('.proto iframe')||{}).src || ''")
                out["d_page2"] = [{"url": u, **v} for u, v in rec.responses.items()
                                  if re.search(r"/r/%s/rounds/1/(?:~[^/]+/)?a/assets/page2\.html$" % re.escape(data_d["id"]),
                                               u.split("?")[0])]
                out["d_sink"] = list(cdn.seen)

                # (F) a round that arrives after boot: a state replacement must refresh S.frameTokens (defect 1)
                state["peer"] = "192.0.2.10"
                f = Tab(cdp, board, f"{board}/r/{data_f['id']}/",
                        cookies=(("mr_session", guest_f["session"]), ("mr_csrf", guest_f["csrf"])),
                        clear_cookies=True, init=RECORD_MESSAGES)
                f.until("!!document.querySelector('.proto iframe')", 60)
                out["f_before_src"] = f.js("(document.querySelector('.proto iframe')||{}).src || ''")
                store.add_round(data_f["id"], store.parse_round_spec(prototype=str(f2_page)))
                f.js("document.querySelector('#dzfoot .decide[data-id=proto] .s-approve').click()")
                f.until("(document.getElementById('proto-status')||{}).textContent === 'Approved'", 10)
                loads = lambda: sum(1 for r in rec.log if r["url"].split("?")[0].endswith("/state.json"))
                before = loads()
                f.js("document.querySelector('#dzfoot .btn-send').click()")
                out["f_refreshed"] = bool(cdp.wait(lambda: loads() > before, time.monotonic() + 20, 0.25))
                # The old round answers 409 and the board opens round 2 in the status banner without a full render;
                # a width change then renders from that refreshed state, which is the point of the shared helper.
                f.until("(document.body.innerText||'').indexOf('Go to round 2') >= 0", 20)
                f.js("document.querySelector('.dzbar .wseg .seg-btn[data-value=\"phone\"]').click()")
                out["f_tabs"] = f.until("document.querySelectorAll('#dzfoot .round-tab').length === 2 ? 2 : 0", 15)
                if out["f_tabs"] == 2:
                    f.js("Array.prototype.find.call(document.querySelectorAll('#dzfoot .round-tab'),"
                         "function(e){return e.textContent.trim()==='R2'}).click()")
                    f.until("document.querySelector('.proto iframe') && "
                            "document.querySelector('.proto iframe').src.indexOf('/rounds/2/') >= 0", 15)
                out["f_src"] = f.js("(document.querySelector('.proto iframe')||{}).src || ''")
                out["f_media"] = cdp.wait(lambda: _ready_media(cdp, rec, f"/r/{data_f['id']}/rounds/2/"),
                                          time.monotonic() + 30, 0.25)

                # (G) a session-bound token is not on a clock: minted with FRAME_TOKEN_SECONDS=0 before the board
                # reads state, it still opens its round after a board re-render (the width toggle rebuilds every
                # frame). A tree from before the frame-token feature leaves `g` empty, and the test fails on that.
                out["g"] = {}
                try:
                    from mockreview import frames as frames_mod
                except ImportError:
                    frames_mod = None
                if frames_mod is not None:
                    data_g = _proto_review(tmp, name="g")
                    made_g = users.share("test", data_g["id"], "create", "viewer")
                    guest_g = users.join_as_guest(users.resolve_share_token(token_of(made_g["path"])))
                    mp.setattr(frames_mod, "FRAME_TOKEN_SECONDS", 0)   # the token is born aged
                    state["peer"] = "192.0.2.10"
                    g = Tab(cdp, board, f"{board}/r/{data_g['id']}/",
                            cookies=(("mr_session", guest_g["session"]), ("mr_csrf", guest_g["csrf"])),
                            clear_cookies=True, init=RECORD_MESSAGES)
                    g.until("!!document.querySelector('.proto iframe')", 60)
                    g_fragment = f"/r/{data_g['id']}/rounds/1/"
                    out["g"]["before_media"] = cdp.wait(lambda: _ready_media(cdp, rec, g_fragment),
                                                        time.monotonic() + 30, 0.25)
                    out["g"]["before_tone"] = [v for u, v in rec.responses.items()
                                               if u.endswith("/assets/tone.wav") and g_fragment in u]
                    out["g"]["before_src"] = g.js("(document.querySelector('.proto iframe')||{}).src || ''")
                    time.sleep(1.2)                  # itsdangerous' timestamp check has 1 s granularity
                    mark = len(rec.history)
                    # The board's width toggle: S.width = 'phone' then keepPlace(render), a full re-render that
                    # rebuilds the prototype frame from S.frameTokens.
                    g.js("document.querySelector('.dzbar .wseg .seg-btn[data-value=\"phone\"]').click()")
                    out["g"]["after_still"] = cdp.wait(
                        lambda: next((r for r in rec.since(mark, "/assets/still.png") if r["status"] == 200), None),
                        time.monotonic() + 30, 0.25)
                    out["g"]["after_tone"] = cdp.wait(
                        lambda: next((r for r in rec.since(mark, "/assets/tone.wav") if r["status"] == 206), None),
                        time.monotonic() + 30, 0.25)
                    out["g"]["after_src"] = g.js("(document.querySelector('.proto iframe')||{}).src || ''")
            finally:
                ws.close()
        yield out
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        leak.close()
        round2.close()
        cdn.close()
        mp.undo()


@pytest.mark.browser
def test_a_guest_page_mode_prototype_loads_its_still_and_audio_with_the_token(frames_browser):
    assert frames_browser["tokens_a"], "state.json carried no frame_tokens for the guest"
    src = frames_browser["a_src"]
    assert re.search(r"/rounds/1/~[^/]+/a/index\.html$", src), src
    media = frames_browser["a_media"]
    assert media, ("the still and the tone never both loaded", frames_browser["a_reports"])
    assert media["img"] > 0, media
    assert math.isfinite(media["duration"]) and media["duration"] > 0, media
    assert media["seekable"] >= 1, media


@pytest.mark.browser
def test_the_audio_inside_the_guest_frame_was_served_as_a_206(frames_browser):
    tone = frames_browser["a_tone"]
    assert tone, "the audio file was never requested"
    assert any(r["status"] == 206 for r in tone), tone


@pytest.mark.browser
def test_b_an_invited_reviewer_artifact_frame_carries_a_token_and_its_page_loads(frames_browser):
    src = frames_browser["b_src"]
    assert "/~" in src, src
    media = frames_browser["b_media"]
    assert media, "the artifact page's still and tone never both loaded"
    assert media["img"] > 0, media
    assert math.isfinite(media["duration"]) and media["duration"] > 0, media
    doc = frames_browser["b_doc"]
    assert doc and doc["status"] == 200, doc
    assert doc["headers"].get("content-security-policy") == board_server.ARTIFACT_ROUND_CSP, doc


@pytest.mark.browser
def test_c_a_hostile_artifact_navigating_another_review_tokenless_page_gets_no_network(frames_browser):
    assert frames_browser["c_sink"] == [], frames_browser["c_sink"]
    got = frames_browser["c_response"]
    assert got and got["status"] == 200, got
    csp = got["headers"].get("content-security-policy", "")
    assert "connect-src 'none'" in csp and csp.startswith("sandbox allow-scripts; "), csp


@pytest.mark.browser
def test_d_a_plain_review_prototype_keeps_network_and_the_token_never_reaches_the_referer(frames_browser):
    assert frames_browser["d_clicked"], "the relative link was never clicked"
    seen = frames_browser["d_sink"]
    got = {"sink": seen,
           "page2": [{"url": x["url"][-70:], "status": x["status"]} for x in frames_browser["d_page2"]],
           "src": frames_browser["d_src"][-70:],
           "frames": [u[-70:] for u in frames_browser["d_frames"]]}
    assert [s for s in seen if s["path"].startswith("/cdn.js")], got
    for request in seen:
        assert "~" not in request["headers"].get("referer", ""), request


@pytest.mark.browser
def test_e_a_round_one_token_does_not_open_round_two(frames_browser):
    assert frames_browser["e_status"] == 401, frames_browser["e_status"]
    assert frames_browser["e_sink"] == [], frames_browser["e_sink"]


@pytest.mark.browser
def test_f_a_round_that_arrives_after_boot_gets_its_token_from_the_refreshed_state(frames_browser):
    assert frames_browser["f_before_src"] and "/~" in frames_browser["f_before_src"], frames_browser["f_before_src"]
    assert frames_browser["f_tabs"] == 2, ("the refreshed state did not render round 2",
                                             frames_browser.get("f_refreshed"))
    src = frames_browser["f_src"]
    assert f"/rounds/2/~" in src, src
    media = frames_browser["f_media"]
    assert media and media["img"] > 0, ("round 2's own image did not load", media)


@pytest.mark.browser
def test_g_a_session_token_aged_at_mint_still_loads_after_a_board_re_render(frames_browser):
    """A share-link guest's token minted with a zero clock keeps opening the round across a width-toggle re-render."""
    got = frames_browser["g"]
    assert got, "case G did not run: this tree has no frame tokens"
    media = got["before_media"]
    assert media and media["img"] > 0, ("the aged token's still did not load", media)
    assert math.isfinite(media["duration"]) and media["duration"] > 0, media
    assert any(r["status"] == 206 for r in got["before_tone"]), got["before_tone"]
    assert re.search(r"/rounds/1/~[^/]+/a/index\.html$", got["before_src"]), got["before_src"]
    still, tone = got["after_still"], got["after_tone"]
    assert still and still["status"] == 200, ("the re-rendered frame's still was not served", still)
    assert tone and tone["status"] == 206, ("the re-rendered frame's tone was not served", tone)
    assert re.search(r"/rounds/1/~[^/]+/a/index\.html$", got["after_src"]), got["after_src"]
