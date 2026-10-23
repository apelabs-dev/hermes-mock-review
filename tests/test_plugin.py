"""The root plugin entry point (__init__.py) against a fake context."""

import argparse
import json
import os

import pytest

from conftest import ROOT, load_plugin
from mockreview import settings, store
from test_store import _decisions as _make_decisions, _design


def _decisions(tmp_path):
    f = tmp_path / "dec" / "decisions.json"
    return f if f.is_file() else _make_decisions(tmp_path)


class OldFakeCtx:
    """The parts of hermes_cli.plugins.PluginContext the plugin uses, as a Hermes without register_platform_handler."""

    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})
        self.config_reads = []
        self.tools, self.skills, self.commands, self.cli = {}, {}, {}, {}
        self.platform_handlers = []

    def get_config(self, key, default=None):
        self.config_reads.append(key)
        return self.cfg.get(key, default)

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False,
                      description="", emoji="", override=False):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "description": description}

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = {"path": path, "description": description}

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = {"setup_fn": setup_fn, "handler_fn": handler_fn, "help": help}


class FakeCtx(OldFakeCtx):
    """A current Hermes: also has register_platform_handler (records, never calls, the factory)."""

    def register_platform_handler(self, platform, factory):
        self.platform_handlers.append((platform, factory))


@pytest.fixture
def plugin():
    return load_plugin()


def snapshot(root):
    return sorted(str(p) for p in root.rglob("*"))


def test_register_registers_everything_and_writes_nothing(env, plugin, tmp_path):
    before = snapshot(tmp_path)
    ctx = FakeCtx({"ask_host_each_time": False, "default_host": "herenow"})
    plugin.register(ctx)
    assert snapshot(tmp_path) == before                       # no writes at register time
    assert set(ctx.tools) == {"mock_review_create", "mock_review_add_round", "mock_review_feedback", "mock_review_list"}
    assert {t["toolset"] for t in ctx.tools.values()} == {"mock-review"}
    assert set(ctx.skills) == {"usage"} and ctx.skills["usage"]["path"].is_file()
    assert set(ctx.commands) == {"mockreview"} and set(ctx.cli) == {"mockreview"}
    assert "ask_host_each_time" in ctx.config_reads              # settings come through get_config
    mode = ctx.tools["mock_review_create"]["schema"]["parameters"]["properties"]["mode"]
    assert "herenow" in mode["description"] and "Ask" not in mode["description"]
    for t in ctx.tools.values():                                # short descriptions (they cost tokens every turn)
        assert len(t["schema"]["description"]) < 120
    assert len(json.dumps([t["schema"] for t in ctx.tools.values()])) < 6000


def test_tools_return_json_and_read_settings_via_get_config(env, plugin, tmp_path):
    ctx = FakeCtx({"devices": ["desktop", "tablet"], "require_note_on_change": True, "mobile_width": 375})
    plugin.register(ctx)
    t = {k: v["handler"] for k, v in ctx.tools.items()}
    res = json.loads(t["mock_review_create"]({"title": "Plugin review", "wake": False,
                                              "decisions": str(_decisions(tmp_path)), "design": str(_design(tmp_path)),
                                              "show_theme_toggle": False}))
    assert "error" not in res, res
    stored = store.load(res["review_id"])["settings"]
    assert stored["devices"] == ["desktop", "tablet"] and stored["require_note_on_change"] is True   # get_config
    assert stored["mobile_width"] == 375 and stored["show_theme_toggle"] is False                    # + tool arg
    rows = json.loads(t["mock_review_list"]({}))["reviews"]
    assert [r["review_id"] for r in rows] == [res["review_id"]]
    fb = json.loads(t["mock_review_feedback"]({"review_id": res["review_id"]}))
    assert fb["round"] == 1 and fb["feedback"] is None and "markdown" in fb
    nxt = json.loads(t["mock_review_add_round"]({"review_id": res["review_id"], "decisions": str(_decisions(tmp_path)),
                                                 "devices": ["mobile"]}))
    assert nxt["round"] == 2 and store.load(res["review_id"])["settings"]["devices"] == ["mobile"]


def test_tool_errors_are_json_never_raised(env, plugin, tmp_path, variants):
    ctx = FakeCtx()
    plugin.register(ctx)
    t = {k: v["handler"] for k, v in ctx.tools.items()}
    page = variants[0].split("|")[2]
    out = json.loads(t["mock_review_create"]({"title": "x", "wake": False, "prototype": page}))
    assert "offer_live_demo" in out["error"]
    out = json.loads(t["mock_review_create"]({"title": "x", "wake": False, "prototype": page,
                                              "allow_live_demo": True}))
    assert out.get("round") == 1, out
    assert "explicit origin_platform and origin_chat_id" in json.loads(t["mock_review_create"]({"title": "x"}))["error"]
    assert "error" in json.loads(t["mock_review_feedback"]({"review_id": "nope-000000"}))
    assert "devices" in json.loads(t["mock_review_create"]({"title": "x", "wake": False,
                                                            "decisions": str(_decisions(tmp_path)),
                                                            "devices": ["watch"]}))["error"]
    ctx2 = FakeCtx({"offer_live_demo": True})                   # the saved setting turns it on
    plugin.register(ctx2)
    out = json.loads(ctx2.tools["mock_review_create"]["handler"]({"title": "y", "wake": False,
                                                                  "prototype": page}))
    assert out.get("round") == 1, out


def test_slash_command_plain_text(env, plugin, tmp_path, monkeypatch):
    monkeypatch.delenv("MOCKREVIEW_PUBLIC_BASE", raising=False)
    ctx = FakeCtx({"public_base": "https://board.test", "default_host": "tailnet"})
    plugin.register(ctx)
    slash = ctx.commands["mockreview"]["handler"]
    assert slash("list") == "No open mock reviews."
    rid = json.loads(ctx.tools["mock_review_create"]["handler"](
        {"title": "Slash", "wake": False, "decisions": str(_decisions(tmp_path))}))["review_id"]
    assert rid in slash("list") and "https://board.test/r/" + rid in slash("list")
    assert slash(f"open {rid}").endswith(f"https://board.test/r/{rid}")
    text = slash("settings")
    assert "public_base = https://board.test  (config.yaml via Hermes get_config" in text
    assert "|" not in text and "---" not in text                  # no markdown tables
    assert slash(f"close {rid}") == f"Closed {rid}."
    assert store.load(rid)["status"] == "closed"
    assert slash("open nope-000000").startswith("Error:")
    assert slash("").startswith("Usage:")


def test_cli_command_wraps_the_cli(env, plugin, capsys, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PORT", "8899")
    ctx = FakeCtx({"max_pins": 9})
    plugin.register(ctx)
    entry = ctx.cli["mockreview"]
    top = argparse.ArgumentParser(prog="hermes")
    subs = top.add_subparsers(dest="command")
    p = subs.add_parser("mockreview")
    entry["setup_fn"](p)
    p.set_defaults(func=entry["handler_fn"])
    args = top.parse_args(["mockreview", "settings", "--json"])
    assert args.func(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["settings"]["max_pins"]["value"] == 9 and "get_config" in out["settings"]["max_pins"]["source"]
    assert out["settings"]["port"] == {"value": 8899, "source": "env MOCKREVIEW_PORT"}
    args = top.parse_args(["mockreview", "list"])
    assert args.func(args) == 0 and json.loads(capsys.readouterr().out) == []


def test_manifest_schema_matches_settings_spec():
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert settings.render_config_schema() in text
    for line in ("name: mock-review", "manifest_version: 2", "api_version: 1", "kind: standalone"):
        assert line in text.splitlines()
    for tool in ("mock_review_create", "mock_review_add_round", "mock_review_feedback", "mock_review_list"):
        assert f"  - {tool}" in text
