"""The three export routes: access, toggles, audit rows and the static capture/print files."""

import hashlib
import json
import urllib.parse

from authkit import account, peer, raw, signed_in, token_of  # noqa: F401
from conftest import http
from mockreview import export, server as board_server, store, threads, users
from test_share_dialog import add_rounds, join, state

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}

DECISIONS = {
    "blocks": [
        {"type": "text", "title": "Intro", "text": "Read me."},
        {"type": "mock", "id": "m1", "html": "<p>mock</p>"},
    ]
}


def new_review(tmp_path, title="Export review"):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    return store.create_review(title, "brief", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(path)), wake=False)


def cookies(pairs):
    out = {}
    for k, v in pairs:
        if k.lower() == "set-cookie":
            name, _, rest = v.partition("=")
            out[name.strip()] = rest.split(";", 1)[0]
    return out


def latest_export():
    rows = [r for r in users.audit_rows(50) if r["action"] == "export"]
    return rows[0] if rows else None


def test_owner_gets_an_attachment_with_the_export_csp(server):
    review = new_review(store.config.data_root().parent)
    status, headers, body = http("GET", f"{server}/r/{review['id']}/export.html")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Disposition"].startswith("attachment; filename=")
    assert headers["Content-Disposition"].endswith('.html"')
    assert headers["Content-Security-Policy"] == export.EXPORT_CSP
    assert headers["Cache-Control"] == "no-store"
    assert body.startswith(b"<!doctype html>")


def test_a_member_viewer_and_a_guest_viewer_can_export(server, review, peer):
    member = account(review_id=review["id"], role="viewer", name="Vera")
    status, headers, body = http("GET", server + "/r/{}/export.html".format(review["id"]),
                                 headers=signed_in(member, False))
    assert status == 200 and b"<!doctype html>" in body
    assert headers["Content-Disposition"].startswith("attachment")

    peer["addr"] = "192.0.2.10"
    link = users.share("test", review["id"], "create", "viewer")
    status, pairs, _ = raw("GET", server + link["path"])
    assert status == 303
    jar = cookies(pairs)
    guest = {"Cookie": "mr_session={}; mr_csrf={}".format(jar["mr_session"], jar["mr_csrf"])}
    status, headers, body = raw("GET", f"{server}/r/{review['id']}/export.html", headers=guest)
    assert status == 200 and b"<!doctype html>" in body
    assert "attachment" in "\n".join(v for k, v in headers if k.lower() == "content-disposition")


def test_a_member_without_a_grant_and_an_anonymous_visitor_get_the_usual_pages(server, review, peer):
    outsider = account(name="Out")
    status, _, denied = raw("GET", f"{server}/r/{review['id']}/export.html", headers=signed_in(outsider, False))
    status2, _, board_denied = raw("GET", f"{server}/r/{review['id']}/", headers=signed_in(outsider, False))
    assert status == status2 == 404 and denied == board_denied

    status, _, anonymous = raw("GET", f"{server}/r/{review['id']}/export.html")
    status2, _, board_signin = raw("GET", f"{server}/r/{review['id']}/")
    assert status == status2 == 401 and anonymous == board_signin


def test_comments_toggle_and_round_validation(server, review):
    rid = review["id"]
    on = http("GET", f"{server}/r/{rid}/export.html?comments=1")[2]
    off = http("GET", f"{server}/r/{rid}/export.html?comments=0")[2]
    assert b'class="mr-comments"' in on and b"Comments" in on
    assert b'class="mr-comments"' not in off
    assert http("GET", f"{server}/r/{rid}/export.html?round=latest")[0] == 200
    assert http("GET", f"{server}/r/{rid}/export.html?round=99")[0] == 400
    assert http("GET", f"{server}/r/{rid}/export.html?round=one")[0] == 400
    assert http("GET", f"{server}/r/{rid}/export.html?comments=yes")[0] == 400
    assert http("GET", f"{server}/r/{rid}/export.html?format=exe")[0] == 400


def test_each_route_writes_one_audit_row_with_who_actor(server, review):
    rid = review["id"]
    owner_id = users.owner_user()["id"]
    before = len([r for r in users.audit_rows(200) if r["action"] == "export"])
    http("GET", f"{server}/r/{rid}/export.html?round=1&format=html")
    row = latest_export()
    assert row["actor"] == f"user:{owner_id}" and row["target"] == f"review:{rid}"
    assert row["detail"] == {"format": "html", "round": 1, "comments": True}

    http("GET", f"{server}/r/{rid}/export/print?round=latest&comments=0")
    row = latest_export()
    assert row["detail"] == {"format": "pdf", "round": 1, "comments": False}

    http("GET", f"{server}/r/{rid}/export/capture?round=1&width=390&comments=1")
    row = latest_export()
    assert row["detail"] == {"format": "png", "round": 1, "comments": False}

    http("GET", f"{server}/r/{rid}/export.html?format=md&comments=1")
    row = latest_export()
    assert row["detail"]["format"] == "md"
    after = len([r for r in users.audit_rows(200) if r["action"] == "export"])
    assert after - before == 4


def test_a_guest_export_is_audited_by_guest_id_not_display_name(server, review, peer):
    peer["addr"] = "192.0.2.10"
    link = users.share("test", review["id"], "create", "viewer")
    _, pairs, _ = raw("GET", server + link["path"])
    jar = cookies(pairs)
    status, _, _ = raw("GET", f"{server}/r/{review['id']}/export.html?comments=0",
                       headers={"Cookie": f"mr_session={jar['mr_session']}"})
    assert status == 200
    row = latest_export()
    assert row["actor"].startswith("guest:")
    assert "Blue" not in row["actor"] and "guest" not in row["detail"].values()


def test_capture_forces_comments_off_and_carries_the_capture_scripts(server, review):
    rid = review["id"]
    status, headers, body = http("GET", f"{server}/r/{rid}/export/capture?round=1&comments=1&width=390")
    text = body.decode()
    assert status == 200
    assert headers["Content-Security-Policy"] == export.EXPORT_SCRIPT_CSP
    assert 'class="mr-comments"' not in text
    assert f"/r/{rid}/vendor/snapdom.min.js" in text
    assert f"/r/{rid}/export-capture.js" in text
    assert 'style="width:390px;max-width:390px;margin:0 auto"' in text
    assert headers["Content-Disposition"].startswith("inline; filename=")


def test_capture_width_is_clamped(server, review):
    text = http("GET", f"{server}/r/{review['id']}/export/capture?width=10")[2].decode()
    assert "width:320px" in text
    text = http("GET", f"{server}/r/{review['id']}/export/capture?width=9999")[2].decode()
    assert "width:1920px" in text
    text = http("GET", f"{server}/r/{review['id']}/export/capture?width=nope")[2].decode()
    assert "width:1280px" in text


def test_print_page_prints_itself_and_has_the_print_stylesheet(server, review):
    rid = review["id"]
    status, headers, body = http("GET", f"{server}/r/{rid}/export/print?round=1&comments=1")
    text = body.decode()
    assert status == 200
    assert headers["Content-Security-Policy"] == export.EXPORT_SCRIPT_CSP
    assert headers["Content-Disposition"].startswith("inline; filename=")
    assert f"/r/{rid}/export-print.js" in text
    assert "@media print" in text
    assert 'class="mr-comments"' in text


def test_the_export_static_files_are_served_from_the_review(server, review):
    rid = review["id"]
    expected = {
        "export-print.js": "text/javascript",
        "export-capture.js": "text/javascript",
        "vendor/snapdom.min.js": "text/javascript",
        "vendor/turndown.min.js": "text/javascript",
    }
    for rel, ctype in expected.items():
        status, headers, body = http("GET", f"{server}/r/{rid}/{rel}")
        assert status == 200 and headers["Content-Type"].startswith(ctype), rel
        assert body
    snapdom = http("GET", f"{server}/r/{rid}/vendor/snapdom.min.js")[2]
    assert hashlib.sha256(snapdom).hexdigest() == "3274620b04a65f42f972dc5de97530b8929d7a6e3bbe922130af6263b8da5ab7"
    turndown = http("GET", f"{server}/r/{rid}/vendor/turndown.min.js")[2]
    assert hashlib.sha256(turndown).hexdigest() == "ead805af2012a8434b58f1dab07b452370a7dcd3e9665ff64fc1d61dd2e38e9a"


def test_the_board_csp_still_allows_only_same_origin_scripts(server, review):
    _, headers, _ = http("GET", f"{server}/r/{review['id']}/")
    assert headers["Content-Security-Policy"] == board_server.BOARD_CSP
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    index = http("GET", f"{server}/r/{review['id']}/")[2].decode()
    assert index.index("vendor/turndown.min.js") < index.index("board.js")


EXPORT_ROUTES = ("export.html", "export/print", "export/capture")


def pinned_viewer(server, review, tmp_path, round_n="2"):
    add_rounds(review, tmp_path, 2)
    rid = review["id"]
    link = users.share("test", rid, "create", "viewer")
    users.share("test", rid, "update", round=str(round_n))
    return join(server, link["path"])


def test_a_pinned_viewer_guest_exports_only_its_round_while_members_and_the_owner_export_all(server, review,
                                                                                            tmp_path, peer):
    rid = review["id"]
    guest = pinned_viewer(server, review, tmp_path)
    assert [r["n"] for r in state(server, rid, guest)["rounds"]] == [2]
    member = account(review_id=rid, role="viewer")

    for route in EXPORT_ROUTES:
        for n in ("1", "3"):
            status, _, body = raw("GET", f"{server}/r/{rid}/{route}?round={n}", headers=signed_in(guest, False))
            assert status != 200, (route, n)
            assert b"Round 1" not in body and b"Round 3" not in body
        for query in ("?round=2", "?round=latest", ""):
            status, _, body = raw("GET", f"{server}/r/{rid}/{route}{query}", headers=signed_in(guest, False))
            assert status == 200, (route, query)
            assert b"Round 2" in body
            assert b"Round 1" not in body and b"Round 3" not in body
        for n in ("1", "2", "3"):
            status, _, body = raw("GET", f"{server}/r/{rid}/{route}?round={n}", headers=signed_in(member, False))
            assert status == 200, (route, n)
            assert f"Round {n}".encode() in body
        peer["addr"] = "127.0.0.1"
        try:
            status, _, body = http("GET", f"{server}/r/{rid}/{route}?round=3")
        finally:
            peer["addr"] = "192.0.2.10"
        assert status == 200 and b"Round 3" in body


def test_a_pinned_viewer_guest_cannot_tell_a_hidden_round_from_a_missing_one(server, review, tmp_path, peer):
    rid = review["id"]
    guest = pinned_viewer(server, review, tmp_path)
    for route in EXPORT_ROUTES:
        hidden = raw("GET", f"{server}/r/{rid}/{route}?round=3", headers=signed_in(guest, False))
        missing = raw("GET", f"{server}/r/{rid}/{route}?round=99", headers=signed_in(guest, False))
        assert hidden[0] == missing[0] == 404, (route, hidden[0], missing[0])
        assert hidden[2] == missing[2]
        assert b"3" not in hidden[2]


def test_a_rejected_export_writes_no_audit_row(server, review, tmp_path, peer):
    rid = review["id"]
    guest = pinned_viewer(server, review, tmp_path)
    before = len([r for r in users.audit_rows(200) if r["action"] == "export"])
    for route in EXPORT_ROUTES:
        status, _, _ = raw("GET", f"{server}/r/{rid}/{route}?round=3", headers=signed_in(guest, False))
        assert status == 404
    after = len([r for r in users.audit_rows(200) if r["action"] == "export"])
    assert after == before


def test_state_json_and_the_export_agree_on_the_visible_rounds(server, review, tmp_path, peer):
    rid = review["id"]
    guest = pinned_viewer(server, review, tmp_path)
    visible = [r["n"] for r in state(server, rid, guest)["rounds"]]
    assert visible == [2]
    for route in EXPORT_ROUTES:
        for n in (1, 2, 3):
            status, _, _ = raw("GET", f"{server}/r/{rid}/{route}?round={n}", headers=signed_in(guest, False))
            assert (status == 200) == (n in visible), (route, n, status)
        status, _, body = raw("GET", f"{server}/r/{rid}/{route}?round=latest", headers=signed_in(guest, False))
        assert status == 200
        assert f"Round {visible[-1]}".encode() in body


def test_a_latest_share_link_guest_exports_every_round_the_board_shows(server, review, tmp_path, peer):
    """A link left on `latest` is not a round restriction: state.json, the board and now the export all show every
    round. This is the control that the pinned-round fix did not over-restrict the Share dialog's shipped behaviour."""
    rid = review["id"]
    add_rounds(review, tmp_path, 2)
    link = users.share("test", rid, "create", "viewer")
    guest = join(server, link["path"])
    assert [r["n"] for r in state(server, rid, guest)["rounds"]] == [1, 2, 3]
    for route in EXPORT_ROUTES:
        for n in (1, 2, 3):
            status, _, body = raw("GET", f"{server}/r/{rid}/{route}?round={n}", headers=signed_in(guest, False))
            assert status == 200 and f"Round {n}".encode() in body, (route, n, status)
        status, _, body = raw("GET", f"{server}/r/{rid}/{route}?round=latest", headers=signed_in(guest, False))
        assert status == 200 and b"Round 3" in body
        status, _, body = raw("GET", f"{server}/r/{rid}/{route}?round=99", headers=signed_in(guest, False))
        assert status == 404 and b"Round 3" not in body


def real_thread(review, text, *, anchor=None, round_n=None):
    """One real thread by the owner, through threads.create (no fake read model anywhere)."""
    owner = users.owner_user()
    return threads.create(review["id"], f"user:{owner['id']}", owner["name"], "owner", text,
                          anchor=anchor, round_n=round_n)


def real_reply(review, thread, text):
    owner = users.owner_user()
    return threads.reply_comment(review["id"], thread["id"], f"user:{owner['id']}", owner["name"], "owner", text)


def test_the_html_export_carries_real_threads_replies_and_resolved_state(server, review, tmp_path):
    rid = review["id"]
    add_rounds(review, tmp_path, 1)
    first = real_thread(review, "Use <b>two</b> columns.", round_n=1,
                        anchor={"block": "a", "x": 0.4, "y": 0.6,
                                "target": {"path": "#alpha", "sig": {"tag": "h1", "h": "abcd"},
                                           "label": "Checkout layout"}})
    real_reply(review, first, "Agreed, two columns.")
    resolved = real_thread(review, "Resolved thread body.", round_n=1, anchor={"block": "b", "x": 0.2, "y": 0.2})
    owner = users.owner_user()
    threads.set_status(rid, resolved["id"], "resolved", f"user:{owner['id']}")
    real_thread(review, "Round two thread body.", anchor={"block": "a", "x": 0.5, "y": 0.5}, round_n=2)

    status, _, body = http("GET", f"{server}/r/{rid}/export.html?round=1&comments=1")
    html = body.decode()
    assert status == 200
    assert '<section class="mr-comments" id="comments">' in html
    assert "Use &lt;b&gt;two&lt;/b&gt; columns." in html and "<b>two</b>" not in html
    assert "Agreed, two columns." in html
    assert "Resolved thread body." in html
    assert "Round two thread body." in html
    assert "on <span>Checkout layout</span>" in html
    assert '<span class="mr-pill">Resolved</span>' in html


def test_the_print_page_carries_real_threads(server, review):
    rid = review["id"]
    first = real_thread(review, "Printed thread body.")
    real_reply(review, first, "Printed reply body.")
    status, _, body = http("GET", f"{server}/r/{rid}/export/print?round=1&comments=1")
    text = body.decode()
    assert status == 200
    assert '<section class="mr-comments"' in text and '<article class="mr-comment">' in text
    assert "Printed thread body." in text and "Printed reply body." in text


def test_the_capture_page_omits_real_threads(server, review):
    rid = review["id"]
    first = real_thread(review, "Captured thread body.")
    real_reply(review, first, "Captured reply body.")
    text = http("GET", f"{server}/r/{rid}/export/capture?round=1&width=390")[2].decode()
    assert '<section class="mr-comments"' not in text
    assert '<article class="mr-comment">' not in text
    assert "Captured thread body." not in text and "Captured reply body." not in text


def test_a_pinned_viewer_guest_exports_only_its_rounds_threads(server, review, tmp_path, peer):
    rid = review["id"]
    add_rounds(review, tmp_path, 1)
    real_thread(review, "Round one visible body.", round_n=1)
    real_thread(review, "Round two hidden body.", round_n=2)
    link = users.share("test", rid, "create", "viewer", round="2")
    guest = join(server, link["path"])

    status, _, body = raw("GET", f"{server}/r/{rid}/export.html?comments=1", headers=signed_in(guest, False))
    text = body.decode()
    assert status == 200
    assert "Round two hidden body." in text
    assert "Round one visible body." not in text

    peer["addr"] = "127.0.0.1"
    try:
        owner_html = http("GET", f"{server}/r/{rid}/export.html?round=2&comments=1")[2].decode()
    finally:
        peer["addr"] = "192.0.2.10"
    assert "Round one visible body." in owner_html and "Round two hidden body." in owner_html
