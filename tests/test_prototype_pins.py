"""Pins on a prototype round: one number per pin, and a working marker while Hermes answers.

Two bugs met on a prototype board that carried a thread from an earlier round:
- the frame numbered a pin you had just placed by its place in the draft list while a carried thread kept the number the
  server gave it, so two different pins both read "1";
- a delivered @Hermes mention opened no activity request on a prototype round (it needs a block with HTML to patch,
  and a prototype has none), so the pin never showed the working dot.
"""
import json
import time

import pytest

from conftest import http
from mockreview import service, store
from test_board_frames_threads import frame_session
from test_cux_send_to_hermes import on
from test_cux_working_pins import working_review  # noqa: F401  (fixture: wake sends are stubbed)
from test_live_round_tool import PAGE, Board, chrome  # noqa: F401
from test_mention_wake import pin_requests, post

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
PROTO_ANCHOR = {"block": "proto", "w": "desktop", "x": 0.25, "y": 0.5}


def prototype_review(tmp_path, **look):
    page = tmp_path / "proto" / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(PAGE, encoding="utf-8")
    spec = store.parse_round_spec(prototype=str(page))
    look = {"pin_fix": True, "pin_fix_min_seconds": 0, "pin_fix_window_seconds": 0, **look}
    return store.create_review("Prototype", "", ORIGIN, "local", spec, wake=True, look=look), spec


def thread_id(body):
    return json.loads(body)["thread"]["id"]


def test_a_mention_on_a_prototype_pin_opens_a_request_and_a_reply_closes_it(working_review, env, server, tmp_path):
    review, _ = prototype_review(tmp_path)
    rid = review["id"]
    status, _, body = post(server, rid, "@Hermes make it bigger", anchor=PROTO_ANCHOR)
    assert status == 201, body
    tid = thread_id(body)
    service.retry_thread_wakes(rid)
    requests = list(pin_requests(rid).values())
    assert len(requests) == 1, "a delivered mention on a prototype pin opened no request, so nothing shows Hermes working"
    q = requests[0]
    assert (q["block"], q["source"], q["thread_id"], q["status"]) == ("proto", "mention", tid, "woken")
    shown = store.public_state(store.load(rid))["rounds"][-1]["pin_requests"]
    assert [r["status"] for r in shown.values()] == ["working"]
    assert service.close_mention_requests(rid, tid) == 1
    assert [r["status"] for r in pin_requests(rid).values()] == ["done"]


def test_a_plain_comment_on_a_prototype_pin_opens_no_request(working_review, env, server, tmp_path):
    review, _ = prototype_review(tmp_path)
    rid = review["id"]
    status, _, body = post(server, rid, "just a note", mentions=(), anchor=PROTO_ANCHOR)
    assert status == 201, body
    service.retry_thread_wakes(rid)
    assert pin_requests(rid) == {}


def test_a_thread_payload_carries_the_number_the_listing_gives_it(working_review, env, server, tmp_path):
    review, spec = prototype_review(tmp_path)
    rid = review["id"]
    status, _, body = post(server, rid, "round one note", mentions=(), anchor=PROTO_ANCHOR)
    assert status == 201, body
    store.add_round(rid, spec)
    status, _, body = post(server, rid, "round two note", mentions=(), anchor=PROTO_ANCHOR, idem="c2")
    assert status == 201, body
    created = json.loads(body)["thread"]
    status, _, body = post(server, rid, "a reply", mentions=(), thread_id=created["id"], idem="c3")
    assert status == 200, body
    replied = json.loads(body)["thread"]
    listing = {t["id"]: t["n"] for t in json.loads(http("GET", f"{server}/r/{rid}/api/threads?round=2")[2])["threads"]}
    assert sorted(listing.values()) == [1, 2]
    assert (created["n"], replied["n"]) == (listing[created["id"]], listing[created["id"]]) == (2, 2), \
        "a lone thread payload said it was thread 1, so the board redrew its pin with the wrong number"


def pins_in_frame(b, sid):
    """[(label, class)] of the pin markers the probe draws in the frame (its overlay is a closed shadow root)."""
    root = b.cdp.call("DOM.getDocument", {"depth": -1, "pierce": True}, session=sid)["root"]
    out = []

    def text(n):
        return (n.get("nodeValue") or "") + "".join(text(c) for c in n.get("children", []))

    def walk(n):
        attrs = dict(zip(n.get("attributes", [])[::2], n.get("attributes", [])[1::2]))
        if n.get("nodeName") == "BUTTON" and "p" in attrs.get("class", "").split():
            out.append((text(n).strip(), attrs["class"]))
        for c in n.get("children", []) + n.get("shadowRoots", []) + ([n["contentDocument"]] if n.get("contentDocument") else []):
            walk(c)

    walk(root)
    return out


def open_frame_board(chrome, server, rid):
    b = Board(chrome, f"{server}/r/{rid}/", width=1440, height=900, wait="!!document.querySelector('iframe')")
    b.until("window.__ready === 1")
    sid = frame_session(b, rid)
    chrome.call("DOM.enable", session=sid)
    return b, sid


def place_pin(b, text, ux=0.5, uy=0.6):
    rect = json.loads(b.js("JSON.stringify(document.querySelector('iframe').getBoundingClientRect())"))
    on(b)
    b.click(rect["x"] + rect["width"] * ux, rect["y"] + rect["height"] * uy)
    assert b.until("!!document.querySelector('.popover textarea')", timeout=15)
    b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})"
         "(document.querySelector('.popover textarea'))")
    b.cdp.call("Input.insertText", {"text": text}, session=b.sid)
    b.press(".popover .composer-actions .btn-round, .popover button[aria-label='Add pin']")


@pytest.mark.browser
def test_a_new_pin_next_to_a_carried_thread_gets_its_own_number(working_review, env, server, chrome, tmp_path):
    review, spec = prototype_review(tmp_path)
    rid = review["id"]
    status, _, body = post(server, rid, "round one note", mentions=(), anchor=PROTO_ANCHOR)
    assert status == 201, body
    old = thread_id(body)
    assert http("POST", f"{server}/r/{rid}/api/threads/{old}/status", {"status": "resolved"})[0] == 200
    store.add_round(rid, spec)
    b, sid = open_frame_board(chrome, server, rid)
    assert b.until("document.querySelector('.rounds [aria-current],.rounds .on')?.innerText === 'R2'", timeout=15)
    place_pin(b, "round two note")
    deadline = time.monotonic() + 10
    while sorted(label for label, _ in pins_in_frame(b, sid)) != ["1", "2"] and time.monotonic() < deadline:
        b.cdp.pump(0.3)
    pins = pins_in_frame(b, sid)
    listing = json.loads(http("GET", f"{server}/r/{rid}/api/threads?round=2")[2])["threads"]
    assert sorted(t["n"] for t in listing) == [1, 2]
    assert sorted(label for label, _ in pins) == ["1", "2"], f"two different pins share a number: {pins}"


@pytest.mark.browser
def test_a_prototype_pin_shows_the_working_dot_while_hermes_answers(working_review, env, server, chrome, tmp_path):
    review, _ = prototype_review(tmp_path)
    rid = review["id"]
    b, sid = open_frame_board(chrome, server, rid)
    place_pin(b, "@Hermes make it bigger")
    deadline = time.monotonic() + 15
    while not json.loads(http("GET", f"{server}/r/{rid}/api/threads")[2])["threads"] and time.monotonic() < deadline:
        b.cdp.pump(0.3)
    deadline = time.monotonic() + 15
    while {q["status"] for q in pin_requests(rid).values()} != {"woken"} and time.monotonic() < deadline:
        service.retry_thread_wakes(rid)   # the worker pass that delivers the mention once its short window has passed
        b.cdp.pump(0.3)
    assert {q["status"] for q in pin_requests(rid).values()} == {"woken"}
    deadline = time.monotonic() + 15
    while not any("h-working" in cls.split() for _, cls in pins_in_frame(b, sid)) and time.monotonic() < deadline:
        b.cdp.pump(0.3)
    assert any("h-working" in cls.split() for _, cls in pins_in_frame(b, sid)), \
        f"the prototype pin never showed Hermes working: {pins_in_frame(b, sid)}"
