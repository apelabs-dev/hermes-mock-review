"""Windows defaults, simulated on any OS (monkeypatched sys.platform / env)."""

import sys
from pathlib import Path

from mockreview import owner, settings


def test_hermes_home_windows_default_is_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert settings.hermes_home() == tmp_path / "Local" / "hermes"
    assert owner.default_owner_root() == tmp_path / "Local" / "hermes" / "workspace" / "mock-reviews"
    monkeypatch.setenv("LOCALAPPDATA", "  ")                     # unset/blank: the usual place under the profile
    assert settings.hermes_home() == Path.home() / "AppData" / "Local" / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "explicit"))  # HERMES_HOME still wins
    assert settings.hermes_home() == tmp_path / "explicit"
    assert owner.default_owner_root() == tmp_path / "explicit" / "workspace" / "mock-reviews"


def test_hermes_home_posix_default(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert settings.hermes_home() == Path.home() / ".hermes"
    monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", "-dev")          # same suffix rule as Hermes
    assert settings.hermes_home() == Path.home() / ".hermes-dev"


def test_owner_error_names_no_service_manager(monkeypatch, tmp_path):
    """The 'no server' refusal tells the user what to run, on any OS (no launchd label)."""
    monkeypatch.setenv("MOCKREVIEW_OWNER", "auto")
    monkeypatch.setenv("MOCKREVIEW_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("MOCKREVIEW_LOCAL_BASE", "http://127.0.0.1:9")    # nothing listens on the discard port
    try:
        owner.resolve()
    except owner.OwnerError as e:
        assert "hermes mockreview serve" in e.message and "autostart" in e.message
        assert "launchd" not in e.message and "com." not in e.message
    else:
        raise AssertionError("resolve() should refuse a private store with no server")
