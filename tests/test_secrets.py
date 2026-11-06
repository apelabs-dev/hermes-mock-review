"""secrets.get_secret: the Hermes scope inside Hermes, the process environment when there is no scope."""

import logging
import sys
import types

from mockreview import notify, secrets

REAL_GET_SECRET = secrets.get_secret   # the production accessor, before the suite's autouse stub replaces it


def restore_real_accessor(monkeypatch):
    monkeypatch.setattr(secrets, "get_secret", REAL_GET_SECRET)


def no_agent(monkeypatch):
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
    monkeypatch.setitem(sys.modules, "agent", None)          # import raises ImportError
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)


def test_environment_value_is_used_without_hermes(monkeypatch, caplog):
    restore_real_accessor(monkeypatch)
    no_agent(monkeypatch)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "  synthetic-env-wake-secret  ")
    secrets._logged_env_secrets.discard("MOCKREVIEW_WEBHOOK_SECRET")
    with caplog.at_level(logging.INFO, logger="mockreview.secrets"):
        assert secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET") == "synthetic-env-wake-secret"
    assert "MOCKREVIEW_WEBHOOK_SECRET" in caplog.text
    assert "synthetic-env-wake-secret" not in caplog.text


def test_missing_environment_value_is_empty_without_hermes(monkeypatch):
    restore_real_accessor(monkeypatch)
    no_agent(monkeypatch)
    monkeypatch.delenv("MOCKREVIEW_WEBHOOK_SECRET", raising=False)
    assert secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET") == ""


def test_scoped_value_wins_over_the_environment(monkeypatch):
    restore_real_accessor(monkeypatch)
    scope = types.ModuleType("agent.secret_scope")
    calls = []

    def get_secret(name, default=None):
        calls.append(name)
        return {"MOCKREVIEW_WEBHOOK_SECRET": "synthetic-scoped-wake-secret"}.get(name, default)

    scope.get_secret = get_secret
    package = types.ModuleType("agent")
    package.__path__ = []
    package.secret_scope = scope
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "synthetic-env-wake-secret")
    assert secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET") == "synthetic-scoped-wake-secret"
    assert calls == ["MOCKREVIEW_WEBHOOK_SECRET"]


def test_notify_load_secret_uses_the_environment_without_hermes(monkeypatch):
    restore_real_accessor(monkeypatch)
    no_agent(monkeypatch)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "synthetic-env-wake-secret")
    assert notify.load_secret() == ("synthetic-env-wake-secret", "")
