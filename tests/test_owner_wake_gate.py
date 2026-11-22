"""Who may wake Hermes, per request: the owner from private ingress, a non-owner reviewer by mention on a toolset-free
messaging board, and nobody through Funnel.

The board gate (wakeguard.exposure) no longer refuses a waking board while Funnel maps it; the request gate
(wakeguard.request_gate, auth.private_ingress) holds every request that arrives through Funnel or another public path
instead. The test server listens on 127.0.0.1, so a request with no proxy headers is the loopback owner; a request that
carries X-Forwarded-For is proxied (Tailscale Serve sets exactly one tailnet address there, Funnel adds its
`Tailscale-Funnel-Request` marker) and is signed in by an admin account's session cookie.
"""

import json

import pytest

from authkit import account, peer, signed_in, token_of  # noqa: F401  (peer is a fixture)
from conftest import FEEDBACK, http
from mockreview import access, api, auth, config, service, store, users, wakeguard
from mockreview.store import ReviewError
from test_all_platform_wake import (LOCAL_CHAT, LOCAL_KEY, MENTION_DECISIONS, bind_session, create_tool,  # noqa: F401
                                    gateway, wait_for)
from test_mention_wake import board as mention_board, tick, wake_records
from test_owner import DEC_FB
from test_plan_format import PLAN, _all_resolved
from test_plugin import _decisions

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
FUNNEL_URL = "https://node.tailnet.example.test"
FUNNEL_HOST = "node.tailnet.example.test"
SERVE = {"X-Forwarded-For": "100.64.0.7"}                              # Tailscale Serve: one tailnet hop
FUNNEL = {**SERVE, "Tailscale-Funnel-Request": "?1"}                  # the same request, marked as Funnel ingress
PIN_DECISIONS = {"blocks": [
    {"type": "decision", "id": "d1", "title": "Layout", "layout": "side", "options": [
        {"id": "a", "label": "Today", "html": "<div class='a'>A</div>"},
        {"id": "b", "label": "All", "recommended": True, "html": "<div class='b'>B</div>"}]},
    {"type": "mock", "id": "m1", "title": "Today", "html": "<p>today</p>"},
]}
QUICK = {"pin_fix": True, "pin_fix_min_seconds": 0.0, "pin_fix_window_seconds": 0.0}


# ---------------------------------------------------------------- helpers


@pytest.fixture
def funnel(monkeypatch):
    """`mockreview public on` mapped Funnel to the board; public_url stays unset."""
    monkeypatch.setattr(config, "funnel_url", lambda: FUNNEL_URL)
    return FUNNEL_URL


@pytest.fixture
def funnel_server(funnel, server):
    """A board server whose allowlist was built while Funnel maps it (the Funnel name is a public name)."""
    return server


@pytest.fixture
def owner():
    """An admin account: the owner of every review, signed in with a session cookie."""
    return account(instance="admin", name="Owner")


def as_(sess, extra=None):
    return {**signed_in(sess), **(extra or {})}


def join_guest(rid, role="reviewer"):
    made = users.share("test", rid, "create", role)
    return users.join_as_guest(users.resolve_share_token(token_of(made["path"])))


def submit(server, rid, token, headers=None, amend=False, feedback=None):
    body = {"round": 1, "token": token, "feedback": feedback or FEEDBACK}
    if amend:
        body["amend"] = True
    code, _, out = http("POST", f"{server}/r/{rid}/api/submit", body, headers)
    return code, json.loads(out)


def mention(server, rid, headers=None, idem="c1", text="@Hermes make this clearer", thread_id=None):
    body = {"text": text, "mentions": ["hermes"], "idem": idem}
    if thread_id is None:
        body["anchor"] = {"block": "m1", "x": 0.2, "y": 0.3}
        path = "threads"
    else:
        path = f"threads/{thread_id}/reply"
    code, _, out = http("POST", f"{server}/r/{rid}/api/{path}", body, headers)
    return code, json.loads(out)


def pin_fix(server, rid, token, headers=None, text="make it bigger", idem=None):
    body = {"round": 1, "token": token, "pin": {"block": "m1", "text": text}}
    if idem:
        body["idem"] = idem
    code, _, out = http("POST", f"{server}/r/{rid}/api/pin-fix", body, headers)
    return code, json.loads(out)


def pin_board(tmp_path, look=None):
    path = tmp_path / "pin-decisions.json"
    path.write_text(json.dumps(PIN_DECISIONS), encoding="utf-8")
    return store.create_review("Pin board", "brief", ORIGIN, "local", store.parse_round_spec(decisions=str(path)),
                               look={**QUICK, **(look or {})})


def plan_board(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(PLAN), encoding="utf-8")
    return store.create_review("Plan", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)))


def pin_requests(rid):
    return store.load(rid)["rounds"][-1].get("pin_requests") or {}


# ---------------------------------------------------------------- 1. the board gate: Funnel no longer refuses


def test_a_funnel_mapping_does_not_refuse_a_waking_tools_board(env, tmp_path, monkeypatch, funnel):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    assert wakeguard.exposure("local", True) is None
    assert wakeguard.exposure("local", True, session=True) is None
    api.check_wake_toolsets("local", wake=True, origin=ORIGIN)
    data = api.create("Tools board", ORIGIN, mode="local", decisions=str(_decisions(tmp_path)))
    assert store.load(data["review_id"]).get("wake") is not False


def test_a_funnel_mapping_does_not_refuse_a_waking_desktop_board(env, tmp_path, monkeypatch, gateway, funnel):
    ctx, _ = gateway
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    assert not wakeguard.granted()
    out = create_tool(ctx, title="Desktop over Funnel", decisions=str(_decisions(tmp_path)))
    assert "error" not in out, out
    assert store.load(out["review_id"])["origin"]["platform"] == "local"


@pytest.mark.parametrize("ungate", ["public_url", "lan"])
def test_public_url_and_a_lan_bind_still_refuse_a_waking_tools_board(env, tmp_path, monkeypatch, funnel, ungate):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    if ungate == "public_url":
        monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", "https://reviews.example.test")
    else:
        monkeypatch.setattr(wakeguard, "bound_host", lambda: "0.0.0.0")
    with pytest.raises(ReviewError) as e:
        api.create("Ungated", ORIGIN, mode="local", decisions=str(_decisions(tmp_path)))
    assert e.value.status == 409 and "wake_toolsets" in e.value.message
    assert store.list_ids() == []


def test_a_funnel_mapping_after_create_does_not_hold_a_delivery(env, tmp_path, monkeypatch, gateway):
    """The owner's Send passed the request gate on the tailnet; Funnel turned on since does not hold its delivery."""
    ctx, _ = gateway
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    rid = create_tool(ctx, title="Later Funnel", decisions=str(_decisions(tmp_path)))["review_id"]
    monkeypatch.setattr(config, "funnel_url", lambda: FUNNEL_URL)
    res = service.submit(rid, 1, DEC_FB, store.load(rid)["token"])
    assert res["notified"] is True, res
    assert len(ctx.injected) == 1


# ---------------------------------------------------------------- 2. the owner: private ingress wakes, Funnel stores


@pytest.mark.parametrize("ingress", ["loopback", "serve"])
def test_the_owners_send_wakes_from_loopback_and_tailnet_serve(env, funnel_server, review, owner, ingress):
    headers = None if ingress == "loopback" else as_(owner, SERVE)
    code, res = submit(funnel_server, review["id"], review["token"], headers)
    assert code == 200 and res["status"] == "notified" and res["notified"] is True, res
    assert len(env["receiver"].wakes) == 1


def test_the_owners_send_over_serve_on_the_shared_funnel_name_wakes(env, funnel_server, review, owner):
    """Funnel and tailnet Serve share the node's name, so the Funnel hostname alone never holds the owner's Send."""
    code, res = submit(funnel_server, review["id"], review["token"], as_(owner, {**SERVE, "Host": FUNNEL_HOST}))
    assert code == 200 and res["status"] == "notified", res
    assert len(env["receiver"].wakes) == 1


def test_the_owners_send_through_funnel_is_stored_and_amend_from_the_tailnet_wakes(env, funnel_server, review,
                                                                                    owner):
    rid = review["id"]
    code, res = submit(funnel_server, rid, review["token"], as_(owner, {**FUNNEL, "Host": FUNNEL_HOST}))
    assert code == 200 and res["status"] == "stored" and res["notified"] is False, res
    assert res["detail"] == wakeguard.FUNNEL_NO_WAKE
    assert store.load(rid)["rounds"][0]["submission"]["status"] == "stored"
    assert service.retry_pending() == 0
    assert env["receiver"].requests == []

    code, res = submit(funnel_server, rid, review["token"], as_(owner, SERVE), amend=True)
    assert code == 200 and res["status"] == "notified" and res["amended"] is True, res
    assert len(env["receiver"].wakes) == 1


@pytest.mark.parametrize("ingress", ["loopback", "serve"])
def test_the_owners_mention_wakes_from_loopback_and_tailnet_serve(env, funnel_server, tmp_path, owner, ingress):
    rid = mention_board(tmp_path)["id"]
    headers = None if ingress == "loopback" else as_(owner, SERVE)
    code, res = mention(funnel_server, rid, headers)
    assert code == 201 and res["wake"]["status"] == "pending_notify", res
    assert res["thread"]["comments"][0]["sent_to_hermes"] is True
    tick()
    assert len(env["receiver"].wakes) == 1


def test_the_owners_mention_through_funnel_is_stored_and_a_tailnet_one_wakes(env, funnel_server, tmp_path, owner):
    rid = mention_board(tmp_path)["id"]
    code, res = mention(funnel_server, rid, as_(owner, FUNNEL), idem="funnel")
    assert code == 201, res
    assert res["wake"] == {"status": "stored", "notified": False, "detail": wakeguard.FUNNEL_NO_WAKE}
    comment = res["thread"]["comments"][0]
    assert comment["text"] == "@Hermes make this clearer" and comment["sent_to_hermes"] is False
    assert res["thread"]["activated"] is False
    tick()
    assert env["receiver"].wakes == [] and wake_records(rid) == {}

    code, res = mention(funnel_server, rid, as_(owner, SERVE), idem="tailnet", thread_id=res["thread"]["id"])
    assert code == 200 and res["wake"]["status"] == "pending_notify", res
    tick()
    assert len(env["receiver"].wakes) == 1


@pytest.mark.parametrize("ingress", ["loopback", "serve"])
def test_the_owners_pin_fix_wakes_from_loopback_and_tailnet_serve(env, funnel_server, tmp_path, owner, ingress):
    data = pin_board(tmp_path)
    headers = None if ingress == "loopback" else as_(owner, SERVE)
    code, res = pin_fix(funnel_server, data["id"], data["token"], headers)
    assert code == 200 and res["notified"] is True, res
    assert len(env["receiver"].wakes) == 1


def test_the_owners_pin_fix_through_funnel_is_stored_quiet_and_a_tailnet_one_wakes(env, funnel_server, tmp_path,
                                                                                    owner):
    data = pin_board(tmp_path)
    rid = data["id"]
    code, res = pin_fix(funnel_server, rid, data["token"], as_(owner, FUNNEL), idem="funnel")
    assert code == 200 and res["notified"] is False and res["detail"] == wakeguard.FUNNEL_NO_WAKE, res
    assert res["request"]["quiet"] is True
    service.flush_pin_fixes(rid)
    service.retry_pending()
    assert env["receiver"].wakes == []

    code, res = pin_fix(funnel_server, rid, data["token"], as_(owner, SERVE), text="and the gap", idem="tailnet")
    assert code == 200 and res["notified"] is True, res
    assert len(env["receiver"].wakes) == 1
    body = json.loads(env["receiver"].wakes[0]["body"])
    assert body["requests"] == [res["request"]["id"]]                     # the quiet Funnel request never rides along


def test_the_owners_desktop_board_send_through_funnel_is_stored(env, tmp_path, monkeypatch, gateway, owner):
    """A Desktop (session) wake gets the same request gate: the owner's Send from Funnel is saved, not injected."""
    ctx, base = gateway
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    rid = create_tool(ctx, title="Desktop gate", decisions=str(_decisions(tmp_path)))["review_id"]
    token = store.load(rid)["token"]
    code, res = submit(base, rid, token, as_(owner, FUNNEL), feedback=DEC_FB)
    assert code == 200 and res["status"] == "stored" and res["detail"] == wakeguard.FUNNEL_NO_WAKE, res
    assert ctx.injected == []
    code, res = submit(base, rid, token, None, amend=True, feedback=DEC_FB)
    assert code == 200 and res["status"] == "notified", res
    assert len(ctx.injected) == 1


# ---------------------------------------------------------------- 3. a missing or forged marker is not private


@pytest.mark.parametrize("addr, headers", [
    ("192.0.2.10", {}),                                                  # a public peer, no proxy headers
    ("10.0.0.8", {}),                                                    # a LAN peer, no proxy headers
    ("127.0.0.1", {"X-Forwarded-For": "203.0.113.5"}),                   # a trusted proxy forwarding a public client
    ("127.0.0.1", {"X-Forwarded-For": "100.64.0.7, 100.64.0.8"}),        # a chain: not straight from Serve
    ("192.0.2.10", SERVE),                                               # an untrusted peer claiming a tailnet hop
])
def test_an_owner_request_without_a_private_signal_is_stored(env, server, review, owner, peer, addr, headers):
    peer["addr"] = addr
    code, res = submit(server, review["id"], review["token"], as_(owner, headers))
    assert code == 200 and res["status"] == "stored", res
    assert res["detail"] == wakeguard.OUTSIDE_NO_WAKE                  # not Funnel: no "public link" wording
    assert env["receiver"].requests == []


def _req(peer_addr, headers=None, local=False, public_host=False):
    return auth.Request(peer_addr, auth.as_mapping(headers or {}), local=local, public_host=public_host)


@pytest.mark.parametrize("request_, private", [
    (_req("127.0.0.1", local=True), True),                                               # this computer
    (_req("127.0.0.1", {"Tailscale-Funnel-Request": "?1"}, local=True), False),
    (_req("127.0.0.1", {"tailscale-funnel-request": "?1"}), False),
    (_req("127.0.0.1", {"TAILSCALE_FUNNEL_REQUEST": "1"}), False),                       # any spelling, any value
    (_req("127.0.0.1", {**SERVE, "Tailscale_Funnel_Request": ""}), False),
    (_req("100.64.0.9"), True),                                                          # a tailnet peer
    (_req("fd7a:115c:a1e0::9"), True),
    (_req("100.64.0.9", {"Tailscale-Funnel-Request": "?1"}), False),
    (_req("127.0.0.1", SERVE), True),                                                    # Tailscale Serve's hop
    (_req("127.0.0.1", {"X-Forwarded-For": "fd7a:115c:a1e0::7"}), True),
    (_req("127.0.0.1", SERVE, public_host=True), True),                                  # the shared *.ts.net name
    (_req("127.0.0.1", FUNNEL, public_host=True), False),
    (_req("127.0.0.1", {"X-Forwarded-For": "100.64.0.7, 100.64.0.8"}), False),           # a chain
    (_req("127.0.0.1", {"X-Forwarded-For": "203.0.113.5"}), False),                      # a public client
    (_req("127.0.0.1", {"X-Forwarded-For": "203.0.113.5, 100.64.0.7"}), False),
    (_req("127.0.0.1", {}), False),                                                      # proxied, no hop at all
    (_req("192.0.2.10"), False),                                                         # a public peer, no marker
    (_req("10.0.0.8"), False),                                                           # a LAN peer, no marker
    (_req("192.0.2.10", SERVE), False),                                                  # an untrusted "Serve hop"
])
def test_private_ingress_table(request_, private):
    assert auth.private_ingress(request_) is private


# ---------------------------------------------------------------- the rule as a table


TOOLS = "tools"


@pytest.mark.parametrize("kind, role, private, origin, funnel, want", [
    # the owner, from private ingress: every kind wakes, whatever the origin and wake_toolsets
    ("submit", "owner", True, ORIGIN, False, ""),
    ("pin-fix", "owner", True, ORIGIN, False, ""),
    ("mention", "owner", True, ORIGIN, False, ""),
    ("mention", "owner", True, {"platform": "local", "chat_id": "x"}, False, ""),
    ("mention", "owner", True, TOOLS, False, ""),
    # the owner, not private: saved, with the wording of the path it came in by
    ("submit", "owner", False, ORIGIN, True, wakeguard.FUNNEL_NO_WAKE),
    ("pin-fix", "owner", False, ORIGIN, True, wakeguard.FUNNEL_NO_WAKE),
    ("mention", "owner", False, ORIGIN, True, wakeguard.FUNNEL_NO_WAKE),
    ("submit", "owner", False, ORIGIN, False, wakeguard.OUTSIDE_NO_WAKE),
    ("pin-fix", "owner", False, ORIGIN, False, wakeguard.OUTSIDE_NO_WAKE),
    ("mention", "owner", False, ORIGIN, False, wakeguard.OUTSIDE_NO_WAKE),
    # a reviewer who is not the owner
    ("submit", "reviewer", True, ORIGIN, False, wakeguard.SUBMIT_OWNER_ONLY),
    ("pin-fix", "reviewer", True, ORIGIN, False, wakeguard.PIN_FIX_OWNER_ONLY),
    ("mention", "reviewer", True, ORIGIN, False, ""),
    ("mention", "reviewer", False, ORIGIN, True, ""),                    # any ingress, Funnel included
    ("mention", "reviewer", True, {"platform": "local", "chat_id": "x"}, False, wakeguard.TOOLS_NO_WAKE),
    ("mention", "reviewer", True, TOOLS, False, wakeguard.TOOLS_NO_WAKE),
    # commenters and viewers never wake
    ("mention", "commenter", True, ORIGIN, False, wakeguard.MENTION_ROLE_NO_WAKE),
    ("mention", "viewer", True, ORIGIN, False, wakeguard.MENTION_ROLE_NO_WAKE),
    ("submit", "commenter", True, ORIGIN, False, wakeguard.SUBMIT_OWNER_ONLY),
    ("pin-fix", "viewer", True, ORIGIN, False, wakeguard.PIN_FIX_OWNER_ONLY),
    # anything unknown never wakes
    ("mention", None, True, ORIGIN, False, wakeguard.MENTION_ROLE_NO_WAKE),
    ("delete-everything", "reviewer", True, ORIGIN, False, wakeguard.SUBMIT_OWNER_ONLY),
])
def test_request_gate_table(monkeypatch, kind, role, private, origin, funnel, want):
    if origin == TOOLS:
        monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
        origin = ORIGIN
    assert wakeguard.request_gate(kind, role, private, origin, funnel=funnel) == want


def test_the_gate_reads_the_role_access_resolves():
    """`role` is access.Decision.role: an admin is the owner, a member gets its grant, a guest its link's role."""
    member = access.resolve(access.Principal("member", user_id=2), "r1", True, "reviewer")
    guest = access.resolve(access.Principal("guest", guest_review="r1", guest_generation=1), "r1", True, None,
                           access.Link(1, "reviewer"))
    admin = access.resolve(access.Principal("owner", user_id=1), "r1", True)
    assert (member.role, guest.role, admin.role) == ("reviewer", "reviewer", "owner")
    assert wakeguard.request_gate("submit", admin.role, True, ORIGIN) == ""
    assert wakeguard.request_gate("submit", member.role, True, ORIGIN) == wakeguard.SUBMIT_OWNER_ONLY


# ---------------------------------------------------------------- 4. Send, amend and Build are the owner's


def test_a_guest_reviewers_send_amend_and_build_are_refused(env, server, tmp_path):
    data = plan_board(tmp_path)
    rid = data["id"]
    guest = join_guest(rid)
    send = {"marks": _all_resolved(), "action": "send"}
    build = {"marks": _all_resolved(), "action": "build"}
    for feedback, amend in ((send, False), (send, True), (build, False)):
        code, res = submit(server, rid, data["token"], signed_in(guest), amend=amend, feedback=feedback)
        assert code == 403 and res["error"] == wakeguard.SUBMIT_OWNER_ONLY, res
    assert store.load(rid)["rounds"][0].get("submission") is None
    assert env["receiver"].requests == []


def test_a_member_reviewers_send_and_build_are_refused_and_the_owners_send_still_works(env, server, tmp_path):
    data = plan_board(tmp_path)
    rid = data["id"]
    member = account(review_id=rid, role="reviewer")
    for action in ("send", "build"):
        code, res = submit(server, rid, data["token"], signed_in(member),
                           feedback={"marks": _all_resolved(), "action": action})
        assert code == 403 and res["error"] == wakeguard.SUBMIT_OWNER_ONLY, res
    assert store.load(rid)["rounds"][0].get("submission") is None
    assert env["receiver"].requests == []

    code, res = submit(server, rid, data["token"], None, feedback={"marks": _all_resolved(), "action": "build"})
    assert code == 200 and res["status"] == "notified", res
    assert len(env["receiver"].wakes) == 1


# ---------------------------------------------------------------- 5. a non-owner reviewer's mention


@pytest.mark.parametrize("ingress", ["loopback", "funnel"])
def test_a_guest_reviewers_mention_on_a_toolset_free_messaging_board_wakes(env, funnel_server, tmp_path, ingress):
    rid = mention_board(tmp_path)["id"]
    guest = join_guest(rid)
    extra = {**FUNNEL, "X-Forwarded-For": "203.0.113.5", "Host": FUNNEL_HOST} if ingress == "funnel" else None
    code, res = mention(funnel_server, rid, as_(guest, extra))
    assert code == 201 and res["wake"]["status"] == "pending_notify", res
    assert res["thread"]["activated"] is True and res["thread"]["comments"][0]["sent_to_hermes"] is True
    rec = next(iter(wake_records(rid).values()))
    assert rec["by_owner"] is False and rec["author"].startswith("guest:")
    tick()
    assert len(env["receiver"].wakes) == 1
    assert json.loads(env["receiver"].wakes[0]["body"])["event_type"] == "review.thread_mention"


def test_a_member_reviewers_mention_behaves_like_a_guest_reviewers(env, server, tmp_path, monkeypatch):
    rid = mention_board(tmp_path)["id"]
    member = account(review_id=rid, role="reviewer")
    code, res = mention(server, rid, signed_in(member))
    assert code == 201 and res["wake"]["status"] == "pending_notify", res
    assert next(iter(wake_records(rid).values()))["by_owner"] is False
    tick()
    assert len(env["receiver"].wakes) == 1

    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    code, res = mention(server, rid, signed_in(member), idem="c2")
    assert code == 201 and res["wake"] == {"status": "stored", "notified": False,
                                           "detail": wakeguard.TOOLS_NO_WAKE}, res
    tick()
    assert len(env["receiver"].wakes) == 1


def test_a_guest_reviewers_mention_on_a_board_with_wake_toolsets_is_stored(env, server, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    rid = mention_board(tmp_path)["id"]
    guest = join_guest(rid)
    code, res = mention(server, rid, signed_in(guest))
    assert code == 201, res
    assert res["wake"] == {"status": "stored", "notified": False, "detail": wakeguard.TOOLS_NO_WAKE}
    assert res["thread"]["activated"] is False and res["thread"]["comments"][0]["sent_to_hermes"] is False
    assert res["thread"]["comments"][0]["text"] == "@Hermes make this clearer"
    tick()
    assert env["receiver"].wakes == [] and wake_records(rid) == {}


def test_a_guest_reviewers_mention_on_a_desktop_board_is_stored(env, tmp_path, monkeypatch, gateway):
    ctx, base = gateway
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    dec = tmp_path / "mention.json"
    dec.write_text(json.dumps(MENTION_DECISIONS), encoding="utf-8")
    rid = create_tool(ctx, title="Desktop mention", decisions=str(dec))["review_id"]
    guest = join_guest(rid)
    code, res = mention(base, rid, signed_in(guest))
    assert code == 201, res
    assert res["wake"] == {"status": "stored", "notified": False, "detail": wakeguard.TOOLS_NO_WAKE}
    service.retry_pending()
    assert ctx.injected == [] and env["receiver"].requests == []

    code, res = mention(base, rid, None, idem="owner")                  # the loopback owner still wakes it
    assert code == 201 and res["wake"]["status"] == "pending_notify", res
    service.retry_pending()
    assert wait_for(lambda: ctx.injected)


@pytest.mark.parametrize("role", ["commenter", "viewer"])
def test_a_guest_commenters_mention_never_wakes(env, server, tmp_path, role):
    rid = mention_board(tmp_path)["id"]
    guest = join_guest(rid, role)
    code, res = mention(server, rid, signed_in(guest))
    if role == "viewer":
        assert code == 403, res                                        # a viewer cannot comment at all
        return
    assert code == 201, res
    assert res["wake"] == {"status": "stored", "notified": False, "detail": wakeguard.MENTION_ROLE_NO_WAKE}
    assert res["thread"]["activated"] is False
    tick()
    assert env["receiver"].wakes == []


def test_a_reviewers_mention_on_a_wake_off_board_never_wakes(env, server, tmp_path):
    rid = mention_board(tmp_path, wake=False)["id"]
    guest = join_guest(rid)
    code, res = mention(server, rid, signed_in(guest))
    assert code == 201 and "wake" not in res and res["thread"]["activated"] is False, res
    tick()
    assert env["receiver"].wakes == [] and wake_records(rid) == {}


def test_the_rate_caps_apply_to_a_reviewers_mentions(env, server, tmp_path):
    rid = mention_board(tmp_path, look={"pin_fix_max_per_round": 1})["id"]
    guest = join_guest(rid)
    code, res = mention(server, rid, signed_in(guest), idem="first")
    assert code == 201 and res["wake"]["status"] == "pending_notify", res
    code, res = mention(server, rid, signed_in(guest), idem="second", text="@Hermes and this")
    assert code == 201 and res["wake"]["status"] == "capped", res
    assert "pin_fix_max_per_round" in res["wake"]["detail"]
    tick()
    assert len(env["receiver"].wakes) == 1

    rid = mention_board(tmp_path, look={"pin_fix_max_per_hour": 1})["id"]
    member = account(review_id=rid, role="reviewer")
    assert mention(server, rid, signed_in(member), idem="h1")[1]["wake"]["status"] == "pending_notify"
    res = mention(server, rid, signed_in(member), idem="h2", text="@Hermes again")[1]
    assert res["wake"]["status"] == "capped" and "pin_fix_max_per_hour" in res["wake"]["detail"], res


# ---------------------------------------------------------------- 6. a non-owner's pin fix


@pytest.mark.parametrize("who", ["guest", "member"])
def test_a_non_owner_reviewers_pin_fix_is_stored_quiet_and_never_wakes(env, server, tmp_path, who):
    data = pin_board(tmp_path)
    rid = data["id"]
    sess = join_guest(rid) if who == "guest" else account(review_id=rid, role="reviewer")
    code, res = pin_fix(server, rid, data["token"], signed_in(sess))
    assert code == 200 and res["notified"] is False and res["detail"] == wakeguard.PIN_FIX_OWNER_ONLY, res
    req = next(iter(pin_requests(rid).values()))
    assert req["status"] == "queued" and req["quiet"] is True
    service.flush_pin_fixes(rid)
    service.retry_pending()
    assert env["receiver"].wakes == []


# ---------------------------------------------------------------- 7. the delivery re-checks a non-owner's mention


def test_a_reviewers_queued_mention_is_failed_when_wake_toolsets_is_set_before_the_tick(env, server, tmp_path,
                                                                                         monkeypatch):
    rid = mention_board(tmp_path)["id"]
    guest = join_guest(rid)
    code, res = mention(server, rid, signed_in(guest))
    assert code == 201 and res["wake"]["status"] == "pending_notify", res
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    tick()
    assert env["receiver"].requests == []
    rec = next(iter(wake_records(rid).values()))
    assert rec["status"] == "failed" and rec["last_error"] == wakeguard.TOOLS_NO_WAKE, rec
    chip = pin_requests(rid)[rec["request_id"]]
    assert chip["status"] == "failed" and chip["detail"] == wakeguard.TOOLS_NO_WAKE
    tick()
    assert env["receiver"].requests == []


def test_the_owners_mention_in_the_same_tick_is_still_sent(env, server, tmp_path, monkeypatch):
    rid = mention_board(tmp_path)["id"]
    guest = join_guest(rid)
    assert mention(server, rid, signed_in(guest), idem="g")[1]["wake"]["status"] == "pending_notify"
    owner_res = mention(server, rid, None, idem="o", text="@Hermes from the owner")[1]
    assert owner_res["wake"]["status"] == "pending_notify"
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    tick()
    assert len(env["receiver"].wakes) == 1
    body = json.loads(env["receiver"].wakes[0]["body"])
    assert [m["text"] for m in body["mentions"]] == ["@Hermes from the owner"]
    statuses = {r["author"].split(":")[0]: r["status"] for r in wake_records(rid).values()}
    assert statuses == {"guest": "failed", "user": "notified"}


def test_a_mention_queued_by_an_older_version_counts_as_the_owners_only_for_an_account_author(env, server, tmp_path,
                                                                                               monkeypatch):
    rid = mention_board(tmp_path)["id"]
    guest = join_guest(rid)
    assert mention(server, rid, signed_in(guest), idem="g")[1]["wake"]["status"] == "pending_notify"
    assert mention(server, rid, None, idem="o", text="@Hermes from the owner")[1]["wake"]["status"] == "pending_notify"
    with store.locked(rid):
        data = store.load(rid)
        for rec in data["thread_wakes"].values():
            rec.pop("by_owner"), rec.pop("author")
        store.save(rid, data)
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    tick()
    assert [m["text"] for m in json.loads(env["receiver"].wakes[0]["body"])["mentions"]] == ["@Hermes from the owner"]
    assert sorted(r["status"] for r in wake_records(rid).values()) == ["failed", "notified"]
