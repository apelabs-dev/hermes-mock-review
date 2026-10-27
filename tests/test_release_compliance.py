"""Portable install layout, scoped secrets, and platform-neutral delivery regressions."""

import builtins
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from conftest import FEEDBACK, ROOT, SECRET, load_plugin
from mockreview import api, cli, config, herenow, notify, owner, secrets, service, settings, store
from mockreview.store import ReviewError


@pytest.mark.parametrize("origin", [
    {"platform": "discord", "chat_id": "test-channel", "thread_id": "test-topic"},
    {"platform": "telegram", "chat_id": "-test-group", "thread_id": "topic:alpha"},
    {"platform": "slack", "chat_id": "C_TEST"},
])
def test_origin_survives_store_payload_and_route(env, variants, origin):
    data = store.create_review("Origin", "", origin, "local", [store.parse_variant_spec(v) for v in variants])
    service.submit(data["id"], 1, FEEDBACK, data["token"])
    request = env["receiver"].wakes[-1]
    payload = json.loads(request["body"])
    assert {k: payload[k] for k in origin} == origin
    assert ("thread_id" in payload) == ("thread_id" in origin)
    assert request["path"] == "/webhooks/mock-review-" + origin["platform"]
    assert request["ok"]
    assert store.load(data["id"])["origin"] == origin


def test_legacy_origin_is_readable_but_never_guessed_for_wake(review, env):
    data = store.load(review["id"])
    data["origin"] = {"platform": "discord", "thread_id": "legacy-topic"}
    store.save(data["id"], data)
    original = (store.review_dir(data["id"]) / "review.json").read_bytes()
    assert api.list_reviews()[0]["origin"] == data["origin"]
    assert api.review_link(data["id"])["review_id"] == data["id"]
    assert (store.review_dir(data["id"]) / "review.json").read_bytes() == original
    result = service.submit(data["id"], 1, FEEDBACK, data["token"])
    assert not result["notified"] and "migrate legacy" in result["detail"]
    assert not env["receiver"].requests
    assert store.load(data["id"])["origin"] == data["origin"]
    assert store.load(data["id"])["rounds"][0]["feedback"] is not None
    with pytest.raises(ReviewError, match="legacy"):
        store.normalize_origin("arbitrary-id")


def test_id_has_at_least_64_random_bits(monkeypatch):
    sizes = []
    monkeypatch.setattr(store.secrets, "token_hex", lambda size: sizes.append(size) or "a" * (size * 2))
    assert re.fullmatch(r"review-[a-f0-9]{16,}", store.new_review_id("Review"))
    assert sizes[0] >= 8


def test_cli_requires_explicit_origin(capsys):
    with pytest.raises(SystemExit):
        cli.main(["create", "--title", "Review", "--thread", "arbitrary-id"])
    assert "--platform" in capsys.readouterr().err


def test_tailnet_runtime_resolution_and_neutral_defaults(monkeypatch):
    monkeypatch.delenv("MOCKREVIEW_PUBLIC_BASE", raising=False)
    assert settings.BY_NAME["public_base"].default == ""
    assert settings.BY_NAME["default_host"].default == "local"
    calls = []
    def status(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "BackendState": "Running", "Self": {"DNSName": "board.example.test."}}))
    monkeypatch.setattr(subprocess, "run", status)
    assert config.public_base() == "https://board.example.test:8790"
    assert calls[0][0] == ["tailscale", "status", "--json"] and calls[0][1]["timeout"] == 5
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_BASE", "https://explicit.example.test/")
    assert config.public_base() == "https://explicit.example.test" and len(calls) == 1
    monkeypatch.delenv("MOCKREVIEW_PUBLIC_BASE")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert config.public_base() == ""
    assert owner.default_owner_root() == config.hermes_home() / "workspace" / "mock-reviews"


def test_manifest_secrets_are_optional_and_capabilities_truthful():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    # The version itself is not asserted here: test_release_config.py owns it, so Release Please can bump it.
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(manifest["version"]))
    assert manifest["requires_hermes"] == ">=0.22.0"
    assert manifest["capabilities"] == []
    assert manifest["platforms"] == ["webhook"] and manifest["provides_hooks"] == []
    assert {s["name"] for s in manifest["requires_env"]} == {"HERENOW_API_KEY", "MOCKREVIEW_WEBHOOK_SECRET"}
    assert all(s["optional"] and s["secret"] for s in manifest["requires_env"])
    for key in ("herenow_api_key", "webhook_secret"):
        assert manifest["config_schema"][key]["type"] == "secret"
        assert manifest["config_schema"][key]["required"] is False
    assert list(ROOT.glob("*/plugin.yaml")) == []            # one manifest, at the root


def test_exact_installed_root_loads_without_checkout_on_path(tmp_path):
    installed = tmp_path / "hermes" / "plugins" / "mock-review"
    installed.mkdir(parents=True)
    for name in ("__init__.py", "plugin.yaml"):
        shutil.copyfile(ROOT / name, installed / name)
    for name in ("mockreview", "skills"):
        shutil.copytree(ROOT / name, installed / name, ignore=shutil.ignore_patterns("__pycache__"))
    script = '''
import importlib.util, pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location("installed_review_plugin", root / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
class Context:
    def __init__(self): self.calls = []
    def get_config(self, name, default=None): return default
    def register_tool(self, **kw): self.calls.append(("tool", kw["name"]))
    def register_skill(self, name, path, **kw):
        assert path.is_file() and path.is_relative_to(root)
        self.calls.append(("skill", name))
    def register_command(self, name, **kw): self.calls.append(("command", name))
    def register_cli_command(self, **kw): self.calls.append(("cli", kw["name"]))
    def register_platform_handler(self, platform, factory): self.calls.append(("platform", platform))
ctx = Context()
plugin.register(ctx)
assert len(ctx.calls) == len(set(ctx.calls)) == 8
assert pathlib.Path(plugin._mockreview()[0].__file__).is_relative_to(root)
'''
    proc = subprocess.run([sys.executable, "-I", "-c", script, str(installed)], cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert proc.returncode == 0, proc.stderr


def install_fake_publish_script():
    script = config.hermes_home() / "skills" / "productivity" / "here-now" / "scripts" / "publish.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    return script


def test_missing_skill_key_and_escaping_skill_never_spawn(review, monkeypatch, secret_values, tmp_path):
    spawned = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(1))
    with pytest.raises(herenow.HereNowError, match="install-here-now-skill.*official/productivity/here-now"):
        herenow.publish(review["id"])
    outside = tmp_path / "outside" / "publish.sh"
    outside.parent.mkdir()
    outside.write_text("exit 9", encoding="utf-8")
    skill = config.hermes_home() / "skills" / "here-now" / "scripts"
    skill.parent.mkdir(parents=True)
    skill.symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(herenow.HereNowError, match="install-here-now-skill"):
        herenow.publish(review["id"])
    install_fake_publish_script()
    secret_values["HERENOW_API_KEY"] = " "
    with pytest.raises(herenow.HereNowError, match="HERENOW_API_KEY is not set"):
        herenow.publish(review["id"])
    assert not spawned


def test_no_credential_store_reads_across_public_operations(env, variants, tmp_path, monkeypatch,
                                                          secret_values, capsys):
    """A tripwire catches both built-in open and pathlib reads, including a positive control."""
    forbidden_names = {".env", "auth.json", "credentials", "credentials.json", "webhook_subscriptions.json"}
    forbidden_dirs = {".aws", ".azure", "gcloud", "vendor", "keychains"}
    original_open, original_io_open, original_read = builtins.open, io.open, Path.read_text
    blocked = []
    def check(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            p = Path(os.fsdecode(path))
            if p.name in forbidden_names or any(part.lower() in forbidden_dirs for part in p.parts):
                blocked.append(p.name)
                raise AssertionError("forbidden secret store read")
    def guarded_open(path, *a, **kw):
        check(path)
        return original_open(path, *a, **kw)
    def guarded_io_open(path, *a, **kw):
        check(path)
        return original_io_open(path, *a, **kw)
    def guarded_read(path, *a, **kw):
        check(path)
        return original_read(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(Path, "read_text", guarded_read)
    controls = [tmp_path / name for name in forbidden_names]
    controls += [tmp_path / name / "secret.json" for name in forbidden_dirs]
    for path in controls:
        for read in (lambda p: open(p, encoding="utf-8"), lambda p: p.read_text(encoding="utf-8"), lambda p: p.read_bytes()):
            with pytest.raises(AssertionError, match="forbidden"):
                read(path)
    assert len(blocked) == len(controls) * 3
    blocked.clear()
    origin = {"platform": "telegram", "chat_id": "test-group", "thread_id": "topic:a"}
    data = store.create_review("Guard", "", origin, "herenow", [store.parse_variant_spec(v) for v in variants])
    script = install_fake_publish_script()
    monkeypatch.setenv("HERENOW_API_KEY", "wrong-process-key")
    def publish(cmd, **kw):
        assert cmd[1] == str(script)
        assert kw["env"]["HERENOW_API_KEY"] == secret_values["HERENOW_API_KEY"]
        return SimpleNamespace(returncode=0, stderr="publish_result.slug=guard-review-ab12\n"
            "publish_result.site_url=https://guard-review-ab12.here.now/\npublish_result.auth_mode=authenticated\n")
    monkeypatch.setattr(subprocess, "run", publish)
    # Exercise account API reads and deletes without network providers.
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, *a): return b'{"records": []}'
    original_urlopen = herenow.urllib.request.urlopen
    def urlopen(request, **kw):
        if not request.full_url.startswith(config.HERENOW_API):
            return original_urlopen(request, **kw)
        assert request.get_header("Authorization") == "Bearer " + secret_values["HERENOW_API_KEY"]
        return Reply()
    monkeypatch.setattr(herenow.urllib.request, "urlopen", urlopen)
    herenow.publish(data["id"])
    assert owner.describe()["wake"]["secret"] == "set"
    assert SECRET not in json.dumps(owner.describe())
    result = service.submit(data["id"], 1, FEEDBACK, data["token"])
    assert result["notified"] and env["receiver"].wakes[-1]["ok"]
    assert cli.main(["notify-test", data["id"], "--to", env["receiver"].url,
                     "--secret-env", "MOCKREVIEW_WEBHOOK_SECRET"]) == 0
    assert cli.main(["settings", "--json"]) == 0
    assert cli.main(["list"]) == 0
    api.get_feedback(data["id"])
    monkeypatch.setattr(api, "server_up", lambda: False)
    created = api.create("API guard", origin, mode="local", prototype=variants[0].split("|")[2],
                         allow_live_demo=True)
    assert created["review_id"]
    assert cli.main(["create", "--title", "CLI guard", "--platform", "telegram", "--chat-id", "test-group",
                     "--mode", "local", "--prototype", variants[0].split("|")[2], "--allow-live-demo"]) == 0
    assert herenow.fetch_records("guard-review-ab12") == []
    assert herenow.delete_site("guard-review-ab12") is True
    secret_values.clear()
    assert not owner.wake_status()["ready"]
    assert not notify.send(origin, "guard-delivery")[0]
    with pytest.raises(herenow.HereNowError, match="not set"):
        herenow.api_key()
    assert not blocked


@pytest.mark.parametrize("origin", [
    {"platform": "telegram", "chat_id": "-test-group", "thread_id": "topic:alpha"},
    {"platform": "matrix", "chat_id": "!room:example.test"},
])
def test_tool_explicit_origin_reaches_store_payload_and_platform_route(env, tmp_path, origin):
    plugin = load_plugin()
    from test_plugin import FakeCtx, _decisions
    ctx = FakeCtx()
    plugin.register(ctx)
    tool = ctx.tools["mock_review_create"]
    props = tool["schema"]["parameters"]["properties"]
    assert "thread_id" not in props
    for key in ("platform", "chat_id", "thread_id"):
        assert "session context in the system prompt" in props["origin_" + key]["description"]
    result = json.loads(tool["handler"]({"title": "Explicit origin", "decisions": str(_decisions(tmp_path)),
        **{"origin_" + k: v for k, v in origin.items()}}))
    assert "error" not in result, result
    data = store.load(result["review_id"])
    assert data["origin"] == origin
    from test_owner import DEC_FB
    assert service.submit(data["id"], 1, DEC_FB, data["token"])["notified"]
    data = store.load(data["id"])
    payload = service.build_payload(data, data["rounds"][0])
    assert {key: payload[key] for key in origin} == origin
    assert notify.send(payload, "test-explicit-origin")[0]
    assert env["receiver"].wakes[-1]["path"].endswith("mock-review-" + origin["platform"])


@pytest.mark.parametrize("fields", [
    {}, {"origin": {"platform": "telegram", "chat_id": "untrusted"}}, {"thread_id": "legacy"},
    {"origin_platform": "telegram"}, {"origin_chat_id": "test-chat"},
    {"origin_platform": "telegram", "origin_chat_id": 123},
    {"origin_platform": "telegram", "origin_chat_id": " "},
    {"origin_platform": "telegram", "origin_chat_id": "a" * 257},
    {"origin_platform": "telegram", "origin_chat_id": "chat\\escape"},
    {"origin_platform": "telegram", "origin_chat_id": "chat\nother"},
    {"origin_platform": "telegram", "origin_chat_id": "chat\x7f"},
    {"origin_platform": "telegram", "origin_chat_id": "chat", "origin_thread_id": ""},
    {"origin_platform": "telegram", "origin_chat_id": "chat", "origin_thread_id": None},
    {"origin_platform": "telegram", "origin_chat_id": "chat", "origin_thread_id": False},
    {"origin_platform": "telegram", "origin_chat_id": "chat", "origin_thread_id": "a" * 257},
    {"origin_platform": "a" * 65, "origin_chat_id": "chat"},
    {"origin_platform": "bad/platform", "origin_chat_id": "chat"},
])
def test_tool_missing_or_invalid_origin_fails_before_writes(env, fields):
    plugin = load_plugin()
    from test_plugin import FakeCtx
    ctx = FakeCtx()
    plugin.register(ctx)
    result = json.loads(ctx.tools["mock_review_create"]["handler"]({"title": "Invalid origin", **fields},
        origin_platform="telegram", origin_chat_id="ignored-kwargs"))
    assert "origin" in result["error"]
    assert not store.list_ids()


def test_explicit_cli_secret_never_falls_back_to_another_key(review, env, capsys):
    assert cli.main(["notify-test", review["id"], "--to", env["receiver"].url,
                     "--secret-env", "MISSING_TEST_SECRET"]) == 2
    assert "MISSING_TEST_SECRET is not set" in capsys.readouterr().err
    assert not env["receiver"].requests
