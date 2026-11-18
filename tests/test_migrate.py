"""Migration: legacy reviews become local or tailnet, dry-run writes nothing, and a rerun is a no-op."""

import json

from mockreview import cli, config, migrate, store
from test_store import _decisions


def _review(directory, mode=migrate.LEGACY_MODE):
    spec = store.parse_round_spec(decisions=str(_decisions(directory)))
    data = store.create_review("Legacy board", "", {"platform": "discord", "chat_id": "test-chat"},
                               "local", spec, wake=False)
    with store.locked(data["id"]):
        record = store.load(data["id"])
        record["mode"] = mode
        record[migrate.LEGACY_KEY] = {"slug": "legacy-board-ab12", "site_url": "https://legacy-board-ab12.example.test/",
                                      "processed_records": []}
        store.save(data["id"], record)
    return data["id"]


def _record_bytes(review_id):
    return (store.review_dir(review_id) / "review.json").read_bytes()


def _run(capsys, *args):
    assert cli.main(["migrate", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_dry_run_reports_one_change_without_writing_then_real_run_then_idempotent(env, tmp_path, capsys):
    rid = _review(tmp_path)
    before = _record_bytes(rid)
    dry = _run(capsys, "--dry-run")
    assert dry["dry_run"] is True and dry["target"] == "local" and dry["changed"] == 1
    assert dry["data_root"] == str(config.data_root().resolve())
    assert dry["changes"] == [{"review_id": rid, "from": migrate.LEGACY_MODE, "to": "local",
                               "url": f"{config.local_link_base()}/r/{rid}"}]
    assert _record_bytes(rid) == before                       # dry run writes nothing

    real = _run(capsys)
    assert real["dry_run"] is False and real["changed"] == 1 and real["changes"] == dry["changes"]
    record = store.load(rid)
    assert record["mode"] == "local" and migrate.LEGACY_KEY not in record
    after = _record_bytes(rid)
    again = _run(capsys)
    assert again["changed"] == 0 and again["changes"] == []
    assert _record_bytes(rid) == after                        # a second run writes nothing


def test_mode_tailnet_stores_tailscale(env, tmp_path, capsys):
    rid = _review(tmp_path)
    out = _run(capsys, "--mode", "tailnet")
    assert out["target"] == "tailscale" and out["changed"] == 1
    assert out["changes"][0]["to"] == "tailscale"
    assert store.load(rid)["mode"] == "tailscale" and migrate.LEGACY_KEY not in store.load(rid)


def test_target_derives_from_default_host(env, tmp_path, capsys, monkeypatch):
    rid = _review(tmp_path)
    monkeypatch.setenv("MOCKREVIEW_DEFAULT_HOST", "tailnet")
    out = _run(capsys)
    assert out["target"] == "tailscale" and out["changed"] == 1
    assert store.load(rid)["mode"] == "tailscale"


def test_a_stale_sub_record_is_removed_even_when_the_mode_is_another(env, tmp_path, capsys):
    rid = _review(tmp_path, mode="local")
    out = _run(capsys)
    assert out["changed"] == 1 and out["changes"][0]["from"] == "local"
    assert out["changes"][0]["to"] == "local"
    record = store.load(rid)
    assert record["mode"] == "local" and migrate.LEGACY_KEY not in record


def test_a_tailscale_review_with_a_stale_sub_record_keeps_tailscale(env, tmp_path, capsys):
    for name, args in (("one", ()), ("two", ("--mode", "local"))):
        (tmp_path / name).mkdir()
        rid = _review(tmp_path / name, mode="tailscale")
        out = _run(capsys, *args)
        assert out["target"] == "local" and out["changed"] == 1
        assert out["changes"] == [{"review_id": rid, "from": "tailscale", "to": "tailscale",
                                   "url": f"{config.public_link_base()}/r/{rid}"}]
        assert out["changes"][0]["url"] != f"{config.local_link_base()}/r/{rid}"
        record = store.load(rid)
        assert record["mode"] == "tailscale" and migrate.LEGACY_KEY not in record


def test_migrate_reports_invalid_json_in_dry_run_and_real_run(env, tmp_path, capsys):
    rid = _review(tmp_path)
    path = store.review_dir(rid) / "review.json"
    path.write_text('{"mode": "%s", "rounds": [' % migrate.LEGACY_MODE, encoding="utf-8")
    before = path.read_bytes()
    for args in (("--dry-run",), ()):
        assert cli.main(["migrate", *args]) == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.err)
        assert captured.out == ""
        assert payload["ok"] is False
        assert rid in payload["error"] and "cannot migrate review" in payload["error"]
        assert path.read_bytes() == before


def test_a_corrupt_record_stops_a_real_run_before_any_conversion(env, tmp_path, capsys, monkeypatch):
    for name in ("aaa", "bbb"):
        (tmp_path / name).mkdir()
    aaa = _review(tmp_path / "aaa")
    bbb = _review(tmp_path / "bbb")
    path = store.review_dir(bbb) / "review.json"
    path.write_text('{"mode": "%s", invalid' % migrate.LEGACY_MODE, encoding="utf-8")
    before = _record_bytes(aaa)
    monkeypatch.setattr(store, "list_ids", lambda: [aaa, bbb])   # the valid record is always read first
    assert cli.main(["migrate"]) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False and bbb in payload["error"]
    assert _record_bytes(aaa) == before
    assert store.load(aaa)["mode"] == migrate.LEGACY_MODE


def test_migrate_reports_unreadable_review(env, tmp_path, capsys, monkeypatch):
    rid = _review(tmp_path)
    before = _record_bytes(rid)

    def boom(review_id):
        raise PermissionError("permission denied")
    monkeypatch.setattr(store, "load", boom)
    for args in (("--dry-run",), ()):
        assert cli.main(["migrate", *args]) == 1
        payload = json.loads(capsys.readouterr().err)
        assert rid in payload["error"] and "permission denied" in payload["error"]
        assert _record_bytes(rid) == before
