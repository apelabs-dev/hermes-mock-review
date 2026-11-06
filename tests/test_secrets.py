"""secrets.get_secret: the Hermes scope inside Hermes, the process environment when there is no scope."""

import importlib
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


# ---------------------------------------------------------------- a broken scope is not an absent scope
#
# Absence is decided by the module spec. An ImportError raised inside agent.secret_scope, or a scope that no longer
# exports get_secret, is a failure: the environment must stay unread there, or a broken install under multiplexing
# would resolve whatever the starting process had in its environment. It fails closed (returns "") instead of
# propagating, because a half-installed Hermes must not take unrelated calls like mock_review_list down with it.

def _fake_scope_package(tmp_path, monkeypatch, source, init_source=""):
    package = tmp_path / "agent"
    package.mkdir()
    (package / "__init__.py").write_text(init_source, encoding="utf-8")
    (package / "secret_scope.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)


def test_a_scope_that_fails_to_import_never_reads_the_environment(tmp_path, monkeypatch, caplog):
    restore_real_accessor(monkeypatch)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "synthetic-env-wake-secret")
    secrets._build_failures.discard(("MOCKREVIEW_WEBHOOK_SECRET", "ImportError"))
    _fake_scope_package(tmp_path, monkeypatch, "raise ImportError('broken install')\n")
    with caplog.at_level(logging.WARNING, logger="mockreview.secrets"):
        assert secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET") == ""
    assert "synthetic-env-wake-secret" not in caplog.text


def test_a_scope_without_get_secret_never_reads_the_environment(tmp_path, monkeypatch):
    restore_real_accessor(monkeypatch)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "synthetic-env-wake-secret")
    _fake_scope_package(tmp_path, monkeypatch, "OTHER = 1\n")
    assert secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET") == ""


def test_a_parent_package_that_fails_to_import_never_reads_the_environment(tmp_path, monkeypatch):
    # find_spec("agent.secret_scope") imports the parent first, so a half-installed Hermes used to look *absent* here
    # and read the environment. The package's presence is decided by the top-level spec, which never runs __init__.py.
    restore_real_accessor(monkeypatch)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "synthetic-env-wake-secret")
    _fake_scope_package(tmp_path, monkeypatch, 'def get_secret(name): return "scoped"\n',
                        init_source="raise ImportError('half-installed')\n")
    assert secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET") == ""


def test_a_parent_package_that_raises_something_else_never_reads_the_environment(tmp_path, monkeypatch):
    # A parent raising anything other than ImportError used to escape as a raw exception, which the docstring said
    # could not happen. It fails closed now, like every other broken-Hermes case.
    restore_real_accessor(monkeypatch)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "synthetic-env-wake-secret")
    _fake_scope_package(tmp_path, monkeypatch, 'def get_secret(name): return "scoped"\n',
                        init_source="raise RuntimeError('boom parent')\n")
    assert secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET") == ""


def test_a_scope_that_holds_nothing_never_reads_the_environment(monkeypatch):
    # The other half of "profile isolation unchanged": the scope exists and answers empty, so the environment must
    # stay unread. No exception here: a profile with no value simply has no value.
    restore_real_accessor(monkeypatch)
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: default
    package = types.ModuleType("agent")
    package.__path__ = []
    package.secret_scope = scope
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    monkeypatch.setenv("MOCKREVIEW_WEBHOOK_SECRET", "synthetic-env-wake-secret")
    assert secrets.get_secret("MOCKREVIEW_WEBHOOK_SECRET") == ""
