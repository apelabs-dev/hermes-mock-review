"""The share view, people, seats, the version picker and the viewer projection."""

import json

from authkit import Recorder, account, header, peer, raw, signed_in, token_of  # noqa: F401
from conftest import FEEDBACK, http
from mockreview import cli, config, db, store, streams, users


def api(rid):
    return "/r/{}/api".format(rid)


def post(server, path, body, headers=None):
    return http("POST", server + path, body, headers)


def join(server, path):
    """Open a share link as a new browser on another computer; returns its session and CSRF cookies."""
    status, pairs, _ = raw("GET", server + path)
    assert status == 303, status
    jar = {}
    for line in header(pairs, "Set-Cookie").split("\n"):
        if not line:
            continue
        name, _, rest = line.partition("=")
        jar[name] = rest.split(";")[0]
    return {"session": jar["mr_session"], "csrf": jar["mr_csrf"]}


def state(server, rid, sess):
    status, _, body = http("GET", server + "/r/{}/state.json".format(rid), headers=signed_in(sess, False))
    assert status == 200, status
    return json.loads(body)


def add_rounds(review, tmp_path, count):
    page = tmp_path / "round-src" / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<!doctype html><html><body><h1>next</h1></body></html>", encoding="utf-8")
    for _ in range(count):
        store.add_round(review["id"], store.parse_round_spec(prototype=str(page)))
    return store.load(review["id"])


def make_review(variants, mode, title):
    specs = [store.parse_variant_spec(v) for v in variants]
    return store.create_review(title, "", {"platform": "discord", "chat_id": "test-chat"}, mode, specs)


def owner_post(server, path, body, peer):
    peer["addr"] = "127.0.0.1"
    try:
        return post(server, path, body)
    finally:
        peer["addr"] = "192.0.2.10"


# ---------------------------------------------------------------- the view

def test_owner_gets_link_people_seats_rounds_and_base(env, server, review, peer):
    peer["addr"] = "127.0.0.1"
    view_status, _, body = http("GET", server + api(review["id"]) + "/share")
    view = json.loads(body)
    assert view_status == 200 and view["ok"] is True
    assert view["review_id"] == review["id"] and view["current_round"] == 1 and view["rounds"] == [1]
    assert view["base"] == "https://board.example.test:8790"
    assert view["link"] is None
    assert view["seats"] == {"live": 1, "max": 25}
    assert [p["role"] for p in view["people"]] == ["owner"]
    assert view["people"][0]["name"] == "Owner"

    reviewer = account(review_id=review["id"], role="reviewer")
    forbidden = http("GET", server + api(review["id"]) + "/share", headers=signed_in(reviewer, False))
    assert forbidden[0] == 403

    missing_a = raw("GET", server + "/r/no-such-review-0000/api/share", headers=signed_in(reviewer, False))
    missing_b = raw("GET", server + "/r/no-such-review-1111/api/share", headers=signed_in(reviewer, False))
    assert missing_a[0] == missing_b[0] == 404 and missing_a[2] == missing_b[2]


def test_create_records_the_role_audience_expiry_and_round(env, server, review):
    status, _, body = post(server, api(review["id"]) + "/share",
                           {"action": "create", "guest_role": "viewer", "audience": "anyone", "expires": "24h"})
    view = json.loads(body)
    link = view["link"]
    assert status == 200 and link["guest_role"] == "viewer" and link["audience"] == "anyone"
    assert link["round"] == "latest" and link["generation"] == 1
    assert link["url"] == view["base"] + link["path"]
    assert abs(link["expires_at"] - users.now() - 86400) < 60
    row = db.connect().execute("SELECT * FROM share_links WHERE review_id = ?", (review["id"],)).fetchone()
    assert (row["guest_role"], row["audience"], row["round"]) == ("viewer", "anyone", "latest")
    assert row["expires_at"] == link["expires_at"]
    created = [r for r in users.audit_rows() if r["action"] == "share.create"][0]
    assert created["detail"]["audience"] == "anyone" and created["detail"]["round"] == "latest"
    assert created["detail"]["expires_at"] == link["expires_at"]


def test_members_only_dead_ends_an_anonymous_visitor_and_switching_back_lets_one_in(env, server, review, peer):
    link = users.share("test", review["id"], "create")
    view = json.loads(owner_post(server, api(review["id"]) + "/share",
                                 {"action": "update", "audience": "members"}, peer)[2])
    assert view["link"]["audience"] == "members"

    status, pairs, _ = raw("GET", server + link["path"])
    assert status == 404 and header(pairs, "Set-Cookie") == ""
    assert users.seats(review["id"])["live"] == 1
    member = account(review_id=review["id"], role="viewer")
    status, _, _ = raw("GET", server + link["path"], headers=signed_in(member, False))
    assert status == 303

    view = json.loads(owner_post(server, api(review["id"]) + "/share",
                                 {"action": "update", "audience": "anyone"}, peer)[2])
    assert view["link"]["audience"] == "anyone"
    guest = join(server, link["path"])
    assert state(server, review["id"], guest)["me"]["kind"] == "guest"
    assert users.seats(review["id"])["live"] == 2


def test_members_only_switch_revokes_the_guests_already_inside(env, server, review, peer):
    rid = review["id"]
    link = users.share("test", rid, "create", "reviewer")
    guest = join(server, link["path"])
    assert state(server, rid, guest)["me"]["kind"] == "guest"
    note = {"text": "a guest note", "anchor": {"block": "a", "x": 0.2, "y": 0.3}}
    assert post(server, api(rid) + "/threads", {**note, "idem": "inside"}, signed_in(guest))[0] == 201

    recorder = Recorder()
    streams.set_closer(recorder)
    try:
        view = json.loads(owner_post(server, api(rid) + "/share",
                                     {"action": "update", "audience": "members"}, peer)[2])
    finally:
        streams.set_closer(None)
    assert view["link"]["audience"] == "members"

    status, _, _ = raw("GET", server + "/r/{}/state.json".format(rid), headers=signed_in(guest, False))
    assert status == 404
    status, _, _ = post(server, api(rid) + "/threads", {**note, "idem": "revoked"}, signed_in(guest))
    assert status in (403, 404)
    status, _, _ = post(server, api(rid) + "/submit",
                        {"round": 1, "token": review["token"], "feedback": FEEDBACK}, signed_in(guest))
    assert status in (403, 404)
    assert users.session(guest["session"]) is None
    assert db.connect().execute("SELECT COUNT(*) FROM guests WHERE review_id = ?", (rid,)).fetchone()[0] == 0
    assert users.seats(rid)["live"] == 1
    assert users.digest(guest["session"]) in recorder.sessions
    assert (rid, link["generation"]) in recorder.links

    peer["addr"] = "127.0.0.1"
    try:
        owner = json.loads(http("GET", server + "/r/{}/state.json".format(rid))[2])
    finally:
        peer["addr"] = "192.0.2.10"
    assert owner["me"]["review_role"] == "owner"
    member = account(review_id=rid, role="reviewer")
    assert state(server, rid, member)["me"]["review_role"] == "reviewer"


def test_a_viewer_guest_sees_only_the_picked_round_without_token_or_feedback(env, server, review, tmp_path, peer):
    add_rounds(review, tmp_path, 2)
    link = users.share("test", review["id"], "create", "viewer")
    users.share("test", review["id"], "update", round="2")
    assert owner_post(server, api(review["id"]) + "/share", {"action": "update", "round": "9"}, peer)[0] == 400
    viewer_guest = join(server, link["path"])
    seen = state(server, review["id"], viewer_guest)
    assert [r["n"] for r in seen["rounds"]] == [2]
    assert seen["current_round"] == 2 and seen["pinned_round"] == 2
    assert "token" not in seen
    assert all(r["feedback"] is None for r in seen["rounds"])

    users.share("test", review["id"], "update", guest_role="commenter")
    commenter_guest = join(server, link["path"])
    seen = state(server, review["id"], commenter_guest)
    assert [r["n"] for r in seen["rounds"]] == [1, 2, 3] and seen["token"]

    member = account(review_id=review["id"], role="viewer")
    seen = state(server, review["id"], member)
    assert [r["n"] for r in seen["rounds"]] == [1, 2, 3] and seen["token"]
    assert "pinned_round" not in seen


def test_a_picked_round_that_no_longer_exists_falls_back_to_the_full_state(env, server, review, tmp_path, peer):
    add_rounds(review, tmp_path, 2)
    link = users.share("test", review["id"], "create", "viewer")
    users.share("test", review["id"], "update", round="3")
    guest = join(server, link["path"])
    data = store.load(review["id"])
    data["rounds"] = data["rounds"][:2]
    store.save(review["id"], data)

    seen = state(server, review["id"], guest)
    assert [r["n"] for r in seen["rounds"]] == [1, 2]
    assert seen["current_round"] == 2 and "pinned_round" not in seen
    assert "token" not in seen


def test_seats_count_live_guests_and_the_twenty_fifth_join_is_refused(env, server, review, peer):
    link = users.share("test", review["id"], "create")
    assert users.seats(review["id"]) == {"live": 1, "max": 25}
    join(server, link["path"])
    assert users.seats(review["id"])["live"] == 2

    resolved = users.resolve_share_token(token_of(link["path"]))
    for _ in range(23):
        users.join_as_guest(resolved)
    assert users.seats(review["id"])["live"] == 25

    status, pairs, page = raw("GET", server + link["path"])
    assert status == 409 and b"This link is full" in page
    assert header(pairs, "Set-Cookie") == ""
    assert users.seats(review["id"])["live"] == 25


# ---------------------------------------------------------------- people

def test_people_invite_grant_and_revoke(env, server, review, peer):
    status, _, body = owner_post(server, api(review["id"]) + "/people",
                                 {"action": "invite", "name": "Maya Kim", "role": "viewer", "expires": "7d"}, peer)
    reply = json.loads(body)
    inv = reply["invite"]
    assert status == 200 and reply["ok"] is True and inv["review_role"] == "viewer"
    assert inv["url"] == users.share_base_url(review["id"]) + inv["path"]
    row = db.connect().execute("SELECT * FROM invites WHERE id = ?", (inv["id"],)).fetchone()
    assert row["review_id"] == review["id"] and row["review_role"] == "viewer"
    assert abs(row["grant_expires_at"] - users.now() - 7 * 86400) < 60
    assert row["label"] == "Maya Kim"

    dana = account(name="Dana")
    status, _, body = owner_post(server, api(review["id"]) + "/people",
                                 {"action": "grant", "user_id": dana["user_id"], "role": "commenter"}, peer)
    people = json.loads(body)["people"]
    assert status == 200 and [(p["name"], p["role"]) for p in people if p["user_id"] == dana["user_id"]] == \
        [("Dana", "commenter")]

    status, _, body = owner_post(server, api(review["id"]) + "/people",
                                 {"action": "grant", "user_id": dana["user_id"], "role": "commenter",
                                  "expires": "7d"}, peer)
    first = [p for p in json.loads(body)["people"] if p["user_id"] == dana["user_id"]][0]
    assert status == 200 and abs(first["expires_at"] - users.now() - 7 * 86400) < 60

    status, _, body = owner_post(server, api(review["id"]) + "/people",
                                 {"action": "grant", "user_id": dana["user_id"], "role": "viewer"}, peer)
    changed = [p for p in json.loads(body)["people"] if p["user_id"] == dana["user_id"]][0]
    assert status == 200 and changed["role"] == "viewer"
    assert abs(changed["expires_at"] - first["expires_at"]) <= 1

    status, _, body = owner_post(server, api(review["id"]) + "/people",
                                 {"action": "grant", "user_id": dana["user_id"], "role": "viewer",
                                  "expires": None}, peer)
    never = [p for p in json.loads(body)["people"] if p["user_id"] == dana["user_id"]][0]
    assert status == 200 and never["expires_at"] is None

    status, _, body = owner_post(server, api(review["id"]) + "/people",
                                 {"action": "grant", "user_id": dana["user_id"], "role": "viewer",
                                  "expires": "24h"}, peer)
    day = [p for p in json.loads(body)["people"] if p["user_id"] == dana["user_id"]][0]
    assert status == 200 and abs(day["expires_at"] - users.now() - 86400) < 60

    status, _, body = owner_post(server, api(review["id"]) + "/people",
                                 {"action": "revoke", "user_id": dana["user_id"]}, peer)
    assert status == 200 and all(p["user_id"] != dana["user_id"] for p in json.loads(body)["people"])

    assert owner_post(server, api(review["id"]) + "/people",
                      {"action": "grant", "user_id": 999999, "role": "viewer"}, peer)[0] == 404
    assert owner_post(server, api(review["id"]) + "/people",
                      {"action": "grant", "user_id": dana["user_id"], "role": "boss"}, peer)[0] == 400
    assert owner_post(server, api(review["id"]) + "/people", {"action": "invite", "name": " "}, peer)[0] == 400
    assert owner_post(server, api(review["id"]) + "/people", {"action": "nonsense"}, peer)[0] == 400

    reviewer = account(review_id=review["id"], role="reviewer")
    assert post(server, api(review["id"]) + "/people",
                {"action": "invite", "name": "Nope", "role": "viewer"}, signed_in(reviewer))[0] == 403


def test_people_separate_direct_and_inherited_access(env, server, review, variants, peer):
    rid = review["id"]
    other = make_review(variants, "tailscale", "Other review")
    ivy = account(all_reviews=True, role="viewer", name="Ivy")
    bea = account(review_id=rid, role="commenter", name="Bea")
    users.grant("test", bea["user_id"], "viewer", all_reviews=True)
    users.grant("test", bea["user_id"], "viewer", review_id=other["id"])

    def view():
        peer["addr"] = "127.0.0.1"
        try:
            status, _, body = http("GET", server + api(rid) + "/share")
        finally:
            peer["addr"] = "192.0.2.10"
        assert status == 200
        return {p["user_id"]: p for p in json.loads(body)["people"]}

    people = view()
    assert (people[ivy["user_id"]]["role"], people[ivy["user_id"]]["direct_role"],
            people[ivy["user_id"]]["inherited_role"]) == ("viewer", None, "viewer")
    assert people[ivy["user_id"]]["all_reviews"] is True
    assert (people[bea["user_id"]]["role"], people[bea["user_id"]]["direct_role"],
            people[bea["user_id"]]["inherited_role"]) == ("commenter", "commenter", "viewer")

    status, _, _ = owner_post(server, api(rid) + "/people", {"action": "revoke", "user_id": ivy["user_id"]}, peer)
    assert status == 404
    assert view()[ivy["user_id"]]["inherited_role"] == "viewer"

    status, _, body = owner_post(server, api(rid) + "/people", {"action": "revoke", "user_id": bea["user_id"]}, peer)
    assert status == 200
    after = {p["user_id"]: p for p in json.loads(body)["people"]}[bea["user_id"]]
    assert (after["role"], after["direct_role"], after["inherited_role"]) == ("viewer", None, "viewer")
    assert users.grant_role(bea["user_id"], other["id"]) == "viewer"
    assert users.granted_review_ids(bea["user_id"])[0] is True


def test_all_reviews_grant_is_listed_with_its_hint(env, review):
    users.owner_user()
    member = account(all_reviews=True, role="viewer", name="Sam")
    people = users.review_people(review["id"])
    sam = [p for p in people if p["user_id"] == member["user_id"]][0]
    assert sam["all_reviews"] is True and sam["role"] == "viewer"
    assert [p["role"] for p in people][0] == "owner"


# ---------------------------------------------------------------- a viewer can never wake Hermes

def test_a_viewer_guest_cannot_submit_and_the_wake_receiver_stays_quiet(env, server, review, peer):
    link = users.share("test", review["id"], "create", "viewer")
    guest = join(server, link["path"])
    body = {"round": 1, "token": review["token"], "feedback": {"choice": "b"}}
    status, _, _ = post(server, api(review["id"]) + "/submit", body, signed_in(guest))
    assert status == 403
    assert env["receiver"].requests == []


# ---------------------------------------------------------------- the link base

def test_share_base_url_follows_the_mode_and_falls_back_for_an_unknown_review(env, review, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", "https://public.example")
    monkeypatch.setattr(config, "funnel_url", lambda: "https://board.example.ts.net")
    assert users.share_base_url(review["id"]) == "https://board.example.test:8790"
    assert users.share_url(review["id"], "/s/x") == "https://board.example.test:8790/s/x"
    assert users.share_base_url("no-such-review-0000") == "https://public.example"

    monkeypatch.delenv("MOCKREVIEW_PUBLIC_URL", raising=False)
    assert users.share_base_url("no-such-review-0000") == "https://board.example.ts.net"

    monkeypatch.setattr(config, "funnel_url", lambda: "")
    assert users.share_base_url("no-such-review-0000") == "https://board.example.test:8790"


def test_share_links_follow_each_reviews_host_mode(env, server, variants, peer, monkeypatch, capsys):
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", "https://public.example")
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_BASE", "https://tail.example")
    monkeypatch.setattr(config, "funnel_url", lambda: "https://funnel.example")
    local = make_review(variants, "local", "Local review")
    tailnet = make_review(variants, "tailscale", "Tailnet review")
    public = make_review(variants, "public", "Public review")

    local_link = users.share("test", local["id"], "create")
    tailnet_link = users.share("test", tailnet["id"], "create")
    public_link = users.share("test", public["id"], "create")
    assert local_link["url"].startswith(config.local_link_base() + "/s/")
    assert "public.example" not in local_link["url"] and "funnel.example" not in local_link["url"]
    assert tailnet_link["url"].startswith("https://tail.example/s/")
    assert "public.example" not in tailnet_link["url"] and "funnel.example" not in tailnet_link["url"]
    assert public_link["url"].startswith("https://public.example/s/")

    for review, base in ((local, config.local_link_base()), (tailnet, "https://tail.example"),
                         (public, "https://public.example")):
        body = json.loads(owner_post(server, api(review["id"]) + "/people",
                                     {"action": "invite", "name": "Maya Kim", "role": "viewer", "expires": "7d"},
                                     peer)[2])
        assert body["invite"]["url"].startswith(base + "/invite/")
        assert cli.main(["share", review["id"], "show"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["url"].startswith(base + "/s/")


# ---------------------------------------------------------------- the CLI checks the round exists

def test_cli_update_and_regenerate_keep_the_stored_deadline(env, review, tmp_path, capsys):
    add_rounds(review, tmp_path, 1)
    assert cli.main(["share", review["id"], "create", "--expires", "24h"]) == 0
    capsys.readouterr()
    first = users.share_link(review["id"])["expires_at"]
    assert abs(first - users.now() - 86400) < 60

    assert cli.main(["share", review["id"], "update", "--round", "2"]) == 0
    capsys.readouterr()
    assert users.share_link(review["id"])["expires_at"] == first

    assert cli.main(["share", review["id"], "regenerate"]) == 0
    capsys.readouterr()
    assert users.share_link(review["id"])["expires_at"] == first

    assert cli.main(["share", review["id"], "update", "--expires", "never"]) == 0
    capsys.readouterr()
    assert users.share_link(review["id"])["expires_at"] is None


def test_cli_share_refuses_a_round_the_review_does_not_have(env, review, tmp_path, capsys):
    add_rounds(review, tmp_path, 1)
    link = users.share("test", review["id"], "create")
    assert link["url"] == users.share_base_url(review["id"]) + link["path"]
    assert cli.main(["share", review["id"], "update", "--round", "9"]) == 1
    assert "round must be latest or one of this review's rounds" in capsys.readouterr().err
    stored = users.share_link(review["id"])
    assert stored["round"] == "latest" and stored["url"] == users.share_base_url(review["id"]) + stored["path"]
    assert cli.main(["share", review["id"], "update", "--round", "2"]) == 0
    assert users.share_link(review["id"])["round"] == "2"
