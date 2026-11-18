"""HTTP Range on round assets.

`rounds/<n>/<v>/assets/<file>` is the one place a plain mock's `<audio>` / `<video>` source comes from, so it has to
answer a byte range: without `Accept-Ranges: bytes` and a `206`, Chrome reports the media as non-seekable (the element
shows `00:00` and a seek snaps back) and Safari may refuse to play a video served from a round. These tests pin the
status and header matrix, the "read only the slice" boundary, and that a range header never widens the access gate.
"""
import json
import math
import struct

import pytest

import authkit
from authkit import peer  # noqa: F401
from conftest import http
from mockreview import config, store
from test_live_round_tool import Board, chrome  # noqa: F401

ORIGIN = {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}
BODY = bytes(range(256)) * 20          # 5120 deterministic bytes
SIZE = len(BODY)

DECISIONS = {"blocks": [{"type": "mock", "id": "m",
                         "html": '<audio controls src="rounds/1/a/assets/click.mp3"></audio>'}]}


def _media_review(tmp_path):
    """A decisions round whose assets/ holds a deterministic .mp3-named file and an empty one."""
    d = tmp_path / "dec"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "click.mp3").write_bytes(BODY)
    (d / "assets" / "empty.bin").write_bytes(b"")
    (d / "decisions.json").write_text(json.dumps(DECISIONS), encoding="utf-8")
    return store.create_review("Media range", "brief", ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(d / "decisions.json")))


def _asset_url(server, data, name="click.mp3"):
    return f"{server}/r/{data['id']}/rounds/1/a/assets/{name}"


def test_first_byte_range_is_a_206_with_only_that_slice(server, env, tmp_path):
    data = _media_review(tmp_path)
    url = _asset_url(server, data)
    code0, h0, body0 = http("GET", url)
    assert code0 == 200 and body0 == BODY and h0["Accept-Ranges"] == "bytes"
    code, h, body = http("GET", url, headers={"Range": "bytes=0-99"})
    assert code == 206
    assert h["Content-Range"] == f"bytes 0-99/{SIZE}"
    assert h["Content-Length"] == "100"
    assert h["Accept-Ranges"] == "bytes"
    assert body == BODY[:100]
    assert h["Content-Type"].startswith("audio/")
    assert h["Content-Security-Policy"] == h0["Content-Security-Policy"]
    assert h["Cache-Control"] == h0["Cache-Control"]


def test_open_ended_range_runs_to_the_last_byte(server, env, tmp_path):
    data = _media_review(tmp_path)
    code, h, body = http("GET", _asset_url(server, data), headers={"Range": "bytes=1234-"})
    assert code == 206
    assert h["Content-Range"] == f"bytes 1234-{SIZE - 1}/{SIZE}"
    assert h["Content-Length"] == str(SIZE - 1234)
    assert body == BODY[1234:]


def test_suffix_range_is_the_tail_of_the_file(server, env, tmp_path):
    data = _media_review(tmp_path)
    code, h, body = http("GET", _asset_url(server, data), headers={"Range": "bytes=-10"})
    assert code == 206
    assert h["Content-Range"] == f"bytes {SIZE - 10}-{SIZE - 1}/{SIZE}"
    assert h["Content-Length"] == "10"
    assert body == BODY[-10:]


def test_unsatisfiable_ranges_are_416_without_file_bytes(server, env, tmp_path):
    data = _media_review(tmp_path)
    url = _asset_url(server, data)
    for rng in (f"bytes={SIZE}-", "bytes=-0", f"bytes={SIZE}-{SIZE + 10}"):
        code, h, body = http("GET", url, headers={"Range": rng})
        assert code == 416, rng
        assert h["Content-Range"] == f"bytes */{SIZE}", rng
        assert h["Accept-Ranges"] == "bytes", rng
        assert body == b"" and BODY not in body, rng
    code, h, body = http("GET", _asset_url(server, data, "empty.bin"), headers={"Range": "bytes=0-0"})
    assert code == 416 and h["Content-Range"] == "bytes */0" and body == b""
    code, h, body = http("GET", _asset_url(server, data, "empty.bin"))
    assert code == 200 and h["Accept-Ranges"] == "bytes" and body == b""


def test_a_plain_get_advertises_byte_ranges(server, env, tmp_path):
    data = _media_review(tmp_path)
    code, h, body = http("GET", _asset_url(server, data))
    assert code == 200 and body == BODY and h["Accept-Ranges"] == "bytes"
    assert "Content-Range" not in h


def test_a_range_this_server_does_not_implement_falls_back_to_the_whole_file(server, env, tmp_path):
    data = _media_review(tmp_path)
    url = _asset_url(server, data)
    # `bytes=200-100` is an invalid spec (last byte before the first), not an unsatisfiable one: ignore it, like the
    # other headers this server does not implement, instead of refusing a request that could be answered in full.
    # A non-ASCII digit (superscript two / one) is not a digit this server can parse (HTTP 500 before this fix).
    for rng in ("bytes=0-9,20-29", "bytes=abc", "items=0-9", "bytes=200-100",
                "bytes=\u00b2-", "bytes=0-\u00b9"):
        code, h, body = http("GET", url, headers={"Range": rng})
        assert code == 200, rng
        assert body == BODY, rng
        assert "Content-Range" not in h, rng


def test_head_reports_the_range_headers_and_sends_no_body(server, env, tmp_path):
    data = _media_review(tmp_path)
    code, h, body = http("HEAD", _asset_url(server, data), headers={"Range": "bytes=0-99"})
    assert code == 206
    assert h["Content-Range"] == f"bytes 0-99/{SIZE}"
    assert h["Content-Length"] == "100"
    assert h["Accept-Ranges"] == "bytes"
    assert body == b""


def test_a_range_header_does_not_open_the_gate(server, env, tmp_path, peer):
    data = _media_review(tmp_path)
    url = _asset_url(server, data)
    plain = authkit.raw("GET", url)
    assert plain[0] in (401, 403)
    ranged = authkit.raw("GET", url, headers={"Range": "bytes=0-99"})
    assert plain[0] == ranged[0] and plain[2] == ranged[2]
    assert authkit.header(ranged[1], "Content-Range") == ""
    assert BODY not in ranged[2]


@pytest.mark.parametrize("rel", [
    "assets/../../../review.json",
    "assets/%2e%2e%2f%2e%2e%2f%2e%2e%2freview.json",
    "%2Fetc%2Fpasswd",
    ".hidden",
])
def test_path_traversal_is_still_refused_with_a_range_header(server, env, tmp_path, rel):
    data = _media_review(tmp_path)
    (config.data_root() / data["id"] / "rounds/1/a/.hidden").write_bytes(b"secret")
    url = f"{server}/r/{data['id']}/rounds/1/a/{rel}"
    plain = http("GET", url)
    ranged = http("GET", url, headers={"Range": "bytes=0-99"})
    assert plain[0] in (400, 404), rel
    assert ranged[0] == plain[0] and ranged[2] == plain[2], rel
    assert "Content-Range" not in ranged[1], rel
    assert BODY not in ranged[2] and b"secret" not in ranged[2], rel


def test_a_range_response_keeps_the_artifact_round_csp(server, env, tmp_path):
    d = tmp_path / "art"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "click.wav").write_bytes(BODY)
    (d / "decisions.json").write_text(json.dumps({"blocks": [
        {"type": "artifact", "id": "film", "html": "<p>art</p>"}]}), encoding="utf-8")
    data = store.create_review("Artifact range", "brief", ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(d / "decisions.json")))
    code, h, body = http("GET", _asset_url(server, data, "click.wav"), headers={"Range": "bytes=0-99"})
    assert code == 206
    assert h["Content-Security-Policy"] == "sandbox allow-scripts; " + store.ARTIFACT_CSP
    assert body == BODY[:100]


def _pcm_wav(seconds=5.0, rate=8000):
    """A real 8 kHz mono 16-bit PCM WAV, so Chrome can decode it and derive a duration."""
    frames = int(seconds * rate)
    data = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(frames))
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
              + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
              + b"data" + struct.pack("<I", len(data)))
    return header + data


PLAYER = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<audio id="a" controls preload="metadata" src="click.wav"></audio>
<script>
var a = document.getElementById('a'), p = {state: 'loading'};
window.__probe = function () { return JSON.stringify(p); };
a.addEventListener('loadedmetadata', function () {
  p = {state: 'metadata', duration: a.duration, seekable: a.seekable.length,
       seekableEnd: a.seekable.length ? a.seekable.end(0) : 0};
  a.currentTime = a.duration / 2;
});
a.addEventListener('seeked', function () { p.state = 'seeked'; p.after = a.currentTime; });
a.addEventListener('error', function () { p = {state: 'error', code: a.error && a.error.code}; });
setTimeout(function () { if (p.state !== 'seeked') { p.state = p.state === 'error' ? 'error' : 'timeout'; } }, 15000);
</script></body></html>"""


@pytest.mark.browser
def test_audio_on_a_round_asset_is_seekable(server, env, tmp_path, chrome):
    """The user-visible bug: duration was known but `seekable.end(0)` was 0 and a seek snapped back to 0."""
    d = tmp_path / "player"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "click.wav").write_bytes(_pcm_wav())
    (d / "assets" / "player.html").write_text(PLAYER, encoding="utf-8")
    (d / "decisions.json").write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Player", "brief", ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(d / "decisions.json")))
    b = Board(chrome, _asset_url(server, data, "player.html"), wait="typeof window.__probe === 'function'")
    assert b.until("(function(){var p=window.__probe&&JSON.parse(window.__probe());"
                   "return p&&(p.state==='seeked'||p.state==='error'||p.state==='timeout');})()", timeout=20)
    probe = json.loads(b.js("window.__probe()"))
    seen = json.dumps(probe)
    assert probe["state"] == "seeked", seen
    assert math.isfinite(probe["duration"]) and abs(probe["duration"] - 5) <= 0.1, seen
    assert probe["seekable"] >= 1, seen
    assert probe["seekableEnd"] >= probe["duration"] - 0.1, seen
    assert abs(probe["after"] - probe["duration"] / 2) <= 0.1, seen
