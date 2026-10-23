"""Settings: precedence, both config.yaml readers, validation, and the features they switch."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, http
from mockreview import api, config, feedback, service, settings, store
from mockreview.store import ReviewError
from test_store import _decisions as _make_decisions, _design


def _decisions(tmp_path):
    f = tmp_path / "dec" / "decisions.json"
    return f if f.is_file() else _make_decisions(tmp_path)

CONFIG = """# a Hermes config with other plugins and YAML the mini reader must skip over
model:
  default: some/model   # comment
  notes: |
    plugins:
      entries: not-this-one
agent:
  tools: [a, b]
plugins:
  disabled:
    - browser-steel
  enabled:
  - example-plugin
  - mock-review
  entries:
    example-plugin:
      allow_tool_override: false
      settings:
        EXAMPLE_PLUGIN_DIR: /x
        devices: [watch]
    'mock-review':
      allow_tool_override: false
      settings:
        devices: [desktop, "tablet"]     # trailing comment
        default_theme: 'light'
        show_theme_toggle: no
        retry_seconds: 12.5
        max_pins: 12
        webhook_url: http://127.0.0.1:9/hook#frag
        public_base: "https://example.test:8790/"
        offer_live_demo: false
        empty_value:
        list_block:
          - desktop
          - 'mobile'
  scan_on_install: true
other: {a: 1}
"""


@pytest.fixture(params=["mini", "pyyaml"])
def reader(request, isolated_settings):
    if request.param == "pyyaml":
        pytest.importorskip("yaml")
    else:
        settings.FORCE_MINI_PARSER = True
    return request.param


def write_config(path, text=CONFIG):
    path.write_text(text, encoding="utf-8")
    settings._cache["key"] = None


def test_mini_parser_reads_only_this_plugins_settings():
    got = settings.parse_plugin_settings(CONFIG)
    assert got == {"devices": ["desktop", "tablet"], "default_theme": "light", "show_theme_toggle": False,
                   "retry_seconds": 12.5, "max_pins": 12, "webhook_url": "http://127.0.0.1:9/hook#frag",
                   "public_base": "https://example.test:8790/", "offer_live_demo": False, "empty_value": None,
                   "list_block": ["desktop", "mobile"]}


def test_mini_parser_agrees_with_pyyaml():
    yaml = pytest.importorskip("yaml")
    assert settings.parse_plugin_settings(CONFIG) == settings._from_yaml(CONFIG, yaml)


@pytest.mark.parametrize("text", [
    "plugins:\n  entries:\n    mock-review:\n      settings:\n        nested:\n          a: 1\n",
    "plugins:\n  entries:\n    mock-review:\n      settings:\n        x: &anchor 1\n",
    "plugins:\n  entries:\n    mock-review:\n      settings:\n        x: |\n          multi\n",
    "plugins:\n  entries:\n    mock-review:\n      settings:\n        x: [a, [b]]\n",
    "plugins:\n  entries:\n    mock-review:\n      settings: {devices: [mobile]}\n",
])
def test_mini_parser_is_strict_inside_our_mapping(text):
    with pytest.raises(settings.SettingsError):
        settings.parse_plugin_settings(text)


def test_mini_parser_no_plugin_entry():
    assert settings.parse_plugin_settings("model: x\nplugins:\n  enabled: []\n") == {}
    assert settings.parse_plugin_settings("") == {}


def test_without_pyyaml_the_builtin_reader_is_used(isolated_settings, monkeypatch):
    monkeypatch.setattr(settings, "_yaml_module", lambda: None)   # standalone interpreter without PyYAML
    write_config(isolated_settings)
    values, err, reader = settings.file_settings()
    assert err is None and reader.startswith("built-in") and values["max_pins"] == 12
    assert settings.resolve_one("devices") == (["desktop", "tablet"], "config.yaml (plugins.entries.mock-review.settings.devices)", [])


def test_unsupported_config_falls_back_to_env_and_defaults_with_a_warning(isolated_settings, monkeypatch, caplog):
    monkeypatch.setattr(settings, "_yaml_module", lambda: None)
    write_config(isolated_settings, "plugins:\n  entries:\n    mock-review:\n      settings:\n        devices:\n          a: 1\n")
    monkeypatch.setenv("MOCKREVIEW_MAX_PINS", "7")
    with caplog.at_level("WARNING"):
        assert settings.get("devices") == ["desktop", "mobile"]          # default
        assert settings.get("max_pins") == 7                               # env still applies
    assert "ignored" in caplog.text
    assert settings.describe()["error"] and "nested" in settings.describe()["error"]


def test_precedence_override_env_yaml_default(reader, isolated_settings, monkeypatch):
    assert settings.resolve_one("max_pins")[:2] == (40, "default")
    write_config(isolated_settings)
    assert settings.resolve_one("max_pins")[:2] == (12, "config.yaml (plugins.entries.mock-review.settings.max_pins)")
    monkeypatch.setenv("MOCKREVIEW_MAX_PINS", "20")
    assert settings.resolve_one("max_pins")[:2] == (20, "env MOCKREVIEW_MAX_PINS")
    assert settings.resolve_one("max_pins", 30)[:2] == (30, "override")
    # the existing env names keep working, above config.yaml
    assert config.public_base() == "https://example.test:8790"
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_BASE", "https://env.test")
    assert config.public_base() == "https://env.test"
    assert config.retry_seconds() == 12.5
    assert config.webhook_url() == "http://127.0.0.1:9/hook#frag"


def test_bad_env_or_config_value_is_skipped_but_a_bad_override_raises(isolated_settings, monkeypatch):
    write_config(isolated_settings, "plugins:\n  entries:\n    mock-review:\n      settings:\n        max_pins: lots\n")
    monkeypatch.setenv("MOCKREVIEW_DEVICES", "watch")
    v, src, notes = settings.resolve_one("max_pins")
    assert (v, src) == (40, "default") and "max_pins" in notes[0]
    assert settings.get("devices") == ["desktop", "mobile"]
    with pytest.raises(settings.SettingsError, match="max_pins"):
        settings.resolve_one("max_pins", 0)


def test_config_reread_when_file_changes(isolated_settings):
    write_config(isolated_settings, "plugins:\n  entries:\n    mock-review:\n      settings:\n        port: 8800\n")
    assert config.port() == 8800
    isolated_settings.write_text("plugins:\n  entries:\n    mock-review:\n      settings:\n        port: 8801   \n", encoding="utf-8")
    os.utime(isolated_settings, ns=(1, 1))
    assert config.port() == 8801


@pytest.mark.parametrize("value,msg", [([], "at least one"), (["watch"], "unknown device"),
                                       (["mobile", "mobile"], "twice"), ("desktop,phone", "unknown device"), (3, "list")])
def test_devices_validation(value, msg):
    with pytest.raises(settings.SettingsError, match=msg):
        settings.coerce(settings.BY_NAME["devices"], value)


def test_devices_normalised_and_default_device_clamped():
    assert settings.coerce(settings.BY_NAME["devices"], "mobile, Desktop") == ["desktop", "mobile"]
    assert settings.review_settings({"devices": ["mobile"]})["default_device"] == "auto"
    with pytest.raises(settings.SettingsError, match="default_device"):
        settings.review_settings({"devices": ["mobile"], "default_device": "desktop"})


def test_create_stores_resolved_look_and_board_gets_it(env, tmp_path, isolated_settings, server):
    write_config(isolated_settings, "plugins:\n  entries:\n    mock-review:\n      settings:\n"
                                    "        devices: [desktop, tablet, mobile]\n        require_note_on_change: true\n")
    res = api.create("Look", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, decisions=str(_decisions(tmp_path)), design=str(_design(tmp_path)),
                     show_theme_toggle=False, devices=["mobile", "tablet"])
    data = store.load(res["review_id"])
    assert data["settings"]["devices"] == ["tablet", "mobile"]                 # override, normalised
    assert data["settings"]["require_note_on_change"] is True                 # config.yaml
    assert data["settings"]["show_theme_toggle"] is False and data["settings"]["mobile_width"] == 390
    # config changes later: the review keeps what it was made with
    write_config(isolated_settings, "plugins:\n  entries:\n    mock-review:\n      settings:\n        devices: [desktop]\n")
    code, _, body = http("GET", f"{server}/r/{res['review_id']}/state.json")
    assert code == 200 and json.loads(body)["settings"]["devices"] == ["tablet", "mobile"]


def test_old_reviews_have_no_settings(review):
    assert "settings" not in store.load(review["id"]) and store.public_state(review)["settings"] is None


def test_mode_defaults_to_default_host(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "tailnet")
    assert api.resolve_mode(None) == "tailscale" and api.resolve_mode("tailnet") == "tailscale"
    assert api.resolve_mode("herenow") == "herenow"
    with pytest.raises(ReviewError):
        api.resolve_mode("funnel")


# ---------------------------------------------------------------- offer_live_demo

def test_prototype_round_refused_unless_live_demo_allowed(env, tmp_path, variants, monkeypatch):
    page = variants[0].split("|")[2]
    with pytest.raises(ReviewError, match="offer_live_demo") as ei:
        api.create("Demo", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, prototype=page)
    assert ei.value.status == 403
    res = api.create("Demo", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, decisions=str(_decisions(tmp_path)))
    with pytest.raises(ReviewError, match="offer_live_demo"):
        api.add_round(res["review_id"], prototype=page)
    assert api.add_round(res["review_id"], prototype=page, allow_live_demo=True)["round"] == 2   # per-call override
    monkeypatch.setenv("MOCKREVIEW_OFFER_LIVE_DEMO", "true")                                       # saved setting on
    assert api.add_round(res["review_id"], prototype=page)["round"] == 3
    with pytest.raises(ReviewError, match="offer_live_demo"):                                    # override beats it
        api.add_round(res["review_id"], prototype=page, allow_live_demo=False)


def test_cli_refuses_prototype_with_the_setting_name(env, variants, capsys):
    from mockreview import cli
    page = variants[0].split("|")[2]
    assert cli.main(["create", "--title", "x", "--platform", "discord", "--chat-id", "test-chat", "--thread", "1490000000000000001", "--prototype", page]) == 1
    assert "offer_live_demo" in capsys.readouterr().err
    assert cli.main(["create", "--title", "x", "--platform", "discord", "--chat-id", "test-chat", "--thread", "1490000000000000001", "--prototype", page,
                     "--allow-live-demo", "--devices", "mobile"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["settings"]["devices"] == ["mobile"]


# ---------------------------------------------------------------- feedback rules stored in the review

def _decision_review(tmp_path, **look):
    res = api.create("Rules", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, decisions=str(_decisions(tmp_path)), **look)
    return store.load(res["review_id"])


def test_require_note_on_change_is_enforced_on_submit(env, tmp_path):
    data = _decision_review(tmp_path, require_note_on_change=True)
    bare = {"marks": {"d1": {"status": "change", "option": "b", "note": " "}}, "pins": [], "guidelines": ""}
    with pytest.raises(ReviewError, match="note to every Change.*d1"):
        service.submit(data["id"], 1, bare, data["token"], do_notify=False)
    ok = {"marks": {"d1": {"status": "change", "option": "b", "note": "smaller"}}, "pins": [], "guidelines": ""}
    assert service.submit(data["id"], 1, ok, data["token"], do_notify=False)["ok"]


def test_require_note_off_by_default_and_for_old_reviews(env, tmp_path):
    data = _decision_review(tmp_path)
    bare = {"marks": {"d1": {"status": "change"}}, "pins": [], "guidelines": ""}
    assert service.submit(data["id"], 1, bare, data["token"], do_notify=False)["ok"]
    r = store.load(data["id"])["rounds"][0]
    assert feedback.sanitize_for_round(bare, r, None)["marks"]["d1"]["status"] == "change"


def test_pins_disabled_and_max_pins(env, tmp_path):
    pins = [{"block": "d1", "option": "a", "w": "tablet", "x": 0.1 * i, "y": 0.5, "text": f"p{i}"} for i in range(5)]
    r = _decision_review(tmp_path, pins_enabled=False)["rounds"][0]
    look_off = {"pins_enabled": False, "max_pins": 40}
    with pytest.raises(ReviewError, match="Nothing to send"):
        feedback.sanitize_for_round({"marks": {}, "pins": pins}, r, look_off)
    out = feedback.sanitize_for_round({"marks": {}, "pins": pins}, r, {"pins_enabled": True, "max_pins": 2})
    assert [p["text"] for p in out["pins"]] == ["p0", "p1"] and out["pins"][0]["w"] == "tablet"


# ---------------------------------------------------------------- housekeeping

def _age(rid, days, field="created_at", status=None):
    import time as _t
    with store.locked(rid):
        d = store.load(rid)
        old = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - days * 86400))
        d[field] = old
        for r in d["rounds"]:
            r["created_at"] = old if field == "created_at" else r["created_at"]
        if status:
            d["status"] = status
        store.save(rid, d)


def test_housekeeping_off_by_default(env, tmp_path):
    data = _decision_review(tmp_path)
    _age(data["id"], 400)
    assert api.housekeeping() == {"closed": [], "pruned": [], "errors": []}
    assert store.load(data["id"])["status"] == "open"


def test_auto_close_idle_reviews_deletes_herenow_site(env, tmp_path, monkeypatch):
    from mockreview import herenow
    calls = []
    monkeypatch.setattr(herenow, "poll_review", lambda rid, republish=True: calls.append(("poll", rid)) or [])
    monkeypatch.setattr(herenow, "fetch_records", lambda slug: calls.append(("fetch", slug)) or [{"id": "r1"}])
    monkeypatch.setattr(herenow, "delete_site", lambda slug: calls.append(("delete", slug)) or True)
    monkeypatch.setenv("MOCKREVIEW_AUTO_CLOSE_IDLE_DAYS", "7")
    idle = _decision_review(tmp_path)
    fresh = _decision_review(tmp_path)
    with store.locked(idle["id"]):
        d = store.load(idle["id"])
        d["mode"] = "herenow"
        d["herenow"] = {"slug": "idle-site-x1", "site_url": "https://idle-site-x1.here.now", "processed_records": []}
        store.save(idle["id"], d)
    _age(idle["id"], 8)
    _age(fresh["id"], 2)
    res = api.housekeeping()
    assert res["closed"] == [idle["id"]] and not res["errors"]
    assert ("delete", "idle-site-x1") in calls
    d = store.load(idle["id"])
    assert d["status"] == "closed" and d["herenow"]["deleted_at"]
    assert (store.review_dir(idle["id"]) / "archive" / "sitedata-records.json").is_file()
    assert store.load(fresh["id"])["status"] == "open"


def test_recent_feedback_keeps_a_review_open(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_AUTO_CLOSE_IDLE_DAYS", "7")
    data = _decision_review(tmp_path)
    service.submit(data["id"], 1, {"marks": {"d1": {"status": "approve"}}, "pins": []}, data["token"], do_notify=False)
    _age(data["id"], 30)   # created + round long ago, feedback just now
    assert api.housekeeping()["closed"] == []


def test_prune_closed_reviews(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_KEEP_CLOSED_DAYS", "30")
    old = _decision_review(tmp_path)
    recent = _decision_review(tmp_path)
    opened = _decision_review(tmp_path)
    api.close(old["id"])
    api.close(recent["id"])
    _age(old["id"], 31, field="closed_at")
    _age(opened["id"], 400)
    res = api.housekeeping()
    assert res["pruned"] == [old["id"]]
    assert not store.exists(old["id"]) and not (config.data_root() / old["id"]).exists()
    assert store.exists(recent["id"]) and store.exists(opened["id"])


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_prune_finishes_after_lock_closes(env, tmp_path, monkeypatch, cleanup_fails):
    import contextlib
    import shutil

    monkeypatch.setenv("MOCKREVIEW_KEEP_CLOSED_DAYS", "30")
    data = _decision_review(tmp_path)
    api.close(data["id"])
    _age(data["id"], 31, field="closed_at")
    directory = store.review_dir(data["id"])
    held = []
    locked, rmtree = store.locked, shutil.rmtree

    @contextlib.contextmanager
    def track_lock(rid):
        with locked(rid):
            held.append(rid)
            try:
                yield
            finally:
                held.pop()

    def windows_rmtree(path, ignore_errors=False):
        if held:
            assert ignore_errors
            for child in path.iterdir():
                if child.name == ".lock":
                    continue  # Windows cannot unlink an open lock file.
                if child.is_dir():
                    rmtree(child)
                else:
                    child.unlink()
        else:
            assert (path / ".lock").is_file()
            if cleanup_fails:
                raise PermissionError("synthetic cleanup refusal")
            rmtree(path)

    monkeypatch.setattr(store, "locked", track_lock)
    monkeypatch.setattr(shutil, "rmtree", windows_rmtree)
    result = api.housekeeping()
    assert result["pruned"] == ([] if cleanup_fails else [data["id"]])
    assert bool(result["errors"]) == cleanup_fails
    assert directory.exists() == cleanup_fails


def test_worker_runs_housekeeping(env, tmp_path, monkeypatch):
    from mockreview import server
    seen = []
    monkeypatch.setattr(api, "housekeeping", lambda: seen.append(1) or {"closed": [], "pruned": []})
    w = server.Worker(tick=0.05)
    w.start()
    try:
        import time as _t
        deadline = _t.time() + 5
        while not seen and _t.time() < deadline:
            _t.sleep(0.05)
    finally:
        w.stop_event.set()
        w.join(2)
    assert seen
