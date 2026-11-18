"""The share link's round pin on raw round files (card FIX-PIN).

`state.json`, threads, SSE, exports and frame-token minting already respect a viewer link's pinned round. A round
file is the one read that did not: a guest could fetch another round's `decisions.json`, page or media by typing the
path. These tests pin the HTTP answer of that route: a pinned guest gets its own round byte for byte, and any other
round answers exactly like a file that does not exist (no status, body or CSP oracle), whatever variant, asset,
Range header or frame token the request carries. A cookieless frame token speaks for the session it is bound to, so
the link's pin still applies: the token supplies identity, never a round. They also pin the read scope's fail-closed
path: review data that cannot be loaded never widens to the unrestricted round-1 view.
"""

import json

from authkit import account, peer, signed_in  # noqa: F401  (peer is a fixture)
from conftest import http
from mockreview import config, frames, store, threads, users
from test_share_dialog import add_rounds, join, state

MEDIA = bytes(range(256)) * 20          # 5120 deterministic bytes
ROUND_FILES = ("decisions.json", "index.html", "assets/media.bin")


def _url(server, rid, n, rel, variant="a"):
    return f"{server}/r/{rid}/rounds/{n}/{variant}/{rel}"


def _token_url(server, rid, n, token, rel, variant="a"):
    return f"{server}/r/{rid}/rounds/{n}/~{token}/{variant}/{rel}"


def _seed_round_files(review):
    """Every round gets the same three files, so only the bytes distinguish a hidden round from a visible one."""
    root = config.data_root() / review["id"] / "rounds"
    for n in (1, 2, 3):
        v = root / str(n) / "a"
        (v / "assets").mkdir(parents=True, exist_ok=True)
        (v / "index.html").write_text(f"<!doctype html><html><body>round {n}</body></html>", encoding="utf-8")
        (v / "decisions.json").write_text(
            json.dumps({"blocks": [{"type": "mock", "id": f"r{n}", "html": f"<p>round {n}</p>"}]}), encoding="utf-8")
        (v / "assets" / "media.bin").write_bytes(MEDIA)


def _pin(server, rid, n=2):
    """A viewer share link pinned to round `n`, and a guest joined through it."""
    link = users.share("test", rid, "create", "viewer")
    users.share("test", rid, "update", round=str(n))
    return join(server, link["path"])


def _fingerprint(response):
    """What must never differ between a hidden round and a missing file: status, body and the round-file CSP."""
    code, headers, body = response
    return code, body, headers.get("Content-Security-Policy")


def test_a_pinned_viewer_guest_reads_every_file_of_its_round(env, server, review, tmp_path, peer):
    add_rounds(review, tmp_path, 2)
    _seed_round_files(review)
    rid = review["id"]
    guest = _pin(server, rid, 2)
    headers = signed_in(guest, False)

    seen = state(server, rid, guest)
    assert [r["n"] for r in seen["rounds"]] == [2]

    for rel in ROUND_FILES:
        code, _, _ = http("GET", _url(server, rid, 2, rel), headers=headers)
        assert code == 200, rel
    code, hdrs, body = http("GET", _url(server, rid, 2, "assets/media.bin"),
                            headers=dict(headers, **{"Range": "bytes=0-99"}))
    assert code == 206 and body == MEDIA[:100]
    assert hdrs["Content-Range"] == f"bytes 0-99/{len(MEDIA)}"
    assert hdrs["Accept-Ranges"] == "bytes"

    token = seen["frame_tokens"]["2"]
    assert http("GET", _token_url(server, rid, 2, token, "index.html"), headers=headers)[0] == 200


def test_a_pinned_viewer_guest_cannot_read_another_round(env, server, review, tmp_path, peer):
    add_rounds(review, tmp_path, 2)
    _seed_round_files(review)
    rid = review["id"]
    guest = _pin(server, rid, 2)
    headers = signed_in(guest, False)

    missing = _fingerprint(http("GET", _url(server, rid, 2, "assets/no-such-file.bin"), headers=headers))
    assert missing[0] == 404

    for n in (1, 3):
        for rel in ROUND_FILES:
            hidden = _fingerprint(http("GET", _url(server, rid, n, rel), headers=headers))
            assert hidden == missing, (n, rel)
    ranged = http("GET", _url(server, rid, 1, "assets/media.bin"), headers=dict(headers, **{"Range": "bytes=0-99"}))
    assert _fingerprint(ranged) == missing
    assert MEDIA[:100] not in ranged[2]

    # A token minted for an unpinned round still grants only identity: the link's pin is enforced on the path.
    leaked = frames.mint(rid, 1, users.digest(guest["session"]))
    for rel in ("index.html", "assets/media.bin"):
        hidden = _fingerprint(http("GET", _token_url(server, rid, 1, leaked, rel), headers=headers))
        assert hidden == missing, rel


def test_an_unpinned_link_a_member_and_the_owner_read_every_round(env, server, review, tmp_path, peer):
    add_rounds(review, tmp_path, 2)
    _seed_round_files(review)
    rid = review["id"]
    link = users.share("test", rid, "create", "viewer")
    users.share("test", rid, "update", round="2")       # a pin that must only bind viewer guests

    member = account(review_id=rid, role="viewer", name="Vera")
    for n in (1, 2, 3):
        for rel in ROUND_FILES:
            assert http("GET", _url(server, rid, n, rel), headers=signed_in(member, False))[0] == 200, (n, rel)

    peer["addr"] = "127.0.0.1"                          # the loopback owner, no session or link
    for n in (1, 2, 3):
        for rel in ROUND_FILES:
            assert http("GET", _url(server, rid, n, rel))[0] == 200, (n, rel)
    peer["addr"] = "192.0.2.10"

    users.share("test", rid, "update", round="latest")  # an unpinned link is not a restriction
    guest = join(server, link["path"])
    for n in (1, 2, 3):
        for rel in ROUND_FILES:
            assert http("GET", _url(server, rid, n, rel), headers=signed_in(guest, False))[0] == 200, (n, rel)


def test_a_load_failure_never_widens_a_pinned_viewer_scope(env, server, review, tmp_path, peer, monkeypatch):
    add_rounds(review, tmp_path, 2)
    _seed_round_files(review)
    rid = review["id"]
    guest = _pin(server, rid, 2)

    def boom(_review_id):
        raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr(store, "load", boom)

    scope = threads.view_scope(rid, guest=True, role="viewer")
    assert scope.restricted is True and scope.rounds == frozenset()
    assert threads.sees_round(scope, 1) is False and threads.sees_round(scope, "2") is False

    code, _, _ = http("GET", _url(server, rid, 2, "index.html"), headers=signed_in(guest, False))
    assert code == 404


def test_a_one_round_review_pinned_to_that_round_still_reports_the_pin(env, server, review, peer):
    """Catches a state.json that only reports `pinned_round` when the pin actually hides another round: the link is
    pinned either way, and the board reads "This link shows round N." from that field."""
    rid = review["id"]
    link = users.share("test", rid, "create", "viewer")
    users.share("test", rid, "update", round="1")        # this review has exactly one round
    guest = join(server, link["path"])

    seen = state(server, rid, guest)
    assert [r["n"] for r in seen["rounds"]] == [1]
    assert seen["current_round"] == 1 and seen["pinned_round"] == 1


def test_a_cookieless_stale_frame_token_after_a_re_pin_is_dead(env, server, review, tmp_path, peer):
    """A token minted while the link was `latest` dies with its binding, but it also cannot outlive the link's pin:
    the cookieless request is the token's guest, so the re-pinned round 1 is hidden from it."""
    add_rounds(review, tmp_path, 2)
    _seed_round_files(review)
    rid = review["id"]
    link = users.share("test", rid, "create", "viewer")          # left on `latest`
    guest = join(server, link["path"])
    tokens = state(server, rid, guest)["frame_tokens"]           # taken while the link was `latest`
    users.share("test", rid, "update", round="2")                # the owner re-pins the link

    missing = _fingerprint(http("GET", _token_url(server, rid, 2, tokens["2"], "assets/no-such-file.bin")))
    assert missing[0] == 404

    hidden = _fingerprint(http("GET", _token_url(server, rid, 1, tokens["1"], "decisions.json")))
    assert hidden == missing
    assert b"round 1" not in hidden[1]


def test_a_cookieless_range_on_a_hidden_round_is_dead(env, server, review, tmp_path, peer):
    """A Range request on a hidden round's media answers 404 like a missing file, never a 206 of the bytes."""
    add_rounds(review, tmp_path, 2)
    _seed_round_files(review)
    rid = review["id"]
    guest = _pin(server, rid, 2)
    pinned_token = state(server, rid, guest)["frame_tokens"]["2"]
    leaked = frames.mint(rid, 1, users.digest(guest["session"]))

    missing = _fingerprint(http("GET", _token_url(server, rid, 2, pinned_token, "assets/no-such-file.bin")))
    assert missing[0] == 404

    hidden = http("GET", _token_url(server, rid, 1, leaked, "assets/media.bin"),
                  headers={"Range": "bytes=0-99"})
    assert _fingerprint(hidden) == missing
    assert MEDIA[:100] not in hidden[2]


def test_a_cookieless_pinned_frame_token_still_loads(env, server, review, tmp_path, peer):
    """The control: a cookieless token for the round the link itself is pinned to keeps working."""
    add_rounds(review, tmp_path, 2)
    _seed_round_files(review)
    rid = review["id"]
    guest = _pin(server, rid, 2)
    token = state(server, rid, guest)["frame_tokens"]["2"]

    assert http("GET", _token_url(server, rid, 2, token, "index.html"))[0] == 200
