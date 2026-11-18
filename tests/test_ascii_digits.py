"""ASCII digits only where a digits predicate is followed by int().

`str.isdigit()` is True for characters `int()` refuses (`'²'.isdigit()` is True, `int('²')` raises), so every pair of
"validate with isdigit(), then parse with int()" is a 500 traceback when a non-ASCII digit reaches it through a query
string, a header or a percent-encoded path. These tests pin the intended answer at every such site in
`mockreview/server.py` -- `?round=`, `Last-Event-ID`, `Content-Length` and the `/api/{tokens,invites,users}/<id>/...`
paths -- plus a control that legal input answers as before, and one that keeps the admin gate above the admin routes.
"""

import json

from authkit import account, raw, signed_in
from conftest import CSRF_HEADERS, http
from mockreview import live, store, threads, users
from test_live import Stream, clean_live  # noqa: F401  (autouse: the SSE registry starts clean)


def payload(body):
    return json.loads(body)


def owner_actor():
    owner = users.owner_user()
    return f"user:{owner['id']}", owner["name"]


def test_a_non_ascii_round_query_behaves_as_absent(server, review, variants):
    rid = review["id"]
    actor, name = owner_actor()
    store.add_round(rid, store.parse_round_spec(variants=variants))
    first = threads.create(rid, actor, name, "owner", "round one", round_n=1)
    second = threads.create(rid, actor, name, "owner", "round two", round_n=2)
    url = f"{server}/r/{rid}/api/threads"

    code, _, body = http("GET", url)
    assert code == 200
    absent = payload(body)
    assert {t["id"] for t in absent["threads"]} == {first["id"], second["id"]}

    code, _, body = http("GET", url + "?round=%C2%B2")
    assert code == 200, (code, body[:300])
    assert payload(body) == absent, "a non-ASCII digit must behave exactly like an absent ?round="

    code, _, body = http("GET", url + "?round=1")
    assert code == 200, (code, body[:300])
    scoped = payload(body)
    assert [t["id"] for t in scoped["threads"]] == [first["id"]], "?round=1 stays scoped to round 1"
    assert scoped["counts"]["open"] == 1

    code, _, body = http("GET", url + "?round=-1")
    assert code == 404, (code, body[:300])
    assert payload(body) == {"ok": False, "error": "round not found"}


def test_a_non_ascii_last_event_id_is_ignored(server, review):
    rid = review["id"]
    actor, name = owner_actor()
    threads.create(rid, actor, name, "owner", "replay me")
    first_id = live.latest(rid)
    assert first_id > 0

    stream = Stream(server, f"/r/{rid}/events", {"Last-Event-ID": "\xb2"})
    try:
        assert stream.until("hello") is not None, "the stream still opens"
        replay = stream.until("thread.created", timeout=2)
        assert replay is not None, "an unparsable cursor reads as 0, the fresh-stream cursor"
        assert int(replay["id"]) == first_id
    finally:
        stream.close()

    stream = Stream(server, f"/r/{rid}/events", {"Last-Event-ID": str(first_id)})
    try:
        assert stream.until("hello") is not None
        assert stream.until("thread.created", timeout=0.5) is None, "a real cursor still replays nothing past itself"
    finally:
        stream.close()


def test_a_non_ascii_content_length_is_a_411(server):
    url = f"{server}/api/sessions/sign-out-others"
    headers = dict(CSRF_HEADERS, **{"Content-Type": "application/json", "Content-Length": "\xb2"})
    status, _, body = raw("POST", url, headers=headers)
    assert status == 411, (status, body[:300])
    assert payload(body) == {"ok": False, "error": "Content-Length required"}

    code, _, body = http("POST", url, {})
    assert code == 200 and payload(body)["ok"] is True, (code, body[:300])


def test_a_non_ascii_token_id_is_not_found(server):
    status, _, body = http("POST", f"{server}/api/tokens/%C2%B2/revoke", {})
    other, _, other_body = http("POST", f"{server}/api/tokens/abc/revoke", {})
    assert status == other == 404, (status, body[:300])
    assert body == other_body, "the encoded id answers exactly like a non-numeric one"
    assert payload(body) == {"ok": False, "error": "not found"}

    owner = users.owner_user()
    token = users.create_token(owner["id"], "test token")
    code, _, body = http("POST", f"{server}/api/tokens/{token['id']}/revoke", {})
    assert code == 200 and payload(body) == {"ok": True, "revoked": token["id"]}, (code, body[:300])


def test_a_non_ascii_invite_id_is_not_found(server):
    status, _, body = http("POST", f"{server}/api/invites/%C2%B2/revoke", {})
    other, _, other_body = http("POST", f"{server}/api/invites/abc/revoke", {})
    assert status == other == 404, (status, body[:300])
    assert body == other_body
    assert payload(body) == {"ok": False, "error": "not found"}

    invite = users.create_invite("test")
    code, _, body = http("POST", f"{server}/api/invites/{invite['id']}/revoke", {})
    assert code == 200 and payload(body) == {"ok": True, "revoked": invite["id"]}, (code, body[:300])


def test_a_non_ascii_user_id_is_not_found(server):
    member = account()
    uid = member["user_id"]
    for action in ("role", "delete"):
        body = {"role": "member"} if action == "role" else {}
        code, _, out = http("POST", f"{server}/api/users/%C2%B2/{action}", body)
        assert code == 404, (action, code, out[:300])
        assert payload(out) == {"ok": False, "error": "not found"}, action

    code, _, out = http("POST", f"{server}/api/users/abc/role", {"role": "member"})
    assert code == 404 and payload(out) == {"ok": False, "error": "not found"}

    code, _, out = http("POST", f"{server}/api/users/{uid}/role", {"role": "member"})
    assert code == 200 and payload(out)["user"]["id"] == uid, (code, out[:300])
    code, _, out = http("POST", f"{server}/api/users/{uid}/delete", {})
    assert code == 200 and payload(out)["ok"] is True, (code, out[:300])


def test_the_admin_gate_stays_above_the_encoded_id_routes(server):
    member = account()
    headers = dict(signed_in(member), **{"Content-Type": "application/json"})
    for path in ("/api/invites/%C2%B2/revoke", "/api/users/%C2%B2/role"):
        status, _, body = raw("POST", f"{server}{path}", b"{}", headers)
        assert status == 403, (path, status, body[:300])
        assert payload(body) == {"ok": False, "error": "admins only"}, "the gate never moves below the id parse"
