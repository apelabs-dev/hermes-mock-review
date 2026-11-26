"""The board swaps a block Hermes patched in place, and shows Hermes's own replies as Hermes."""

import json

import pytest

from mockreview import api, config, live, owner, service, store, threads
from test_cux_working_pins import CHIP, WorkingBoard, exists, has_text, mention
from test_live_round_tool import chrome  # noqa: F401
from test_cux_thread_actions import on, open_board
from test_mention_wake import call_tool, plugin_tools, post

pytestmark = pytest.mark.browser



def copy_has(text):
    return ("(document.querySelector('.mock-host')?.shadowRoot?.querySelector('#copy')?.textContent || '')"
            ".includes(%s)" % json.dumps(text))


def _html(price):
    return ('<section style="padding:24px;background:#f7f5f0;color:#232321;font:16px sans-serif">'
            '<h2>Order summary</h2><p id="copy" style="width:220px;height:64px">Subtotal · %s</p>'
            '<p>Shipping calculated at checkout</p></section>' % price)


@pytest.fixture
def tall_review(env, monkeypatch, tmp_path):
    live.reset()
    threads.reset_limits()
    monkeypatch.setattr(owner, "resolve_soft", lambda: owner.Context(config.data_root(), False))
    monkeypatch.setattr(service.notify, "send", lambda *args, **kwargs: (True, "accepted"))
    monkeypatch.setattr(service.notify, "send_notice", lambda *args, **kwargs: (True, "accepted"))
    filler = "\n\n".join(f"Filler paragraph {i}: the order flow, reviewed one step at a time." for i in range(60))
    path = tmp_path / "tall.json"
    path.write_text(json.dumps({"blocks": [
        {"type": "text", "id": "t1", "title": "Before", "text": filler},
        {"type": "mock", "id": "m1", "title": "Order summary", "html": _html("$48.00")},
        {"type": "text", "id": "t2", "title": "After", "text": filler},
    ]}), encoding="utf-8")
    review = store.create_review("Tall review", "", {"platform": "discord", "chat_id": "test-chat"}, "local",
                                 store.parse_round_spec(decisions=str(path)), wake=True,
                                 look={"pin_fix": False, "pin_fix_min_seconds": 0, "pin_fix_window_seconds": 0})
    yield review
    live.reset()


def test_a_patched_block_swaps_in_place_without_a_reload_or_a_scroll(tall_review, server, chrome):
    rid = tall_review["id"]
    b = WorkingBoard(chrome, server, tall_review, mobile=False)
    instance = b.reset_observation()
    b.js("window.scrollTo(0, document.querySelector('.mock').getBoundingClientRect().top + scrollY - 160)")
    b.cdp.pump(0.3)
    y = b.js("Math.round(scrollY)")
    assert y > 400, "the page never scrolled: the test would prove nothing"
    b.js("window.hostBefore = document.querySelector('.mock-host')")
    # an @Hermes mention opens the block's "working" chip; the patch must clear it in the same repaint
    mention(server, rid)
    service.retry_thread_wakes(rid)
    b.shows(exists(CHIP), "the working chip never appeared")
    api.patch_block(rid, "m1", _html("$52.00"), round_n=1, if_rev=0)
    b.shows(copy_has("$52.00"), "the patched block never appeared")
    assert b.js("document.querySelector('.mock-host') !== window.hostBefore"), "the block was not re-rendered"
    b.shows("!" + exists(CHIP), "the working chip survived the repaint")
    b.same_document(instance)
    assert abs(b.js("Math.round(scrollY)") - y) <= 2, "the patch moved the reader"
    assert b.js("document.querySelectorAll('.mock-host').length") == 1


def test_a_stale_block_patched_event_does_nothing(tall_review, server, chrome):
    rid = tall_review["id"]
    b = WorkingBoard(chrome, server, tall_review, mobile=False)
    api.patch_block(rid, "m1", _html("$52.00"), round_n=1, if_rev=0)
    b.shows(copy_has("$52.00"), "the patched block never appeared")
    b.js("window.hostAfter = document.querySelector('.mock-host')")
    b.reset_observation()
    b.js("boardEvents.dispatchEvent(new MessageEvent('block.patched', {data: JSON.stringify("
         "{round: 1, block: 'm1', option: null, rev: 1})}))")
    b.shows("boardGets.some(function (u) { return u.indexOf('state.json') >= 0; })", "the event never fetched state")
    b.cdp.pump(0.5)
    assert b.js("document.querySelector('.mock-host') === window.hostAfter"), "a stale event re-rendered the block"
    assert b.js(copy_has("$52.00"))
    assert not b.js("boardGets.some(function (u) { return u.indexOf('decisions.json') >= 0; })")


def test_hermes_replies_wear_the_hermes_mark(tall_review, server, chrome):
    rid = tall_review["id"]
    code, _, out = post(server, rid, "Is this subtotal right?", mentions=(), anchor={"block": "m1", "x": 0.2, "y": 0.3})
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    b = WorkingBoard(chrome, server, tall_review, mobile=False)
    instance = b.reset_observation()
    reply = call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "Yes, it matches."})
    assert "error" not in reply, reply
    b.press(".tcomments")
    b.shows(exists(".tpanel .trow"), "the Comments list never opened")
    b.press(".tpanel .trow")
    b.shows(exists(".tcomment .hermes-mark"), "Hermes's reply carries no Hermes mark")
    assert b.js(has_text(".tcomment .hermes-mark", "agent"))
    assert b.js(exists(".tcomment .av.hermes"))
    names = json.loads(b.js("JSON.stringify(Array.from(document.querySelectorAll('.tcomment .cname'), "
                            "function (n) { return n.textContent; }))"))
    assert names[-1] == "Hermes"
    editable = b.js("Array.from(document.querySelectorAll('.tcomment')).filter(function (r) {"
                    " return r.textContent.indexOf('Yes, it matches.') >= 0 && r.querySelector('.cmore'); }).length")
    assert editable == 0, "Hermes's comment offers the owner Edit/Delete as if it were theirs"
    b.same_document(instance)


def _open_pin(b, tid):
    assert b.until("!!document.querySelector('.mpin[data-thread=\"%s\"]')" % tid, timeout=20), "the pin never drew"
    b.press('.mpin[data-thread="%s"]' % tid)
    assert b.until("!!document.querySelector('.tpop')", timeout=20)


def _gone(server, rid, tid):
    from conftest import http
    code, _, body = http("GET", f"{server}/r/{rid}/api/threads")
    assert code == 200
    return all(t["id"] != tid for t in json.loads(body)["threads"])


def test_deleting_a_pin_that_hermes_answered_removes_the_pin(tall_review, server, chrome):
    rid = tall_review["id"]
    code, _, out = post(server, rid, "Is this subtotal right?", mentions=(), anchor={"block": "m1", "option": None, "x": 0.2, "y": 0.3})
    assert code == 201, out
    tid = json.loads(out)["thread"]["id"]
    call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "Yes, it matches."})
    b = open_board(chrome, server, rid)
    on(b)
    _open_pin(b, tid)
    b.press(".tpop .tp-more")
    assert b.until("!!document.querySelector('.tp-menu')")
    b.press(".tp-menu .menu-item:last-child")
    assert b.until("!document.querySelector('.tpop')", timeout=20), "Delete left the popover open"
    assert b.until("!document.querySelector('.mpin[data-thread=\"%s\"]')" % tid), "the pin stayed on the board"
    assert _gone(server, rid, tid), "the thread survived on the server, so the pin returns on reload"


def test_a_pin_whose_own_comment_was_deleted_can_still_be_deleted_when_only_hermes_is_left(tall_review, server, chrome):
    from conftest import http
    rid = tall_review["id"]
    code, _, out = post(server, rid, "Is this subtotal right?", mentions=(), anchor={"block": "m1", "option": None, "x": 0.2, "y": 0.3})
    thread = json.loads(out)["thread"]
    tid, root = thread["id"], thread["comments"][0]["id"]
    call_tool(plugin_tools(), "mock_review_reply", {"thread_id": tid, "text": "Yes, it matches."})
    code, _, out = http("POST", f"{server}/r/{rid}/api/comments/{root}/delete", {})
    assert code == 200, out
    b = open_board(chrome, server, rid)
    on(b)
    _open_pin(b, tid)
    b.press(".tpop .tp-more")
    assert b.until("!!document.querySelector('.tp-menu')")
    texts = json.loads(b.js("JSON.stringify([].map.call(document.querySelectorAll('.tp-menu .menu-item'),"
                            "function(x){return x.innerText.trim()}))"))
    assert texts == ["Copy link", "Delete"], texts
    b.press(".tp-menu .menu-item:last-child")
    assert b.until("!document.querySelector('.mpin[data-thread=\"%s\"]')" % tid), "the pin stayed on the board"
    assert _gone(server, rid, tid)
