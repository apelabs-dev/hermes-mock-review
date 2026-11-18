"""One capped parse for every "digits then int()" site.

`str.isdigit()` is True for characters `int()` refuses (`'²'`), and `int()` itself refuses a digit string over 4300
digits, so a 5000-digit id in a mention, a query, a header or an account path is a 500 traceback instead of the answer
the same site gives a non-numeric value. These tests pin that answer at every site -- the mention create and reply, the
`?round=` query, `Last-Event-ID`, `Content-Length`, the `/api/{tokens,invites,users}/<id>/...` paths, the export round,
the capture width and the share round -- plus a legal input control per site, so the fix can never be "answer 400/404
always".
"""

import json

from authkit import account, raw, signed_in
from conftest import CSRF_HEADERS, http
from mockreview import live, store, threads, users
from test_live import Stream, clean_live  # noqa: F401  (autouse: the SSE registry starts clean)

BIG = "1" * 5000


def payload(body):
    return json.loads(body)


def owner_actor():
    owner = users.owner_user()
    return f"user:{owner['id']}", owner["name"]


def test_a_huge_or_non_ascii_mention_is_unknown_on_create(server, review):
    rid = review["id"]
    url = f"{server}/r/{rid}/api/threads"
    code, _, baseline = http("POST", url, {"text": "hi", "mentions": ["user:abc"]})
    assert code == 400, ("mention create: a non-numeric mention", code, baseline[:300])
    assert payload(baseline) == {"ok": False, "error": "unknown mention"}, "mention create: the 400 body"
    for mention in ("user:²", f"user:{BIG}", "user:"):
        code, _, body = http("POST", url, {"text": "hi", "mentions": [mention]})
        assert code == 400, ("mention create", mention[:16], code, body[:300])
        assert body == baseline, ("mention create: must answer exactly like user:abc", mention[:16])

    actor, name = owner_actor()
    uid = int(actor[5:])
    code, _, body = http("POST", url, {"text": "hi", "mentions": [f"user: {uid}"]})
    assert code == 400, ("mention create: a padded target", code, body[:300])
    assert body == baseline, "mention create: a padded target answers exactly like user:abc"

    code, _, out = http("POST", url, {"text": "hi", "mentions": [f"user:{uid}"]})
    assert code == 201, ("mention create: a real user id still works", code, out[:300])
    comment = payload(out)["thread"]["comments"][0]
    assert comment["mentions"] == [{"kind": "user", "target": f"user:{uid}"}], "mention create: the mention is stored"


def test_a_huge_or_non_ascii_mention_is_unknown_on_reply(server, review):
    rid = review["id"]
    actor, name = owner_actor()
    thread = threads.create(rid, actor, name, "owner", "first")
    url = f"{server}/r/{rid}/api/threads/{thread['id']}/reply"
    code, _, baseline = http("POST", url, {"text": "hi", "mentions": ["user:abc"]})
    assert code == 400, ("mention reply: a non-numeric mention", code, baseline[:300])
    assert payload(baseline) == {"ok": False, "error": "unknown mention"}, "mention reply: the 400 body"
    for mention in ("user:²", f"user:{BIG}", "user:"):
        code, _, body = http("POST", url, {"text": "hi", "mentions": [mention]})
        assert code == 400, ("mention reply", mention[:16], code, body[:300])
        assert body == baseline, ("mention reply: must answer exactly like user:abc", mention[:16])

    uid = int(actor[5:])
    code, _, body = http("POST", url, {"text": "hi", "mentions": [f"user: {uid}"]})
    assert code == 400, ("mention reply: a padded target", code, body[:300])
    assert body == baseline, "mention reply: a padded target answers exactly like user:abc"

    code, _, out = http("POST", url, {"text": "hi", "mentions": [f"user:{uid}"]})
    assert code == 200, ("mention reply: a real user id still works", code, out[:300])
    assert payload(out)["comment"]["mentions"] == [{"kind": "user", "target": f"user:{uid}"}], \
        "mention reply: the mention is stored"


def test_a_huge_round_query_behaves_as_non_numeric(server, review, variants):
    rid = review["id"]
    actor, name = owner_actor()
    store.add_round(rid, store.parse_round_spec(variants=variants))
    first = threads.create(rid, actor, name, "owner", "round one", round_n=1)
    second = threads.create(rid, actor, name, "owner", "round two", round_n=2)
    url = f"{server}/r/{rid}/api/threads"

    code, _, baseline = http("GET", url + "?round=abc")
    assert code == 200, ("round query: a non-numeric ?round=", code, baseline[:300])
    absent = payload(baseline)
    assert {t["id"] for t in absent["threads"]} == {first["id"], second["id"]}, \
        "round query: ?round=abc is the full listing"

    code, _, body = http("GET", url + "?round=" + BIG)
    assert code == 200, ("round query: a 5000-digit ?round=", code, body[:300])
    assert body == baseline, "round query: a too-long ?round= must answer exactly like ?round=abc"

    code, _, body = http("GET", url + "?round=1")
    assert code == 200, ("round query: a real round still scopes", code, body[:300])
    scoped = payload(body)
    assert [t["id"] for t in scoped["threads"]] == [first["id"]], "round query: ?round=1 stays scoped to round 1"

    code, _, padded = http("GET", url + "?round=%201")
    assert code == 200, ("round query: a padded ?round=", code, padded[:300])
    assert padded == body, "round query: ?round=%201 answers exactly like ?round=1"


def test_a_huge_token_id_is_not_found(server):
    status, _, body = http("POST", f"{server}/api/tokens/{BIG}/revoke", {})
    other, _, other_body = http("POST", f"{server}/api/tokens/abc/revoke", {})
    assert status == other == 404, ("token id: the 404", status, body[:300])
    assert body == other_body, "token id: a too-long id answers exactly like a non-numeric one"
    assert payload(body) == {"ok": False, "error": "not found"}, "token id: the 404 body"

    owner = users.owner_user()
    token = users.create_token(owner["id"], "test token")
    code, _, out = http("POST", f"{server}/api/tokens/{token['id']}/revoke", {})
    assert code == 200, ("token id: a real token still revokes", code, out[:300])
    assert payload(out) == {"ok": True, "revoked": token["id"]}, "token id: the revoke body"


def test_a_padded_token_id_is_not_found(server):
    owner = users.owner_user()
    token = users.create_token(owner["id"], "padded token")
    status, _, body = http("POST", f"{server}/api/tokens/%20{token['id']}/revoke", {})
    assert status == 404, ("token id: a padded id", status, body[:300])
    assert payload(body) == {"ok": False, "error": "not found"}, "token id: the 404 body for a padded id"

    code, _, out = http("GET", f"{server}/api/tokens")
    assert code == 200, ("token id: the token list still answers", code, out[:300])
    assert token["id"] in [t["id"] for t in payload(out)["tokens"]], "token id: a padded id does not revoke"


def test_a_huge_invite_id_is_not_found(server):
    status, _, body = http("POST", f"{server}/api/invites/{BIG}/revoke", {})
    other, _, other_body = http("POST", f"{server}/api/invites/abc/revoke", {})
    assert status == other == 404, ("invite id: the 404", status, body[:300])
    assert body == other_body, "invite id: a too-long id answers exactly like a non-numeric one"
    assert payload(body) == {"ok": False, "error": "not found"}, "invite id: the 404 body"

    invite = users.create_invite("test")
    status, _, body = http("POST", f"{server}/api/invites/%20{invite['id']}/revoke", {})
    assert status == 404, ("invite id: a padded id", status, body[:300])
    assert payload(body) == {"ok": False, "error": "not found"}, "invite id: the 404 body for a padded id"

    code, _, out = http("POST", f"{server}/api/invites/{invite['id']}/revoke", {})
    assert code == 200, ("invite id: a real invite still revokes", code, out[:300])
    assert payload(out) == {"ok": True, "revoked": invite["id"]}, "invite id: the revoke body"


def test_a_huge_user_id_is_not_found(server):
    member = account()
    uid = member["user_id"]
    for action in ("role", "delete"):
        bad = {"role": "member"} if action == "role" else {}
        code, _, out = http("POST", f"{server}/api/users/{BIG}/{action}", bad)
        other, _, other_body = http("POST", f"{server}/api/users/abc/{action}", bad)
        assert code == 404, ("user id", action, code, out[:300])
        assert out == other_body, ("user id: a too-long id answers exactly like a non-numeric one", action)
        assert payload(out) == {"ok": False, "error": "not found"}, ("user id: the 404 body", action)

        code, _, out = http("POST", f"{server}/api/users/%20{uid}/{action}", bad)
        assert code == 404, ("user id: a padded id", action, code, out[:300])
        assert payload(out) == {"ok": False, "error": "not found"}, ("user id: the 404 body for a padded id", action)

    code, _, out = http("POST", f"{server}/api/users/{uid}/role", {"role": "member"})
    assert code == 200 and payload(out)["user"]["id"] == uid, ("user id: a real role change", code, out[:300])
    code, _, out = http("POST", f"{server}/api/users/{uid}/delete", {})
    assert code == 200 and payload(out)["ok"] is True, ("user id: a real delete", code, out[:300])


def test_the_admin_gate_stays_above_a_huge_id(server):
    member = account()
    headers = dict(signed_in(member), **{"Content-Type": "application/json"})
    for path in (f"/api/invites/{BIG}/revoke", f"/api/users/{BIG}/role"):
        status, _, body = raw("POST", f"{server}{path}", b"{}", headers)
        assert status == 403, ("admin gate: a member on a huge id", path, status, body[:300])
        assert payload(body) == {"ok": False, "error": "admins only"}, \
            ("admin gate: the gate never moves below the id parse", path)


def test_a_huge_last_event_id_is_ignored(server, review):
    rid = review["id"]
    actor, name = owner_actor()
    threads.create(rid, actor, name, "owner", "replay me")
    first_id = live.latest(rid)
    assert first_id > 0

    for cursor in (BIG, "abc"):
        stream = Stream(server, f"/r/{rid}/events", {"Last-Event-ID": cursor})
        try:
            assert stream.until("hello") is not None, ("last-event-id: the stream still opens", cursor[:16])
            replay = stream.until("thread.created", timeout=2)
            assert replay is not None, ("last-event-id: an unparsable cursor reads as 0", cursor[:16])
            assert int(replay["id"]) == first_id, ("last-event-id: replay starts at the first event", cursor[:16])
        finally:
            stream.close()

    stream = Stream(server, f"/r/{rid}/events", {"Last-Event-ID": str(first_id)})
    try:
        assert stream.until("hello") is not None, "last-event-id: the control stream opens"
        assert stream.until("thread.created", timeout=0.5) is None, \
            "last-event-id: a real cursor still replays nothing past itself"
    finally:
        stream.close()


def test_a_huge_content_length_is_a_411(server):
    url = f"{server}/api/sessions/sign-out-others"
    headers = dict(CSRF_HEADERS, **{"Content-Type": "application/json", "Content-Length": BIG})
    status, _, body = raw("POST", url, headers=headers)
    assert status == 411, ("content-length: a 5000-digit header", status, body[:300])
    assert payload(body) == {"ok": False, "error": "Content-Length required"}, "content-length: the 411 body"

    headers = dict(CSRF_HEADERS, **{"Content-Type": "application/json", "Content-Length": "2 "})
    status, _, body = raw("POST", url, b"{}", headers)
    assert status == 411, ("content-length: a padded header", status, body[:300])
    assert payload(body) == {"ok": False, "error": "Content-Length required"}, \
        "content-length: the 411 body for a padded header"

    code, _, body = http("POST", url, {})
    assert code == 200 and payload(body)["ok"] is True, ("content-length: a normal request still works", code,
                                                         body[:300])


def test_a_huge_export_round_is_the_same_as_an_unknown_round(server, review, variants):
    rid = review["id"]
    store.add_round(rid, store.parse_round_spec(variants=variants))
    url = f"{server}/r/{rid}/export.html?round="

    code, _, baseline = http("GET", url + "999999")
    assert code == 400, ("export round: an unknown round", code, baseline[:300])
    status, _, body = http("GET", url + BIG)
    assert status == code, ("export round: a 5000-digit round takes the unknown-round status", status, body[:300])
    assert body == baseline, "export round: a 5000-digit round answers exactly like an unknown round"

    code, _, body = http("GET", url + "abc")
    assert code == 400, ("export round: a non-numeric round", code, body[:300])

    code, _, body = http("GET", url + "1")
    assert code == 200 and body.startswith(b"<!doctype html"), ("export round: a real round still exports", code,
                                                                body[:300])


def test_a_padded_export_round_is_refused(server, review, variants):
    rid = review["id"]
    store.add_round(rid, store.parse_round_spec(variants=variants))
    url = f"{server}/r/{rid}/export.html?round="

    code, _, baseline = http("GET", url + "999999")
    assert code == 400, ("export round: an unknown round", code, baseline[:300])
    status, _, body = http("GET", url + "%201")
    assert status == 400, ("export round: a padded round", status, body[:300])
    assert body == baseline, "export round: a padded round answers exactly like an unknown round"

    code, _, out = http("GET", url + "1")
    assert code == 200 and out.startswith(b"<!doctype html"), ("export round: round 1 still exports", code, out[:300])


def test_a_huge_capture_width_takes_the_default(server, review):
    url = f"{server}/r/{review['id']}/export/capture?width="

    code, _, baseline = http("GET", url + "abc")
    assert code == 200, ("capture width: a non-numeric width takes the default", code, baseline[:300])
    status, _, body = http("GET", url + BIG)
    assert status == 200, ("capture width: a 5000-digit width still renders", status, body[:300])
    assert body == baseline, "capture width: a 5000-digit width is byte-identical to the default"

    status, _, body = http("GET", url + "%201200")
    assert status == 200, ("capture width: a padded width still renders", status, body[:300])
    assert body == baseline, "capture width: a padded width is byte-identical to the default"

    code, _, wider = http("GET", url + "1200")
    assert code == 200, ("capture width: a real width still renders", code, wider[:300])
    assert wider != baseline, "capture width: the width is baked into the page"


def test_a_huge_share_round_is_refused_like_a_non_numeric_one(server, review):
    url = f"{server}/r/{review['id']}/api/share"

    code, _, baseline = http("POST", url, {"round": "abc"})
    assert code == 400, ("share round: a non-numeric round", code, baseline[:300])
    status, _, body = http("POST", url, {"round": BIG})
    assert status == code == 400, ("share round: a 5000-digit round is the same 400", status, body[:300])
    assert body == baseline, "share round: a 5000-digit round answers exactly like a non-numeric one"
    assert payload(body) == {"ok": False, "error": "round must be latest or one of this review's rounds"}, \
        "share round: the 400 message"

    code, _, out = http("POST", url, {"round": 1})
    assert code == 200, ("share round: a real round still shares", code, out[:300])


def test_a_padded_share_round_is_refused(server, review):
    url = f"{server}/r/{review['id']}/api/share"

    status, _, body = http("POST", url, {"round": " 1"})
    assert status == 400, ("share round: a padded round", status, body[:300])
    assert payload(body) == {"ok": False, "error": "round must be latest or one of this review's rounds"}, \
        "share round: a padded round gets the share message"
