"""wake_toolsets: the opt-in that lets the woken session act, the loopback guard, and the hand-off a tool-less woken
session posts instead of saying it cannot build."""

import builtins
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ROOT
from mockreview import api, config, runtime, service, settings, setup, store, wakeguard
from mockreview.store import ReviewError
from test_notice import DEC_FB, ORIGIN, _decisions_file
from test_owner import Service, start_service, worker  # noqa: F401 (fixtures)
from test_setup import fake_create, make_shim, no_real_tailscale_serve, run_setup  # noqa: F401 (autouse fixture)

WAKE, NOTICE = "mock-review-telegram", "mock-review-telegram-notice"


def _subs_path():
    return Path(os.environ["HERMES_HOME"]) / "webhook_subscriptions.json"


def _subs():
    return json.loads(_subs_path().read_text(encoding="utf-8"))


def _shim(tmp_path):
    return make_shim(tmp_path, {"webhook_enabled": True, "subs": True})


# ---------------------------------------------------------------- the setting

def test_wake_toolsets_defaults_to_empty_and_validates_names(monkeypatch):
    assert settings.BY_NAME["wake_toolsets"].default == []
    assert settings.get("wake_toolsets") == []
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "Terminal, file")
    assert settings.get("wake_toolsets") == ["terminal", "file"]
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "mock-review,terminal,file,terminal")   # the plugin's own toolset
    assert settings.get("wake_toolsets") == ["mock-review", "terminal", "file"]
    with pytest.raises(settings.SettingsError, match="wake_toolsets"):
        settings.get("wake_toolsets", ["terminal; rm -rf /"])


# ---------------------------------------------------------------- setup prints the route's manual toolsets line

def _line(toolsets):
    return '"toolsets": ' + json.dumps(toolsets)


def _no_toolsets_on_disk():
    return not _subs_path().exists() or all("toolsets" not in r for r in _subs().values())


def test_setup_prints_the_toolsets_line_for_the_wake_route_only_and_never_writes_it(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal,file")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0, out
    assert f'add this line to the "{WAKE}" route' in out and _line(["terminal", "file"]) in out
    assert f'"{NOTICE}" route' not in out                         # the deliver-only route runs no agent
    assert re.search(r"^\s*wake toolsets\s+manual\s+\(add by hand, see above; not verified\)", out, re.M)
    assert _no_toolsets_on_disk()                                 # setup never edits the route file itself
    assert _subs()[WAKE]["secret"] not in out

    before = _subs()
    rc, again, _ = run_setup(monkeypatch, shim, capsys)            # re-run: same instructions, file untouched
    assert rc == 0 and _line(["terminal", "file"]) in again and _no_toolsets_on_disk()
    assert set(_subs()) == set(before)


def test_setup_tells_the_user_a_rerun_drops_a_hand_added_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    assert "re-subscribes" in out and "add the line again" in out


def test_setup_dry_run_prints_the_toolsets_line_and_writes_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal,file")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--dry-run"))
    assert rc == 0
    assert _line(["terminal", "file"]) in out
    assert re.search(r"^\s*wake toolsets\s+manual", out, re.M)
    assert not _subs_path().exists()


def test_setup_without_wake_toolsets_never_mentions_or_writes_them(monkeypatch, tmp_path, capsys):
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    assert "toolsets" not in out
    assert all("toolsets" not in r for r in _subs().values())


def test_setup_refuses_wake_toolsets_with_lan_hosting(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--hosting", "lan"))
    assert rc == 1
    assert "refusing to set wake_toolsets" in out and "lan hosting" in out
    assert '"toolsets":' not in out and _no_toolsets_on_disk()


def test_setup_with_wake_toolsets_never_opens_a_hermes_state_file(monkeypatch, tmp_path, capsys):
    """With wake_toolsets set, setup still changes Hermes state only through `hermes` subcommands: it never opens
    webhook_subscriptions.json (nor .env, auth.json, config.yaml) itself, for reading or writing."""
    forbidden = {".env", "auth.json", "config.yaml", "webhook_subscriptions.json", ".webhook_subscriptions.lock"}
    opened = []
    original_open, original_io_open, original_read = builtins.open, io.open, Path.read_text
    original_os_open = os.open

    def check(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(path)).name
            opened.append(name)
            if name in forbidden or name.startswith(".webhook_subscriptions."):
                raise AssertionError(f"forbidden Hermes state access: {name}")

    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: (check(p), original_open(p, *a, **k))[1])
    monkeypatch.setattr(io, "open", lambda p, *a, **k: (check(p), original_io_open(p, *a, **k))[1])
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k: (check(p), original_read(p, *a, **k))[1])
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (check(p), original_os_open(p, *a, **k))[1])
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0, out
    assert not forbidden & set(opened)


def test_setup_module_has_no_route_file_writer():
    """The sanctioned path is `hermes webhook ...`; the old direct reader/writer must not come back."""
    src = (ROOT / "mockreview" / "setup.py").read_text(encoding="utf-8")
    for name in ("_read_subscriptions", "_write_subscriptions", "sync_wake_toolsets", "SUBSCRIPTIONS_LOCK"):
        assert name not in src, name


def test_hermes_reads_the_line_setup_printed(monkeypatch, tmp_path, capsys):
    """Paste the printed line into the route the way the user would, then read it with Hermes's own
    WebhookAdapter.toolsets_for_source (keyed on user_id webhook:<route>)."""
    src = os.environ.get("HERMES_AGENT_SRC")
    if src:
        sys.path.insert(0, str(Path(src).expanduser()))
    try:
        from gateway.platforms.webhook import WebhookAdapter
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Hermes webhook adapter unavailable ({type(e).__name__}); set HERMES_AGENT_SRC")
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal,file")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    printed = next(l.strip() for l in out.splitlines() if l.strip().startswith('"toolsets":'))
    subs = _subs()
    subs[WAKE].update(json.loads("{" + printed.rstrip(",") + "}"))   # the user's manual edit
    adapter = WebhookAdapter.__new__(WebhookAdapter)
    adapter._routes = subs
    assert adapter.toolsets_for_source(SimpleNamespace(user_id=f"webhook:{WAKE}")) == ["terminal", "file"]
    assert adapter.toolsets_for_source(SimpleNamespace(user_id=f"webhook:{NOTICE}")) is None


# ---------------------------------------------------------------- the loopback guard on create / add-round

def test_guard_allows_wake_off_and_loopback_hosts(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    api.check_wake_toolsets("tailscale", wake=True)
    api.check_wake_toolsets("local", wake=True)
    api.check_wake_toolsets("tailscale", wake=False)
    monkeypatch.delenv("MOCKREVIEW_WAKE_TOOLSETS")
    api.check_wake_toolsets("local", wake=True)


def test_guard_refuses_every_waking_board_while_the_server_listens_beyond_loopback(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    monkeypatch.setenv("MOCKREVIEW_HOST", "0.0.0.0")
    for mode in ("local", "tailscale"):
        with pytest.raises(ReviewError, match="beyond loopback"):
            api.check_wake_toolsets(mode, wake=True)
    api.check_wake_toolsets("local", wake=False)


def test_guard_refuses_a_waking_board_while_a_public_url_is_configured(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", "https://reviews.example.com")
    for mode in ("local", "tailscale"):
        with pytest.raises(ReviewError, match="public URL"):
            api.check_wake_toolsets(mode, wake=True)
    api.check_wake_toolsets("local", wake=False)


def test_guard_allows_a_waking_board_without_a_public_url(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    monkeypatch.delenv("MOCKREVIEW_PUBLIC_URL", raising=False)
    monkeypatch.delenv("MOCKREVIEW_HOST", raising=False)
    assert config.public_url() == ""                       # not vacuous: no public URL and the default loopback host
    api.check_wake_toolsets("local", wake=True)
    api.check_wake_toolsets("tailscale", wake=True)


def test_guard_still_refuses_public_mode_without_a_public_url(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    monkeypatch.delenv("MOCKREVIEW_PUBLIC_URL", raising=False)
    with pytest.raises(ReviewError, match="public URL"):
        api.check_wake_toolsets("public", wake=True)


def test_create_refuses_a_waking_board_while_wake_toolsets_is_set(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    monkeypatch.setenv("MOCKREVIEW_HOST", "0.0.0.0")
    with pytest.raises(ReviewError) as e:
        api.create("LAN board", ORIGIN, mode="local", decisions=str(_decisions_file(tmp_path)))
    assert e.value.status == 409 and "wake_toolsets" in e.value.message and "beyond loopback" in e.value.message
    assert store.list_ids() == []                                # nothing was created


def test_create_sees_the_host_the_server_bound_not_the_setting(tmp_path, start_service, worker, monkeypatch):
    """`serve --host 0.0.0.0` is the real exposure even when the host setting still says loopback."""
    svc = start_service(host="0.0.0.0")
    worker(svc.port)
    monkeypatch.setenv("MOCKREVIEW_HOST", "127.0.0.1")
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    with pytest.raises(ReviewError) as e:
        api.create("LAN board", ORIGIN, mode="local", decisions=str(_decisions_file(tmp_path)))
    assert e.value.status == 409 and "wake_toolsets" in e.value.message and "beyond loopback" in e.value.message
    assert [p.name for p in svc.root.iterdir() if (p / "review.json").is_file()] == []


def test_a_stale_descriptor_never_masks_a_non_loopback_host_setting(env, monkeypatch):
    """A descriptor a dead server left behind must not make the setting's 0.0.0.0 look like loopback."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    root = config.data_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / runtime.NAME
    path.write_text(json.dumps({"api": 1, "service": "mock-review", "pid": dead.pid, "base": "http://127.0.0.1:9",
                                "host": "127.0.0.1", "port": 9, "data_root": str(root), "started_at": time.time(),
                                "secret": "x" * 32}), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("MOCKREVIEW_HOST", "0.0.0.0")
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    assert runtime.read(root) is not None                 # the descriptor itself is still accepted
    assert wakeguard.lan_reason() is not None
    with pytest.raises(ReviewError, match="beyond loopback"):
        api.check_wake_toolsets("local", wake=True)


def test_add_round_refuses_a_waking_board_while_wake_toolsets_is_set(env, monkeypatch, tmp_path):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    data = store.create_review("Local", "", ORIGIN, "local", spec)
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    monkeypatch.setenv("MOCKREVIEW_HOST", "0.0.0.0")
    with pytest.raises(ReviewError, match="wake_toolsets"):
        api.add_round(data["id"], decisions=str(_decisions_file(tmp_path)))
    assert len(store.load(data["id"])["rounds"]) == 1


def test_wake_is_held_for_any_board_once_the_server_listens_beyond_loopback(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    data = store.create_review("Tailnet", "", ORIGIN, "tailscale", spec)
    monkeypatch.setenv("MOCKREVIEW_HOST", "0.0.0.0")
    service.submit(data["id"], 1, DEC_FB, data["token"])
    assert env["receiver"].wakes == []


def test_wake_is_held_for_any_board_while_a_public_url_is_configured(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    data = store.create_review("Local", "", ORIGIN, "local", spec, wake=True)
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", "https://reviews.example.com")
    service.submit(data["id"], 1, DEC_FB, data["token"])
    assert env["receiver"].wakes == []


# ---------------------------------------------------------------- graceful degrade without tools

def test_wake_prompt_hands_off_to_the_thread_without_claiming_it_cannot():
    line = setup.HANDOFF_LINE
    assert line in setup.PROMPT_TEMPLATE
    assert "continues in this thread" in line
    assert not re.search(r"can.?t|cannot|unable|no shell|no file", line, re.I)
    assert "mock_review_feedback(review_id='{review_id}', round={round})" in setup.PROMPT_TEMPLATE


def test_packaged_skill_tells_a_tool_less_session_to_hand_off_and_the_thread_to_take_over():
    text = (ROOT / "skills" / "usage" / "SKILL.md").read_text(encoding="utf-8")
    assert "continues in this thread" in text
    assert "mock_review_ack" in text
    assert "Do not say you cannot" in text


def test_hermes_renders_the_hand_off_line_from_the_real_wake_payload(env, tmp_path):
    src = os.environ.get("HERMES_AGENT_SRC")
    if src:
        sys.path.insert(0, str(Path(src).expanduser()))
    try:
        from gateway.platforms.webhook import WebhookAdapter
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Hermes webhook adapter unavailable ({type(e).__name__}); set HERMES_AGENT_SRC")
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    data = store.create_review("Decide", "", ORIGIN, "tailscale", spec)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    payload = json.loads(env["receiver"].wakes[0]["body"])
    adapter = WebhookAdapter.__new__(WebhookAdapter)
    rendered = adapter._render_prompt(setup.PROMPT_TEMPLATE, payload, "review.submitted", WAKE)
    assert "Round 1 feedback is saved; the build continues in this thread's chat session." in rendered
    assert f"mock_review_feedback(review_id='{data['id']}', round=1)" in rendered
