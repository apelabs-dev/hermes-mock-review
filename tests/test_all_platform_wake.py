"""Every chat Hermes hosts a session on can be woken, or is refused at create (platforms.py).

deliver      a messaging chat: the board is created from the calling session's own context, Send POSTs to
             `<route>-<platform>` and the ack goes to `<route>-<platform>-notice`.
session      a Desktop chat (`local`): the board server inside the gateway queues the same wake prompt into the chat's
             own session through `ctx.inject_message(prompt, session_key=...)`, for Send and for an @Hermes mention;
             a refused injection is reported, and a board whose Send is not gated is refused at create and at Send.
unsupported  the surfaces that are not a chat (and a name this Hermes does not know): refused at create with the
             reason, nothing posted; a stored board whose chat became unreachable fails at Send, never silently.
"""

import contextvars
import json
import sys
import time

import pytest

from conftest import http, load_plugin
from mockreview import (api, autostart, config, notify, owner, platforms, service, sessionwake, setup, store,
                        wakeguard, wakeprompt)
from mockreview.store import ReviewError
from test_owner import DEC_FB
from test_plugin import FakeCtx, _decisions
from test_serve import free_port

REAL_HERMES_KNOWS = platforms._hermes_knows       # conftest replaces it per test; a test may ask the real registry

LOCAL_CHAT = "local-" + "0" * 64
LOCAL_KEY = "agent:main:local:dm:" + LOCAL_CHAT
OTHER_CHAT = "local-" + "a" * 64
MENTION_DECISIONS = {"blocks": [{"type": "mock", "id": "m1", "html": "<p>today</p>"}]}


class GatewayCtx(FakeCtx):
    """A plugin context inside the gateway: inject_message records what it was given and answers `answer`."""

    def __init__(self, cfg=None, answer=True, allowed=True):
        super().__init__(cfg)
        self.injected = []
        self.answer = answer
        self.allowed = allowed

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        self.injected.append({"content": content, "role": role, "session_key": session_key, "origin": origin})
        return self.answer

    def _gateway_injection_allowed(self):
        return self.allowed


def bind_session(monkeypatch, **values):
    """The calling turn's session context, as Hermes binds it (HERMES_SESSION_<NAME>)."""
    bound = {"HERMES_SESSION_" + k.upper(): v for k, v in values.items()}
    monkeypatch.setattr(platforms, "_session_env", lambda: (lambda name, default="": bound.get(name, default)))
    monkeypatch.setattr(platforms, "_bound_key", lambda: bound.get("HERMES_SESSION_KEY", ""))


def create_tool(ctx, **args):
    return json.loads(ctx.tools["mock_review_create"]["handler"](args))


@pytest.fixture
def gateway(env, monkeypatch):
    """The plugin registered in a gateway whose webhook platform connected: the board serves in this process."""
    plugin = load_plugin()
    ctx = GatewayCtx({"autostart": True, "port": free_port()})
    plugin.register(ctx)
    [(_, factory)] = ctx.platform_handlers
    assert factory(None, None) == autostart.STARTED
    try:
        yield ctx, f"http://127.0.0.1:{autostart.running().port}"
    finally:
        autostart.stop_running(2)
        owner.mark_owner_process(False)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------- deliver: a messaging chat


@pytest.mark.parametrize("bound, origin", [
    ({"platform": "telegram", "chat_id": "-100200300", "thread_id": "77",
      "key": "agent:main:telegram:group:-100200300:77"},
     {"platform": "telegram", "chat_id": "-100200300", "thread_id": "77"}),
    ({"platform": "discord", "chat_id": "test-channel", "key": "agent:main:discord:group:test-channel"},
     {"platform": "discord", "chat_id": "test-channel"}),
])
def test_a_messaging_chat_board_is_made_from_its_session_and_wakes_its_own_route(env, tmp_path, monkeypatch,
                                                                                 bound, origin):
    bind_session(monkeypatch, **bound)
    ctx = FakeCtx()
    load_plugin().register(ctx)
    out = create_tool(ctx, title="From the session", decisions=str(_decisions(tmp_path)))
    assert "error" not in out, out
    data = store.load(out["review_id"])
    assert data["origin"] == origin                       # nothing copied by hand, no session key for a route
    assert service.submit(data["id"], 1, DEC_FB, data["token"])["notified"]
    platform = origin["platform"]
    assert [w["path"].rsplit("/", 1)[1] for w in env["receiver"].wakes] == [f"mock-review-{platform}"]
    assert json.loads(env["receiver"].wakes[0]["body"])["chat_id"] == origin["chat_id"]
    assert wait_for(lambda: env["receiver"].notices)
    assert [n["path"].rsplit("/", 1)[1] for n in env["receiver"].notices] == [f"mock-review-{platform}-notice"]


def test_explicit_origin_arguments_win_over_the_session(env, tmp_path, monkeypatch):
    bind_session(monkeypatch, platform="telegram", chat_id="-100200300", key="agent:main:telegram:dm:-100200300")
    ctx = FakeCtx()
    load_plugin().register(ctx)
    out = create_tool(ctx, title="Explicit", decisions=str(_decisions(tmp_path)), origin_platform="discord",
                      origin_chat_id="test-chat")
    assert store.load(out["review_id"])["origin"] == {"platform": "discord", "chat_id": "test-chat"}


@pytest.mark.parametrize("bound, why", [
    ({"key": LOCAL_KEY}, platforms.TUI_PROCESS),          # a terminal UI in its own process: only a key
    ({}, "no calling chat in this context"),
    ({"platform": "local", "chat_id": LOCAL_CHAT}, "the Desktop session has no route key bound in this turn"),
])
def test_no_readable_calling_chat_is_refused_and_never_guessed(env, tmp_path, monkeypatch, bound, why):
    bind_session(monkeypatch, **bound)
    ctx = FakeCtx()
    load_plugin().register(ctx)
    err = create_tool(ctx, title="No chat", decisions=str(_decisions(tmp_path)))["error"]
    assert why in err and "never guess" in err
    assert store.list_ids() == [] and env["receiver"].requests == []


def test_half_an_explicit_origin_is_refused_even_with_a_session(env, tmp_path, monkeypatch):
    bind_session(monkeypatch, platform="telegram", chat_id="-100200300", key="agent:main:telegram:dm:-100200300")
    ctx = FakeCtx()
    load_plugin().register(ctx)
    err = create_tool(ctx, title="Half", decisions=str(_decisions(tmp_path)), origin_platform="discord")["error"]
    assert "pass both or neither" in err and store.list_ids() == []


# ---------------------------------------------------------------- session: a Desktop chat


def test_a_desktop_board_send_through_the_board_server_queues_the_wake_prompt_in_its_session(env, tmp_path,
                                                                                           monkeypatch, gateway):
    ctx, base = gateway
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    out = create_tool(ctx, title="Desktop board", decisions=str(_decisions(tmp_path)))
    assert "error" not in out, out
    rid = out["review_id"]
    data = store.load(rid)
    assert data["origin"] == {"platform": "local", "chat_id": LOCAL_CHAT, "session_key": LOCAL_KEY}

    code, _, body = http("POST", f"{base}/r/{rid}/api/submit", {"round": 1, "token": data["token"],
                                                                "feedback": DEC_FB})
    assert code == 200, body
    assert len(ctx.injected) == 1
    injected = ctx.injected[0]
    assert injected["session_key"] == LOCAL_KEY and injected["origin"] is None and injected["role"] == "user"
    data = store.load(rid)
    payload = service.build_payload(data, data["rounds"][0])
    assert injected["content"] == wakeprompt.render(setup.PROMPT_TEMPLATE, payload)
    assert injected["content"].startswith(f"Mock review wake: review.submitted for {rid} round 1 — Desktop board.")
    sub = data["rounds"][0]["submission"]
    assert sub["status"] == "notified" and sub["gateway_response"] == "queued in the Desktop chat"
    assert env["receiver"].requests == []                 # no webhook route was asked
    assert wait_for(lambda: (store.load(rid)["rounds"][0]["submission"].get("notice") or {}).get("status"))
    notice = store.load(rid)["rounds"][0]["submission"]["notice"]
    assert notice["status"] == "skipped" and notice["reason"] == sessionwake.NO_NOTICE

    # A retry of the same delivery (the worker, a re-sent Send) is not queued twice: Hermes keeps no duplicate filter.
    assert notify.send(payload, data["rounds"][0]["submission"]["delivery_id"])[1].startswith("duplicate")
    assert len(ctx.injected) == 1


def test_a_desktop_board_mention_queues_the_mention_wake_in_its_session(env, tmp_path, monkeypatch, gateway):
    ctx, base = gateway
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    dec = tmp_path / "mention.json"
    dec.write_text(json.dumps(MENTION_DECISIONS), encoding="utf-8")
    out = create_tool(ctx, title="Desktop mention", decisions=str(dec))
    assert "error" not in out, out
    rid = out["review_id"]
    code, _, body = http("POST", f"{base}/r/{rid}/api/threads", {
        "text": "@Hermes make this clearer", "mentions": ["hermes"], "idem": "c1",
        "anchor": {"block": "m1", "x": 0.2, "y": 0.3}})
    assert code == 201, body
    service.retry_pending()                               # the worker tick that delivers queued mention wakes
    assert wait_for(lambda: ctx.injected)
    time.sleep(0.2)
    assert len(ctx.injected) == 1
    injected = ctx.injected[0]
    assert injected["session_key"] == LOCAL_KEY
    assert injected["content"].startswith(f"Mock review wake: review.thread_mention for {rid} round 1")
    assert "a reviewer mentioned Hermes in a comment thread: " in injected["content"]
    wakes = store.load(rid)["thread_wakes"]
    assert [w["status"] for w in wakes.values()] == ["notified"]
    assert env["receiver"].requests == []


@pytest.mark.parametrize("answer, allowed, why", [
    (False, True, sessionwake.NOT_LIVE),                  # Hermes refused: the chat is not live in this profile
    (False, False, sessionwake.INJECTION_OFF),            # the consent was taken back after the board was made
])
def test_a_refused_desktop_wake_is_reported_and_retried(env, tmp_path, monkeypatch, gateway, answer, allowed, why):
    ctx, base = gateway
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    rid = create_tool(ctx, title="Refused", decisions=str(_decisions(tmp_path)))["review_id"]
    ctx.answer, ctx.allowed = answer, allowed
    data = store.load(rid)
    res = service.submit(rid, 1, DEC_FB, data["token"])
    assert not res["notified"]
    sub = store.load(rid)["rounds"][0]["submission"]
    assert sub["status"] == "pending_notify" and sub["last_error"] == why
    assert env["receiver"].requests == []
    ctx.answer, ctx.allowed = True, True                  # the chat is open again / consent given: the retry wakes it
    service.retry_pending()
    assert store.load(rid)["rounds"][0]["submission"]["status"] == "notified"


def test_a_desktop_board_is_refused_at_create_while_hermes_has_not_consented(env, tmp_path, monkeypatch, gateway):
    ctx, _ = gateway
    ctx.allowed = False
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    err = create_tool(ctx, title="No consent", decisions=str(_decisions(tmp_path)))["error"]
    assert sessionwake.INJECTION_OFF in err and store.list_ids() == [] and ctx.injected == []


def test_a_desktop_board_is_refused_by_a_board_server_outside_the_gateway(env, tmp_path):
    with pytest.raises(ReviewError) as e:
        api.create("Standalone", {"platform": "local", "chat_id": LOCAL_CHAT, "session_key": LOCAL_KEY},
                   mode="local", decisions=str(_decisions(tmp_path)))
    assert e.value.status == 409 and sessionwake.NOT_IN_GATEWAY in e.value.message
    assert store.list_ids() == []


def test_an_explicit_desktop_origin_without_its_session_key_is_refused(env, tmp_path, monkeypatch, gateway):
    ctx, _ = gateway
    bind_session(monkeypatch, platform="local", chat_id=OTHER_CHAT, key="agent:main:local:dm:" + OTHER_CHAT)
    err = create_tool(ctx, title="Another chat", decisions=str(_decisions(tmp_path)), origin_platform="local",
                      origin_chat_id=LOCAL_CHAT)["error"]  # not the calling chat: its key is not ours to take
    assert "needs that session's key" in err and store.list_ids() == []


@pytest.mark.parametrize("ungate", ["public_url", "public_mode", "funnel", "lan"])
def test_an_ungated_desktop_board_is_refused_at_create(env, tmp_path, monkeypatch, gateway, ungate):
    ctx, _ = gateway
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    args = {"title": "Ungated", "decisions": str(_decisions(tmp_path))}
    if ungate == "public_url":
        monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", "https://reviews.example.test")
    elif ungate == "public_mode":
        args["mode"] = "public"
    elif ungate == "funnel":                              # `mockreview public on` maps Funnel, public_url stays unset
        monkeypatch.setattr(config, "funnel_url", lambda: "https://board.example.test")
    else:
        monkeypatch.setattr(wakeguard, "bound_host", lambda: "0.0.0.0")
    assert not wakeguard.granted()                        # the gate holds for a Desktop chat without wake_toolsets
    err = create_tool(ctx, **args)["error"]
    assert "refusing a board that wakes a Desktop chat" in err and store.list_ids() == [] and ctx.injected == []


@pytest.mark.parametrize("later", ["public_url", "funnel"])
def test_a_desktop_board_ungated_after_create_is_held_at_send(env, tmp_path, monkeypatch, gateway, later):
    ctx, _ = gateway
    bind_session(monkeypatch, platform="local", chat_id=LOCAL_CHAT, key=LOCAL_KEY)
    rid = create_tool(ctx, title="Later public", decisions=str(_decisions(tmp_path)))["review_id"]
    if later == "public_url":
        monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", "https://reviews.example.test")
    else:                                                 # `mockreview public on` after the board was made
        monkeypatch.setattr(config, "funnel_url", lambda: "https://board.example.test")
    res = service.submit(rid, 1, DEC_FB, store.load(rid)["token"])
    assert not res["notified"]
    assert store.load(rid)["rounds"][0]["submission"]["last_error"].startswith("held, not woken: ")
    assert ctx.injected == []


def test_the_serving_board_wakes_through_the_newest_plugin_context_and_lets_go_when_stopped(env, gateway):
    ctx, _ = gateway
    assert sessionwake.attached()
    newer = GatewayCtx()
    load_plugin().register(newer)         # a plugin rescan: register() again, gateway_factory is not called again
    assert sessionwake._ctx is newer
    autostart.stop_running(2)
    assert not sessionwake.attached() and sessionwake.status()[1] == sessionwake.NOT_IN_GATEWAY


def test_only_the_owning_profiles_context_is_attached(env, gateway, tmp_path, monkeypatch):
    ctx, _ = gateway
    other = GatewayCtx()
    monkeypatch.setattr(sessionwake, "_calling_home", lambda: tmp_path / "profiles" / "beta")
    autostart.gateway_factory(None, None, other)    # another profile's webhook connect under multiplexing
    load_plugin().register(other)                   # and its plugin rescan
    assert sessionwake._ctx is ctx


def test_a_desktop_board_without_a_session_key_fails_for_good(env, gateway):
    ok, why = sessionwake.send({"session_key": None}, "d1")   # a Desktop board made before Desktop wakes
    assert not ok and why.startswith(notify.CANNOT_WAKE) and why.startswith(service.MENTION_PERMANENT)


def test_a_cli_register_attaches_nothing(env):
    load_plugin().register(GatewayCtx())  # register() also runs in the CLI and `plugins validate`: no board, no wake
    assert not sessionwake.attached()


def test_a_desktop_origin_keeps_its_key_and_old_origins_still_load():
    assert store.normalize_origin({"platform": "local", "chat_id": LOCAL_CHAT, "session_key": LOCAL_KEY}) == {
        "platform": "local", "chat_id": LOCAL_CHAT, "session_key": LOCAL_KEY}
    assert store.normalize_origin({"platform": "local", "chat_id": "demo"}) == {"platform": "local", "chat_id": "demo"}
    with pytest.raises(ReviewError, match="only for a Desktop"):
        store.normalize_origin({"platform": "telegram", "chat_id": "x", "session_key": LOCAL_KEY})
    with pytest.raises(ReviewError, match="session_key must"):
        store.normalize_origin({"platform": "local", "chat_id": LOCAL_CHAT, "session_key": "bad key\n"})


def test_the_desktop_prompt_is_the_one_hermes_renders_for_a_route():
    """The same template and payload give the same text through Hermes's own webhook renderer."""
    webhook = pytest.importorskip("gateway.platforms.webhook")
    adapter = webhook.WebhookAdapter.__new__(webhook.WebhookAdapter)
    payload = {"event_type": "review.thread_mention", "review_id": "review-x-000000", "round": 2,
               "platform": "local", "chat_id": LOCAL_CHAT, "session_key": LOCAL_KEY, "title": "T {not_a_key}",
               "summary": "thread on m1", "review_url": "http://127.0.0.1:8790/r/review-x-000000",
               "nested": {"a": [1, 2]}}
    template = setup.PROMPT_TEMPLATE + " {nested} {nested.a} {missing} {__raw__}"
    assert wakeprompt.render(template, payload) == adapter._render_prompt(template, payload, payload["event_type"],
                                                                          "mock-review-local")


# ---------------------------------------------------------------- unsupported


@pytest.mark.parametrize("platform", sorted(platforms.NOT_A_CHAT))
def test_a_surface_that_is_not_a_chat_is_refused_at_create_and_nothing_is_posted(env, tmp_path, platform):
    ctx = FakeCtx()
    load_plugin().register(ctx)
    err = create_tool(ctx, title="Not a chat", decisions=str(_decisions(tmp_path)), origin_platform=platform,
                      origin_chat_id="test-chat")["error"]
    assert err == platforms.unsupported_message(platform, platforms.NOT_A_CHAT[platform])
    assert "wake=false" in err and store.list_ids() == [] and env["receiver"].requests == []


def test_a_platform_this_hermes_does_not_know_is_refused_at_create(env, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms, "_hermes_knows", lambda p: p == "telegram")
    with pytest.raises(ReviewError) as e:
        api.create("Unknown", {"platform": "irc", "chat_id": "#room"}, mode="local", decisions=str(_decisions(tmp_path)))
    assert e.value.status == 409 and "is its platform plugin installed and enabled?" in e.value.message
    assert store.list_ids() == [] and env["receiver"].requests == []


def test_a_wake_off_demo_board_needs_no_wakeable_chat(env, tmp_path):
    data = api.create("Demo", {"platform": "api_server", "chat_id": "x"}, mode="local",
                      decisions=str(_decisions(tmp_path)), wake=False)
    assert data["review_id"]


def test_a_stored_board_whose_chat_cannot_be_woken_fails_at_send_not_silently(env, tmp_path, monkeypatch):
    data = store.create_review("Stored", "brief", {"platform": "api_server", "chat_id": "x"}, "local",
                               store.parse_round_spec(decisions=str(_decisions(tmp_path))))
    res = service.submit(data["id"], 1, DEC_FB, data["token"])
    assert not res["notified"]
    sub = store.load(data["id"])["rounds"][0]["submission"]
    assert sub["status"] == "pending_notify" and sub["last_error"].startswith(notify.CANNOT_WAKE)
    assert env["receiver"].requests == []
    service.retry_pending()
    notice = store.load(data["id"])["rounds"][0]["submission"].get("notice") or {}
    assert notice.get("status") in (None, "skipped") and env["receiver"].requests == []


def test_a_mention_on_a_chat_that_cannot_be_woken_fails_for_good(env, tmp_path, monkeypatch, server):
    dec = tmp_path / "mention.json"
    dec.write_text(json.dumps(MENTION_DECISIONS), encoding="utf-8")
    data = store.create_review("Mention", "brief", {"platform": "telegram", "chat_id": "x"}, "local",
                               store.parse_round_spec(decisions=str(dec)),
                               look={"pin_fix": True, "pin_fix_min_seconds": 0.0, "pin_fix_window_seconds": 0.0})
    monkeypatch.setattr(platforms, "_hermes_knows", lambda p: False)   # its platform plugin was disabled since
    code, _, body = http("POST", f"{server}/r/{data['id']}/api/threads", {
        "text": "@Hermes look", "mentions": ["hermes"], "idem": "c1", "anchor": {"block": "m1", "x": 0.2, "y": 0.3}})
    assert code == 201, body
    service.retry_pending()
    [wake] = store.load(data["id"])["thread_wakes"].values()
    assert wake["status"] == "failed" and wake["last_error"].startswith(notify.CANNOT_WAKE)
    assert env["receiver"].requests == []


# ---------------------------------------------------------------- the classifier against the running Hermes


class StubPluginSystem:
    """hermes_cli.plugins as a Hermes process has it loaded: discovery registers `adds`."""

    def __init__(self, registered, adds=()):
        self.registered, self.adds, self.discovered = registered, set(adds), 0

    def discover_plugins(self):
        self.discovered += 1
        self.registered |= self.adds


def hermes_with(monkeypatch, plugin_system):
    """The real `gateway.webhook_targets` check over a stand-in registry, inside or outside a Hermes process."""
    targets = pytest.importorskip("gateway.webhook_targets")
    monkeypatch.setattr(platforms, "_hermes_knows", REAL_HERMES_KNOWS)
    registered = plugin_system.registered if plugin_system else set()
    monkeypatch.setattr(targets, "is_known_platform",
                        lambda name: name in targets.BUILTIN_DELIVER_PLATFORMS or name in registered)
    if plugin_system is None:
        monkeypatch.delitem(sys.modules, "hermes_cli.plugins", raising=False)
    else:
        monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugin_system)


def test_the_classifier_asks_the_hermes_registry_and_fails_closed(monkeypatch):
    monkeypatch.setattr(platforms, "_hermes_knows", REAL_HERMES_KNOWS)
    assert platforms.classify("local") == (platforms.SESSION, "")
    assert platforms.classify("api_server")[0] == platforms.UNSUPPORTED
    assert platforms.classify("Bad Name")[0] == platforms.UNSUPPORTED
    plugin_system = StubPluginSystem(set())
    hermes_with(monkeypatch, plugin_system)
    assert platforms.classify("telegram") == (platforms.DELIVER, "")
    assert platforms.classify("discord") == (platforms.DELIVER, "")
    assert plugin_system.discovered == 0                  # a built-in needs no plugin discovery
    assert platforms.classify("no_such_chat_platform")[0] == platforms.UNSUPPORTED
    assert plugin_system.discovered == 1                  # asked again after discovery, still unknown: refused


def test_a_plugin_platform_is_known_once_hermes_has_discovered_its_plugins(monkeypatch):
    plugin_system = StubPluginSystem(set(), adds={"irc"})
    hermes_with(monkeypatch, plugin_system)
    assert platforms.classify("irc") == (platforms.DELIVER, "")
    assert plugin_system.discovered == 1


def test_outside_a_hermes_process_a_plugin_platform_is_left_to_the_receiver(monkeypatch):
    hermes_with(monkeypatch, None)                        # Hermes's interpreter, but its plugin system never loaded
    assert platforms.classify("irc") == (platforms.DELIVER, "")
    assert platforms.classify("telegram") == (platforms.DELIVER, "")


def test_outside_hermes_an_explicit_origin_is_left_to_the_receiver(monkeypatch):
    monkeypatch.setattr(platforms, "_hermes_knows", lambda p: None)
    assert platforms.classify("any-chat") == (platforms.DELIVER, "")


def test_the_registry_check_fails_closed_when_it_raises(monkeypatch):
    import sys
    import types
    broken = types.ModuleType("gateway.webhook_targets")

    def boom(name):
        raise RuntimeError("registry not ready")
    broken.is_known_platform = boom
    gateway_pkg = sys.modules.get("gateway") or types.ModuleType("gateway")
    monkeypatch.setitem(sys.modules, "gateway", gateway_pkg)
    monkeypatch.setitem(sys.modules, "gateway.webhook_targets", broken)
    monkeypatch.setattr(gateway_pkg, "webhook_targets", broken, raising=False)
    assert REAL_HERMES_KNOWS("telegram") is False


def test_the_session_origin_is_read_from_hermes_own_session_context(monkeypatch):
    session_context = pytest.importorskip("gateway.session_context")
    monkeypatch.setattr(platforms, "_session_env", lambda: session_context.get_session_env)

    def in_turn():
        session_context.set_session_vars(platform="local", chat_id=LOCAL_CHAT, thread_id="", session_key=LOCAL_KEY)
        return platforms.session_origin()

    assert contextvars.copy_context().run(in_turn) == (
        {"platform": "local", "chat_id": LOCAL_CHAT, "session_key": LOCAL_KEY}, "")


def test_a_desktop_key_only_in_the_environment_is_not_taken(monkeypatch):
    session_context = pytest.importorskip("gateway.session_context")
    if not hasattr(session_context, "bound_session_key"):
        pytest.skip("this Hermes has no bound_session_key")
    monkeypatch.setattr(platforms, "_session_env", lambda: session_context.get_session_env)
    for name, value in (("PLATFORM", "local"), ("CHAT_ID", LOCAL_CHAT), ("KEY", LOCAL_KEY)):
        monkeypatch.setenv("HERMES_SESSION_" + name, value)   # inherited by a child process, bound for no turn
    origin, why = contextvars.Context().run(platforms.session_origin)
    assert origin is None and "no route key bound" in why
