import json

import pytest

from conftest import FEEDBACK, http
from mockreview import config, pages, store
from mockreview.store import ReviewError


@pytest.mark.parametrize("bad", ["..", "../etc", "a/b", "A", "", "-x", "x-", "a" * 80, "x\x00y", "%2e%2e", "r.json"])
def test_review_id_validation(env, bad):
    with pytest.raises(ReviewError):
        store.review_dir(bad)


@pytest.mark.parametrize("rel", ["../../review.json", "../b/index.html", "/etc/passwd", ".lock", "a/../../x",
                                 "assets/../../../review.json", "x\x00.html", "..\\..\\review.json"])
def test_variant_file_traversal_rejected(review, rel):
    with pytest.raises(ReviewError):
        store.safe_variant_file(review["id"], "1", "a", rel)


def test_variant_file_symlink_escape_rejected(review):
    d = config.data_root() / review["id"] / "rounds/1/a"
    (d / "evil.html").symlink_to(config.data_root() / review["id"] / "review.json")
    with pytest.raises(ReviewError):
        store.safe_variant_file(review["id"], "1", "a", "evil.html")


@pytest.mark.parametrize("path", [
    "/r/../etc/passwd",
    "/r/%2e%2e/review.json",
    "/r/{id}/rounds/1/a/..%2f..%2f..%2freview.json",
    "/r/{id}/rounds/1/a/%2e%2e/%2e%2e/%2e%2e/review.json",
    "/r/{id}/rounds/1/../../review.json",
    "/r/{id}/rounds/1/e/index.html",
    "/r/{id}/rounds/x/a/index.html",
    "/r/{id}/review.json",
    "/r/{id}/.lock",
])
def test_http_traversal_returns_404(server, review, path):
    code, _, body = http("GET", server + path.format(id=review["id"]))
    assert code in (400, 404), (path, code)
    assert b"token" not in body and b"thread_id" not in body


def test_board_and_variant_headers(server, review):
    code, h, body = http("GET", f"{server}/r/{review['id']}/")
    assert code == 200 and "frame-ancestors 'none'" in h["Content-Security-Policy"]
    code, h, body = http("GET", f"{server}/r/{review['id']}/rounds/1/a/index.html")
    csp = h["Content-Security-Policy"]
    # A round document without a token has no network: the artifact-grade policy plus its own round's files.
    assert code == 200 and csp.startswith("sandbox allow-scripts; ") and "connect-src 'none'" in csp
    assert csp != "sandbox allow-scripts"
    assert b"<h1>alpha</h1>" in body
    code, h, _ = http("GET", f"{server}/r/{review['id']}/rounds/1/a/assets/logo.svg")
    assert code == 200 and h["Content-Type"].startswith("image/svg+xml")
    assert h["Content-Security-Policy"] == csp
    # The token the board mints is the prototype policy: a frame document and its relative files keep the network.
    state = json.loads(http("GET", f"{server}/r/{review['id']}/state.json")[2])
    tok = state["frame_tokens"]["1"]
    code, h, body = http("GET", f"{server}/r/{review['id']}/rounds/1/~{tok}/a/index.html")
    assert code == 200 and h["Content-Security-Policy"] == "sandbox allow-scripts"
    assert b"<h1>alpha</h1>" in body
    code, h, _ = http("GET", f"{server}/r/{review['id']}")
    assert code in (200, 301)


def test_state_json_has_no_thread_id(server, review):
    code, _, body = http("GET", f"{server}/r/{review['id']}/state.json")
    st = json.loads(body)
    assert code == 200 and st["id"] == review["id"] and "origin" not in st
    assert b"1490000000000000001" not in body


def test_body_size_capped(server, review):
    """A huge Content-Length is refused with 413 before any body is read."""
    import socket
    host, port = server.replace("http://", "").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.sendall((f"POST /r/{review['id']}/api/submit HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {config.MAX_BODY + 1}\r\n\r\n").encode() + b"{")
        resp = b""
        while chunk := s.recv(4096):
            resp += chunk
    assert resp.startswith(b"HTTP/1.1 413") and b"2 MB" in resp
    # just under the cap is read and parsed normally (then rejected as bad feedback, not as too large)
    body = b'{"round":1,"token":"x","feedback":{"guidelines":"' + b"a" * (config.MAX_BODY - 200) + b'"}}'
    code, _, _ = http("POST", f"{server}/r/{review['id']}/api/submit", body)
    assert code == 403


def test_content_type_and_null_origin(server, review):
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _, _ = http("POST", f"{server}/r/{review['id']}/api/submit", json.dumps(body).encode(),
                      {"Content-Type": "text/plain"})
    assert code == 415
    code, _, _ = http("POST", f"{server}/r/{review['id']}/api/submit", body, {"Origin": "null"})
    assert code == 403


def test_index_escapes_titles(env, server, variants):
    specs = [store.parse_variant_spec(variants[0])]
    store.create_review("<script>alert(1)</script>", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, "tailscale", specs)
    code, _, body = http("GET", server + "/")
    assert code == 200 and b"<script>alert(1)" not in body and b"&lt;script&gt;" in body


def test_server_refuses_public_bind():
    """Loopback by default; 0.0.0.0 only as the explicit local-network opt-in; any other address refused."""
    from mockreview import server as srv
    for bad in ("192.168.1.5", "100.64.0.1", "example.com", "::"):
        with pytest.raises(SystemExit):
            srv.make_server(bad, 0)
    s = srv.make_server("0.0.0.0", 0)  # opt-in: share local links with the network
    try:
        assert s.server_address[0] == "0.0.0.0"
    finally:
        s.server_close()


def test_local_mode_links(env, tmp_path, monkeypatch):
    from mockreview import api, config, store
    assert api.resolve_mode("local") == "local" and api.resolve_mode("lan") == "local"
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "local")
    assert api.resolve_mode(None) == "local"
    data = {"id": "x-123abc", "mode": "local"}
    # loopback bind: a local link can only mean this computer
    monkeypatch.setenv("MOCKREVIEW_HOST", "127.0.0.1")
    assert store.review_url(data) == f"http://127.0.0.1:{config.port()}/r/x-123abc"
    # network bind: the computer's own detected address
    monkeypatch.setenv("MOCKREVIEW_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "detect_lan_ip", lambda: "10.0.0.42")
    assert store.review_url(data) == f"http://10.0.0.42:{config.port()}/r/x-123abc"
    assert config.local_base().startswith("http://127.0.0.1:")  # the CLI still talks to it over loopback
    # explicit address wins
    monkeypatch.setenv("MOCKREVIEW_LAN_ADDRESS", "review-box.example.test")
    assert store.review_url(data) == f"http://review-box.example.test:{config.port()}/r/x-123abc"
    # tailnet links unchanged
    assert store.review_url({"id": "x-123abc", "mode": "tailscale"}).startswith(config.public_base())


# ---------------------------------------------------------------- artifact pages

def _artifact_review(tmp_path, html="<p id=x>art</p><script>1</script>"):
    f = tmp_path / "adec" / "decisions.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "film", "html": html}]}), encoding="utf-8")
    return store.create_review("art", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"},
                               "tailscale", store.parse_round_spec(decisions=str(f)))


def test_artifact_served_sandboxed(server, env, tmp_path):
    data = _artifact_review(tmp_path)
    url = f"{server}/r/{data['id']}/rounds/1/a/blocks/film.html"
    code, h, body = http("GET", url)
    assert code == 200 and h["Content-Security-Policy"] == "sandbox allow-scripts; " + store.ARTIFACT_CSP    # sandbox, plus no network
    assert "connect-src 'none'" in h["Content-Security-Policy"] and "frame-src 'none'" in h["Content-Security-Policy"]
    assert h["Content-Type"].startswith("text/html") and h.get("Cache-Control") == "no-cache"
    assert b"<p id=x>art</p>" in body and b"connect-src 'none'" in body                 # and the page's own meta CSP
    assert b"allow-same-origin" not in h["Content-Security-Policy"].encode()


def test_every_file_of_a_round_with_an_artifact_carries_the_artifact_header(server, env, tmp_path):
    """A page can navigate its own frame to any other file of the round; that document must have no network either."""
    d = tmp_path / "dec"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "escape.html").write_text("<script>fetch('http://example.test/x')</script>", encoding="utf-8")
    (d / "assets" / "click.wav").write_bytes(b"RIFF....WAVE")
    (d / "decisions.json").write_text(json.dumps({"blocks": [
        {"type": "artifact", "id": "film", "html": "<p>art</p>"}, {"type": "mock", "id": "m", "html": "<p>m</p>"}]}), encoding="utf-8")
    data = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"},
                               "tailscale", store.parse_round_spec(decisions=str(d / "decisions.json")))
    base = f"{server}/r/{data['id']}/rounds/1/a"
    want = "sandbox allow-scripts; " + store.ARTIFACT_CSP
    for rel in ("blocks/film.html", "assets/escape.html", "assets/click.wav", "decisions.json"):
        code, h, _ = http("GET", f"{base}/{rel}")
        assert code == 200 and h["Content-Security-Policy"] == want, rel
    assert "connect-src 'none'" in want and "allow-same-origin" not in want


def test_a_round_without_an_artifact_without_a_token_has_no_network_and_with_one_keeps_the_plain_header(server, env, tmp_path):
    d = tmp_path / "dec"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "click.wav").write_bytes(b"RIFF....WAVE")
    (d / "assets" / "page.html").write_text("<p>p</p>", encoding="utf-8")
    (d / "decisions.json").write_text(json.dumps({"blocks": [{"type": "mock", "id": "m", "html": "<p>m</p>"}]}), encoding="utf-8")
    data = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"},
                               "tailscale", store.parse_round_spec(decisions=str(d / "decisions.json")))
    base = f"{server}/r/{data['id']}/rounds/1/a"
    for rel in ("assets/click.wav", "assets/page.html", "decisions.json"):
        h = http("GET", f"{base}/{rel}")[1]
        assert h["Content-Security-Policy"].startswith("sandbox allow-scripts; "), rel
        assert "connect-src 'none'" in h["Content-Security-Policy"], rel
    tok = json.loads(http("GET", f"{server}/r/{data['id']}/state.json")[2])["frame_tokens"]["1"]
    token_base = f"{server}/r/{data['id']}/rounds/1/~{tok}/a"
    for rel in ("assets/click.wav", "assets/page.html", "decisions.json"):
        h = http("GET", f"{token_base}/{rel}")[1]
        assert h["Content-Security-Policy"] == "sandbox allow-scripts", rel


def test_a_prototype_round_in_a_mixed_review_has_no_network_but_loads_its_own_files(server, env, tmp_path):
    proto = tmp_path / "proto" / "index.html"
    proto.parent.mkdir()
    proto.write_text("<p>proto</p><img src=\"assets/still.png\">", encoding="utf-8")
    (proto.parent / "assets").mkdir()
    (proto.parent / "assets" / "still.png").write_bytes(b"PNG")
    data = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001",
                                        "thread_id": "1490000000000000001"}, "tailscale",
                               store.parse_round_spec(prototype=str(proto)))
    f = tmp_path / "dec" / "decisions.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "film", "html": "<p>art</p>"}]}), encoding="utf-8")
    store.add_round(data["id"], store.parse_round_spec(decisions=str(f)))
    code, h, _ = http("GET", f"{server}/r/{data['id']}/rounds/1/a/index.html")
    csp = h["Content-Security-Policy"]
    host = server.replace("http://", "")
    own = f"http://{host}/r/{data['id']}/rounds/1/a/ https://{host}/r/{data['id']}/rounds/1/a/"
    assert code == 200 and csp.startswith("sandbox allow-scripts; ") and "connect-src 'none'" in csp
    assert "img-src " + own + " data:" in csp and "media-src " + own + " data: blob:" in csp
    assert "script-src " + own + " 'unsafe-inline'" in csp and "style-src " + own + " 'unsafe-inline'" in csp
    assert "font-src " + own + " data:" in csp
    # the artifact's own round keeps the exact artifact policy: its page is fully bundled
    code, h, _ = http("GET", f"{server}/r/{data['id']}/rounds/2/a/blocks/film.html")
    assert h["Content-Security-Policy"] == "sandbox allow-scripts; " + store.ARTIFACT_CSP


def test_a_symlinked_or_empty_blocks_folder_does_not_switch_the_header(server, env, tmp_path):
    """The header switch reads the round's own folder; planting a `blocks` symlink or page there changes nothing."""
    d = tmp_path / "dec"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "page.html").write_text("<p>p</p>", encoding="utf-8")
    (d / "decisions.json").write_text(json.dumps({"blocks": [{"type": "mock", "id": "m", "html": "<p>m</p>"}]}), encoding="utf-8")
    data = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"},
                               "tailscale", store.parse_round_spec(decisions=str(d / "decisions.json")))
    base = config.data_root() / data["id"] / "rounds/1/a"
    (base / "blocks").mkdir()
    assert not store.variant_has_artifact(data["id"], "1", "a")                        # empty folder
    (base / "blocks" / "x.html").symlink_to(base / "decisions.json")
    assert not store.variant_has_artifact(data["id"], "1", "a")                        # a symlinked page
    (base / "blocks" / "x.html").unlink()
    (base / "blocks").rmdir()
    (base / "blocks").symlink_to(tmp_path / "dec" / "assets")
    assert not store.variant_has_artifact(data["id"], "1", "a")                        # a symlinked folder
    assert not store.variant_has_artifact(data["id"], "1", "../a") and not store.variant_has_artifact(data["id"], "x", "a")
    csp = http("GET", f"{server}/r/{data['id']}/rounds/1/a/assets/page.html")[1]["Content-Security-Policy"]
    assert csp.startswith("sandbox allow-scripts; ") and "connect-src 'none'" in csp


def test_artifact_files_are_not_a_way_around_the_variant_rules(server, env, tmp_path):
    data = _artifact_review(tmp_path)
    base = f"{server}/r/{data['id']}/rounds/1/a"
    for rel in ("blocks/../../../review.json", "blocks/%2e%2e/%2e%2e/%2e%2e/review.json", "blocks/..%2f..%2f..%2freview.json",
                "blocks/.hidden.html", "blocks/missing.html", "blocks/film.html/x", "decisions.json/../../review.json"):
        code, _, body = http("GET", f"{base}/{rel}")
        assert code in (400, 404) and b"token" not in body, rel
    # a symlink planted inside blocks/ that points at the review's own secrets is refused by the resolver
    d = config.data_root() / data["id"] / "rounds/1/a/blocks"
    (d / "evil.html").symlink_to(config.data_root() / data["id"] / "review.json")
    with pytest.raises(ReviewError):
        store.safe_variant_file(data["id"], "1", "a", "blocks/evil.html")
    code, _, body = http("GET", f"{base}/blocks/evil.html")
    assert code == 404 and b"token" not in body


def test_a_missing_round_file_is_the_dead_page_with_the_artifact_header(server, env, tmp_path):
    """A dead end under a round of a mixed review is sandboxed and has no network; elsewhere it keeps the plain page."""
    data = _artifact_review(tmp_path)
    for rel in ("assets/missing.png", "blocks/missing.html"):
        code, h, body = http("GET", f"{server}/r/{data['id']}/rounds/1/a/{rel}")
        assert code == 404 and body == pages.DEAD
        assert h["Content-Security-Policy"] == "sandbox allow-scripts; " + store.ARTIFACT_CSP
    d = tmp_path / "plain"
    (d / "assets").mkdir(parents=True)
    (d / "decisions.json").write_text(json.dumps({"blocks": [{"type": "mock", "id": "m", "html": "<p>m</p>"}]}),
                                      encoding="utf-8")
    plain = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001",
                                          "thread_id": "1490000000000000001"}, "tailscale",
                                store.parse_round_spec(decisions=str(d / "decisions.json")))
    code, h, body = http("GET", f"{server}/r/{plain['id']}/rounds/1/a/assets/missing.png")
    # The header is chosen from the path shape alone, never from the review content, so missing and forbidden cannot
    # differ: even a plain review's dead end under rounds/<n>/<v>/ carries the artifact-grade policy.
    assert code == 404 and body == pages.DEAD and h["Content-Security-Policy"] == "sandbox allow-scripts; " + store.ARTIFACT_CSP
    # a 404 outside a round keeps its plain-text body and gains no artifact policy
    code, h, body = http("GET", f"{server}/r/{data['id']}/nope")
    assert code == 404 and body == b"404 not found\n" and "Content-Security-Policy" not in h


def test_artifact_page_cannot_close_its_csp_meta_or_inject_a_looser_one(env, tmp_path):
    data = _artifact_review(tmp_path, '<meta http-equiv="Content-Security-Policy" content="default-src *"><p>x</p>')
    served = (config.data_root() / data["id"] / "rounds/1/a/blocks/film.html").read_text(encoding="utf-8")
    # ours comes first; a second, looser policy in the page can only add restrictions (policies intersect)
    assert served.index("default-src 'none'") < served.index("default-src *")


def test_decisions_sibling_assets_are_served_same_origin_for_plain_mock_media(server, env, tmp_path):
    """docs/decisions-format.md, "Media in a plain mock": rounds/<n>/a/assets/<file> next to decisions.json."""
    d = tmp_path / "dec"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "click.wav").write_bytes(b"RIFF....WAVE")
    (d / "decisions.json").write_text(json.dumps({"blocks": [{"type": "mock", "id": "m", "html": "<audio controls src=\"rounds/1/a/assets/click.wav\"></audio>"}]}), encoding="utf-8")
    data = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"},
                               "tailscale", store.parse_round_spec(decisions=str(d / "decisions.json")))
    code, h, body = http("GET", f"{server}/r/{data['id']}/rounds/1/a/assets/click.wav")
    assert code == 200 and h["Content-Type"].startswith("audio/") and body == b"RIFF....WAVE"
