"""The first-run sign-in note: a real, minted one-time link on create/add_round, and the setup line that offers it.

Every test here fails on 44d89b3: the note did not exist, `next_steps` had no sign-in line, and the docs named a
hosting mode that does not exist. The signal behind "already signed in" is a stored `login_link.use` audit row
(users.owner_signed_in_elsewhere), so the "absent after" cases redeem a real link through users.redeem.
"""

import json
import re
from pathlib import Path

import pytest

from mockreview import api, config, setup, users

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def public_url(monkeypatch):
    """A synthetic public URL: config.public_link_base() prefers it, so no real host or tailscale call gets in."""
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_URL", "https://board.example.test")


def decisions(tmp_path, name="decisions.json", items=("one",)):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"blocks": [{"type": "recap", "id": "recap", "items": list(items)}]}),
                    encoding="utf-8")
    return str(path)


def note_url(res):
    """The one-time link a result's note carries."""
    note = res.get("note") or ""
    assert "Sign in once per device" in note, note
    found = re.search(r"(https://\S+)$", note)
    assert found, note
    return found.group(1)


def token_of(url):
    return url.rsplit("/", 1)[1]


def test_create_carries_a_one_time_sign_in_link(env, monkeypatch, tmp_path, public_url):
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "tailnet")
    res = api.create("Sign-in note", ORIGIN, decisions=decisions(tmp_path))
    assert res["mode"] == "tailscale"
    url = note_url(res)
    assert url.startswith("https://board.example.test/login/")
    link = users.check_link("login", token_of(url))
    assert link["purpose"] == "login" and link["expires_at"] > users.now()


def test_the_note_is_gone_after_a_sign_in_from_another_device(env, monkeypatch, tmp_path, public_url):
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "tailnet")
    first = api.create("Before sign-in", ORIGIN, decisions=decisions(tmp_path))
    assert not users.owner_signed_in_elsewhere()
    users.redeem("login", token_of(note_url(first)))      # the phone: the stored signal is the audit row it writes
    assert users.owner_signed_in_elsewhere()
    second = api.create("After sign-in", ORIGIN, decisions=decisions(tmp_path, name="second.json"))
    assert "Sign in once per device" not in (second.get("note") or "")


def test_add_round_carries_the_note_and_loses_it_after_a_sign_in(env, monkeypatch, tmp_path, public_url):
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "tailnet")
    res = api.create("Round note", ORIGIN, decisions=decisions(tmp_path))
    url = note_url(res)
    again = api.add_round(res["review_id"], decisions=decisions(tmp_path, name="round-2.json"), allow_reask=True)
    assert note_url(again).startswith("https://board.example.test/login/")
    users.redeem("login", token_of(url))
    third = api.add_round(res["review_id"], decisions=decisions(tmp_path, name="round-3.json"), allow_reask=True)
    assert "Sign in once per device" not in (third.get("note") or "")


def test_the_note_is_only_for_boards_reachable_off_this_computer(env, monkeypatch, tmp_path, public_url):
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "local")
    local = api.create("Local board", ORIGIN, decisions=decisions(tmp_path, name="local.json"))
    assert "Sign in once per device" not in (local.get("note") or "")
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "tailnet")
    remote = api.create("Tailnet board", ORIGIN, decisions=decisions(tmp_path, name="remote.json"))
    assert note_url(remote).startswith("https://board.example.test/login/")


@pytest.mark.parametrize("mode, stored", [("tailnet", "tailscale"), ("public", "public")])
def test_an_explicit_mode_carries_the_note_without_default_host(env, monkeypatch, tmp_path, public_url, mode, stored):
    monkeypatch.delenv("MOCKREVIEW_DEFAULT_HOST", raising=False)
    assert not users.owner_signed_in_elsewhere()
    res = api.create("Explicit mode", ORIGIN, mode=mode, decisions=decisions(tmp_path, name=f"{mode}.json"))
    assert res["mode"] == stored
    assert note_url(res).startswith("https://board.example.test/login/")


def test_setup_next_steps_offer_the_same_link(env, monkeypatch, public_url):
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "tailnet")
    sign_in = setup._sign_in_link("tailnet")
    assert sign_in.startswith("https://board.example.test/login/")
    lines = setup.next_steps(board_running=True, autostart=True, gateway="running", restart_for=[], windows=False,
                             sign_in=sign_in)
    text = "\n".join(lines)
    assert f"sign in from your phone or laptop: {sign_in}" in text
    assert "for 15 minutes" in text and "one link per device" in text


def test_setup_offers_no_link_for_a_local_board(env, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "local")
    assert setup._sign_in_link("local") == ""


def test_the_skill_hands_over_the_login_link():
    text = (ROOT / "skills" / "usage" / "SKILL.md").read_text(encoding="utf-8")
    section = text.split("## First-run sign-in", 1)[1].split("\n## ", 1)[0]
    assert "hermes mockreview login" in section
    assert "sign-in page (HTTP 401)" in section
    assert "LAN" not in section


def test_the_migration_doc_covers_signing_in_on_another_device():
    text = (ROOT / "docs" / "migrating-to-2.md").read_text(encoding="utf-8")
    section = text.split("## Signing in from a phone or laptop", 1)[1]
    assert "sign-in page (HTTP 401)" in section
    assert "mockreview login" in section
    assert "15 minutes" in section
    assert "LAN" not in section


def test_a_public_board_without_a_public_base_gets_no_note(env, monkeypatch, tmp_path):
    """A loopback link in a public note cannot work from another device, so no public base means no note at all."""
    monkeypatch.delenv("MOCKREVIEW_PUBLIC_URL", raising=False)
    monkeypatch.delenv("MOCKREVIEW_PUBLIC_BASE", raising=False)

    def no_cli(args, timeout=5):
        raise OSError("no tailscale CLI")

    monkeypatch.setattr(config, "run_tailscale", no_cli)
    res = api.create("Public without a base", ORIGIN, mode="public", decisions=decisions(tmp_path))
    assert res["mode"] == "public"
    note = res.get("note") or ""
    assert "Sign in once per device" not in note, note
    assert "127.0.0.1" not in note and "localhost" not in note, note
    assert api.sign_in_note("public") is None
