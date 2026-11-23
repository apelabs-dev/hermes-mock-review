"""Invite and sign-in links, throttling, audit rows, access tokens and the SQLite store."""

import hashlib
import json
import re
import sqlite3

import pytest

from authkit import account, header, raw, signed_in, token_of
from conftest import http
from mockreview import cli, db, threads, users


def _form_post(base, path, csrf="form-csrf", extra=""):
    """POST an invite / sign-in confirmation form the way a browser does: form body + the cookie its GET set."""
    body = "csrf={}{}".format(csrf, extra).encode()
    status, pairs, page = raw("POST", base + path, body, {"Content-Type": "application/x-www-form-urlencoded",
                                                          "Cookie": "mr_csrf=" + csrf})
    return status, {"Location": header(pairs, "Location"), "Set-Cookie": header(pairs, "Set-Cookie")}, page


# ---------------------------------------------------------------- invites

def test_an_invite_works_once(env, server):
    inv = users.create_invite("test")
    status, _, page = http("GET", server + inv["path"])
    assert status == 200 and b"This invite works once." in page
    status, _, page = http("GET", server + inv["path"])     # a GET (link preview, mail scanner) never uses it
    assert status == 200
    status, headers, _ = _form_post(server, inv["path"], extra="&name=Dana")
    assert status == 303 and headers["Location"] == "/"
    assert "mr_session=" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
    assert "SameSite=Lax" in headers["Set-Cookie"]
    assert [u["name"] for u in users.list_users()] == ["Dana"]
    status, _, page = _form_post(server, inv["path"])
    assert status == 404 and b"This link is not valid" in page
    status, _, _ = http("GET", server + inv["path"])
    assert status == 404
    with pytest.raises(users.AuthError):
        users.redeem("invite", token_of(inv["path"]))


def test_the_confirm_page_lets_a_browser_send_its_origin(env, server):
    """Under Referrer-Policy no-referrer a browser's form POST carries `Origin: null`, which the server refuses as a
    sandboxed frame. The confirm page is same-origin, so the real Origin arrives; every other page stays no-referrer."""
    inv = users.create_invite("test")
    status, pairs, page = raw("GET", server + inv["path"])
    assert status == 200 and header(pairs, "Referrer-Policy") == "same-origin"
    assert b'<meta name="referrer" content="same-origin">' in page
    status, pairs, page = raw("GET", server + "/invite/not-a-token")
    assert status == 404 and header(pairs, "Referrer-Policy") == "no-referrer"
    assert b'<meta name="referrer" content="no-referrer">' in page
    body = b"csrf=form-csrf&name=Dana"
    form = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": "mr_csrf=form-csrf",
            "Sec-Fetch-Site": "same-origin"}
    assert raw("POST", server + inv["path"], body, dict(form, Origin="null"))[0] == 403
    assert raw("POST", server + inv["path"], body, dict(form, Origin=server))[0] == 303


def test_an_expired_invite_is_refused(env, server, monkeypatch):
    inv = users.create_invite("test", expires="2d")
    real = users.now
    monkeypatch.setattr(users, "now", lambda: real() + 2 * 86400 + 1)
    assert http("GET", server + inv["path"])[0] == 404
    assert _form_post(server, inv["path"])[0] == 404
    with pytest.raises(users.AuthError):
        users.redeem("invite", token_of(inv["path"]))
    assert users.list_invites()[0]["state"] == "expired"


def test_an_invite_lasts_seven_days_by_default(env):
    inv = users.create_invite("test")
    assert abs(inv["expires_at"] - users.now() - 7 * 86400) < 5


@pytest.mark.parametrize("forge", ["flip", "other-key", "other-nonce", "other-purpose", "junk"])
def test_a_forged_invite_is_refused(env, server, forge):
    from itsdangerous import URLSafeTimedSerializer
    inv = users.create_invite("test")
    token = token_of(inv["path"])
    if forge == "flip":
        token = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    elif forge == "other-key":
        token = URLSafeTimedSerializer("not-the-key", salt="mock-review.invite").dumps({"i": inv["id"], "n": "x" * 32})
    elif forge == "other-nonce":   # signed right, but not the nonce the database holds a hash of
        with db.tx() as conn:
            token = users._timed(conn, "invite").dumps({"i": inv["id"], "n": "guessed-nonce"})
    elif forge == "other-purpose":  # a valid invite token presented as a sign-in link
        assert http("GET", server + "/login/" + token)[0] == 404
        return
    else:
        token = "not-a-token"
    assert http("GET", server + "/invite/" + token)[0] == 404
    assert _form_post(server, "/invite/" + token)[0] == 404
    assert users.list_users() == []


def test_a_revoked_invite_is_refused(env, server):
    inv = users.create_invite("test")
    users.revoke_invite("test", inv["id"])
    assert http("GET", server + inv["path"])[0] == 404
    assert users.list_invites()[0]["state"] == "revoked"


def test_using_an_invite_needs_the_form_token(env, server):
    inv = users.create_invite("test")
    body = b"csrf=abc"
    status, _, _ = http("POST", server + inv["path"], body, {"Content-Type": "application/x-www-form-urlencoded",
                                                             "Cookie": "mr_csrf=xyz"}, csrf=False)
    assert status == 403
    status, _, _ = http("POST", server + inv["path"], body, {"Content-Type": "application/x-www-form-urlencoded"},
                        csrf=False)
    assert status == 403
    assert users.list_invites()[0]["state"] == "open"       # a refused POST does not use the invite


def test_a_review_invite_lands_on_the_review_with_its_role(env, server, review):
    inv = users.create_invite("test", review_id=review["id"], review_role="viewer")
    status, headers, _ = _form_post(server, inv["path"])
    assert status == 303 and headers["Location"] == "/r/{}/".format(review["id"])
    uid = users.list_users()[0]["id"]
    assert users.grant_role(uid, review["id"]) == "viewer"


# ---------------------------------------------------------------- sign-in links and the CLI

def test_mockreview_login_prints_a_one_time_link(env, server, capsys):
    assert cli.main(["login"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["url"].startswith("http://127.0.0.1:") and "/login/" in out["url"]
    path = "/login/" + token_of(out["url"])
    status, headers, _ = _form_post(server, path)
    assert status == 303 and "mr_session=" in headers["Set-Cookie"]
    assert _form_post(server, path)[0] == 404


def test_a_sign_in_link_lasts_fifteen_minutes(env, monkeypatch):
    link = users.create_login_link("test")
    real = users.now
    monkeypatch.setattr(users, "now", lambda: real() + 15 * 60 + 1)
    with pytest.raises(users.AuthError):
        users.redeem("login", token_of(link["path"]))


def test_admin_login_link_for_another_account(env, capsys):
    dana = account(name="Dana")
    assert cli.main(["admin", "login-link", "--user", "Dana"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["user_id"] == dana["user_id"] and out["url"].startswith("https://board.example.test:8790/login/")
    assert users.redeem("login", token_of(out["url"]))["user_id"] == dana["user_id"]


def test_cli_users_and_invites(env, capsys, review):
    owner = users.owner_user()
    dana = account(name="Dana")
    assert cli.main(["users", "promote", "Dana"]) == 0
    assert json.loads(capsys.readouterr().out)["role"] == "admin"
    assert cli.main(["users", "demote", str(owner["id"])]) == 0
    capsys.readouterr()
    assert cli.main(["users", "demote", "Dana"]) == 1          # now the last admin
    assert "last admin" in capsys.readouterr().err
    assert cli.main(["users", "delete", "Dana"]) == 1           # and the only one
    assert cli.main(["invite", "--review", review["id"], "--role", "reviewer", "--expires", "3d"]) == 0
    inv = json.loads(capsys.readouterr().out)
    assert inv["review_role"] == "reviewer" and inv["url"].startswith("https://board.example.test:8790/invite/")
    assert cli.main(["invite", "--review", "no-such-review"]) == 1
    assert cli.main(["users", "grant", "Dana", "--review", review["id"], "--role", "viewer"]) == 0
    assert users.grant_role(dana["user_id"], review["id"]) == "viewer"
    assert cli.main(["users", "list"]) == 0


# ---------------------------------------------------------------- throttling

def test_ten_failures_a_minute_block_the_ip_for_a_minute(env, server, monkeypatch):
    good = users.create_invite("test")
    for _ in range(users.THROTTLE_FAILURES):
        assert http("GET", server + "/invite/bad-token")[0] == 404
    status, _, page = http("GET", server + good["path"])        # even a good link waits now
    assert status == 429 and b"Too many attempts" in page
    assert _form_post(server, good["path"])[0] == 429
    assert http("GET", server + "/s/bad-token")[0] == 429
    real = users.now
    monkeypatch.setattr(users, "now", lambda: real() + users.THROTTLE_BLOCK + 1)
    assert http("GET", server + good["path"])[0] == 200


def test_failures_spread_over_more_than_a_minute_do_not_block(env, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(users, "now", lambda: t[0])
    for _ in range(users.THROTTLE_FAILURES * 2):
        users.record_failure("192.0.2.1")
        t[0] += 7
    assert users.throttled("192.0.2.1") == 0


# ---------------------------------------------------------------- audit

def test_every_grant_role_and_link_change_writes_an_audit_row(env, review):
    users.owner_user()
    dana = account(review_id=review["id"], role="viewer", name="Dana")
    users.grant("user:1", dana["user_id"], "reviewer", review_id=review["id"])
    users.revoke_grant("user:1", dana["user_id"], review_id=review["id"])
    users.set_role("cli", dana["user_id"], "admin")
    users.set_role("cli", dana["user_id"], "member")
    inv = users.create_invite("cli")
    users.revoke_invite("cli", inv["id"])
    users.create_login_link("cli", dana["user_id"])
    users.share("cli", review["id"], "create", "viewer")
    users.share("cli", review["id"], "update", "commenter")
    users.share("cli", review["id"], "regenerate")
    users.share("cli", review["id"], "stop")
    tok = users.create_token(dana["user_id"], "ci")
    users.revoke_token(dana["user_id"], tok["id"])
    users.delete_user("cli", dana["user_id"])
    actions = [r["action"] for r in reversed(users.audit_rows())]
    for action in ("invite.create", "user.create", "grant.set", "invite.use", "grant.revoke", "user.role",
                   "invite.revoke", "login_link.create", "share.create", "share.update", "share.regenerate",
                   "share.stop", "token.create", "token.revoke", "user.delete"):
        assert action in actions, action
    role_rows = [r for r in users.audit_rows() if r["action"] == "user.role"]
    assert {(r["detail"]["old"], r["detail"]["new"]) for r in role_rows} == {("member", "admin"), ("admin", "member")}
    assert all(r["actor"] for r in users.audit_rows())


MO = {"source": "tailscale", "subject": "mo@example.test", "name": "Mo", "email": "mo@example.test"}


def link_id(link):
    """The invites row id of the sign-in link create_login_link() just made."""
    return db.connect().execute("SELECT MAX(id) FROM invites WHERE purpose = 'login' AND login_user_id = ?",
                                (link["user_id"],)).fetchone()[0]


def test_a_sign_in_link_used_through_another_accounts_identity_is_refused_and_stays_usable(env):
    """The session would be the link's account while every request names the identity's account: refuse, record it,
    and leave the link for its own person."""
    mo = account(name="Mo", identity=MO)
    dana = account(name="Dana")
    link = users.create_login_link("cli", dana["user_id"])
    with pytest.raises(users.AuthError) as e:
        users.redeem("login", token_of(link["path"]), identity=MO)
    assert e.value.status == 404 and e.value.message == users.DEAD_LINK
    [row] = [r for r in users.audit_rows() if r["action"] == "login_link.identity_conflict"]
    assert row["actor"] == "user:{}".format(mo["user_id"]) and row["target"] == "user:{}".format(dana["user_id"])
    assert row["detail"] == {"link": link_id(link), "source": "tailscale"}
    assert users.user_for_identity("tailscale", "mo@example.test")["id"] == mo["user_id"]
    assert users.redeem("login", token_of(link["path"]))["user_id"] == dana["user_id"]     # still usable, once
    with pytest.raises(users.AuthError):
        users.redeem("login", token_of(link["path"]))


def test_a_sign_in_link_through_its_own_accounts_identity_works(env):
    mo = account(name="Mo", identity=MO)
    link = users.create_login_link("cli", mo["user_id"])
    assert users.redeem("login", token_of(link["path"]), identity=MO)["user_id"] == mo["user_id"]


def test_a_sign_in_link_leaves_the_proxy_identity_unlinked(env):
    dana = account(name="Dana")
    link = users.create_login_link("cli", dana["user_id"])
    identity = {"source": "tailscale", "subject": "dane@example.test", "name": "Dane"}
    assert users.redeem("login", token_of(link["path"]), identity=identity)["user_id"] == dana["user_id"]
    assert users.user_for_identity("tailscale", "dane@example.test") is None


def test_an_invite_to_an_existing_account_promotes_it_but_never_demotes(env, review):
    owner = users.owner_user()
    mo = account(name="Mo", identity=MO)
    inv = users.create_invite("user:{}".format(owner["id"]), "admin")
    assert users.redeem("invite", token_of(inv["path"]), identity=MO)["user_id"] == mo["user_id"]
    assert users.get_user(mo["user_id"])["role"] == "admin"
    [row] = [r for r in users.audit_rows() if r["action"] == "user.role"]
    assert row["actor"] == "user:{}".format(owner["id"]) and row["target"] == "user:{}".format(mo["user_id"])
    assert row["detail"] == {"old": "member", "new": "admin", "invite": inv["id"]}
    inv = users.create_invite("cli", "member", review["id"], "viewer")
    assert users.redeem("invite", token_of(inv["path"]), identity=MO)["user_id"] == mo["user_id"]
    assert users.get_user(mo["user_id"])["role"] == "admin"                       # a member invite never demotes
    assert users.grant_role(mo["user_id"], review["id"]) == "viewer"              # its review grant still applies
    assert len([r for r in users.audit_rows() if r["action"] == "user.role"]) == 1
    assert len(users.list_users()) == 2


def test_a_deleted_accounts_id_is_never_reused(env, review):
    avery = account(name="Avery")
    made = threads.create(review["id"], "user:{}".format(avery["user_id"]), "Avery", None, "Avery's comment",
                          anchor={"block": "a", "x": 0.2, "y": 0.3})
    comment_id = made["comments"][0]["id"]
    users.delete_user("test", avery["user_id"])
    blake = account(name="Blake")
    assert blake["user_id"] != avery["user_id"]
    listed = threads.listing(review["id"], principal="user:{}".format(blake["user_id"]))
    [comment] = [c for t in listed["threads"] if t["id"] == made["id"] for c in t["comments"]]
    assert comment["mine"] is False
    with pytest.raises(threads.ReviewError) as exc:
        threads.edit_comment(review["id"], comment_id, "user:{}".format(blake["user_id"]), "hijacked")
    assert exc.value.status == 403


def test_a_refused_change_writes_no_audit_row(env):
    owner = users.owner_user()
    before = len(users.audit_rows())
    with pytest.raises(users.AuthError):
        users.set_role("cli", owner["id"], "member")
    assert len(users.audit_rows()) == before


# ---------------------------------------------------------------- access tokens

def test_access_tokens_are_stored_hashed(env):
    uid = users.owner_user()["id"]
    tok = users.create_token(uid, "deploy bot")
    raw = tok["token"]
    assert raw.startswith(users.TOKEN_PREFIX)
    listed = users.list_tokens(uid)
    assert [t["name"] for t in listed] == ["deploy bot"] and "token" not in listed[0] and "token_hash" not in listed[0]
    conn = sqlite3.connect(str(db.path()))
    try:
        dump = "\n".join(conn.iterdump())
        stored = conn.execute("SELECT token_hash FROM access_tokens").fetchone()[0]
    finally:
        conn.close()
    assert raw not in dump
    assert stored == hashlib.sha256(raw.encode()).hexdigest()
    assert users.token_user(raw)["id"] == uid
    users.revoke_token(uid, tok["id"])
    assert users.token_user(raw) is None and users.list_tokens(uid) == []
    with pytest.raises(users.AuthError):
        users.revoke_token(uid, tok["id"])


def test_tokens_over_http_and_the_cli(env, server, capsys):
    dana = account(name="Dana")
    status, _, body = http("POST", server + "/api/tokens", {"name": "laptop"}, signed_in(dana))
    created = json.loads(body)
    assert status == 200 and created["token"].startswith("mrp_")
    status, _, body = http("GET", server + "/api/tokens", headers=signed_in(dana, csrf=False))
    assert [t["id"] for t in json.loads(body)["tokens"]] == [created["id"]]
    status, _, _ = http("POST", server + "/api/tokens/{}/revoke".format(created["id"]), {}, signed_in(dana))
    assert status == 200 and users.list_tokens(dana["user_id"]) == []
    assert cli.main(["tokens", "create", "ci"]) == 0
    assert json.loads(capsys.readouterr().out)["token"].startswith("mrp_")


def test_sessions_and_link_nonces_are_stored_hashed(env):
    dana = account(name="Dana")
    inv = users.create_invite("test")
    conn = sqlite3.connect(str(db.path()))
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert dana["session"] not in dump
    payload = token_of(inv["path"]).split(".")[0]
    assert payload not in dump


# ---------------------------------------------------------------- the store

def test_migrations_are_versioned_idempotent_and_include_comments_and_events(env):
    conn = db.connect()
    assert db.version(conn) == db.MIGRATIONS[-1][0] == 7
    assert db.migrate(conn) == 7
    assert [n for n, _ in db.MIGRATIONS] == [1, 2, 3, 4, 5, 6, 7]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"users", "identities", "sessions", "grants", "invites", "share_links", "guests", "access_tokens", "audit",
            "login_throttle", "revoked_sessions", "threads", "comments", "mentions", "reads", "events"} <= tables
    assert "round" in {r["name"] for r in conn.execute("PRAGMA table_info(share_links)")}
    for table in ("share_links", "grants", "invites"):
        assert "wake_ok" in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert re.search(r"\b2\b.*comments", db.__doc__, re.S) and re.search(r"\b3\b.*events", db.__doc__, re.S)
    assert re.search(r"\b4\b.*round", db.__doc__, re.S) and re.search(r"\b5\b.*revoked", db.__doc__, re.S)
    assert re.search(r"\b6\b.*wake_ok", db.__doc__, re.S) and re.search(r"\b7\b.*invites", db.__doc__, re.S)


def test_each_thread_gets_its_own_connection(env):
    import threading
    mine = db.connect()
    seen = []
    t = threading.Thread(target=lambda: seen.append(db.connect()))
    t.start()
    t.join()
    assert seen and seen[0] is not mine and db.connect() is mine


# ---------------------------------------------------------------- sign-in links and the shared cookie

def _confirm(server, path):
    """GET a link's confirmation form: (its form token, the cookie the GET set)."""
    status, pairs, page = raw("GET", server + path)
    assert status == 200
    found = re.search(rb'name="csrf" value="([^"]+)"', page)
    assert found, page[:400]
    return found.group(1).decode(), header(pairs, "Set-Cookie")


def _post_form(server, path, token, cookie=""):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    return raw("POST", server + path, ("csrf=" + token).encode(), headers)


def test_a_share_link_in_the_same_browser_does_not_break_the_sign_in(env, server, review, monkeypatch):
    """The reported bug: GET /login/<t>, then a share link in the same browser, then press Sign in.

    `mr_csrf` is one host-scoped cookie, so the share link's session replaces the value the confirm form rendered.
    The form's token is bound to the link, so the POST still redeems it; on 44d89b3 this answered a raw JSON 403.
    The loopback owner is off so the browser is a phone or laptop, not this computer (which is already the owner).
    """
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    link = users.create_login_link("cli")
    form_token, _ = _confirm(server, link["path"])
    share = users.share("cli", review["id"], action="create", guest_role="commenter", audience="anyone")
    status, pairs, _ = raw("GET", server + share["path"])
    assert status == 303
    moved = header(pairs, "Set-Cookie")
    assert "mr_csrf=" in moved, moved                       # the share link overwrote the cookie this browser holds
    cookie = "mr_csrf=" + moved.split("mr_csrf=")[1].split(";")[0]
    assert cookie.split("=", 1)[1] != form_token
    status, headers, page = _post_form(server, link["path"], form_token, cookie)
    assert status == 303 and header(headers, "Location") == "/", page[:400]


def test_a_link_token_signs_in_with_no_cookie_at_all(env, server, monkeypatch):
    """The form's token carries the proof now: a browser that lost the cookie (or never had it) still signs in."""
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    link = users.create_login_link("cli")
    form_token, _ = _confirm(server, link["path"])
    status, headers, page = _post_form(server, link["path"], form_token)
    assert status == 303 and header(headers, "Location") == "/", page[:400]
    assert "mr_session=" in header(headers, "Set-Cookie")


def test_a_wrong_token_rerenders_the_confirm_page_and_keeps_the_link(env, server, monkeypatch):
    """A mismatch is never JSON, never uses the link, and never counts as a failed sign-in."""
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    link = users.create_login_link("cli")
    form_token, _ = _confirm(server, link["path"])
    for cookie in ("mr_csrf=another-token", ""):
        status, pairs, page = _post_form(server, link["path"], "stale-token", cookie)
        assert status == 403, (status, page[:200])
        assert header(pairs, "Content-Type").startswith("text/html")
        assert b"<form" in page and form_token.encode() in page
        assert users.check_link("login", token_of(link["path"]))["purpose"] == "login"
    assert users.throttled("127.0.0.1") == 0.0            # a stale token is not a failed sign-in
    status, headers, _ = _post_form(server, link["path"], form_token)
    assert status == 303                                   # the same link still works


def test_a_valid_link_token_from_a_foreign_origin_is_refused(env, server, monkeypatch):
    """The Origin / Fetch-Metadata guard, not the token, is what stops another site posting this form."""
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    link = users.create_login_link("cli")
    form_token, _ = _confirm(server, link["path"])
    body = ("csrf=" + form_token).encode()
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert raw("POST", server + link["path"], body, dict(form, Origin="https://evil.example.test"))[0] == 403
    assert raw("POST", server + link["path"], body, dict(form, **{"Sec-Fetch-Site": "cross-site"}))[0] == 403
    assert users.check_link("login", token_of(link["path"]))["purpose"] == "login"


# ---------------------------------------------------------------- a confirm page leaves a signed-in tab alone

@pytest.mark.parametrize("purpose", ["login", "invite"])
def test_opening_a_confirm_page_keeps_a_signed_in_board_tab_working(env, server, review, purpose):
    """A confirm GET shares one host-scoped cookie jar with every board tab, so it must not move `mr_csrf`.

    A signed-in reviewer's tab reads `mr_csrf` from `document.cookie` and sends it as `X-CSRF-Token`. A link page
    that overwrote it with the link's HMAC made the tab's next write answer 403.
    """
    from conftest import FEEDBACK
    member = account(instance="admin", name="Admin")      # Send is the owner's: an admin account is an owner
    link = users.create_login_link("cli") if purpose == "login" else users.create_invite("test")
    jar = {"mr_session": member["session"], "mr_csrf": member["csrf"]}
    status, pairs, _ = raw("GET", server + link["path"],
                           headers={"Cookie": "mr_session={}; mr_csrf={}".format(member["session"], member["csrf"])})
    assert status == 200
    for pair in header(pairs, "Set-Cookie").splitlines():     # a browser applies them to that one jar
        name, _, rest = pair.partition("=")
        if name.strip():
            jar[name.strip()] = rest.split(";", 1)[0]
    current = "mr_session={}; mr_csrf={}".format(jar["mr_session"], jar["mr_csrf"])
    status, _, page = http("POST", server + "/r/%s/api/submit" % review["id"],
                           {"round": 1, "token": review["token"], "feedback": FEEDBACK},
                           {"Cookie": current, "X-CSRF-Token": jar["mr_csrf"]})
    assert status == 200, page[:400]
    assert "mr_csrf=" not in header(pairs, "Set-Cookie")
