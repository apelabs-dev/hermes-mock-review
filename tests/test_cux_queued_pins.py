"""A wake that waits behind another Hermes turn says so, on desktop and coarse-touch phone boards."""

import json

import pytest

from mockreview import service
from test_cux_send_to_hermes import on
from test_cux_working_pins import (  # noqa: F401
    CHIP, COUNT, STATUS, WORKING, WorkingBoard, exists, has_text, mobile, query, working_review,
)
from test_live_round_tool import chrome  # noqa: F401
from test_mention_wake import call_tool, plugin_tools, post

pytestmark = pytest.mark.browser

ROW = ".tpanel .trow"


def mention_at(server, rid, idem, anchor):
    status, _, body = post(server, rid, "@Hermes Clarify the subtotal", idem=idem, anchor=anchor)
    assert status == 201, body
    return json.loads(body)["thread"]["id"]


def open_requests(server, rid, count):
    ids = [mention_at(server, rid, f"queued-{i}", {"block": "m1", "x": 0.15 + i * 0.2, "y": 0.3})
           for i in range(count)]
    service.retry_thread_wakes(rid)
    return ids


def pin(tid):
    return f'.mpin[data-thread="{tid}"]'


def label(b, tid):
    return b.js(f"{query(pin(tid))}.getAttribute('aria-label')")


def board_with(chrome, server, review, mobile, ids):
    b = WorkingBoard(chrome, server, review, mobile)
    on(b, b.width)
    b.shows(f"document.querySelectorAll({json.dumps(WORKING)}).length >= {len(ids)}",
            "the open requests never showed as working pins")
    return b


def test_younger_request_queues_behind_the_older_one(working_review, server, chrome, mobile):
    review = working_review()
    rid = review["id"]
    older, younger = open_requests(server, rid, 2)
    b = board_with(chrome, server, review, mobile, (older, younger))
    b.shows(has_text(COUNT, "Hermes · 2 working"), "the header count changed its wording")
    assert "Hermes is working on this" in label(b, older)
    assert "Queued behind 1" in label(b, younger)
    assert "Queued behind" not in label(b, older)
    assert b.js(f"document.querySelectorAll({json.dumps(CHIP)}).length") == 1
    assert b.js(has_text(CHIP, "Hermes is working…")), "the block's oldest request lost the working chip"
    b.click_sel(COUNT)
    b.shows(f"document.querySelectorAll({json.dumps(ROW)}).length === 2", "the working list is incomplete")
    rows = b.js(f"JSON.stringify([...document.querySelectorAll({json.dumps(ROW + ' .badge.ac')})]"
                ".map(e => e.textContent).sort())")
    assert json.loads(rows) == ["Queued behind 1", "Working"]
    assert b.js("document.documentElement.scrollWidth === innerWidth")
    assert b.js(f"[...document.querySelectorAll({json.dumps(ROW)})].every(r => r.scrollWidth <= r.clientWidth + 1)"), \
        "a thread row overflows"
    b.screenshot(mobile, "queued")
    b.js("document.querySelectorAll('.tpanel .trow').forEach(r => { if (r.textContent.includes('Queued')) r.click(); })")
    b.shows(has_text(STATUS, "Queued behind 1"), "the queued thread's status did not say so")
    assert b.js(f'{query(STATUS)}.querySelector("[role=status]").textContent === "Queued behind 1"')


def test_the_next_request_runs_when_the_older_one_completes(working_review, server, chrome, mobile):
    review = working_review()
    rid = review["id"]
    older, middle, youngest = open_requests(server, rid, 3)
    b = board_with(chrome, server, review, mobile, (older, middle, youngest))
    assert "Queued behind 1" in label(b, middle)
    assert "Queued behind 2" in label(b, youngest)
    result = call_tool(plugin_tools(), "mock_review_resolve", {"thread_id": older})
    assert result.get("closed") == 1, result
    b.shows(f'{query(pin(middle))}.getAttribute("aria-label").includes("Hermes is working on this")',
            "the next request did not start working when the older one finished")
    assert "Queued behind 1" in label(b, youngest)
    assert b.js(has_text(COUNT, "Hermes · 2 working"))
    assert b.js(f'{query(pin(middle))}.dataset.hermesState === "working"')
    assert b.js(has_text(CHIP, "Hermes is working…"))


def test_a_single_request_is_unchanged(working_review, server, chrome, mobile):
    review = working_review()
    rid = review["id"]
    (only,) = open_requests(server, rid, 1)
    b = board_with(chrome, server, review, mobile, (only,))
    assert "Hermes is working on this" in label(b, only)
    assert b.js(has_text(CHIP, "Hermes is working…"))
    assert b.js(has_text(COUNT, "Hermes · 1 working"))
    b.click_sel(COUNT)
    b.shows(has_text(ROW, "Working"), "the working badge changed")
    assert not b.js(has_text(".tpanel", "Queued"))
