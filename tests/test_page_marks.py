"""A markable artifact page may suggest a mark, but it must never overrule the reviewer.

The page can post `{__mockreview: 1, type: 'mark', ...}`; the board used to patch the reviewer's draft with it
unconditionally, so a page could flip an Approve to Change or replace the reviewer's note. These are real-browser tests:
the page posts from its own frame, and the ack is read back through the frame's own message handler.
"""

import json
import time

import pytest

from mockreview import store
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}

PAGE = "<!doctype html><html><body><h1>Page</h1></body></html>"
BLOCKS = {"blocks": [{"type": "artifact", "id": "art", "title": "Page", "markable": True, "html": PAGE}]}
WAIT = "!!document.getElementById('dz-art')"


def _review(env, tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(BLOCKS), encoding="utf-8")
    return store.create_review("Marks", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)), wake=False)


def _open(chrome, server, data):
    b = Board(chrome, f"{server}/r/{data['id']}/", width=1440, height=900, wait=WAIT)
    b.review_id = data["id"]
    assert b.until("!!document.querySelector('#dz-art .dnote')", timeout=15), "the artifact has no decide row"
    return b


def _frame_session(b, suffix, timeout=10.0):
    rid = b.review_id
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        targets = b.cdp.call("Target.getTargets")["targetInfos"]
        frame = next((t for t in targets
                      if t["type"] == "iframe" and rid in t["url"] and t["url"].split("?")[0].endswith(suffix)), None)
        if frame:
            sid = b.cdp.call("Target.attachToTarget", {"targetId": frame["targetId"], "flatten": True})["sessionId"]
            b.cdp.call("Runtime.enable", session=sid)
            return sid
        b.cdp.pump(0.1)
    return None


def _page_mark(b, sid, status, note=None):
    """Post one mark from the page's own frame and return the board's mark-ack."""
    payload = {"__mockreview": 1, "type": "mark", "status": status, "id": 1}
    if note is not None:
        payload["note"] = note
    b.cdp.evaluate("window.__acks=[];addEventListener('message',function(e){"
                   "if(e.data&&e.data.cmd==='mark-ack')window.__acks.push(e.data)});"
                   "parent.postMessage(" + json.dumps(payload) + ",'*');1", session=sid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if b.cdp.evaluate("window.__acks.length > 0", session=sid):
            return json.loads(b.cdp.evaluate("JSON.stringify(window.__acks[0])", session=sid))
        b.cdp.pump(0.1)
    assert False, "the board never answered the page's mark"


def _mark_pressed(b, status):
    return b.js("document.querySelector('#dz-art .s-%s').getAttribute('aria-pressed')" % status)


def _draft_note(b, rid):
    """The note the board will send: read from its saved draft."""
    time.sleep(0.5)
    return b.js("(function(){var d=JSON.parse(localStorage.getItem('mr:%s:r1:draft')||'{}');"
                "return ((d.marks||{}).art||{}).note||''})()" % rid)


def test_a_page_mark_never_overwrites_the_reviewers_status(env, server, tmp_path, chrome):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data)
    b.press('#dz-art .decide[data-id="art"] .s-change')
    assert b.until("document.querySelector('#dz-art .s-change').getAttribute('aria-pressed') === 'true'"), \
        "the reviewer's Change did not land"
    sid = _frame_session(b, "blocks/art.html")
    assert sid, "the artifact page never appeared as a frame"
    ack = _page_mark(b, sid, "approve")
    assert ack["ok"] is False and "reviewer" in ack.get("error", ""), ack
    assert _mark_pressed(b, "change") == "true", "the page overwrote the reviewer's Change"
    assert _mark_pressed(b, "approve") == "false", "the page's Approve replaced the reviewer's Change"
    assert b.until("(document.querySelector('#dz-art .status') || {}).textContent === 'Change'"), \
        "the block's status no longer reads Change"


def test_a_page_mark_is_accepted_while_the_reviewer_has_none(env, server, tmp_path, chrome):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data)
    sid = _frame_session(b, "blocks/art.html")
    assert sid, "the artifact page never appeared as a frame"
    ack = _page_mark(b, sid, "approve", note="the page's own note")
    assert ack["ok"] is True, ack
    assert _mark_pressed(b, "approve") == "true", "the page's Approve did not land"
    assert _draft_note(b, data["id"]) == "the page's own note"


def test_a_page_mark_never_overwrites_the_reviewers_note(env, server, tmp_path, chrome):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data)
    b.js("(function(t){t.value='keep this note';t.dispatchEvent(new Event('input',{bubbles:true}))})"
         "(document.querySelector('#dz-art .dnote'))")
    sid = _frame_session(b, "blocks/art.html")
    assert sid, "the artifact page never appeared as a frame"
    ack = _page_mark(b, sid, "approve", note="the page's own note")
    assert ack["ok"] is True, ack
    assert _mark_pressed(b, "approve") == "true", "the page's Approve did not land"
    assert _draft_note(b, data["id"]) == "keep this note", "the page overwrote the reviewer's note"


def test_a_page_updates_its_own_mark_but_never_the_reviewers(env, server, tmp_path, chrome):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data)
    sid = _frame_session(b, "blocks/art.html")
    assert sid, "the artifact page never appeared as a frame"
    assert _page_mark(b, sid, "approve")["ok"] is True
    ack = _page_mark(b, sid, "change")
    assert ack["ok"] is True, ack
    assert _mark_pressed(b, "change") == "true", "the page could not update its own mark"
    b.press('#dz-art .decide[data-id="art"] .s-skip')
    assert b.until("document.querySelector('#dz-art .s-skip').getAttribute('aria-pressed') === 'true'"), \
        "the reviewer's Skip did not land"
    ack = _page_mark(b, sid, "approve")
    assert ack["ok"] is False and "reviewer" in ack.get("error", ""), ack
    assert _mark_pressed(b, "skip") == "true", "the page overwrote the reviewer's Skip"
    assert _mark_pressed(b, "approve") == "false", "the page's Approve replaced the reviewer's Skip"
    assert b.until("(function(){var d=JSON.parse(localStorage.getItem('mr:%s:r1:draft')||'{}');"
                   "return ((d.marks||{}).art||{}).status === 'skip'})()" % data["id"]), \
        "the saved draft no longer reads Skip"


def test_a_page_can_update_the_note_on_its_own_mark(env, server, tmp_path, chrome):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data)
    sid = _frame_session(b, "blocks/art.html")
    assert sid, "the artifact page never appeared as a frame"
    ack = _page_mark(b, sid, "approve", note="first")
    assert ack["ok"] is True, ack
    assert _draft_note(b, data["id"]) == "first"
    ack = _page_mark(b, sid, "change", note="second")
    assert ack["ok"] is True, ack
    assert ack.get("note_kept") is not True, ack
    assert _draft_note(b, data["id"]) == "second", "the page could not update its own note"
    assert _mark_pressed(b, "change") == "true"


def test_a_page_note_shows_in_the_note_box(env, server, tmp_path, chrome):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data)
    sid = _frame_session(b, "blocks/art.html")
    assert sid, "the artifact page never appeared as a frame"
    ack = _page_mark(b, sid, "approve", note="first")
    assert ack["ok"] is True, ack
    assert b.until("document.querySelector('#dz-art .dnote').value === 'first'"), \
        "the note the page wrote never reached the block's note box"
    ack = _page_mark(b, sid, "change", note="second")
    assert ack["ok"] is True, ack
    assert b.until("document.querySelector('#dz-art .dnote').value === 'second'"), \
        "the note box still shows the page's first note"


def test_a_page_note_never_clobbers_the_box_the_reviewer_is_typing_in(env, server, tmp_path, chrome):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data)
    sid = _frame_session(b, "blocks/art.html")
    assert sid, "the artifact page never appeared as a frame"
    assert b.js("(function(){var t=document.querySelector('#dz-art .dnote');t.focus();"
                "return document.activeElement === t})()"), "headless Chrome never focused the note box"
    ack = _page_mark(b, sid, "approve", note="page note")
    assert ack["ok"] is True, ack
    assert _draft_note(b, data["id"]) == "page note"
    assert b.js("document.querySelector('#dz-art .dnote').value") == "", \
        "the page's note replaced what the reviewer is typing"


def test_a_reviewer_note_still_wins_after_the_page_sets_a_status(env, server, tmp_path, chrome):
    data = _review(env, tmp_path)
    b = _open(chrome, server, data)
    b.js("(function(t){t.value='mine';t.dispatchEvent(new Event('input',{bubbles:true}))})"
         "(document.querySelector('#dz-art .dnote'))")
    sid = _frame_session(b, "blocks/art.html")
    assert sid, "the artifact page never appeared as a frame"
    ack = _page_mark(b, sid, "approve", note="page")
    assert ack["ok"] is True and ack.get("note_kept") is True, ack
    assert _draft_note(b, data["id"]) == "mine"
    ack = _page_mark(b, sid, "change", note="page2")
    assert ack["ok"] is True and ack.get("note_kept") is True, ack
    assert _draft_note(b, data["id"]) == "mine", "the page overwrote the reviewer's note"
    assert _mark_pressed(b, "change") == "true"
