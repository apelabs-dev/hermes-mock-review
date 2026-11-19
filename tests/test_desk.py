"""The Mocks tab of Hermes Desktop: the plugin_api routes (list, read-only viewer, delete), the one delete path
(api.delete_review) and its CLI, and the package files the Desktop loads."""

import base64
import importlib.util
import json
import re
import time

import pytest

from conftest import ROOT, http
from mockreview import api, cli, config, db, store, threads, users
from mockreview.store import ReviewError

ORIGIN = {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}
DAY = 86400


def _decisions(tmp_path, name, mock_html="<p>mock body</p>"):
    src = tmp_path / f"round-{name}"
    src.mkdir(parents=True, exist_ok=True)
    (src / "decisions.json").write_text(json.dumps({"intro": f"Intro of {name}", "blocks": [
        {"type": "text", "title": f"Prose {name}", "text": "Frames the round."},
        {"type": "mock", "id": "m1", "title": "Today", "html": mock_html}]}), encoding="utf-8")
    return str(src / "decisions.json")


def make(tmp_path, title, **kw):
    spec = store.parse_round_spec(decisions=_decisions(tmp_path, re.sub(r"\W+", "-", title), kw.pop("mock", "<p>mock</p>")))
    return store.create_review(title, "brief", ORIGIN, "tailscale", spec, wake=False, **kw)


def age(rid, days, **fields):
    """Move a review's creation, rounds and (optionally) other timestamps `days` into the past."""
    with store.locked(rid):
        d = store.load(rid)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * DAY))
        d["created_at"] = stamp
        for r in d["rounds"]:
            r["created_at"] = stamp
        d.update(fields)
        store.save(rid, d)
        path = store.review_dir(rid) / "review.json"
        j = json.loads(path.read_text(encoding="utf-8"))
        j["updated_at"] = stamp
        path.write_text(json.dumps(j), encoding="utf-8")


def load_plugin_api():
    pytest.importorskip("fastapi")
    spec = importlib.util.spec_from_file_location("_mockreview_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decode(data_url):
    head, _, payload = data_url.partition(",")
    assert head == "data:text/html;charset=utf-8;base64"
    return base64.b64decode(payload).decode("utf-8")


@pytest.fixture(autouse=True)
def no_tailscale(monkeypatch):
    """Board addresses must not depend on the tailscale CLI of the machine the tests run on."""
    def missing(args, timeout=5):
        raise FileNotFoundError("tailscale")
    monkeypatch.setattr(config, "run_tailscale", missing)


# ---------------------------------------------------------------- the list

def test_list_includes_closed_and_older_reviews_newest_activity_first(env, tmp_path):
    plugin = load_plugin_api()
    oldest = make(tmp_path, "Oldest board")
    closed = make(tmp_path, "Closed board")
    newest = make(tmp_path, "Newest board")
    legacy = make(tmp_path, "Legacy board")
    api.close(closed["id"])
    age(oldest["id"], 40)
    age(closed["id"], 10, status="closed")
    age(newest["id"], 0)
    # A 2.0.0 board: written before updated_at, closed_at and the origin's thread existed.
    age(legacy["id"], 20)
    path = store.review_dir(legacy["id"]) / "review.json"
    j = json.loads(path.read_text(encoding="utf-8"))
    for key in ("updated_at", "closed_at", "wake", "no_design"):
        j.pop(key, None)
    j["origin"].pop("thread_id", None)
    path.write_text(json.dumps(j), encoding="utf-8")

    rows = plugin.reviews()["reviews"]
    assert [r["title"] for r in rows] == ["Newest board", "Closed board", "Legacy board", "Oldest board"]
    by = {r["title"]: r for r in rows}
    assert by["Closed board"]["status"] == "closed" and by["Newest board"]["status"] == "open"
    assert all(r["round"] == 1 and r["last_activity"] and r["board_url"] for r in rows)
    assert by["Legacy board"]["review_id"] == legacy["id"]


def test_list_is_empty_without_reviews(env):
    assert load_plugin_api().reviews() == {"reviews": []}


def test_board_url_prefers_the_configured_host_and_falls_back_to_loopback(env, tmp_path, monkeypatch):
    review = make(tmp_path, "Where")
    assert api.desk_list()[0]["board_url"] == f"https://board.example.test:8790/r/{review['id']}"
    monkeypatch.delenv("MOCKREVIEW_PUBLIC_BASE")
    row = api.desk_list()[0]
    assert row["board_url"] == f"{config.local_base()}/r/{review['id']}"
    assert row["board_url"].startswith("http://127.0.0.1:")


def test_listing_resolves_the_public_base_once(env, tmp_path, monkeypatch):
    for name in ("One", "Two", "Three"):
        make(tmp_path, name)
    calls = []

    def counted(args, timeout=5):
        calls.append(args)
        raise FileNotFoundError("tailscale")
    monkeypatch.setattr(config, "run_tailscale", counted)
    assert len(api.desk_list()) == 3
    assert len(calls) == 1


# ---------------------------------------------------------------- the viewer

def test_viewer_payload_is_sanitised(env, tmp_path):
    plugin = load_plugin_api()
    review = make(tmp_path, "Hostile", mock=('<div class="x">Visible</div><script>window.__pwned = 1</script>'
                                              '<img src="https://evil.example.test/x.png" onerror="window.__e = 1">'))
    payload = plugin.view(review["id"])
    assert payload["title"] == "Hostile" and payload["round"] == 1 and payload["status"] == "open"
    assert payload["too_large"] is False and payload["board_url"].endswith(f"/r/{review['id']}")
    html = decode(payload["data_url"])
    assert "Visible" in html and "Prose Hostile" in html
    assert "<script" not in html.lower() and "__pwned" not in html
    assert "onerror" not in html.lower() and "evil.example.test" not in html
    assert "default-src 'none'" in html


def test_viewer_shows_the_current_round(env, tmp_path):
    review = make(tmp_path, "Two rounds")
    api.add_round(review["id"], decisions=_decisions(tmp_path, "second", "<p>second mock</p>"), no_design=True)
    payload = api.desk_view(review["id"])
    assert payload["round"] == 2 and payload["rounds"] == 2
    assert "Prose second" in decode(payload["data_url"])


def test_viewer_does_not_truncate_a_large_snapshot(env, tmp_path, monkeypatch):
    review = make(tmp_path, "Big")
    monkeypatch.setattr(api, "DESK_DATA_URL_LIMIT", 1000)
    payload = api.desk_view(review["id"])
    assert payload["too_large"] is True and payload["data_url"] is None and payload["bytes"] > 0


def test_viewer_unknown_review_is_a_404(env):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        load_plugin_api().view("nope-0123456789abcdef")
    assert e.value.status_code == 404 and e.value.detail == "review not found"


# ---------------------------------------------------------------- the board is untouched

def test_the_board_still_refuses_to_be_framed(server, env, tmp_path):
    review = make(tmp_path, "Framed")
    code, h, _ = http("GET", f"{server}/r/{review['id']}/")
    assert code == 200
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert h.get("X-Frame-Options", "DENY").upper() == "DENY"


# ---------------------------------------------------------------- delete

def _count(conn, table, rid):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE review_id = ?", (rid,)).fetchone()[0]


def fill(rid, tmp_path):
    """One row in every table that carries this review's id (and the rows that hang off them)."""
    owner = users.owner_user()
    actor = f"user:{owner['id']}"
    thread = threads.create(rid, actor, owner["name"], "owner", "First", anchor={"block": "m1", "x": 0.2, "y": 0.3},
                            mentions=["hermes"])
    threads.reply(rid, thread["id"], actor, owner["name"], "owner", "Reply")
    threads.mark_read(rid, actor, thread["id"])
    users.share("owner", rid, "create", "commenter")
    with db.tx() as conn:
        conn.execute("INSERT INTO guests (id, review_id, generation, display_name, created_at) VALUES (?, ?, 1, 'G', 1)",
                     (f"g-{rid}", rid))
        conn.execute("INSERT INTO sessions (id_hash, guest_id, csrf, created_at, last_seen, expires_at) "
                     "VALUES (?, ?, 'c', 1, 1, ?)", (f"s-{rid}", f"g-{rid}", time.time() + 3600))
        conn.execute("INSERT INTO revoked_sessions (id_hash, review_id, revoked_at) VALUES (?, ?, 1)", (f"r-{rid}", rid))
        conn.execute("INSERT INTO invites (nonce_hash, role, review_id, review_role, created_by, created_at, expires_at) "
                     "VALUES (?, 'member', ?, 'viewer', 'test', 1, ?)", (f"n-{rid}", rid, time.time() + 3600))
        conn.execute("INSERT INTO grants (review_id, all_reviews, user_id, role, granted_by, created_at) "
                     "VALUES (?, 0, ?, 'viewer', 'test', 1)", (rid, owner["id"]))


def snapshot(rid):
    conn = db.connect()
    snap = {t: _count(conn, t, rid) for t in users.REVIEW_TABLES}
    snap["comments"] = conn.execute("SELECT COUNT(*) FROM comments WHERE thread_id IN "
                                    "(SELECT id FROM threads WHERE review_id = ?)", (rid,)).fetchone()[0]
    snap["mentions"] = conn.execute("SELECT COUNT(*) FROM mentions WHERE comment_id IN (SELECT c.id FROM comments c "
                                    "JOIN threads t ON t.id = c.thread_id WHERE t.review_id = ?)", (rid,)).fetchone()[0]
    snap["guest_sessions"] = conn.execute("SELECT COUNT(*) FROM sessions s JOIN guests g ON g.id = s.guest_id "
                                          "WHERE g.review_id = ?", (rid,)).fetchone()[0]
    return snap


def test_delete_removes_the_directory_and_every_row_and_leaves_another_review_alone(env, tmp_path):
    gone, kept = make(tmp_path, "Gone"), make(tmp_path, "Kept")
    for r in (gone, kept):
        fill(r["id"], tmp_path)
    before_gone, before_kept = snapshot(gone["id"]), snapshot(kept["id"])
    assert all(v > 0 for v in before_gone.values()), before_gone     # every table really had a row to lose
    kept_files = sorted(p.name for p in store.review_dir(kept["id"]).rglob("*"))

    res = api.delete_review(gone["id"])

    assert res["deleted"] is True and res["title"] == "Gone" and res["was_open"] is True
    assert not store.review_dir(gone["id"]).exists() and gone["id"] not in store.list_ids()
    assert set(snapshot(gone["id"]).values()) == {0}
    assert snapshot(kept["id"]) == before_kept
    assert sorted(p.name for p in store.review_dir(kept["id"]).rglob("*")) == kept_files
    assert store.load(kept["id"])["status"] == "open"
    # The audit log is history, not review state: it records the delete and keeps the rest.
    assert any(a["action"] == "review.delete" and a["target"] == f"review:{gone['id']}" for a in users.audit_rows())


def test_delete_closes_an_open_review_first(env, tmp_path, monkeypatch):
    review = make(tmp_path, "Open one")
    order = []
    real_close, real_purge = api._close, users.purge_review
    monkeypatch.setattr(api, "_close", lambda rid: order.append(("close", store.load(rid)["status"])) or real_close(rid))
    monkeypatch.setattr(users, "purge_review", lambda rid, *a: order.append(("purge", store.load(rid)["status"]))
                        or real_purge(rid, *a))
    api.delete_review(review["id"])
    assert order == [("close", "open"), ("purge", "closed")]


def test_delete_of_a_closed_review_does_not_close_again(env, tmp_path, monkeypatch):
    review = make(tmp_path, "Closed one")
    api.close(review["id"])
    monkeypatch.setattr(api, "_close", lambda rid: pytest.fail("a closed review is not closed again"))
    assert api.delete_review(review["id"])["was_open"] is False


def test_delete_unknown_id_is_a_clean_error(env):
    with pytest.raises(ReviewError) as e:
        api.delete_review("nope-0123456789abcdef")
    assert e.value.status == 404 and e.value.message == "review not found"
    with pytest.raises(ReviewError):
        api.delete_review("../escape")


def test_every_table_with_a_review_id_is_purged(env):
    """A new table keyed by review_id must be added to users.REVIEW_TABLES (or hang off one of them)."""
    conn = db.connect()
    keyed = set()
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
        if "review_id" in [c[1] for c in conn.execute(f"PRAGMA table_info({name})")]:
            keyed.add(name)
    assert keyed == set(users.REVIEW_TABLES)


def test_housekeeping_prune_goes_through_delete_review(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_KEEP_CLOSED_DAYS", "30")
    old, recent = make(tmp_path, "Old closed"), make(tmp_path, "Recent closed")
    for r in (old, recent):
        fill(r["id"], tmp_path)
        api.close(r["id"])
    with store.locked(old["id"]):
        d = store.load(old["id"])
        d["closed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 31 * DAY))
        store.save(old["id"], d)
    calls = []
    real = api.delete_review
    monkeypatch.setattr(api, "delete_review", lambda rid: calls.append(rid) or real(rid))
    res = api.housekeeping()
    assert calls == [old["id"]] and res["pruned"] == [old["id"]] and not res["errors"]
    assert not store.review_dir(old["id"]).exists() and set(snapshot(old["id"]).values()) == {0}
    assert store.exists(recent["id"]) and all(v > 0 for v in snapshot(recent["id"]).values())


def test_plugin_api_delete_route(env, tmp_path):
    from fastapi import HTTPException
    plugin = load_plugin_api()
    review = make(tmp_path, "Via route")
    assert plugin.delete(review["id"])["deleted"] is True
    assert plugin.reviews() == {"reviews": []}
    with pytest.raises(HTTPException) as e:
        plugin.delete(review["id"])
    assert e.value.status_code == 404


def test_cli_delete_refuses_without_yes(env, tmp_path, capsys):
    review = make(tmp_path, "Cli keep")
    assert cli.main(["delete", review["id"]]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["ok"] is False and "--yes" in err["error"]
    assert store.exists(review["id"])


def test_cli_delete_with_yes_and_unknown_id(env, tmp_path, capsys):
    review = make(tmp_path, "Cli gone")
    assert cli.main(["delete", review["id"], "--yes"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"] is True and out["review_id"] == review["id"]
    assert not store.exists(review["id"])
    assert cli.main(["delete", "nope-0123456789abcdef", "--yes"]) == 1
    assert json.loads(capsys.readouterr().err) == {"ok": False, "error": "review not found"}


# ---------------------------------------------------------------- the package the Desktop loads

def test_dashboard_manifest_names_the_router_file():
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {"name": "mock-review", "api": "plugin_api.py"}
    assert hasattr(load_plugin_api(), "router")


def test_importing_plugin_api_starts_nothing(env):
    import threading
    before = threading.active_count()
    load_plugin_api()
    assert threading.active_count() == before


def test_only_the_router_file_imports_fastapi():
    for p in (ROOT / "mockreview").rglob("*.py"):
        assert not re.search(r"^\s*(import|from)\s+fastapi", p.read_text(encoding="utf-8"), re.M), p


def test_desktop_plugin_imports_only_what_the_loader_resolves():
    src = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    specs = set(re.findall(r"""from\s+['"]([^'"]+)['"]""", src)) | set(re.findall(r"""import\(\s*['"]([^'"]+)['"]""", src))
    assert specs == {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    assert "<div" not in src and "</" not in src.replace("//", "")          # no JSX: the file loads uncompiled
    assert "id: 'mock-review'" in src and "ROUTES_AREA" in src and "SIDEBAR_NAV_AREA" in src
    assert "label: 'Mocks'" in src


def test_the_board_surface_files_are_not_the_desktops_business():
    """The Mocks tab reads through plugin_api and never asks the board for an embed, a token or a header."""
    src = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "frame-ancestors" not in src and "embed" not in src.lower()
