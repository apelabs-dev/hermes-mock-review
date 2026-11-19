"""Round files are one review's files, not the board origin's public shelf.

SECAUDIT cand_dca1bfe61e26a35ce41d589f: a prototype in review A could load review B's round scripts and
styles, because every round file answered the same way whatever asked for it. A round file now carries
`Cross-Origin-Resource-Policy`, and the only thing that renders a prototype or an artifact - a sandboxed frame - has
an opaque origin:

- a file under this review's and this round's token is `cross-origin` (the frame's own origin is opaque, so media,
  fonts and `rounds/<n>/a/assets/` keep loading, and a Range request still answers 206);
- a file with no token for this review and round is `same-origin`, and a cross-site request for it is a dead end,
  so another review's prototype (or an outside site) cannot read it.

The board's own document is same-origin with the server, so a direct view of a round keeps working.
"""

import base64
import json
import time

from authkit import account, peer, signed_in  # noqa: F401 - fixtures
from conftest import http
from mockreview import frames, server as board_server, store

ORIGIN = {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="assets/style.css">
<script src="assets/script.js"></script></head><body><img id="still" alt="still" src="assets/still.png"></body></html>"""


def _tree(tmp_path, name):
    d = tmp_path / name
    (d / "assets").mkdir(parents=True, exist_ok=True)
    (d / "assets" / "still.png").write_bytes(PNG)
    (d / "assets" / "style.css").write_text("body{color:#111}", encoding="utf-8")
    (d / "assets" / "script.js").write_text("window.mock = 1;", encoding="utf-8")
    (d / "index.html").write_text(PAGE, encoding="utf-8")
    return d / "index.html"


def _review(tmp_path, name):
    return store.create_review("Round files", "", ORIGIN, "tailscale",
                               store.parse_round_spec(prototype=str(_tree(tmp_path, name))))


def _tokens(base, rid, sess):
    code, _, body = http("GET", f"{base}/r/{rid}/state.json", headers=signed_in(sess, False))
    assert code == 200, body
    return json.loads(body)["frame_tokens"]


FRAME = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "script", "Sec-Fetch-Mode": "no-cors"}


def test_a_tokened_round_file_stays_loadable_from_the_sandboxed_frame(env, server, tmp_path):
    data = _review(tmp_path, "one")
    member = account(review_id=data["id"], role="reviewer")
    tok = _tokens(server, data["id"], member)["1"]

    for rel, ctype in (("index.html", "text/html"), ("assets/style.css", "text/css"),
                       ("assets/script.js", "javascript"), ("assets/still.png", "image/png")):
        code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/{rel}", headers=FRAME)
        assert code == 200, (rel, code)
        assert headers["Cross-Origin-Resource-Policy"] == "cross-origin", rel   # the frame's origin is opaque
        assert ctype in headers["Content-Type"], rel

    # media keeps its byte ranges: the timeline and the scrubber depend on the 206
    code, headers, body = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/assets/still.png",
                               headers={"Range": "bytes=0-7", **FRAME})
    assert code == 206 and len(body) == 8 and headers["Accept-Ranges"] == "bytes"
    assert headers["Cross-Origin-Resource-Policy"] == "cross-origin"


def test_another_review_cannot_read_a_round_file_without_its_own_token(env, server, tmp_path):
    one = _review(tmp_path, "one")
    two = _review(tmp_path, "two")
    member = account(all_reviews=True, role="reviewer")
    tok_one = _tokens(server, one["id"], member)["1"]

    # review A's prototype has only A's token: it can name B's path but not B's credential
    code, headers, body = http("GET", f"{server}/r/{two['id']}/rounds/1/a/assets/script.js", headers=FRAME)
    assert code == 404 and "window.mock" not in body.decode("utf-8", "replace")
    assert headers.get("Cross-Origin-Resource-Policy") in (None, "same-origin")
    code, _, _ = http("GET", f"{server}/r/{two['id']}/rounds/1/~{tok_one}/a/assets/script.js", headers=FRAME)
    assert code in (401, 404)          # a token for A, on B's round: never a grant


def test_an_untokened_round_file_is_same_origin_only(env, server, tmp_path):
    data = _review(tmp_path, "one")
    member = account(review_id=data["id"], role="reviewer")
    tok = _tokens(server, data["id"], member)["1"]

    # a document of the board's own origin (a direct view, the export capture) still reads the round
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/a/assets/style.css",
                            headers={"Sec-Fetch-Site": "same-origin"})
    assert code == 200 and headers["Cross-Origin-Resource-Policy"] == "same-origin"

    # a sandboxed frame without a token for this round gets the dead end, exactly like a missing file
    code, headers, body = http("GET", f"{server}/r/{data['id']}/rounds/1/a/assets/style.css", headers=FRAME)
    assert code == 404 and b"#111" not in body          # the stylesheet itself never comes back
    assert "frame_tokens" not in json.dumps(dict(headers))

    # and the same path with its own token is the file (the frame's normal URL)
    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/assets/style.css", headers=FRAME)
    assert code == 200 and headers["Cross-Origin-Resource-Policy"] == "cross-origin"


def test_round_media_still_loads_for_a_page_of_the_same_review(env, server, tmp_path):
    """The card's risk: a round page served with the `sandbox` CSP has an opaque origin even as a top-level document,
    so its own `<img>`/`<audio>` requests are cross-site. They must keep loading, untokened and tokened alike."""
    data = _review(tmp_path, "one")
    member = account(review_id=data["id"], role="reviewer")
    tok = _tokens(server, data["id"], member)["1"]

    for url in (f"{server}/r/{data['id']}/rounds/1/a/assets/still.png",
                f"{server}/r/{data['id']}/rounds/1/~{tok}/a/assets/still.png"):
        code, headers, body = http("GET", url, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "image"})
        assert code == 200 and body == PNG
        assert headers["Cross-Origin-Resource-Policy"] == "cross-origin"


def _local_token(server, rid, peer):
    peer["addr"] = "127.0.0.1"
    code, _, body = http("GET", f"{server}/r/{rid}/state.json")
    assert code == 200, body
    return json.loads(body)["frame_tokens"]["1"]


def _expire(monkeypatch):
    monkeypatch.setattr(frames, "FRAME_TOKEN_SECONDS", 0)
    time.sleep(1.2)                                  # itsdangerous' timestamp check has 1 s granularity


def test_an_expired_frame_token_keeps_the_frames_own_styles_and_scripts_loadable(env, server, tmp_path, peer,
                                                                                 monkeypatch):
    """A `local` token past its two-hour clock no longer authorises anything, but it is still signed for this review
    and round: the frame's own derived requests must not become cross-site dead ends."""
    data = _review(tmp_path, "one")
    tok = _local_token(server, data["id"], peer)
    _expire(monkeypatch)

    for rel, dest in (("assets/script.js", "script"), ("assets/style.css", "style")):
        code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/{rel}",
                                headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": dest})
        assert code == 200, (rel, code)
        assert headers["Cross-Origin-Resource-Policy"] == "cross-origin", rel

    code, headers, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/~{tok}/a/index.html",
                            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "document"})
    assert code == 200
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Content-Security-Policy"] != board_server.VARIANT_CSP


def test_an_expired_frame_token_stays_inside_its_own_review_and_round(env, server, tmp_path, peer, monkeypatch):
    one = _review(tmp_path, "one")
    two = _review(tmp_path, "two")
    tok = _local_token(server, one["id"], peer)
    _expire(monkeypatch)

    # A tamper must change payload bytes: base64's trailing padding bits are ignored, so replacing the token's last
    # character can decode to the same signature and leave the token valid.
    i = 4
    tampered = tok[:i] + ("A" if tok[i] != "A" else "B") + tok[i + 1:]
    code, _, _ = http("GET", f"{server}/r/{one['id']}/rounds/1/~{tampered}/a/assets/script.js", headers=FRAME)
    assert code == 404

    code, _, _ = http("GET", f"{server}/r/{two['id']}/rounds/1/~{tok}/a/assets/script.js", headers=FRAME)
    assert code == 404
