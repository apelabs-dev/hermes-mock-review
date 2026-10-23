"""The `hermes mockreview setup` wizard: exact argv, redaction, idempotency, exit codes, tripwire."""

import builtins
import io
import json
import re
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ROOT
from mockreview import api, cli, setup
from mockreview.owner import OwnerError

SHIM_SRC = '''
import json, os, sys
args = sys.argv[1:]
log = os.environ["MOCKREVIEW_SHIM_LOG"]
state = os.environ.get("MOCKREVIEW_SHIM_STATE", "")
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\\n")

def load():
    try:
        with open(state, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save(s):
    if state:
        tmp = state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f)
        os.replace(tmp, state)

st = load()
scenario = json.loads(os.environ.get("MOCKREVIEW_SHIM_SCENARIO", "{}"))
version = scenario.get("version", "Hermes Agent v0.22.0 (2026.9.24) · stable · local deadbeef")
envpath = os.environ.get("MOCKREVIEW_SHIM_ENVPATH", "<env path>")
configpath = os.environ.get("MOCKREVIEW_SHIM_CONFIGPATH", "<config path>")

def out(s):
    print(s)

def has_webhook():
    return bool(st.get("webhook_enabled") or scenario.get("webhook_enabled"))

cmd = args[0] if args else ""

if cmd == "--version":
    out(version)
elif cmd == "config":
    sub = args[1] if len(args) > 1 else ""
    key = args[2] if len(args) > 2 else ""
    if sub == "set" and key == "MOCKREVIEW_WEBHOOK_SECRET":
        st["secret"] = args[3]
        save(st)
        out("✓ Set MOCKREVIEW_WEBHOOK_SECRET in " + envpath)
    elif sub == "set" and key == "platforms.webhook.enabled":
        st["webhook_enabled"] = True
        save(st)
        out(configpath)
    elif sub == "set" and key.startswith("plugins.entries.mock-review.settings."):
        k = key.rsplit(".", 1)[1]
        st.setdefault("settings", {})[k] = args[3]
        save(st)
        out(configpath)
    elif sub == "get" and key == "MOCKREVIEW_WEBHOOK_SECRET":
        if st.get("secret"):
            out("supe...7890")
        else:
            out("Config key not set: MOCKREVIEW_WEBHOOK_SECRET")
            sys.exit(1)
    elif sub == "get" and key == "--raw" and args[3] == "MOCKREVIEW_WEBHOOK_SECRET":
        if st.get("secret"):
            out(st["secret"])
        else:
            sys.exit(1)
    elif sub == "get" and key == "--json":
        target = args[3] if len(args) > 3 else ""
        if target == "platforms.webhook.enabled":
            if has_webhook():
                out("true")
            else:
                out("Config key not set: platforms.webhook.enabled")
                sys.exit(1)
        elif target == "platforms":
            out(json.dumps(scenario.get("platforms", {"telegram": {"enabled": True}})))
        elif target.startswith("plugins.entries.mock-review.settings."):
            k = target.rsplit(".", 1)[1]
            obsolete = scenario.get("obsolete", {})
            if k in obsolete:
                out(json.dumps(obsolete[k]))
            else:
                out("Config key not set: " + target)
                sys.exit(1)
        else:
            out("Config key not set: " + target)
            sys.exit(1)
    elif sub == "unset":
        out("✓ Unset " + args[2] + " from " + configpath)
    else:
        out("")
elif cmd == "webhook" and args[1] == "subscribe":
    secret = ""
    for i, a in enumerate(args):
        if a == "--secret" and i + 1 < len(args):
            secret = args[i + 1]
    name = args[2]
    if scenario.get("fail_notice") and name.endswith("-notice"):
        out("error: cannot write the route")
        sys.exit(1)
    if has_webhook():
        out("Updated webhook subscription: " + name)
        out("Secret: " + secret)
    else:
        out("the webhook platform is disabled; enable it first (hermes config set platforms.webhook.enabled true)")
elif cmd == "webhook" and args[1] == "list":
    out(scenario.get("webhook_list", ""))
elif cmd == "webhook" and args[1] == "remove":
    out("Removed webhook subscription: " + args[2])
elif cmd == "gateway" and args[1] == "status":
    out(scenario.get("gateway_status", ""))
    sys.exit(scenario.get("gateway_rc", 0))
elif cmd == "plugins" and args[1] == "validate":
    if scenario.get("validate_fail"):
        out("Validation failed.")
        sys.exit(1)
    out("Validation passed.")
else:
    out("")
'''


_SHIM_N = 0


def make_shim(tmp_path, scenario=None):
    global _SHIM_N
    _SHIM_N += 1
    base = tmp_path / f"hermes-{_SHIM_N}"
    shim = base
    shim.write_text(SHIM_SRC, encoding="utf-8")
    log = base.with_suffix(".log")
    state = base.with_suffix(".state")
    env = {
        "PYTHONIOENCODING": "utf-8",
        "MOCKREVIEW_SHIM_LOG": str(log),
        "MOCKREVIEW_SHIM_STATE": str(state),
        "MOCKREVIEW_SHIM_SCENARIO": json.dumps(scenario or {}),
        "MOCKREVIEW_SHIM_ENVPATH": str(base.with_name("home") / ".env"),
        "MOCKREVIEW_SHIM_CONFIGPATH": str(base.with_name("home") / "config.yaml"),
    }
    return SimpleNamespace(shim=shim, log=log, state=state, env=env)


def read_log(shim):
    if not shim.log.exists():
        return []
    return [json.loads(line) for line in shim.log.read_text(encoding="utf-8").splitlines() if line.strip()]


def shim_secret(shim):
    if not shim.state.exists():
        return None
    return json.loads(shim.state.read_text(encoding="utf-8")).get("secret")


def fake_create(monkeypatch, result=None, exc=None):
    calls = {}

    def create(**kwargs):
        calls.update(kwargs)
        if exc is not None:
            raise exc
        return result or {"review_id": "review-setuptest-000000",
                          "url": "http://127.0.0.1:8790/r/review-setuptest-000000"}

    monkeypatch.setattr(api, "create", create)
    return calls


def run_setup(monkeypatch, shim, capsys, args=("--yes",)):
    for k, v in shim.env.items():
        monkeypatch.setenv(k, v)
    # Python doubles run through this interpreter on every OS; no executable shebang association is required. The real
    # run_hermes stays in the loop, so its timeout and cannot-start handling is what the tests exercise.
    monkeypatch.setattr(setup, "_hermes_cmd", lambda argv: [sys.executable, str(shim.shim), *argv])
    rc = cli.main(["setup", *args])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def find(log, *prefix):
    return [entry for entry in log if list(entry[:len(prefix)]) == list(prefix)]


class FakeTTY:
    """A terminal stdin that hands back canned answers (one per line) and then EOF."""

    def __init__(self, lines):
        self._lines = list(lines)

    def isatty(self):
        return True

    def readline(self, *a):
        return (self._lines.pop(0) + "\n") if self._lines else ""


# ---------------------------------------------------------------- core runs

def test_full_yes_run_records_exact_argv(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    log = read_log(shim)

    writes = find(log, "config", "set", "MOCKREVIEW_WEBHOOK_SECRET")
    assert len(writes) == 1 and len(writes[0]) == 4 and writes[0][3]

    subs = find(log, "webhook", "subscribe")
    assert len(subs) == 2
    wake, notice = subs[0], subs[1]
    name, secret = wake[2], dict(zip(wake, wake[1:])).get("--secret")
    assert name == "mock-review-telegram"
    assert secret == writes[0][3]
    assert "--deliver" in wake and wake[wake.index("--deliver") + 1] == "telegram"
    assert "--deliver-chat-id" in wake and wake[wake.index("--deliver-chat-id") + 1] == "{chat_id}"
    assert "--deliver-only" not in wake and "--mirror-to-session" not in wake

    # the notice route: same name suffix, same secret, deliver-only and mirrored into the session
    secret_value = writes[0][3]
    assert wake == ["webhook", "subscribe", "mock-review-telegram", "--secret", secret_value,
                    "--prompt", setup.PROMPT_TEMPLATE, "--description", setup.ROUTE_DESCRIPTION,
                    "--deliver", "telegram", "--deliver-chat-id", "{chat_id}"]
    assert notice == ["webhook", "subscribe", "mock-review-telegram-notice", "--secret", secret_value,
                      "--prompt", "{message}", "--description", setup.NOTICE_ROUTE_DESCRIPTION,
                      "--deliver", "telegram", "--deliver-chat-id", "{chat_id}",
                      "--deliver-only", "--mirror-to-session"]
    assert re.search(r"^\s*routes\s+ok\s+\(1 route\(s\)\)", out, re.M)
    assert re.search(r"^\s*notice routes\s+ok\s+\(1 route\(s\)\)", out, re.M)

    assert find(log, "config", "set", "plugins.entries.mock-review.settings.autostart", "true")
    assert find(log, "plugins", "validate")


def test_secret_never_printed_but_shim_output_contained_it(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    rc, out, err = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    secret = shim_secret(shim)
    assert secret and len(secret) >= 40
    assert secret not in out and secret not in err
    assert "***" in out                        # the subscribe `Secret: …` line was redacted
    assert secret in json.dumps(read_log(shim))  # the shim saw (and logged) the real value


def test_rerun_is_idempotent(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    run_setup(monkeypatch, shim, capsys)
    secret = shim_secret(shim)
    shim.log.write_text("", encoding="utf-8")                    # keep only the second run's argv

    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    log = read_log(shim)
    assert not find(log, "config", "set", "MOCKREVIEW_WEBHOOK_SECRET")   # no re-write, no rotation
    assert find(log, "config", "get", "--raw", "MOCKREVIEW_WEBHOOK_SECRET")  # reused via --raw
    subs = find(log, "webhook", "subscribe")
    assert subs and dict(zip(subs[0], subs[0][1:])).get("--secret") == secret
    assert "reusing" in out


def test_non_interactive_without_yes_returns_2_zero_argv(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path)
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=())
    assert rc == 2
    assert setup.NON_TTY_USAGE in out
    assert read_log(shim) == []


def test_hermes_too_old_returns_2(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"version": "Hermes Agent v0.21.0 (2026.1.1) · stable · local deadbeef"})
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 2
    assert "0.22.0" in out
    log = read_log(shim)
    assert log == [["--version"]]              # only the version probe; nothing changed


def test_webhook_disabled_yes_runs_enable_then_subscribes(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": False})
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    assert setup.WEBHOOK_ENABLE_CMD in out
    log = read_log(shim)
    enable = find(log, "config", "set", "platforms.webhook.enabled", "true")
    subs = find(log, "webhook", "subscribe")
    assert enable and subs
    assert log.index(enable[0]) < log.index(subs[0])   # enable happened before subscribing


def test_rerun_resubscribes_the_same_route_names_and_keeps_the_secret(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    assert run_setup(monkeypatch, shim, capsys)[0] == 0
    assert run_setup(monkeypatch, shim, capsys)[0] == 0
    log = read_log(shim)
    names = [c[2] for c in find(log, "webhook", "subscribe")]
    assert names == ["mock-review-telegram", "mock-review-telegram-notice"] * 2   # same names: updated, not duplicated
    assert len(find(log, "config", "set", "MOCKREVIEW_WEBHOOK_SECRET")) == 1       # the secret is reused


def test_a_failing_notice_route_fails_setup_and_is_reported(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True, "fail_notice": True})
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 1
    assert re.search(r"^\s*routes\s+ok\s+\(1 route\(s\)\)", out, re.M)
    assert re.search(r"^\s*notice routes\s+failed\s+\(1 of 1 route\(s\)\)", out, re.M)


# ---------------------------------------------------------------- hosting

def test_tailnet_base_parses_valid_and_rejects_bad():
    valid = {"BackendState": "Running", "Self": {"DNSName": "box.example.ts.net."}}
    assert setup.tailnet_base(runner=lambda: SimpleNamespace(returncode=0, stdout=json.dumps(valid), stderr="")) \
        == ("https://box.example.ts.net", "")
    assert setup.tailnet_base(runner=lambda: SimpleNamespace(returncode=1, stdout="", stderr="boom"))[0] is None
    assert setup.tailnet_base(runner=lambda: SimpleNamespace(returncode=0, stdout="not json", stderr=""))[0] is None
    assert setup.tailnet_base(runner=lambda: SimpleNamespace(returncode=0, stdout=json.dumps(
        {"BackendState": "Starting", "Self": {"DNSName": "box.example.ts.net."}}), stderr=""))[0] is None


def test_hosting_tailnet_sets_public_base_and_falls_back(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    monkeypatch.setattr(setup, "tailnet_base", lambda: ("https://box.example.ts.net", ""))
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--hosting", "tailnet"))
    assert rc == 0
    assert find(read_log(shim), "config", "set", "plugins.entries.mock-review.settings.public_base",
                "https://box.example.ts.net")
    assert "tailscale serve --bg 8790" in out

    shim2 = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    monkeypatch.setattr(setup, "tailnet_base", lambda: (None, "tailscale is not installed"))
    rc, out, _ = run_setup(monkeypatch, shim2, capsys, args=("--yes", "--hosting", "tailnet"))
    assert rc == 0
    assert not find(read_log(shim2), "config", "set", "plugins.entries.mock-review.settings.public_base")
    assert "tailnet unavailable" in out and "local" in out


def test_hosting_lan_warns_then_writes_host(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--hosting", "lan"))
    assert rc == 0
    assert setup.LAN_WARNING in out
    assert out.index(setup.LAN_WARNING) < out.index("will change: set host to 0.0.0.0")
    assert find(read_log(shim), "config", "set", "plugins.entries.mock-review.settings.host", "0.0.0.0")


def test_hosting_herenow_prints_install_never_runs(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--hosting", "herenow"))
    assert rc == 0
    assert setup.HERENOW_INSTALL_CMD in out and "HERENOW_API_KEY" in out
    assert not any("skills" in entry or "install" in entry for entry in read_log(shim))


# ---------------------------------------------------------------- migration + validate + test review

def test_migration_unsets_obsolete_and_names_them(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True, "obsolete": {
        "herenow_credentials": "x", "subscriptions_file": "/tmp/y", "herenow_publish_script": "z"}})
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    log = read_log(shim)
    unsets = find(log, "config", "unset")
    assert {u[2] for u in unsets} == {f"plugins.entries.mock-review.settings.{k}" for k in
                                      ("herenow_credentials", "subscriptions_file", "herenow_publish_script")}
    for key in ("herenow_credentials", "subscriptions_file", "herenow_publish_script"):
        assert key in out

    shim2 = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim2, capsys)
    assert rc == 0 and not find(read_log(shim2), "config", "unset")


def test_validate_failure_marks_failed_exit_1(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True, "validate_fail": True})
    fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 1
    assert "Validation failed" in out
    assert "validate" in out and "failed" in out


def test_test_review_records_kwargs(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    calls = fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    assert calls["wake"] is False and calls["origin"] is None
    assert calls["title"] == setup.TEST_TITLE and calls["mode"] == "local"
    assert Path(calls["decisions"]).is_absolute()
    assert "review-setuptest-000000" in out


def test_test_review_owner_error_is_pending_not_traceback(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch, exc=OwnerError("the board server is not running"))
    rc, out, err = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    assert "pending" in out and "next:" in out
    assert "Traceback" not in out and "Traceback" not in err


def test_run_path_never_opens_hermes_state_files(monkeypatch, tmp_path, capsys):
    forbidden_names = {".env", "auth.json", "webhook_subscriptions.json", "config.yaml"}
    original_open, original_io_open, original_read = builtins.open, io.open, Path.read_text
    blocked = []

    def check(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            p = Path(os.fsdecode(path))
            if p.name in forbidden_names:
                blocked.append(p.name)
                raise AssertionError("forbidden Hermes state read")

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
    for path in controls:
        with pytest.raises(AssertionError, match="forbidden"):
            path.read_text(encoding="utf-8")
    assert len(blocked) == len(controls)
    blocked.clear()

    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    rc, _, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    assert not blocked


def test_parse_version_and_redact():
    assert setup.parse_version("Hermes Agent v0.22.0 (2026.9.24) · stable · local abc") == (0, 22, 0)
    assert setup.parse_version("garbage") is None
    assert setup.parse_version("") is None
    assert setup.redact("Secret: abc123", "abc123") == "Secret: ***"
    assert setup.redact("no secret here", "abc123") == "no secret here"
    assert setup.redact("plain", "") == "plain"
    assert setup.redact(None, "x") == ""


# ---------------------------------------------------------------- addendum gaps

def test_python_ok_boundary():
    assert setup.python_ok((3, 9, 0)) is True
    assert setup.python_ok((3, 9)) is True
    assert setup.python_ok((4, 0, 0)) is True
    assert setup.python_ok((3, 8, 10)) is False
    assert setup.python_ok((2, 7, 18)) is False


def test_interactive_tailnet_recommendation_accepted_writes_public_base(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    monkeypatch.setattr(setup, "tailnet_base", lambda: ("https://box.example.ts.net", ""))
    monkeypatch.setattr(sys, "stdin", FakeTTY(["y"]))
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--origin-platform", "telegram"))
    assert rc == 0
    assert "Tailscale is up on box.example.ts.net" in out
    assert find(read_log(shim), "config", "set", "plugins.entries.mock-review.settings.public_base",
                "https://box.example.ts.net")
    assert "tailscale serve --bg 8790" in out


def test_yes_never_changes_hosting_to_tailnet_unattended(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    monkeypatch.setattr(setup, "tailnet_base", lambda: ("https://box.example.ts.net", ""))
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--origin-platform", "telegram"))
    assert rc == 0
    assert not find(read_log(shim), "config", "set", "plugins.entries.mock-review.settings.public_base")
    assert "hosting: local" in out


def test_unusable_tailnet_falls_back_with_reason(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    monkeypatch.setattr(setup, "tailnet_base", lambda: (None, "tailscale is not installed"))
    monkeypatch.setattr(sys, "stdin", FakeTTY([]))
    rc, out, _ = run_setup(monkeypatch, shim, capsys, args=("--origin-platform", "telegram"))
    assert rc == 0
    assert "tailnet unavailable (tailscale is not installed)" in out
    assert not find(read_log(shim), "config", "set", "plugins.entries.mock-review.settings.public_base")


def test_unreadable_secret_says_it_will_be_replaced(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    monkeypatch.setattr(setup, "existing_secret", lambda: (None, "secret is set but not readable"))
    rc, out, _ = run_setup(monkeypatch, shim, capsys)
    assert rc == 0
    assert "secret is set but not readable" in out
    assert out.index("secret is set but not readable") < out.index("generate and store a new webhook secret")
    assert find(read_log(shim), "config", "set", "MOCKREVIEW_WEBHOOK_SECRET")


def test_dry_run_prints_the_plan_and_changes_nothing(monkeypatch, tmp_path, capsys):
    """--dry-run: every step is announced, and no `hermes` call can mutate state."""
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    calls = fake_create(monkeypatch)
    rc, out, _ = run_setup(monkeypatch, shim, capsys,
                           args=("--yes", "--dry-run", "--origin-platform", "telegram"))
    assert rc == 0
    log = read_log(shim)
    assert not find(log, "config", "set")
    assert not find(log, "config", "unset")
    assert not find(log, "webhook", "subscribe")
    assert calls == {}                                   # the test review is announced, never created
    for step in ("generate and store a new webhook secret",
                 "subscribe webhook route mock-review-telegram (deliver telegram)",
                 "subscribe webhook route mock-review-telegram-notice (deliver telegram, notice)",
                 "enable autostart", "validate the plugin", "create a test review"):
        assert f"will change: {step}" in out, step
    assert not shim.state.exists() or not json.loads(shim.state.read_text(encoding="utf-8")).get("secret")
    assert re.search(r"^\s*routes\s+planned\s+\(1 route\(s\)\)", out, re.M)
    assert re.search(r"^\s*notice routes\s+planned\s+\(1 route\(s\)\)", out, re.M)


def test_explicit_port_is_saved_and_default_port_is_not(monkeypatch, tmp_path, capsys):
    shim = make_shim(tmp_path, {"webhook_enabled": True})
    fake_create(monkeypatch)
    rc, _, _ = run_setup(monkeypatch, shim, capsys, args=("--yes", "--port", "8797"))
    assert rc == 0
    assert find(read_log(shim), "config", "set", "plugins.entries.mock-review.settings.port", "8797")

    (tmp_path / "second").mkdir()
    shim2 = make_shim(tmp_path / "second", {"webhook_enabled": True})
    rc, _, _ = run_setup(monkeypatch, shim2, capsys)
    assert rc == 0
    assert not find(read_log(shim2), "config", "set", "plugins.entries.mock-review.settings.port")
