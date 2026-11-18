"""Roles, grants, guests, CSRF and the one access rule."""

import json

import pytest

from authkit import Recorder, account, header, login, loopback_owner_off, peer, raw, signed_in, token_of  # noqa: F401
from conftest import FEEDBACK, http
from mockreview import access, db, store, streams, users


@pytest.fixture
def recorder():
    rec = Recorder()
    streams.set_closer(rec)
    yield rec
    streams.set_closer(None)


def other_review():
    return store.create_review("Second review", "", {"platform": "discord", "chat_id": "c"}, "local",
                               [store.parse_variant_spec("Only|one|" + str(_variant()))])


def _variant():
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp(dir=str(store.config.data_root())))
    (d / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    return d / "index.html"


# ---------------------------------------------------------------- the rule itself

P_OWNER = access.Principal("owner", user_id=1)
P_MEMBER = access.Principal("member", user_id=2)
P_GUEST = access.Principal("guest", guest_review="r1", guest_generation=2)


@pytest.mark.parametrize("principal, exists, grant, link, result, role", [
    (access.ANONYMOUS, True, None, None, access.SIGNIN, None),
    (access.ANONYMOUS, False, None, None, access.SIGNIN, None),
    (P_OWNER, True, None, None, access.OK, "owner"),
    (P_OWNER, False, None, None, access.DEAD, None),
    (P_MEMBER, True, "viewer", None, access.OK, "viewer"),
    (P_MEMBER, True, "reviewer", None, access.OK, "reviewer"),
    (P_MEMBER, True, None, None, access.DEAD, None),
    (P_MEMBER, False, "reviewer", None, access.DEAD, None),
    (P_GUEST, True, None, access.Link(2, "commenter"), access.OK, "commenter"),
    (P_GUEST, True, None, access.Link(3, "commenter"), access.DEAD, None),     # regenerated
    (P_GUEST, True, None, None, access.DEAD, None),                           # stopped
    (P_GUEST, False, None, access.Link(2, "commenter"), access.DEAD, None),
])
def test_resolve(principal, exists, grant, link, result, role):
    d = access.resolve(principal, "r1", exists, grant, link)
    assert (d.result, d.role) == (result, role)


def test_a_guest_link_opens_only_its_own_review():
    assert access.resolve(P_GUEST, "r2", True, None, access.Link(2, "reviewer")).result == access.DEAD


def test_capabilities_grow_with_the_role():
    assert access.CAPS["viewer"] < access.CAPS["commenter"] < access.CAPS["reviewer"] < access.CAPS["owner"]


# ---------------------------------------------------------------- roles × write routes

def _write_routes(review, admin_target):
    rid = review["id"]
    return {
        "submit": ("/r/{}/api/submit".format(rid), {"round": 1, "token": review["token"], "feedback": FEEDBACK}),
        "share": ("/r/{}/api/share".format(rid), {"action": "create"}),
        "guest-name": ("/r/{}/api/guest-name".format(rid), {"name": "Blue Fox"}),
        "invites": ("/api/invites", {"role": "admin"}),
        "user-role": ("/api/users/{}/role".format(admin_target), {"role": "member"}),
        "user-delete": ("/api/users/{}/delete".format(admin_target), {}),
    }


EXPECTED = {   # role -> route -> allowed?
    "viewer": {"submit": False, "share": False, "guest-name": False},
    "commenter": {"submit": False, "share": False, "guest-name": False},
    "reviewer": {"submit": True, "share": False, "guest-name": False},
}


@pytest.mark.parametrize("role", ["viewer", "commenter", "reviewer"])
def test_each_role_on_every_write_route(env, server, review, role):
    owner = users.owner_user()
    member = account(review_id=review["id"], role=role)
    for name, (path, body) in _write_routes(review, owner["id"]).items():
        status, _, out = http("POST", server + path, body, signed_in(member))
        allowed = EXPECTED[role].get(name, False)
        assert (status == 200) is allowed, (role, name, status, out)
        if not allowed:
            assert status == 403, (role, name, status, out)
    assert users.get_user(owner["id"])["role"] == "admin"
    assert users.share_link(review["id"]) is None
    assert [i for i in users.list_invites() if i["role"] == "admin"] == []


def test_the_owner_may_do_everything_but_a_guest_name(env, server, review):
    owner = users.owner_user()
    routes = _write_routes(review, owner["id"])
    for name in ("submit", "share", "invites"):
        path, body = routes[name]
        assert http("POST", server + path, body)[0] == 200, name
    assert http("POST", server + routes["guest-name"][0], routes["guest-name"][1])[0] == 403


def test_a_read_only_role_sees_its_capabilities_in_state(env, server, review):
    viewer = account(review_id=review["id"], role="viewer")
    status, _, body = http("GET", server + "/r/{}/state.json".format(review["id"]), headers=signed_in(viewer, False))
    me = json.loads(body)["me"]
    assert status == 200 and me["review_role"] == "viewer" and me["caps"] == ["read"]


def test_roles_are_read_fresh_on_every_request(env, server, review):
    member = account(review_id=review["id"], role="viewer")
    url = server + "/r/{}/api/submit".format(review["id"])
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    assert http("POST", url, body, signed_in(member))[0] == 403
    users.grant("test", member["user_id"], "reviewer", review_id=review["id"])
    assert http("POST", url, body, signed_in(member))[0] == 200
    users.revoke_grant("test", member["user_id"], review_id=review["id"])
    assert http("GET", server + "/r/{}/".format(review["id"]), headers=signed_in(member, False))[0] == 404


def test_an_expired_grant_ends_access(env, server, review, monkeypatch):
    member = account(review_id=review["id"], role="reviewer")
    users.grant("test", member["user_id"], "reviewer", review_id=review["id"], expires="1d")
    real = users.now
    monkeypatch.setattr(users, "now", lambda: real() + 86400 + 1)
    assert users.grant_role(member["user_id"], review["id"]) is None


def test_an_all_reviews_grant_covers_every_review_and_a_review_grant_wins(env, review):
    member = account(all_reviews=True, role="viewer")
    second = other_review()
    assert users.grant_role(member["user_id"], review["id"]) == "viewer"
    assert users.grant_role(member["user_id"], second["id"]) == "viewer"
    users.grant("test", member["user_id"], "reviewer", review_id=second["id"])
    assert users.grant_role(member["user_id"], second["id"]) == "reviewer"


def test_the_index_lists_only_what_a_member_may_open(env, server, review):
    second = other_review()
    member = account(review_id=second["id"], role="viewer")
    status, _, page = http("GET", server + "/", headers=signed_in(member, False))
    assert status == 200 and second["id"].encode() in page and review["id"].encode() not in page


# ---------------------------------------------------------------- dead and forbidden look the same

def _comparable(resp):
    status, pairs, body = resp
    return status, sorted((k, v) for k, v in pairs if k.lower() != "date"), body


@pytest.mark.parametrize("method, suffix", [("GET", "/"), ("GET", "/state.json"), ("GET", "/board.js"),
                                            ("GET", "/rounds/1/a/index.html"), ("POST", "/api/submit"),
                                            ("POST", "/api/share"), ("GET", "/api/share")])
def test_a_forbidden_review_and_a_missing_one_answer_byte_for_byte_alike(env, server, review, method, suffix):
    member = account(review_id=other_review()["id"], role="reviewer")
    headers = {**signed_in(member), "Content-Type": "application/json"} if method == "POST" else signed_in(member, False)
    body = b"{}" if method == "POST" else None
    forbidden = raw(method, server + "/r/{}{}".format(review["id"], suffix), body, headers)
    missing = raw(method, server + "/r/{}{}".format("no-such-review-0000", suffix), body, headers)
    assert forbidden[0] == 404
    assert _comparable(forbidden) == _comparable(missing)


def test_a_forbidden_review_with_an_artifact_and_a_missing_one_answer_alike(env, server, tmp_path):
    """A forbidden review that has an artifact round must not reveal that through the dead end's policy header."""
    d = tmp_path / "art"
    d.mkdir()
    (d / "decisions.json").write_text(json.dumps({"blocks": [{"type": "artifact", "id": "film", "html": "<p>art</p>"}]}),
                                     encoding="utf-8")
    hidden = store.create_review("Hidden", "", {"platform": "discord", "chat_id": "c"}, "local",
                                 store.parse_round_spec(decisions=str(d / "decisions.json")))
    member = account(review_id=other_review()["id"], role="reviewer")
    for suffix in ("/rounds/1/a/index.html", "/rounds/1/a/assets/missing.png"):
        forbidden = raw("GET", server + "/r/{}{}".format(hidden["id"], suffix), headers=signed_in(member, False))
        missing = raw("GET", server + "/r/{}{}".format("no-such-review-0000", suffix),
                      headers=signed_in(member, False))
        assert forbidden[0] == 404
        assert _comparable(forbidden) == _comparable(missing)


def test_a_stopped_share_link_and_a_forged_one_answer_alike(env, server, review):
    link = users.share("test", review["id"], "create")
    users.share("test", review["id"], "stop")
    stopped = raw("GET", server + link["path"])
    forged = raw("GET", server + "/s/" + token_of(link["path"])[:-3] + "xyz")
    assert stopped[0] == 404 and _comparable(stopped) == _comparable(forged)


# ---------------------------------------------------------------- CSRF

def _every_post(review):
    users.owner_user()
    target = account(name="Target")
    tok = users.create_token(target["user_id"], "t")
    inv = users.create_invite("test")
    login_link = users.create_login_link("test", target["user_id"])
    rid = review["id"]
    return [
        "/r/{}/api/submit".format(rid), "/r/{}/api/share".format(rid), "/r/{}/api/guest-name".format(rid),
        "/api/logout", "/api/sessions/sign-out-others", "/api/tokens", "/api/tokens/{}/revoke".format(tok["id"]),
        "/api/invites", "/api/invites/{}/revoke".format(inv["id"]),
        "/api/users/{}/role".format(target["user_id"]), "/api/users/{}/delete".format(target["user_id"]),
        inv["path"], login_link["path"],
    ], target, inv["id"]


@pytest.mark.parametrize("csrf", ["none", "header-only", "cookie-only", "mismatch"])
def test_every_post_needs_the_csrf_token(env, server, review, csrf):
    paths, target, invite_id = _every_post(review)
    headers = {"Content-Type": "application/json"}
    if csrf in ("header-only", "mismatch"):
        headers["X-CSRF-Token"] = "token-a"
    if csrf in ("cookie-only", "mismatch"):
        headers["Cookie"] = "mr_csrf=" + ("token-a" if csrf == "cookie-only" else "token-b")
    for path in paths:
        status, _, body = raw("POST", server + path, json.dumps({"role": "admin", "action": "create"}).encode(),
                              headers)
        assert status == 403, (path, status, body)
    assert users.get_user(target["user_id"])["role"] == "member"
    assert users.share_link(review["id"]) is None
    assert [i["state"] for i in users.list_invites() if i["id"] == invite_id] == ["open"]
    assert store.load(review["id"])["rounds"][0].get("submission") is None


def test_a_session_post_needs_that_sessions_own_token(env, server, review):
    member = account(review_id=review["id"], role="reviewer")
    url = server + "/r/{}/api/submit".format(review["id"])
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    forged = {"Cookie": "mr_session={}; mr_csrf=chosen".format(member["session"]), "X-CSRF-Token": "chosen"}
    assert http("POST", url, body, forged)[0] == 403
    assert http("POST", url, body, signed_in(member))[0] == 200


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example.test"}, {"Sec-Fetch-Site": "cross-site"},
                                     {"Origin": "null"}])
def test_a_cross_site_post_is_refused_before_the_token_check(env, server, review, headers):
    status, _, _ = http("POST", server + "/r/{}/api/share".format(review["id"]), {"action": "create"}, headers)
    assert status == 403 and users.share_link(review["id"]) is None


# ---------------------------------------------------------------- instance roles

def test_the_last_admin_cannot_be_demoted_and_the_only_admin_cannot_delete_themself(env, server):
    owner = users.owner_user()
    status, _, body = http("POST", server + "/api/users/{}/role".format(owner["id"]), {"role": "member"})
    assert status == 409 and b"last admin" in body
    status, _, body = http("POST", server + "/api/users/{}/delete".format(owner["id"]), {})
    assert status == 409 and b"only admin" in body
    status, _, body = http("GET", server + "/api/users")
    rows = json.loads(body)["users"]
    assert rows[0]["locked"] is True and rows[0]["you"] is True
    second = account(instance="admin", name="Second")
    assert users.set_role("test", owner["id"], "member")["role"] == "member"
    with pytest.raises(users.AuthError):
        users.set_role("test", second["user_id"], "member")
    with pytest.raises(users.AuthError):
        users.delete_user("test", second["user_id"])


def test_a_member_sees_devices_but_not_members_or_invites(env, server):
    users.owner_user()
    member = account()
    h = signed_in(member, False)
    assert http("GET", server + "/settings", headers=h)[0] == 200
    assert http("GET", server + "/api/sessions", headers=h)[0] == 200
    assert http("GET", server + "/api/users", headers=h)[0] == 403
    assert http("GET", server + "/api/invites", headers=h)[0] == 403


def test_deleting_an_account_ends_its_sessions_and_streams(env, server, recorder):
    users.owner_user()
    member = account()
    users.delete_user("test", member["user_id"])
    assert http("GET", server + "/api/me", headers=signed_in(member, False))[0] == 200   # loopback: the owner now
    assert json.loads(http("GET", server + "/api/me", headers=signed_in(member, False))[2])["me"]["source"] == "loopback"
    assert users.session(member["session"]) is None and len(recorder.sessions) == 1


# ---------------------------------------------------------------- devices

def test_sign_out_other_devices_ends_them_and_closes_their_streams(env, server, recorder):
    users.owner_user()
    phone = account(name="Dana")
    laptop = login(phone["user_id"])
    other = account(name="Someone else")
    status, _, body = http("GET", server + "/api/sessions", headers=signed_in(laptop, False))
    listed = json.loads(body)["sessions"]
    assert len(listed) == 2 and [s["current"] for s in listed].count(True) == 1
    status, _, body = http("POST", server + "/api/sessions/sign-out-others", {}, signed_in(laptop))
    assert status == 200 and json.loads(body)["signed_out"] == 1
    assert users.session(phone["session"]) is None
    assert users.session(laptop["session"]) is not None and users.session(other["session"]) is not None
    assert recorder.sessions == [users.digest(phone["session"])]
    assert any(r["action"] == "session.sign_out_others" for r in users.audit_rows())


def test_a_closer_that_raises_does_not_undo_the_sign_out(env, monkeypatch):
    class Broken:
        def close_sessions(self, ids):
            raise RuntimeError("registry down")

        def close_link(self, rid, gen):
            raise RuntimeError("registry down")

        def close_guests(self, rid):
            raise RuntimeError("registry down")

    streams.set_closer(Broken())
    try:
        dana = account()
        login(dana["user_id"])
        assert users.sign_out_others(dana["user_id"], users.digest(dana["session"]))["signed_out"] == 1
    finally:
        streams.set_closer(None)


def test_logout_ends_the_session(env, server):
    member = account()
    status, pairs, _ = raw("POST", server + "/api/logout", b"{}", {**signed_in(member), "Content-Type": "application/json"})
    assert status == 200 and "mr_session=;" in header(pairs, "Set-Cookie")
    assert users.session(member["session"]) is None


TAILNET = {"Tailscale-User-Login": "dana@example.test", "X-Forwarded-For": "100.101.102.103"}


def _tailnet_account():
    """An account linked to a Tailscale login, with a session cookie of its own (a browser that used an invite)."""
    return account(name="Dana", identity={"source": "tailscale", "subject": "dana@example.test", "name": "Dana",
                                          "email": "dana@example.test"})


def _row_exists(sess):
    return db.connect().execute("SELECT COUNT(*) FROM sessions WHERE id_hash = ?",
                                (users.digest(sess["session"]),)).fetchone()[0] == 1


def test_logout_under_a_proxy_identity_ends_the_cookies_session(env, server, peer, loopback_owner_off):
    """The proxy identity signs the request in before the cookie is read; sign-out must still end the cookie's
    session, or a copy of that cookie keeps working after a successful sign-out."""
    dana = _tailnet_account()
    peer["addr"] = "127.0.0.1"
    status, pairs, _ = raw("POST", server + "/api/logout", b"{}",
                           {**signed_in(dana), **TAILNET, "Content-Type": "application/json"})
    assert status == 200 and "mr_session=;" in header(pairs, "Set-Cookie")
    assert not _row_exists(dana)
    assert http("GET", server + "/api/me", headers=signed_in(dana, False))[0] == 401


def test_sign_out_others_under_a_proxy_identity_keeps_this_devices_session(env, server, peer):
    dana = _tailnet_account()
    phone = login(dana["user_id"])
    peer["addr"] = "127.0.0.1"
    status, _, body = http("GET", server + "/api/sessions", headers={**signed_in(dana, False), **TAILNET})
    assert [s["current"] for s in json.loads(body)["sessions"]].count(True) == 1
    status, _, body = http("POST", server + "/api/sessions/sign-out-others", {}, {**signed_in(dana), **TAILNET})
    assert status == 200 and json.loads(body)["signed_out"] == 1
    assert _row_exists(dana) and not _row_exists(phone)


# ---------------------------------------------------------------- the sliding session cookie

def _cookie_lines(pairs, name):
    return [c for c in header(pairs, "Set-Cookie").split("\n") if c.startswith(name + "=")]


def _age(sess, seconds):
    with db.tx() as conn:
        conn.execute("UPDATE sessions SET last_seen = last_seen - ? WHERE id_hash = ?",
                     (seconds, users.digest(sess["session"])))


@pytest.mark.parametrize("path", ["/api/me", "/api/sessions"])
def test_a_session_that_slides_gets_its_cookie_again(env, server, peer, path):
    """The server pushes the session's expiry out on use; the browser's cookie must move with it, or it is dropped
    30 days after sign-in however often it is used."""
    dana = account()
    status, pairs, _ = raw("GET", server + path, None, signed_in(dana, False))
    assert status == 200 and _cookie_lines(pairs, "mr_session") == []          # fresh: nothing to renew
    _age(dana, users.SESSION_TOUCH_SECONDS + 5)
    status, pairs, _ = raw("GET", server + path, None, signed_in(dana, False))
    assert status == 200
    [sess] = _cookie_lines(pairs, "mr_session")
    [csrf] = _cookie_lines(pairs, "mr_csrf")
    assert sess.startswith("mr_session={};".format(dana["session"]))
    assert "Max-Age={}".format(users.SESSION_SECONDS) in sess and "HttpOnly" in sess and "SameSite=Lax" in sess
    assert "Path=/" in sess and "Secure" not in sess
    assert csrf.startswith("mr_csrf={};".format(dana["csrf"])) and "Max-Age={}".format(users.SESSION_SECONDS) in csrf
    status, pairs, _ = raw("GET", server + path, None, signed_in(dana, False))
    assert _cookie_lines(pairs, "mr_session") == []                             # slid a moment ago


def test_a_renewed_cookie_is_secure_behind_an_https_proxy(env, server, peer):
    dana = account()
    _age(dana, users.SESSION_TOUCH_SECONDS + 5)
    peer["addr"] = "127.0.0.1"
    status, pairs, _ = raw("GET", server + "/api/me", None, {**signed_in(dana, False), "X-Forwarded-Proto": "https"})
    assert status == 200 and "Secure" in _cookie_lines(pairs, "mr_session")[0]


def test_a_guest_session_is_never_extended(env, server, review, peer):
    link = users.share("test", review["id"], "create", "commenter")
    guest = _join(server, link["path"])
    before = db.connect().execute("SELECT expires_at FROM sessions WHERE id_hash = ?",
                                  (users.digest(guest["session"]),)).fetchone()[0]
    _age(guest, users.SESSION_TOUCH_SECONDS + 5)
    status, pairs, _ = raw("GET", server + "/r/{}/state.json".format(review["id"]), None, signed_in(guest, False))
    assert status == 200 and _cookie_lines(pairs, "mr_session") == []
    assert db.connect().execute("SELECT expires_at FROM sessions WHERE id_hash = ?",
                                (users.digest(guest["session"]),)).fetchone()[0] == before


# ---------------------------------------------------------------- guests

def _join(server, path):
    """Open a share link as a new browser on another computer; returns its session and CSRF cookies."""
    status, pairs, _ = raw("GET", server + path)
    assert status == 303, status
    jar = {}
    for line in header(pairs, "Set-Cookie").split("\n"):
        name, _, rest = line.partition("=")
        jar[name] = rest.split(";")[0]
    return {"session": jar["mr_session"], "csrf": jar["mr_csrf"]}


def test_a_guest_joins_by_link_with_a_server_assigned_id(env, server, review, peer):
    link = users.share("test", review["id"], "create", "commenter")
    a, b = _join(server, link["path"]), _join(server, link["path"])
    me_a = json.loads(http("GET", server + "/r/{}/state.json".format(review["id"]), headers=signed_in(a, False))[2])["me"]
    me_b = json.loads(http("GET", server + "/r/{}/state.json".format(review["id"]), headers=signed_in(b, False))[2])["me"]
    assert me_a["kind"] == "guest" and me_a["guest_id"].startswith("g_") and me_a["guest_id"] != me_b["guest_id"]
    assert me_a["caps"] == ["comment", "read"]
    status, _, body = http("POST", server + "/r/{}/api/guest-name".format(review["id"]), {"name": " Blue\nFox "},
                           signed_in(a))
    assert status == 200 and json.loads(body)["name"] == "Blue Fox"
    assert users.guest(me_a["guest_id"])["display_name"] == "Blue Fox"
    second = other_review()
    assert http("GET", server + "/r/{}/".format(second["id"]), headers=signed_in(a, False))[0] == 404


def test_regenerating_a_link_kicks_its_guests_and_only_them(env, server, review, recorder, peer):
    member = account(review_id=review["id"], role="viewer")
    link = users.share("test", review["id"], "create")
    guest = _join(server, link["path"])
    state = server + "/r/{}/state.json".format(review["id"])
    assert http("GET", state, headers=signed_in(guest, False))[0] == 200
    peer["addr"] = "127.0.0.1"                                                   # the owner, on this computer
    status, _, body = http("POST", server + "/r/{}/api/share".format(review["id"]), {"action": "regenerate"})
    peer["addr"] = "192.0.2.10"
    new = json.loads(body)["link"]
    assert status == 200 and new["generation"] == link["generation"] + 1 and new["path"] != link["path"]
    assert http("GET", state, headers=signed_in(guest, False))[0] == 401          # the guest session is gone
    assert http("GET", state, headers=signed_in(member, False))[0] == 200         # members are not touched
    assert raw("GET", server + link["path"])[0] == 404                           # the old link is dead
    assert recorder.links == [(review["id"], link["generation"])] and len(recorder.sessions) == 1
    fresh = _join(server, new["path"])
    assert http("GET", state, headers=signed_in(fresh, False))[0] == 200


def test_stopping_a_link_ends_guest_access(env, server, review, peer):
    link = users.share("test", review["id"], "create", "viewer")
    guest = _join(server, link["path"])
    users.share("test", review["id"], "stop")
    assert http("GET", server + "/r/{}/".format(review["id"]), headers=signed_in(guest, False))[0] == 401
    assert users.share_link(review["id"]) is None


def test_share_link_seats_free_up_after_a_guest_has_been_idle(env, server, review, peer):
    link = users.share("test", review["id"], "create", "commenter")
    guests = [_join(server, link["path"]) for _ in range(users.SEATS - users.OWNER_SEAT)]
    assert users.seats(review["id"])["live"] == users.SEATS
    status, _, page = raw("GET", server + link["path"])
    assert status == 409 and b"This link is full" in page
    for guest in guests:
        _age(guest, 16 * 60)       # 960 s: past SEAT_IDLE (900 s); expires_at must stay where it is
    fresh = _join(server, link["path"])
    assert users.seats(review["id"])["live"] == users.OWNER_SEAT + 1
    assert http("GET", server + "/r/{}/state.json".format(review["id"]),
                headers=signed_in(fresh, False))[0] == 200


def test_a_viewer_guest_cannot_send_feedback(env, server, review, peer):
    link = users.share("test", review["id"], "create", "viewer")
    guest = _join(server, link["path"])
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    assert http("POST", server + "/r/{}/api/submit".format(review["id"]), body, signed_in(guest))[0] == 403


def test_the_share_link_url_is_recomputed_not_stored(env, review):
    link = users.share("test", review["id"], "create")
    assert users.share_link(review["id"])["path"] == link["path"]
    assert users.resolve_share_token(token_of(link["path"]))["generation"] == link["generation"]


def test_the_stream_hook_is_a_no_op_until_a_registry_installs_one(env):
    streams.close_sessions(["abc"])
    streams.close_link("r1", 1)
    rec = Recorder()
    streams.set_closer(rec)
    try:
        streams.close_sessions(["abc", ""])
        streams.close_link("r1", 1)
    finally:
        streams.set_closer(None)
    streams.close_sessions(["def"])
    assert rec.sessions == ["abc"] and rec.links == [("r1", 1)]
