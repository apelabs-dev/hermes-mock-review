"""wake_toolsets: the opt-in that lets the woken session act, the here.now guard, and the hand-off a tool-less woken
session posts instead of saying it cannot build."""

import builtins
import io
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ROOT
from mockreview import api, service, settings, setup, store
from mockreview.store import ReviewError
from test_notice import DEC_FB, ORIGIN, _decisions_file
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


# ---------------------------------------------------------------- setup writes the route's manual toolsets key

def test_setup_grants_wake_toolsets_on_the_wake_route_only_and_is_idempotent(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal,file")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0, out
    subs = _subs()
    assert subs[WAKE]["toolsets"] == ["terminal", "file"]
    assert "toolsets" not in subs[NOTICE]                        # the deliver-only route runs no agent
    assert f"will change: set toolsets terminal, file on webhook route {WAKE}" in out
    assert re.search(r"^\s*wake toolsets\s+ok\s+\(terminal, file\)", out, re.M)
    assert subs[WAKE]["secret"] not in out

    rc, out, _ = run_setup(monkeypatch, shim, capsys)            # re-run: the same file
    assert rc == 0 and _subs() == subs

    monkeypatch.delenv("MOCKREVIEW_WAKE_TOOLSETS")               # emptied: the key goes away
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    after = _subs()
    assert "toolsets" not in after[WAKE] and "toolsets" not in after[NOTICE]
    assert "set toolsets" not in out


def test_setup_dry_run_shows_the_toolsets_change_and_writes_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal,file")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--dry-run"))
    assert rc == 0
    assert f"will change: set toolsets terminal, file on webhook route {WAKE}" in out
    assert re.search(r"^\s*wake toolsets\s+planned\s+\(terminal, file\)", out, re.M)
    assert not _subs_path().exists()


def test_setup_without_wake_toolsets_never_mentions_or_writes_them(monkeypatch, tmp_path, capsys):
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    assert "toolsets" not in out
    assert all("toolsets" not in r for r in _subs().values())


def test_setup_refuses_wake_toolsets_while_a_herenow_review_is_open(env, monkeypatch, tmp_path, capsys):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    public = store.create_review("Public", "", ORIGIN, "herenow", spec)
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 1
    assert "refusing to set wake_toolsets" in out and public["id"] in out
    assert re.search(r"^\s*wake toolsets\s+failed", out, re.M)
    assert all("toolsets" not in r for r in _subs().values())


def test_setup_refuses_wake_toolsets_with_herenow_hosting(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--hosting", "herenow"))
    assert rc == 1
    assert "refusing to set wake_toolsets" in out and "here.now" in out
    assert all("toolsets" not in r for r in _subs().values())


def test_setup_with_wake_toolsets_opens_only_the_subscriptions_file(monkeypatch, tmp_path, capsys):
    """The one sanctioned exception to "setup never touches Hermes state files": the subscriptions file, and only when
    the user set wake_toolsets. Never .env, auth.json or config.yaml."""
    forbidden = {".env", "auth.json", "config.yaml"}
    opened = []
    original_open, original_io_open = builtins.open, io.open

    def check(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(path)).name
            opened.append(name)
            if name in forbidden:
                raise AssertionError("forbidden Hermes state read")

    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: (check(p), original_open(p, *a, **k))[1])
    monkeypatch.setattr(io, "open", lambda p, *a, **k: (check(p), original_io_open(p, *a, **k))[1])
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, _, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    assert "webhook_subscriptions.json" in opened and not forbidden & set(opened)


def test_hermes_reads_the_toolsets_setup_wrote(monkeypatch, tmp_path, capsys):
    """Read the written file with Hermes's own WebhookAdapter.toolsets_for_source (keyed on user_id webhook:<route>)."""
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
    assert run_setup(monkeypatch, shim, capsys)[0] == 0
    adapter = WebhookAdapter.__new__(WebhookAdapter)
    adapter._routes = _subs()
    assert adapter.toolsets_for_source(SimpleNamespace(user_id=f"webhook:{WAKE}")) == ["terminal", "file"]
    assert adapter.toolsets_for_source(SimpleNamespace(user_id=f"webhook:{NOTICE}")) is None


# ---------------------------------------------------------------- the here.now guard on create / add-round

def test_create_refuses_a_herenow_board_while_wake_toolsets_is_set(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    with pytest.raises(ReviewError) as e:
        api.create("Public", ORIGIN, mode="herenow", decisions=str(_decisions_file(tmp_path)))
    assert e.value.status == 409 and "wake_toolsets" in e.value.message
    assert store.list_ids() == []                                # nothing was created


def test_herenow_guard_allows_wake_off_and_other_hosts(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    api.check_wake_toolsets("herenow", wake=False)
    api.check_wake_toolsets("tailscale", wake=True)
    api.check_wake_toolsets("local", wake=True)
    monkeypatch.delenv("MOCKREVIEW_WAKE_TOOLSETS")
    api.check_wake_toolsets("herenow", wake=True)


def test_add_round_refuses_a_herenow_review_while_wake_toolsets_is_set(env, monkeypatch, tmp_path):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    public = store.create_review("Public", "", ORIGIN, "herenow", spec)
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    with pytest.raises(ReviewError, match="wake_toolsets"):
        api.add_round(public["id"], decisions=str(_decisions_file(tmp_path)))
    assert len(store.load(public["id"])["rounds"]) == 1


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
