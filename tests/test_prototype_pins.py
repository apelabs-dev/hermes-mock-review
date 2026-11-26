"""Pins on a prototype round: one number per pin, and a working marker while Hermes answers.

Two bugs met on a prototype board that carried a thread from an earlier round:
- the frame numbered a pin you had just placed by its place in the draft list while a carried thread kept the number the
  server gave it, so two different pins both read "1";
- a delivered @Hermes mention opened no activity request on a prototype round (it needs a block with HTML to patch,
  and a prototype has none), so the pin never showed the working dot.
"""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import http
from mockreview import api, live, service, store
from test_board_frames_threads import frame_session
from test_cux_send_to_hermes import on
from test_cux_working_pins import working_review  # noqa: F401  (fixture: wake sends are stubbed)
from test_live_round_tool import PAGE, Board, chrome  # noqa: F401
from test_mention_wake import pin_requests, post

BOARD_JS = Path(__file__).resolve().parent.parent / "mockreview" / "board" / "board.js"
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


def test_a_prototype_request_does_not_send_the_agent_to_patch_a_block(working_review, env, server, tmp_path):
    review, _ = prototype_review(tmp_path)
    rid = review["id"]
    status, _, body = post(server, rid, "@Hermes make it bigger", anchor=PROTO_ANCHOR)
    assert status == 201, body
    service.retry_thread_wakes(rid)
    assert [q["block"] for q in pin_requests(rid).values()] == ["proto"]
    step = api.get_feedback(rid).get("next_step") or ""
    assert "patch" not in step and "do not add a round" not in step, \
        f"patch_block answers 404 on a prototype page, so this step cannot be followed: {step!r}"


def test_a_round_restricted_stream_drops_the_global_thread_number():
    frame = {"thread": {"id": "t1", "n": 4, "round": 2}, "actor": "a"}
    pinned = SimpleNamespace(allow_round=lambda r: True)
    shaped = live.Registry._shape(pinned, "thread.created", frame)
    assert shaped == {"thread": {"id": "t1", "round": 2}, "actor": "a"}
    assert frame["thread"]["n"] == 4, "the shared frame is also sent to other subscribers and must not change"
    full = SimpleNamespace(allow_round=None)
    assert live.Registry._shape(full, "thread.created", frame) is frame
    assert live.Registry._shape(pinned, "presence", {"thread": {"n": 1}}) == {"thread": {"n": 1}}


def pin_numbers(draft_pins, threads, pin):
    """Run board.js's own pinNo/nextPinNo lines in node: the label a pin wears while the board holds this state."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    src = BOARD_JS.read_text(encoding="utf-8")
    parts = [re.search(r"  function pinNo\(p, i\) \{[^\n]*\n", src), re.search(r"  function nextPinNo\(\) \{.*?\n  \}\n", src, re.S)]
    assert all(parts), "pinNo or nextPinNo moved; update this extraction"
    js = ("var S = {draft: {pins: %s}}; var N = %s;\nvar T = {threads: function () { return Object.keys(N).map(function (k) { return {id: k}; }); },"
          " number: function (id) { return N[id] || 0; }};\n%s\nconsole.log(JSON.stringify(pinNo(%s, 0)));"
          % (json.dumps(draft_pins), json.dumps(threads), "".join(p.group(0) for p in parts), json.dumps(pin)))
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_saving_pin_is_not_counted_in_its_own_number():
    saving = {"thread": "tmp:1"}
    assert pin_numbers([saving], {}, saving) == 1, "the first pin on an empty board read 2 while it saved"
    saved = [{"thread": "t1"}, {"thread": "t2"}]
    assert pin_numbers(saved + [saving], {"t1": 1, "t2": 2}, saving) == 3
    assert pin_numbers([saving], {"carried": 1}, saving) == 2, "a carried thread still takes the first number"


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


def wait_labels(b, sid, want):
    """The pin labels once they have read `want` on two reads in a row (a saving pin and its saved thread overlap for a frame)."""
    deadline = time.monotonic() + 12
    stable = 0
    while True:
        got = sorted(label for label, _ in pins_in_frame(b, sid))
        stable = stable + 1 if got == want else 0
        if stable >= 2 or time.monotonic() > deadline:
            return got
        b.cdp.pump(0.4)


def open_frame_board(chrome, server, rid):
    b = Board(chrome, f"{server}/r/{rid}/", width=1440, height=900, wait="!!document.querySelector('iframe')")
    b.until("window.__ready === 1")
    sid = frame_session(b, rid)
    chrome.call("DOM.enable", session=sid)
    return b, sid


def place_pin(b, text, ux=0.5, uy=0.6):
    rect = json.loads(b.js("JSON.stringify(document.querySelector('iframe').getBoundingClientRect())"))
    if b.js("document.querySelector('#comment-toggle,#comment-toggle-mobile').getAttribute('aria-pressed')") != 'true':
        on(b)   # the toggle stays on after a pin, so a second pin must not press it off again
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
    wait_labels(b, sid, ["1", "2"])
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


@pytest.mark.browser
def test_pins_are_renumbered_after_a_thread_is_deleted(working_review, env, server, chrome, tmp_path):
    review, _ = prototype_review(tmp_path)
    rid = review["id"]
    b, sid = open_frame_board(chrome, server, rid)

    place_pin(b, "first note", ux=0.3)
    assert wait_labels(b, sid, ["1"]) == ["1"]
    place_pin(b, "second note", ux=0.5)
    assert wait_labels(b, sid, ["1", "2"]) == ["1", "2"]
    threads = json.loads(http("GET", f"{server}/r/{rid}/api/threads")[2])["threads"]
    first = next(t["id"] for t in threads if t["n"] == 1)
    assert http("POST", f"{server}/r/{rid}/api/threads/{first}/delete", {})[0] == 200
    assert wait_labels(b, sid, ["1"]) == ["1"], "the remaining pin kept its old number after the delete"
    place_pin(b, "third note", ux=0.7)
    assert wait_labels(b, sid, ["1", "2"]) == ["1", "2"], "a new pin shared a number with the one that was renumbered"
