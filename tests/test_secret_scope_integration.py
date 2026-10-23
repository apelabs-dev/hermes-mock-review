"""Real Hermes scopes across server threads; optional outside a Hermes runtime."""

import argparse
import contextvars
import importlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from conftest import FEEDBACK, http, load_plugin
from mockreview import autostart, config, herenow, notify, owner, secrets, server, service, settings, store
from test_plugin import FakeCtx


PUBLIC_ACCESSOR = secrets.get_secret


@pytest.fixture
def real_scope(monkeypatch):
    source = os.environ.get("HERMES_AGENT_SRC")
    if source:
        monkeypatch.syspath_prepend(source)
    try:
        scope = importlib.import_module("agent.secret_scope")
    except ImportError:
        pytest.skip("Real agent.secret_scope unavailable; rerun under Hermes or with HERMES_AGENT_SRC")
    # Replace the suite's accessor stub with the production wrapper, using the real public module.
    monkeypatch.setattr(secrets, "get_secret", PUBLIC_ACCESSOR)
    token = scope.set_multiplex_context(True)
    try:
        yield scope
    finally:
        scope.reset_multiplex_context(token)


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("server scope integration did not complete")


def test_real_accessor_does_not_fall_back_between_profile_scopes(real_scope, monkeypatch, tmp_path):
    monkeypatch.setenv("HERENOW_API_KEY", "synthetic-process-key")
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "synthetic-process-signing-key")
    token = real_scope.set_secret_scope(None)
    try:
        with pytest.raises(real_scope.UnscopedSecretError):
            herenow.api_key()
        a = real_scope.set_secret_scope({"HERENOW_API_KEY": "synthetic-key-a"},
                                        profile_home=str(tmp_path / "profile-a"))
        try:
            assert herenow.api_key() == "synthetic-key-a"
            captured = contextvars.copy_context()
        finally:
            real_scope.reset_secret_scope(a)
        b = real_scope.set_secret_scope({}, profile_home=str(tmp_path / "profile-b"))
        try:
            with pytest.raises(herenow.HereNowError, match="not set"):
                herenow.api_key()
            assert notify.load_secret()[0] is None
            assert captured.run(herenow.api_key) == "synthetic-key-a"
        finally:
            real_scope.reset_secret_scope(b)
    finally:
        real_scope.reset_secret_scope(token)


def test_real_scoped_publish_keys_are_passed_only_to_children(real_scope, review, monkeypatch, tmp_path):
    script = config.hermes_home() / "skills" / "productivity" / "here-now" / "scripts" / "publish.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    monkeypatch.setenv("HERENOW_API_KEY", "synthetic-wrong-process-key")
    seen = []

    def publish(cmd, **kwargs):
        assert cmd[1] == str(script)
        seen.append(kwargs["env"]["HERENOW_API_KEY"])
        return SimpleNamespace(returncode=0, stderr="publish_result.slug=synthetic-scope-site\n"
            "publish_result.site_url=https://synthetic-scope-site.here.now/\npublish_result.auth_mode=authenticated\n")

    monkeypatch.setattr(herenow.subprocess, "run", publish)
    a = real_scope.set_secret_scope({"HERENOW_API_KEY": "synthetic-key-a"},
                                   profile_home=str(tmp_path / "profile-a"))
    try:
        captured = contextvars.copy_context()
    finally:
        real_scope.reset_secret_scope(a)
    b = real_scope.set_secret_scope({"HERENOW_API_KEY": "synthetic-key-b"},
                                   profile_home=str(tmp_path / "profile-b"))
    try:
        captured.run(herenow.publish, review["id"])
        herenow.publish(review["id"])
        assert seen == ["synthetic-key-a", "synthetic-key-b"]
        assert os.environ["HERENOW_API_KEY"] == "synthetic-wrong-process-key"
    finally:
        real_scope.reset_secret_scope(b)


@pytest.mark.parametrize("runner", ["gateway", "foreground"])
def test_real_scopes_in_http_retry_poll_and_foreground_cli(real_scope, env, variants, tmp_path,
                                                         monkeypatch, runner, request):
    plugin = load_plugin()

    autostart.stop_running()
    monkeypatch.delenv("MOCKREVIEW_HOME", raising=False)
    monkeypatch.setenv("HERENOW_API_KEY", "synthetic-wrong-process-key")
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "synthetic-wrong-process-signing-key")
    base_worker = server.Worker

    class FastWorker(base_worker):
        def __init__(self):
            super().__init__(tick=0.02)

    monkeypatch.setattr(server, "Worker", FastWorker)
    bound = threading.Event()
    servers = []
    make_server = server.make_server

    def capture_server(*args, **kwargs):
        srv = make_server(*args, **kwargs)
        servers.append(srv)
        bound.set()
        return srv

    monkeypatch.setattr(server, "make_server", capture_server)
    posted = []
    polls = []
    permit_retry = threading.Event()

    def post(url, payload, key, delivery_id, **kwargs):
        posted.append((payload["review_id"], key, settings.get("data_dir"), url))
        return permit_retry.is_set(), "synthetic transport"

    monkeypatch.setattr(notify, "post", post)
    original_urlopen = herenow.urllib.request.urlopen

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            return b'{"records": []}'

    def urlopen(request, **kwargs):
        if request.full_url.startswith("https://here.now/api/"):
            polls.append((request.get_header("Authorization"), settings.get("data_dir")))
            return Reply()
        return original_urlopen(request, **kwargs)

    monkeypatch.setattr(herenow.urllib.request, "urlopen", urlopen)
    root_a = str(tmp_path / "profile-a" / "reviews")
    root_b = str(tmp_path / "profile-b" / "reviews")
    # Under multiplexing the board binds the owning home's own scope (its .env), whatever scope the caller had.
    config.hermes_home().mkdir(parents=True, exist_ok=True)
    (config.hermes_home() / ".env").write_text("HERENOW_API_KEY=synthetic-key-a\n"
                                               "MOCKREVIEW_WEBHOOK_SECRET=synthetic-signing-a\n", encoding="utf-8")
    scope_a = real_scope.set_secret_scope({"HERENOW_API_KEY": "synthetic-caller-key",
        "MOCKREVIEW_WEBHOOK_SECRET": "synthetic-caller-signing"}, profile_home=str(tmp_path / "profile-a"))
    ctx_a = FakeCtx({"data_dir": root_a, "autostart": True})
    foreground = None
    exit_codes = []

    def cleanup():
        autostart.stop_running()
        if foreground is not None and foreground.is_alive():
            servers[0].shutdown()
            foreground.join(8)
        owner.mark_owner_process(False)

    request.addfinalizer(cleanup)
    try:
        plugin.register(ctx_a)
        specs = [store.parse_variant_spec(v) for v in variants]
        origin = {"platform": "telegram", "chat_id": "test-chat", "thread_id": "topic:alpha"}
        local = store.create_review("HTTP scope", "", origin, "local", specs)
        retry = store.create_review("Retry scope", "", origin, "local", specs)
        service.submit(retry["id"], 1, FEEDBACK, retry["token"], do_notify=False)
        public = store.create_review("Poll scope", "", origin, "herenow", specs)
        public["herenow"]["slug"] = "synthetic-scope-site"
        store.save(public["id"], public)
        if runner == "gateway":
            # Port zero through the explicit config override avoids any live listener.
            monkeypatch.setattr(autostart.config, "port", lambda: 0)
            [(platform, factory)] = ctx_a.platform_handlers
            assert platform == "webhook"
            assert factory(None, object()) == autostart.STARTED
        else:
            parser = argparse.ArgumentParser()
            ctx_a.cli["mockreview"]["setup_fn"](parser)
            args = parser.parse_args(["serve", "--port", "0"])
            context = contextvars.copy_context()

            def run_cli():
                exit_codes.append(ctx_a.cli["mockreview"]["handler_fn"](args))

            foreground = threading.Thread(target=context.run, args=(run_cli,), daemon=True)
            foreground.start()
        assert bound.wait(5)
    finally:
        real_scope.reset_secret_scope(scope_a)
    try:
        scope_b = real_scope.set_secret_scope({"HERENOW_API_KEY": "synthetic-key-b",
            "MOCKREVIEW_WEBHOOK_SECRET": "synthetic-signing-b"}, profile_home=str(tmp_path / "profile-b"))
        try:
            ctx_b = FakeCtx({"data_dir": root_b})
            plugin.register(ctx_b)
            assert herenow.api_key() == "synthetic-key-b"
            assert store.list_ids() == []
            base = "http://127.0.0.1:" + str(servers[0].server_address[1])
            status, _, raw = http("POST", base + "/r/" + local["id"] + "/api/submit",
                {"round": 1, "token": local["token"], "feedback": FEEDBACK})
            assert status == 200 and json.loads(raw)["status"] == "pending_notify"
            # Concurrent requests exercise independent Context copies on the same captured server.
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(lambda _: http("GET", base + "/api/owner"), range(8)))
            assert all(code == 200 and json.loads(body)["data_root"] == root_a
                       and json.loads(body)["wake"]["secret"] == "set" for code, _, body in responses)
            permit_retry.set()
            wait_for(lambda: polls and {p[0] for p in posted} == {local["id"], retry["id"]}
                     and any(p[0] == local["id"] for p in posted[1:]))
            # Check persisted success via HTTP while the caller is still in the opposing profile.
            wait_for(lambda: all(json.loads(http("GET", base + "/r/" + data["id"] + "/state.json")[2])
                                ["rounds"][0]["submission"]["status"] == "notified" for data in (local, retry)))
            assert posted and all(key == "synthetic-signing-a" and root == root_a
                                  and url.endswith("mock-review-telegram") for _, key, root, url in posted)
            assert polls and all(auth == "Bearer synthetic-key-a" and root == root_a for auth, root in polls)
            assert herenow.api_key() == "synthetic-key-b" and settings.get("data_dir") == root_b
        finally:
            real_scope.reset_secret_scope(scope_b)
    finally:
        if runner == "gateway":
            autostart.stop_running()
        elif foreground is not None:
            servers[0].shutdown()
            foreground.join(8)
            assert not foreground.is_alive() and exit_codes == [0]
        owner.mark_owner_process(False)
