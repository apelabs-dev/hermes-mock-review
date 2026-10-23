import json
import os
import subprocess

import pytest

from conftest import FEEDBACK
from mockreview import config, herenow, service, store


@pytest.fixture
def hn_review(env, variants):
    specs = [store.parse_variant_spec(v) for v in variants]
    data = store.create_review("Public review", "", {"platform": "discord", "chat_id": "1490000000000000002", "thread_id": "1490000000000000002"}, "herenow", specs)
    with store.locked(data["id"]):
        d = store.load(data["id"])
        d["herenow"].update(slug="fake-slug-ab12", site_url="https://fake-slug-ab12.here.now/")
        store.save(data["id"], d)
    return store.load(data["id"])


def rec(rid, data, created="2026-10-05T10:00:00Z"):
    return {"id": rid, "data": data, "createdAt": created, "updatedAt": created}


def payload(fb=FEEDBACK):
    return json.dumps(fb)


def test_poller_ingests_valid_record_and_notifies(hn_review, env):
    tok = hn_review["token"]
    records = [rec("rec_1", {"token": tok, "round": 1, "amend": False, "payload": payload()})]
    out = herenow.poll_review(hn_review["id"], fetch=lambda slug: records, republish=False)
    assert out == [{"record": "rec_1", "result": "accepted", "round": 1, "amended": False}]
    reqs = env["receiver"].requests
    assert [r["path"].endswith("-notice") for r in reqs] == [False, True]   # wake first, then the ack
    assert reqs[0]["ok"] and reqs[1]["ok"]
    p = json.loads(reqs[0]["body"])
    assert p["thread_id"] == "1490000000000000002" and p["review_url"] == "https://fake-slug-ab12.here.now/"
    data = store.load(hn_review["id"])
    assert data["rounds"][0]["submission"]["source"] == "herenow"
    assert data["rounds"][0]["submission"]["record_id"] == "rec_1"
    assert data["rounds"][0]["submission"]["status"] == "notified"
    assert data["rounds"][0]["submission"]["notice"]["status"] == "notified"
    # the same record is never processed twice
    assert herenow.poll_review(hn_review["id"], fetch=lambda slug: records, republish=False) == []
    assert len(reqs) == 2


def test_poller_treats_records_as_untrusted(hn_review, env):
    tok = hn_review["token"]
    evil = json.loads(json.dumps(FEEDBACK))
    evil["guidelines"] = "<img src=x onerror=alert(1)>" + "z" * 10000
    evil["variants"]["b"]["pins"][0]["text"] = "<script>alert(1)</script>"
    evil["admin"] = True
    records = [
        rec("rec_bad_token", {"token": "guess", "round": 1, "payload": payload()}, "2026-10-05T10:00:00Z"),
        rec("rec_no_token", {"round": 1, "payload": payload()}, "2026-10-05T10:00:01Z"),
        rec("rec_bad_json", {"token": tok, "round": 1, "payload": "{not json"}, "2026-10-05T10:00:02Z"),
        rec("rec_bad_round", {"token": tok, "round": "1", "payload": payload()}, "2026-10-05T10:00:03Z"),
        rec("rec_no_data", {}, "2026-10-05T10:00:04Z"),
        rec("rec_ok", {"token": tok, "round": 1, "payload": payload(evil), "extra": "ignored"}, "2026-10-05T10:00:05Z"),
        rec("rec_dupe", {"token": tok, "round": 1, "payload": payload()}, "2026-10-05T10:00:06Z"),
        {"id": None}, "not a dict",
    ]
    out = herenow.poll_review(hn_review["id"], fetch=lambda slug: [r for r in records if isinstance(r, dict)], republish=False)
    res = {o["record"]: o["result"] for o in out}
    assert res == {"rec_bad_token": "rejected", "rec_no_token": "rejected", "rec_bad_json": "rejected",
                   "rec_bad_round": "rejected", "rec_no_data": "rejected", "rec_ok": "accepted", "rec_dupe": "rejected"}
    dupe = [o for o in out if o["record"] == "rec_dupe"][0]
    assert "already sent" in dupe["reason"]
    fb = store.load(hn_review["id"])["rounds"][0]["feedback"]
    assert "admin" not in fb and len(fb["guidelines"]) == 5000
    assert fb["variants"]["b"]["pins"][0]["text"] == "<script>alert(1)</script>"  # stored as text; board uses textContent
    assert len(env["receiver"].requests) == 2


def test_poller_amend_replaces(hn_review, env):
    tok = hn_review["token"]
    a2 = dict(FEEDBACK, guidelines="second thoughts")
    records = [rec("r1", {"token": tok, "round": 1, "payload": payload()}, "2026-10-05T10:00:00Z"),
               rec("r2", {"token": tok, "round": 1, "amend": True, "payload": payload(a2)}, "2026-10-05T10:01:00Z")]
    out = herenow.poll_review(hn_review["id"], fetch=lambda s: records, republish=False)
    assert [o["result"] for o in out] == ["accepted", "accepted"]
    assert store.load(hn_review["id"])["rounds"][0]["feedback"]["guidelines"] == "second thoughts"
    # one wake-up + one ack per round per poll, carrying the latest (amended) submission
    assert len(env["receiver"].requests) == 2
    assert json.loads(env["receiver"].wakes[0]["body"])["amended"] is True


def test_poller_skips_closed_and_tailscale(hn_review, review):
    called = []
    assert herenow.poll_review(review["id"], fetch=lambda s: called.append(s) or []) == []
    with store.locked(hn_review["id"]):
        d = store.load(hn_review["id"])
        d["status"] = "closed"
        store.save(hn_review["id"], d)
    assert herenow.poll_review(hn_review["id"], fetch=lambda s: called.append(s) or []) == []
    assert called == [] and herenow.open_reviews() == []


def test_open_reviews_lists_only_open_herenow(hn_review, review):
    assert herenow.open_reviews() == [hn_review["id"]]


def test_build_site(hn_review, tmp_path):
    service.submit(hn_review["id"], 1, FEEDBACK, hn_review["token"], do_notify=False)
    site = herenow.build_site(store.load(hn_review["id"]), tmp_path / "site")
    names = {p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file()}
    assert all("\\" not in name for name in names)
    assert {"index.html", "board.js", "board.css", "favicon.svg", "state.json", ".herenow/data.json",
            "rounds/1/a/index.html", "rounds/1/a/assets/logo.svg"} <= names
    assert not any("feedback" in n for n in names)
    st = json.loads((site / "state.json").read_text(encoding="utf-8"))
    assert st["submit"] == {"kind": "sitedata", "collection": "feedback", "max_payload": 15000}
    assert "1490000000000000002" not in (site / "state.json").read_text(encoding="utf-8")
    m = json.loads((site / ".herenow/data.json").read_text(encoding="utf-8"))["collections"]["feedback"]
    assert m["access"] == {"read": "owner", "insert": "public", "update": "owner", "delete": "owner"}
    assert set(m["fields"]) == {"token", "round", "amend", "payload"}


@pytest.mark.skipif(os.name == "nt", reason="here.now publish.sh integration requires a POSIX Bash environment")
def test_publish_with_fake_helper(hn_review, tmp_path, monkeypatch):
    script = config.hermes_home() / "skills" / "productivity" / "here-now" / "scripts" / "publish.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n"
                      "[ \"$HERENOW_API_KEY\" = test-public-key ] || exit 4\n"
                      "[ -f \"$1/.herenow/data.json\" ] || exit 3\n"
                      "echo 'publish_result.site_url=https://new-slug-zz99.here.now/' >&2\n"
                      "echo 'publish_result.slug=new-slug-zz99' >&2\n"
                      "echo 'publish_result.auth_mode=authenticated' >&2\n"
                      "echo \"$@\" > " + str(tmp_path / "args") + "\n", encoding="utf-8")
    data = herenow.publish(hn_review["id"])
    assert data["herenow"]["site_url"] == "https://new-slug-zz99.here.now/"
    args = (tmp_path / "args").read_text(encoding="utf-8")
    assert "--slug fake-slug-ab12" in args and "--client hermes" in args


@pytest.mark.skipif(os.name == "nt", reason="here.now publish.sh integration requires a POSIX Bash environment")
def test_publish_failure_is_reported(hn_review, tmp_path, monkeypatch):
    script = config.hermes_home() / "skills" / "productivity" / "here-now" / "scripts" / "publish.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\necho 'error: unauthorized' >&2\nexit 1\n", encoding="utf-8")
    with pytest.raises(herenow.HereNowError, match="unauthorized"):
        herenow.publish(hn_review["id"])


def test_poller_sends_no_ack_while_the_wake_fails(hn_review, env):
    records = [rec("rec_1", {"token": hn_review["token"], "round": 1, "payload": payload()})]
    env["receiver"].status = 500
    herenow.poll_review(hn_review["id"], fetch=lambda slug: records, republish=False)
    assert env["receiver"].notices == [] and len(env["receiver"].wakes) == 1
    env["receiver"].status = 202
    assert service.retry_pending() == 1
    assert [r["path"].endswith("-notice") for r in env["receiver"].requests][-2:] == [False, True]


@pytest.mark.parametrize("success", [True, False])
def test_publish_protocol_without_bash(hn_review, tmp_path, monkeypatch, success):
    """Keep subprocess arguments, result parsing and error reporting covered on every OS."""
    script = tmp_path / "publish.sh"
    monkeypatch.setattr(config, "herenow_publish_script", lambda: script)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        stderr = ("publish_result.slug=new-slug-zz99\n"
                  "publish_result.site_url=https://new-slug-zz99.here.now/\n"
                  "publish_result.auth_mode=authenticated\n") if success else "error: unauthorized\n"
        return subprocess.CompletedProcess(argv, 0 if success else 1, "", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    if success:
        result = herenow.publish(hn_review["id"])
        assert result["herenow"]["site_url"] == "https://new-slug-zz99.here.now/"
    else:
        with pytest.raises(herenow.HereNowError, match="unauthorized"):
            herenow.publish(hn_review["id"])
    argv, kwargs = calls[0]
    assert argv[:3] == ["bash", str(script), str(store.review_dir(hn_review["id"]) / "site")]
    assert argv[-2:] == ["--slug", "fake-slug-ab12"]
    assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"
    assert kwargs["env"]["HERENOW_API_KEY"] == "test-public-key"
