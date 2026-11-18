"""2.0.0 migration keeps a 1.2.0 review's already-sent pins: plain text stays readable, and no pin becomes a thread."""

import json

import pytest

from authkit import peer  # noqa: F401  (fixture)
from conftest import http
from mockreview import cli, config, migrate, store, threads
from test_live_round_tool import Board, chrome  # noqa: F401  (fixtures)

PIN_TEXT = "Make the total read louder than the line items."

DECISIONS = {"intro": "A round created before 2.0.0.",
             "blocks": [{"type": "mock", "id": "m1", "title": "Checkout",
                         "html": '<section><h2>Checkout summary</h2><p id="copy">Total due today</p></section>'}]}


@pytest.fixture
def legacy_review(env, tmp_path):
    src = tmp_path / "legacy-decisions.json"
    src.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Legacy checkout review", "", {"platform": "discord", "chat_id": "legacy-chat"},
                               "local", store.parse_round_spec(decisions=str(src)), wake=False)
    rid = data["id"]
    with store.locked(rid):
        record = store.load(rid)
        record["mode"] = migrate.LEGACY_MODE
        record[migrate.LEGACY_KEY] = {
            "slug": "legacy-checkout-ab12", "site_url": "https://legacy-checkout-ab12.example.test/",
            "processed_records": [], "last_poll": "2025-11-02T10:00:00Z", "password_enabled": True,
            "password": None, "password_applied": True, "password_status": "applied"}
        record["rounds"][0]["feedback"] = {
            "kind": "decisions", "marks": {},
            "pins": [{"block": "m1", "option": None, "w": "desktop", "x": 0.42, "y": 0.38, "page_h": 760,
                      "text": PIN_TEXT}],
            "page_state": {}, "page_marks": {}, "page_notes": {}, "guidelines": "Keep the checkout one screen."}
        record["rounds"][0]["submission"] = {
            "status": "delivered", "submitted_at": "2025-11-02T10:00:00Z", "notified_at": "2025-11-02T10:00:01Z",
            "record_id": "legacy-record-1", "seq": 1}
        store.save(rid, record)
    feedback_json = config.data_root() / rid / "rounds" / "1" / "feedback.json"
    feedback_json.write_text(json.dumps({"review_id": rid, "round": 1,
                                         "submission": record["rounds"][0]["submission"],
                                         "feedback": record["rounds"][0]["feedback"]}, indent=2), encoding="utf-8")
    return rid, feedback_json


def test_migrate_keeps_a_legacy_plain_text_pin(env, server, legacy_review, peer):
    rid, feedback_json = legacy_review
    before = feedback_json.read_bytes()
    assert cli.main(["migrate"]) == 0
    record = store.load(rid)
    assert record["mode"] == "local" and migrate.LEGACY_KEY not in record
    pin = record["rounds"][0]["feedback"]["pins"][0]
    assert pin["text"] == PIN_TEXT and "thread" not in pin
    assert feedback_json.read_bytes() == before

    peer["addr"] = "127.0.0.1"
    status, _, body = http("GET", f"{server}/r/{rid}/state.json")
    state = json.loads(body)
    assert status == 200
    assert state["rounds"][0]["feedback"]["pins"][0]["text"] == PIN_TEXT
    status, _, body = http("GET", f"{server}/r/{rid}/api/threads")
    assert status == 200 and json.loads(body)["threads"] == []


@pytest.mark.browser
def test_the_migrated_board_shows_the_plain_text_pin(legacy_review, server, chrome):
    rid, _ = legacy_review
    assert cli.main(["migrate"]) == 0
    b = Board(chrome, f"{server}/r/{rid}/",
              wait="!!document.querySelector('#pins-m1 .pin-text')")
    assert b.js("document.querySelector('#pins-m1 .pin-text').textContent") == PIN_TEXT
    assert b.js("String(document.querySelectorAll('.mpin[data-thread]').length)") == "0"
    assert b.js("String(document.querySelectorAll('.mpin').length)") == "1"


def test_old_pins_are_not_threads(env, legacy_review):
    rid, _ = legacy_review
    assert threads.list_threads(rid) == []
