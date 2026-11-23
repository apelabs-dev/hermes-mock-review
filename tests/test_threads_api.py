"""The thread REST surface: the same Host/Origin/CSRF rules as submit, roles, own-only edits and state.json."""

import json
import socket
import sys

import pytest

from authkit import account, signed_in, token_of
from conftest import http
from mockreview import server as srv
from mockreview import store, threads, users

CSRF = "Cookie: mr_csrf=t0ken\r\nX-CSRF-Token: t0ken\r\n"


@pytest.fixture(autouse=True)
def clean():
    threads.reset_limits()
    yield
    threads.reset_limits()


def owner_actor():
    return users.owner_user()


def make_thread(rid, text="hello"):
    owner = owner_actor()
    return threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", text,
                          anchor={"block": "a", "x": 0.2, "y": 0.3})


def route(rid, name, tid=None, cid=None):
    if name == "create":
        return f"/r/{rid}/api/threads", {"anchor": {"block": "a", "x": 0.1, "y": 0.2}, "text": "hello"}
    if name == "reply":
        return f"/r/{rid}/api/threads/{tid}/reply", {"text": "a reply"}
    if name == "status":
        return f"/r/{rid}/api/threads/{tid}/status", {"status": "resolved"}
    if name == "read":
        return f"/r/{rid}/api/threads/{tid}/read", {}
    if name == "edit":
        return f"/r/{rid}/api/comments/{cid}", {"text": "edited"}
    if name == "delete":
        return f"/r/{rid}/api/comments/{cid}/delete", {}
    if name == "typing":
        return f"/r/{rid}/api/typing", {"thread": tid}
    if name == "activity":
        return f"/r/{rid}/api/activity/read", {}
    raise AssertionError(name)


ROUTES = ("create", "reply", "status", "read", "edit", "delete", "typing", "activity")
WRITES = ("create", "reply", "status", "edit", "delete", "typing")


def fresh(rid, name):
    t = make_thread(rid, "route " + name)
    if name in ("edit", "delete"):
        return t["id"], t["comments"][0]["id"]
    return t["id"], None


# ---------------------------------------------------------------- Host, Origin, CSRF


def _raw(port, method, path, host, body=b""):
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
    if body:
        lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    req = ("\r\n".join(lines) + "\r\n" + CSRF + "Connection: close\r\n\r\n").encode() + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.settimeout(5)
        s.sendall(req)
        resp = b""
        while chunk := s.recv(65536):
            resp += chunk
    return int(resp.split(b" ", 2)[1]), resp


@pytest.mark.parametrize("name", ROUTES)
def test_every_route_checks_host_origin_and_csrf(server, review, name):
    rid = review["id"]
    tid, cid = fresh(rid, name)
    path, body = route(rid, name, tid, cid)
    port = int(server.rsplit(":", 1)[1])
    code, resp = _raw(port, "POST", path, "attacker.example", json.dumps(body).encode())
    assert code == 421, (name, resp[:200])
    assert http("POST", server + path, body, {"Origin": "null"})[0] == 403, name
    assert http("POST", server + path, body, csrf=False)[0] == 403, name


# ---------------------------------------------------------------- roles


def test_viewer_is_forbidden_on_every_writing_route(server, review):
    viewer = account(review_id=review["id"], role="viewer", name="Vera")
    tid, cid = fresh(review["id"], "view")
    for name in WRITES:
        path, body = route(review["id"], name, tid, cid)
        code, _, out = http("POST", server + path, body, signed_in(viewer))
        assert code == 403, (name, code, out)
    for name in ("read", "activity"):
        path, body = route(review["id"], name, tid, cid)
        assert http("POST", server + path, body, signed_in(viewer))[0] == 200, name
    assert http("GET", server + f"/r/{review['id']}/api/threads", headers=signed_in(viewer, False))[0] == 200


def test_commenter_can_use_the_whole_thread_surface(server, review):
    member = account(review_id=review["id"], role="commenter", name="Maya")
    path, body = route(review["id"], "create")
    code, _, out = http("POST", server + path, body, signed_in(member))
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    for name in ("reply", "status", "read", "typing"):
        path, body = route(review["id"], name, tid, None)
        assert http("POST", server + path, body, signed_in(member))[0] == 200, name
    path, body = route(review["id"], "activity", tid, None)
    assert http("POST", server + path, body, signed_in(member))[0] == 200
    cid = threads.list_threads(review["id"])[0]["comments"][0]["id"]
    path, body = route(review["id"], "edit", tid, cid)
    assert http("POST", server + path, body, signed_in(member))[0] == 200
    path, body = route(review["id"], "delete", tid, cid)
    assert http("POST", server + path, body, signed_in(member))[0] == 200


def _guest(rid, role):
    if users.share_link(rid) is None:
        users.share("test", rid, "create", role)
    else:
        users.share("test", rid, "update", role)
    link = users.share_link(rid)
    return users.join_as_guest(users.resolve_share_token(token_of(link["path"])))


def test_guest_commenter_posts_and_guest_viewer_cannot(server, review):
    rid = review["id"]
    commenter = _guest(rid, "commenter")
    path, body = route(rid, "create")
    code, _, out = http("POST", server + path, body, signed_in(commenter))
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    assert http("POST", server + f"/r/{rid}/api/threads/{tid}/reply", {"text": "guest reply"},
                signed_in(commenter))[0] == 200

    viewer = _guest(rid, "viewer")
    assert http("POST", server + f"/r/{rid}/api/threads/{tid}/reply", {"text": "no"},
                signed_in(viewer))[0] == 403
    assert http("POST", server + path, body, signed_in(viewer))[0] == 403


def test_edit_and_delete_are_own_only(server, review):
    rid = review["id"]
    maya = account(review_id=rid, role="commenter", name="Maya")
    theo = account(review_id=rid, role="commenter", name="Theo")
    path, body = route(rid, "create")
    code, _, out = http("POST", server + path, body, signed_in(maya))
    thread = json.loads(out)["thread"]
    cid = thread["comments"][0]["id"]
    for name in ("edit", "delete"):
        p, b = route(rid, name, thread["id"], cid)
        code, _, out = http("POST", server + p, b, signed_in(theo))
        assert code == 403, (name, code, out)
        code, _, out = http("POST", server + p, b, signed_in(maya))
        assert code == 200, (name, code, out)


# ---------------------------------------------------------------- state.json and body rules


def test_state_json_carries_thread_counts_and_no_text(server, review):
    make_thread(review["id"], "a comment body that must not leak into state.json")
    code, _, body = http("GET", f"{server}/r/{review['id']}/state.json")
    st = json.loads(body)
    assert code == 200 and st["threads"]["open"] == 1 and st["threads"]["rev"] >= 1
    assert b"must not leak" not in body


def test_body_size_and_content_type_follow_the_api(server, review):
    path, body = route(review["id"], "create")
    code, _, _ = http("POST", server + path, json.dumps(body).encode(), {"Content-Type": "text/plain"})
    assert code == 415
    port = int(server.rsplit(":", 1)[1])
    huge = b"{" + b" " * (srv.API_MAX_BODY + 1)
    code, resp = _raw(port, "POST", path, f"127.0.0.1:{port}", huge)
    assert code == 413 and b"too large" in resp.lower()


def wake_off_review(tmp_path):
    path = tmp_path / "wake-off.json"
    path.write_text(json.dumps({"blocks": [{"type": "mock", "id": "m1", "html": "<p>Wake off</p>"}]}),
                    encoding="utf-8")
    return store.create_review("Wake off", "", {"platform": "discord", "chat_id": "test-chat"}, "local",
                               store.parse_round_spec(decisions=str(path)), wake=False)["id"]


def test_the_wake_up_guard_reads_the_review_only_in_the_branches_that_use_it(server, review, tmp_path,
                                                                             monkeypatch):
    """Only a new thread and a reply can activate @Hermes, so only those two branches may read review.json for the
    `wake` flag. Typing leases, read marks and status changes must not pay that read. The thread helpers behind the
    routes keep their own loads (status -> _thread_dict, create -> _round_data), so this counts the calls the request
    handler makes itself: exactly the lazy guard this covers."""
    rid = review["id"]
    tid = make_thread(rid)["id"]
    real_load = store.load
    guard_loads = []

    def counting(review_id):
        if sys._getframe(1).f_code.co_name in ("_thread_post", "_mention_gate"):
            guard_loads.append(review_id)
        return real_load(review_id)

    monkeypatch.setattr(srv.store, "load", counting)

    for name in ("typing", "read", "status", "activity"):
        path, body = route(rid, name, tid)
        guard_loads.clear()
        assert http("POST", server + path, body)[0] == 200, name
        assert guard_loads == [], f"a {name} POST still loaded the review for the wake-up guard"

    for wake_on, want in ((rid, True), (wake_off_review(tmp_path), False)):
        path, body = route(wake_on, "create")
        body.update({"text": "@Hermes please look", "mentions": ["hermes"]})
        guard_loads.clear()
        code, _headers, out = http("POST", server + path, body)
        assert code == 201, out
        assert len(guard_loads) == 1, f"the guard read the review {len(guard_loads)} times on a create"
        assert json.loads(out)["thread"]["activated"] is want, \
            f"a create on a wake-{'on' if want else 'off'} board activated={not want}"


def test_a_review_created_before_threads_still_submits(server, review):
    from conftest import FEEDBACK
    code, _, out = http("POST", f"{server}/r/{review['id']}/api/submit",
                        {"round": 1, "token": review["token"], "feedback": FEEDBACK})
    assert code == 200, out
    assert threads.list_threads(review["id"]) == []
