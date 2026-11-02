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


def test_setup_refuses_wake_toolsets_while_an_unpassworded_herenow_review_is_open(env, monkeypatch, tmp_path, capsys):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    public = store.create_review("Public", "", ORIGIN, "herenow", spec, password=False)
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 1
    assert "refusing to set wake_toolsets" in out and public["id"] in out
    assert re.search(r"^\s*wake toolsets\s+failed", out, re.M)
    assert '"toolsets":' not in out                                # no line to paste while a board is ungated
    assert _no_toolsets_on_disk()


def test_setup_prints_the_line_with_a_passworded_herenow_review_open(env, monkeypatch, tmp_path, capsys):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    store.create_review("Gated", "", ORIGIN, "herenow", spec, password=True)
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0, out
    assert "refusing to set wake_toolsets" not in out
    assert _line(["terminal"]) in out and _no_toolsets_on_disk()


def test_setup_refuses_wake_toolsets_with_lan_hosting(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--hosting", "lan"))
    assert rc == 1
    assert "refusing to set wake_toolsets" in out and "lan hosting" in out
    assert '"toolsets":' not in out and _no_toolsets_on_disk()


def test_setup_refuses_wake_toolsets_with_herenow_hosting_and_password_off(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOCKREVIEW_HERENOW_PASSWORD", "false")
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    shim = _shim(tmp_path)
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--hosting", "herenow"))
    assert rc == 1
    assert "refusing to set wake_toolsets" in out and "herenow_password off" in out
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


# ---------------------------------------------------------------- the here.now guard on create / add-round

def test_create_refuses_an_unpassworded_herenow_board_while_wake_toolsets_is_set(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    with pytest.raises(ReviewError) as e:
        api.create("Public", ORIGIN, mode="herenow", decisions=str(_decisions_file(tmp_path)), password=False)
    assert e.value.status == 409 and "wake_toolsets" in e.value.message and "no password" in e.value.message
    assert store.list_ids() == []                                # nothing was created


def test_herenow_guard_allows_a_password_wake_off_and_loopback_hosts(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    api.check_wake_toolsets("herenow", wake=True, password=True)
    api.check_wake_toolsets("herenow", wake=False)
    api.check_wake_toolsets("tailscale", wake=True)
    api.check_wake_toolsets("local", wake=True)
    with pytest.raises(ReviewError, match="no password"):
        api.check_wake_toolsets("herenow", wake=True, password=False)
    monkeypatch.delenv("MOCKREVIEW_WAKE_TOOLSETS")
    api.check_wake_toolsets("herenow", wake=True, password=False)


def test_guard_refuses_every_waking_board_while_the_server_listens_beyond_loopback(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    monkeypatch.setenv("MOCKREVIEW_HOST", "0.0.0.0")
    for mode in ("local", "tailscale", "herenow"):
        with pytest.raises(ReviewError, match="beyond loopback"):
            api.check_wake_toolsets(mode, wake=True, password=True)
    api.check_wake_toolsets("local", wake=False)


def test_add_round_refuses_an_unpassworded_herenow_review_while_wake_toolsets_is_set(env, monkeypatch, tmp_path):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    public = store.create_review("Public", "", ORIGIN, "herenow", spec, password=False)
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    with pytest.raises(ReviewError, match="wake_toolsets"):
        api.add_round(public["id"], decisions=str(_decisions_file(tmp_path)))
    assert len(store.load(public["id"])["rounds"]) == 1


def test_password_off_is_refused_on_a_waking_herenow_board_while_wake_toolsets_is_set(env, monkeypatch, tmp_path):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    gated = store.create_review("Gated", "", ORIGIN, "herenow", spec, password=True)
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    with pytest.raises(ReviewError) as e:
        api.password(gated["id"], False)
    assert e.value.status == 409 and "no password" in e.value.message
    assert store.load(gated["id"])["herenow"]["password_enabled"] is True     # untouched


def _gated_submission(tmp_path, applied):
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    data = store.create_review("Gated", "", ORIGIN, "herenow", spec, password=True)
    with store.locked(data["id"]):
        d = store.load(data["id"])
        d["herenow"].update(slug="gated-site-ab12", password_status="applied" if applied else "error",
                            password_applied=True if applied else None)
        store.save(data["id"], d)
    return data


def test_wake_is_held_until_here_now_confirms_the_password(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    data = _gated_submission(tmp_path, applied=False)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    assert env["receiver"].wakes == []                                   # not woken
    sub = store.load(data["id"])["rounds"][0]["submission"]
    assert sub["status"] == "pending_notify" and "held, not woken" in sub["last_error"]


def test_wake_goes_out_for_a_confirmed_password_board(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    data = _gated_submission(tmp_path, applied=True)
    service.submit(data["id"], 1, DEC_FB, data["token"])
    assert len(env["receiver"].wakes) == 1
    assert store.load(data["id"])["rounds"][0]["submission"]["status"] == "notified"


def test_wake_is_held_for_any_board_once_the_server_listens_beyond_loopback(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
    spec = store.parse_round_spec(decisions=str(_decisions_file(tmp_path)))
    data = store.create_review("Tailnet", "", ORIGIN, "tailscale", spec)
    monkeypatch.setenv("MOCKREVIEW_HOST", "0.0.0.0")
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
