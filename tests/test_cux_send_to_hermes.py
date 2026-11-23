"""CUX item 3: one composer with a 'Send to Hermes' box, in the new-pin composer and the thread reply.

The box is on by default and its token is the same `hermes` mention the @ picker inserts, so the server sees exactly one
wake path and exactly one mention event per comment. Real headless Chrome where the behaviour is UI; the POST body and
the stream are checked against a scratch server.
"""

import json
import time

import pytest

from authkit import account, signed_in, token_of
from conftest import http
from mockreview import service, store, threads, users, wakeguard
from test_live import Stream
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
MOCK = ('<section><h2>Checkout</h2><p id="copy" style="width:300px;height:50px;margin:20px">'
        'Review this total</p></section>')
DECISIONS = {"blocks": [{"type": "mock", "id": "m1", "html": MOCK}]}


def make_review(tmp_path, wake=True):
    path = tmp_path / ("hermes-%s.json" % wake)
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Send to Hermes", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(path)), wake=wake)
    threads.reset_limits()
    return data["id"]


@pytest.fixture
def hermes_review(env, tmp_path):
    return make_review(tmp_path, wake=True)


@pytest.fixture
def wake_off_review(env, tmp_path):
    return make_review(tmp_path, wake=False)


def open_board(chrome, server, rid, width=1440, height=900, mobile=False):
    return Board(chrome, f"{server}/r/{rid}/", width=width, height=height, mobile=mobile,
                 wait="!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && "
                      "document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))")


def on(b, width=1440):
    b.press(".foot-comment" if width <= 600 else "#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle,#comment-toggle-mobile')"
                   ".getAttribute('aria-pressed') === 'true'")


FITTED_JS = ("(function(){var h=document.querySelector('.mock-host'),m=h&&h.closest('.mock');if(!m)return false;"
             "return Math.round(h.getBoundingClientRect().width)<=Math.round(m.getBoundingClientRect().width)})()")


def mock_point(b, sel="#copy"):
    # The mock host first lays out at the full desktop width and is then fitted to its column; measuring before that
    # aims at where the element was, not where it ends up (the inline layout fits it fast enough to land in between).
    assert b.until(FITTED_JS, timeout=10), "the mock never fitted its column"
    return json.loads(b.js(
        "JSON.stringify((function(){var e=document.querySelector('.mock-host').shadowRoot.querySelector(%s);"
        "Element.prototype.scrollIntoView.call(e,{block:'center'});"
        "var r=Element.prototype.getBoundingClientRect.call(e);return [r.left+r.width/2,r.top+r.height/2]})())"
        % json.dumps(sel)))


def open_new_pin(b):
    b.click(*mock_point(b))
    assert b.until("!!document.querySelector('.popover textarea')", timeout=10)


def capture_posts(b):
    b.js("window.__posts=[];(function(){var of=window.fetch;window.fetch=function(u,o){"
         "if(String(u)==='api/threads'||String(u).indexOf('api/threads/')===0)"
         "window.__posts.push({url:String(u),body:o&&o.body});return of.apply(this,arguments)}})()")


def posts(b):
    got = b.js("JSON.stringify(window.__posts||[])")
    return json.loads(got) if got else []


def hold_reply(b, tid, fail=False):
    release = ("window.fetch=of;res(new Response('{\"error\":\"x\"}',"
               "{status:500,headers:{'Content-Type':'application/json'}}))" if fail
               else "window.fetch=of;res(of.call(window,u,o))")
    b.js("window.__replyHeld=false;window.__releaseReply=null;(function(){var of=window.fetch;"
         "window.fetch=function(u,o){"
         "if(String(u)===%s){window.__replyHeld=true;"
         "return new Promise(function(res){window.__releaseReply=function(){"
         "%s};});}"
         "return of.apply(this,arguments);};})()"
         % (json.dumps("api/threads/%s/reply" % tid), release))


def save_new_pin(b, text):
    ta = "document.querySelector('.popover textarea')"
    b.js("document.querySelector('.popover .hermes-box') ? 1 : 1")
    b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})(%s)" % ta)
    b.cdp.call("Input.insertText", {"text": text}, session=b.sid)
    b.press(".popover .composer-actions .btn-round")
    assert b.until("!!document.querySelector('.mpin[data-thread]')", timeout=15)


def server_threads(server, rid, member=None):
    code, _, body = http("GET", f"{server}/r/{rid}/api/threads", headers=signed_in(member) if member else {})
    assert code == 200
    return json.loads(body)["threads"]


def test_a_new_pin_opens_checked_and_posts_one_hermes_mention(hermes_review, server, chrome):
    rid = hermes_review
    b = open_board(chrome, server, rid)
    try:
        on(b)
        capture_posts(b)
        open_new_pin(b)
        assert b.js("!!document.querySelector('.popover .hermes-send')"), "the box is missing"
        assert b.js("document.querySelector('.popover .hermes-box').checked") is True
        assert b.js("document.querySelector('.popover textarea').value") == "@Hermes "
        save_new_pin(b, "Make this clearer")
        body = json.loads(posts(b)[0]["body"])
        assert body["mentions"] == ["hermes"], body
        assert body["text"] == "@Hermes Make this clearer", body
        listed = server_threads(server, rid)
        assert len(listed) == 1
        t = listed[0]
        assert t["activated"] is True
        assert t["comments"][0]["text"] == "@Hermes Make this clearer"
        assert t["comments"][0]["sent_to_hermes"] is True
        assert t["comments"][0]["mentions"] == [{"kind": "hermes", "target": "hermes"}]
    finally:
        b.js("localStorage.clear()")


def test_unchecked_posts_no_hermes_mention_and_no_leading_token(hermes_review, server, chrome):
    rid = hermes_review
    b = open_board(chrome, server, rid)
    try:
        on(b)
        capture_posts(b)
        open_new_pin(b)
        b.press(".popover .hermes-box")
        assert b.until("!document.querySelector('.popover .hermes-box').checked")
        assert b.js("document.querySelector('.popover textarea').value") == ""
        save_new_pin(b, "No agent needed")
        body = json.loads(posts(b)[0]["body"])
        assert body["mentions"] == [], body
        assert not body["text"].startswith("@Hermes"), body
        t = server_threads(server, rid)[0]
        assert t["activated"] is False
        assert t["comments"][0]["text"] == "No agent needed"
        assert t["comments"][0]["sent_to_hermes"] is False
    finally:
        b.js("localStorage.clear()")


def test_toggling_while_typing_moves_only_the_leading_token(hermes_review, server, chrome):
    rid = hermes_review
    b = open_board(chrome, server, rid)
    try:
        on(b)
        open_new_pin(b)
        ta = "document.querySelector('.popover textarea')"
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})(%s)" % ta)
        b.cdp.call("Input.insertText", {"text": "keep this text"}, session=b.sid)
        assert b.js(ta + ".value") == "@Hermes keep this text"
        b.press(".popover .hermes-box")
        assert b.until(ta + ".value === 'keep this text'"), b.js(ta + ".value")
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})(%s)" % ta)
        b.cdp.call("Input.insertText", {"text": " and more"}, session=b.sid)
        assert b.js(ta + ".value") == "keep this text and more"
        b.press(".popover .hermes-box")
        assert b.until(ta + ".value === '@Hermes keep this text and more'")
        assert b.js(ta + ".selectionStart") >= 8
    finally:
        b.js("localStorage.clear()")


def test_typing_hermes_by_hand_syncs_both_ways_and_the_picker_checks_it(hermes_review, server, chrome):
    rid = hermes_review
    b = open_board(chrome, server, rid)
    try:
        on(b)
        open_new_pin(b)
        ta = "document.querySelector('.popover textarea')"
        box = "document.querySelector('.popover .hermes-box')"
        b.press(".popover .hermes-box")
        assert b.until("!" + box + ".checked && " + ta + ".value === ''")
        b.js("(function(t){t.focus();t.value='@Hermes typed by hand';t.dispatchEvent(new Event('input'));return 1})(%s)" % ta)
        assert b.until(box + ".checked === true"), "a hand-typed @Hermes must check the box"
        b.js("(function(t){t.value='typed again without it';t.dispatchEvent(new Event('input'));return 1})(%s)" % ta)
        assert b.until(box + ".checked === false"), "deleting the token must uncheck the box"
        b.js("(function(t){t.focus();t.value='';t.dispatchEvent(new Event('input'));return 1})(%s)" % ta)
        b.cdp.call("Input.insertText", {"text": "@Her"}, session=b.sid)
        assert b.until("!!document.querySelector('.ml')"), "the mention picker did not open"
        b.key("Enter", "Enter", 13)
        assert b.until(ta + ".value === '@Hermes '"), b.js(ta + ".value")
        assert b.until(box + ".checked === true"), "accepting Hermes from the picker must check the box"
    finally:
        b.js("localStorage.clear()")


def test_the_reply_composer_opens_checked_and_posts_the_same_mention(hermes_review, server, chrome):
    rid = hermes_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned note",
                       anchor={"block": "m1", "option": None, "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    try:
        on(b)
        capture_posts(b)
        b.press('.mpin[data-thread="%s"]' % t["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is True
        assert b.js("document.querySelector('.tpop textarea').value") == "@Hermes "
        ta = "document.querySelector('.tpop textarea')"
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})(%s)" % ta)
        b.cdp.call("Input.insertText", {"text": "reply with the agent"}, session=b.sid)
        b.press(".tpop .tp-send")
        assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2")
        body = json.loads(posts(b)[0]["body"])
        assert body["mentions"] == ["hermes"], body
        assert body["text"] == "@Hermes reply with the agent"
        got = server_threads(server, rid)[0]
        assert got["activated"] is True
        assert got["comments"][-1]["sent_to_hermes"] is True
        assert got["comments"][-1]["text"] == "@Hermes reply with the agent"
    finally:
        b.js("localStorage.clear()")


def test_a_second_reply_reopens_the_composer_checked_with_the_token(hermes_review, server, chrome):
    rid = hermes_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned note",
                       anchor={"block": "m1", "option": None, "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    try:
        on(b)
        capture_posts(b)
        b.press('.mpin[data-thread="%s"]' % t["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        ta = "document.querySelector('.tpop textarea')"
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})(%s)" % ta)
        b.cdp.call("Input.insertText", {"text": "first reply"}, session=b.sid)
        b.press(".tpop .tp-send")
        assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2")
        assert b.until("!!document.querySelector('.tpop .hermes-box')"), "the box is missing after the first reply"
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is True, \
            "the composer reopened unchecked after the first reply"
        assert b.js("document.querySelector('.tpop textarea').value") == "@Hermes "
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})(%s)" % ta)
        b.cdp.call("Input.insertText", {"text": "second reply"}, session=b.sid)
        b.press(".tpop .tp-send")
        assert b.until("document.querySelectorAll('.tpop .tcomment').length === 3")
        got = posts(b)
        assert len(got) == 2, got
        body = json.loads(got[1]["body"])
        assert body["mentions"] == ["hermes"], body
        assert body["text"] == "@Hermes second reply", body
        latest = server_threads(server, rid)[0]
        assert latest["comments"][-1]["sent_to_hermes"] is True
        assert latest["comments"][-1]["text"] == "@Hermes second reply"
    finally:
        b.js("localStorage.clear()")


GEO = (
    "JSON.stringify((function(){"
    "function r(e){var b=e.getBoundingClientRect();"
    "return {left:b.left,right:b.right,top:b.top,bottom:b.bottom,width:b.width,height:b.height}}"
    "var reply=document.querySelector('.tpop .tp-reply');"
    "var ta=document.querySelector('.tpop .tp-reply .tp-input');"
    "var acts=document.querySelector('.tpop .tp-reply .tp-actions');"
    "var label=document.querySelector('.tpop .tp-reply .hermes-send');"
    "var box=document.querySelector('.tpop .tp-reply .hermes-box');"
    "var send=document.querySelector('.tpop .tp-reply .tp-send');"
    "var title=document.querySelector('.tpop .tp-title');"
    "return {reply:r(reply),ta:r(ta),acts:r(acts),label:r(label),box:r(box),send:r(send),"
    "radius:parseFloat(getComputedStyle(send).borderTopLeftRadius),titleText:title.textContent,"
    "strong:r(title.querySelector('strong')),faint:r(title.querySelector('.faint'))}})())"
)


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop-1440", "phone-390"])
def test_the_reply_composer_stacks_and_the_title_stays_on_one_line(hermes_review, server, chrome,
                                                                   width, height, mobile):
    rid = hermes_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned note",
                       anchor={"block": "m1", "option": None, "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid, width=width, height=height, mobile=mobile)
    try:
        on(b, width)
        if mobile:
            b.cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}, session=b.sid)
        b.press('.mpin[data-thread="%s"]' % t["id"])
        assert b.until("!!document.querySelector('.tpop .tp-input')")
        geo = json.loads(b.js(GEO))
        assert abs(geo["ta"]["width"] - geo["reply"]["width"]) <= 2, \
            f"the textarea must be full width, not squeezed by the box: {geo}"
        assert geo["acts"]["top"] >= geo["ta"]["bottom"] - 0.5, \
            f"the actions row must sit under the textarea: {geo}"
        assert abs(geo["acts"]["left"] - geo["reply"]["left"]) <= 2, geo
        assert abs(geo["acts"]["right"] - geo["reply"]["right"]) <= 2, geo
        assert geo["box"]["left"] >= geo["acts"]["left"] - 1 and geo["label"]["left"] <= geo["acts"]["left"] + 2, \
            f"the checkbox/label must sit at the left of the row: {geo}"
        assert abs(geo["send"]["right"] - geo["acts"]["right"]) <= 2, \
            f"the send button must sit at the right of the row: {geo}"
        assert geo["label"]["right"] < geo["send"]["left"], geo
        assert geo["radius"] >= max(12, geo["send"]["height"] / 2), \
            f"the send button must be round: {geo}"
        if mobile:
            assert geo["send"]["height"] >= 44, geo
        assert abs(geo["strong"]["top"] - geo["faint"]["top"]) <= 4, \
            f"the name and time must share a line in the header: {geo}"
        assert geo["faint"]["left"] >= geo["strong"]["right"] - 1, \
            f"the time must start to the right of the name: {geo}"
        title = geo["titleText"]
        assert title.startswith(owner["name"] + " · "), \
            f"the header must read 'name · time' with one space around the dot: {title!r}"
        assert title.count("·") == 1 and "  " not in title and not title[len(owner["name"]) + 3:].startswith(" "), \
            f"the header must keep exactly one space on each side of the dot: {title!r}"
        assert geo["faint"]["left"] - geo["strong"]["right"] >= 2, \
            f"the time must sit at least 2 px clear of the name: {geo}"
    finally:
        b.js("localStorage.clear()")


def test_the_reply_box_keeps_its_default_state_while_the_send_is_in_flight(hermes_review, server, chrome):
    rid = hermes_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned note",
                       anchor={"block": "m1", "option": None, "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    try:
        on(b)
        b.press('.mpin[data-thread="%s"]' % t["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        hold_reply(b, t["id"])
        ta = "document.querySelector('.tpop .tp-input')"
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})(%s)" % ta)
        b.cdp.call("Input.insertText", {"text": "first reply"}, session=b.sid)
        b.press(".tpop .tp-send")
        assert b.until("window.__replyHeld === true"), "the reply POST was not captured"
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is True, \
            "the composer unchecked the box while the reply was in flight"
        assert b.js(ta + ".value") == "@Hermes ", \
            f"the composer was empty while the reply was in flight: {b.js(ta + '.value')!r}"
        b.js("window.__releaseReply()")
        assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2")
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is True, \
            "the composer reopened unchecked after the reply"
        assert b.js(ta + ".value") == "@Hermes "
    finally:
        b.js("localStorage.clear()")


def test_an_unchecked_send_rechecks_the_box_while_in_flight(hermes_review, server, chrome):
    rid = hermes_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned note",
                       anchor={"block": "m1", "option": None, "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    try:
        on(b)
        b.press('.mpin[data-thread="%s"]' % t["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        b.press(".tpop .hermes-box")
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is False
        ta = "document.querySelector('.tpop .tp-input')"
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})(%s)" % ta)
        b.cdp.call("Input.insertText", {"text": "plain reply"}, session=b.sid)
        hold_reply(b, t["id"])
        b.press(".tpop .tp-send")
        assert b.until("window.__replyHeld === true"), "the reply POST was not captured"
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is True, \
            "an unchecked send left the box unchecked while the textarea held the token"
        assert b.js(ta + ".value") == "@Hermes ", \
            f"the composer did not go back to its default text: {b.js(ta + '.value')!r}"
        b.js("window.__releaseReply()")
        assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2")
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is True, \
            "the composer reopened unchecked after the reply"
        assert b.js(ta + ".value") == "@Hermes "
    finally:
        b.js("localStorage.clear()")


def test_a_failed_reply_restores_the_box_to_agree_with_the_text(hermes_review, server, chrome):
    rid = hermes_review
    owner = users.owner_user()
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned note",
                       anchor={"block": "m1", "option": None, "x": 0.3, "y": 0.4})
    b = open_board(chrome, server, rid)
    try:
        on(b)
        b.press('.mpin[data-thread="%s"]' % t["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        b.press(".tpop .hermes-box")
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is False
        ta = "document.querySelector('.tpop .tp-input')"
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})(%s)" % ta)
        b.cdp.call("Input.insertText", {"text": "plain reply"}, session=b.sid)
        hold_reply(b, t["id"], fail=True)
        b.press(".tpop .tp-send")
        assert b.until("window.__replyHeld === true"), "the reply POST was not captured"
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is True, \
            "an unchecked send left the box unchecked while the textarea held the token"
        assert b.js(ta + ".value") == "@Hermes ", \
            f"the composer did not go back to its default text: {b.js(ta + '.value')!r}"
        b.js("window.__releaseReply()")
        assert b.until(ta + ".value === 'plain reply'"), \
            f"a failed send must restore the typed text: {b.js(ta + '.value')!r}"
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is False, \
            "the box must agree with the restored text, which has no token"
    finally:
        b.js("localStorage.clear()")


def test_a_mention_event_fires_once_per_comment(hermes_review, server, chrome):
    rid = hermes_review
    b = open_board(chrome, server, rid)
    live = Stream(server, f"/r/{rid}/events")
    try:
        assert live.until("hello") is not None
        on(b)
        open_new_pin(b)
        save_new_pin(b, "wake once")
        frame = live.until("mention", timeout=10)
        assert frame is not None, "no mention event"
        assert json.loads(frame["data"])["targets"] == ["hermes"]
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            extra = live.frame(timeout=0.3)
            if extra is None:
                continue
            assert extra.get("event") != "mention", "a second mention event was published for one comment"
    finally:
        live.close()
        b.js("localStorage.clear()")


def test_a_commenter_without_submit_gets_no_box_and_no_mention(hermes_review, server, chrome, monkeypatch):
    rid = hermes_review
    member = account(review_id=rid, role="commenter", name="Maya")
    b = open_board(chrome, server, rid)
    try:
        monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
        b.cdp.call("Network.enable", session=b.sid)
        for key, value in [("mr_session", member["session"]), ("mr_csrf", member["csrf"])]:
            b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                       session=b.sid)
        b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
        assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot"
                       " && document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))", timeout=60)
        on(b)
        capture_posts(b)
        open_new_pin(b)
        assert not b.js("!!document.querySelector('.popover .hermes-box')"), "a commenter without submit got the box"
        assert b.js("document.querySelector('.popover textarea').value") == ""
        save_new_pin(b, "Just a comment")
        body = json.loads(posts(b)[0]["body"])
        assert body["mentions"] == [], body
        got = server_threads(server, rid, member)[0]
        assert got["activated"] is False
        assert got["comments"][0]["text"] == "Just a comment"
        b.press('.mpin[data-thread="%s"]' % got["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        assert not b.js("!!document.querySelector('.tpop .hermes-box')"), "a commenter without submit got the reply box"
        assert b.js("document.querySelector('.tpop textarea').value") == ""
    finally:
        b.js("localStorage.clear()")


def test_a_wake_off_board_keeps_the_old_composer(wake_off_review, server, chrome):
    rid = wake_off_review
    b = open_board(chrome, server, rid)
    try:
        on(b)
        capture_posts(b)
        open_new_pin(b)
        assert not b.js("!!document.querySelector('.popover .hermes-box')"), "a wake-off board got the box"
        assert b.js("document.querySelector('.popover textarea').value") == ""
        save_new_pin(b, "Saved only")
        body = json.loads(posts(b)[0]["body"])
        assert body["mentions"] == [], body
        got = server_threads(server, rid)[0]
        assert got["activated"] is False
        assert got["comments"][0]["text"] == "Saved only"
        b.press('.mpin[data-thread="%s"]' % got["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        assert not b.js("!!document.querySelector('.tpop .hermes-box')"), "a wake-off board got the reply box"
        assert b.js("document.querySelector('.tpop textarea').value") == ""
    finally:
        b.js("localStorage.clear()")


TOASTS = ("window.__toasts=[];new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach("
          "function(n){if(n.classList&&n.classList.contains('toast'))window.__toasts.push("
          "{text:n.textContent,cls:n.className})})})}).observe(document.body,{childList:true});1")


def test_a_guest_reviewer_gets_the_box_and_hears_why_a_mention_was_saved(hermes_review, server, chrome, monkeypatch):
    """A share-link guest with the Reviewer role may wake Hermes with a mention (wakeguard.request_gate), so the
    composer offers them the box; when the board's chat holds tools a guest may not trigger, the mention is saved and
    the board says why instead of showing Hermes at work."""
    rid = hermes_review
    made = users.share("test", rid, "create", "reviewer")
    guest = users.join_as_guest(users.resolve_share_token(token_of(made["path"])))
    b = open_board(chrome, server, rid)
    try:
        monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
        b.cdp.call("Network.enable", session=b.sid)
        for key, value in [("mr_session", guest["session"]), ("mr_csrf", guest["csrf"])]:
            b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                       session=b.sid)
        b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
        assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot"
                       " && document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))", timeout=60)
        on(b)
        capture_posts(b)
        b.js(TOASTS)
        open_new_pin(b)
        assert b.js("!!document.querySelector('.popover .hermes-box')"), "a reviewer guest must get the box"
        assert b.js("document.querySelector('.popover .hermes-box').checked") is True
        save_new_pin(b, "Make this clearer")
        assert json.loads(posts(b)[0]["body"])["mentions"] == ["hermes"]
        first = server_threads(server, rid, guest)[0]
        assert first["activated"] is True, first
        assert not [t for t in json.loads(b.js("JSON.stringify(window.__toasts)")) if "warn" in t["cls"]]

        monkeypatch.setenv("MOCKREVIEW_WAKE_TOOLSETS", "terminal")
        b.press('.mpin[data-thread="%s"]' % first["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        assert b.js("document.querySelector('.tpop .hermes-box').checked") is True
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})"
             "(document.querySelector('.tpop textarea'))")
        b.cdp.call("Input.insertText", {"text": "And the total"}, session=b.sid)
        b.press(".tpop .tp-send")
        assert b.until("(window.__toasts||[]).some(function(t){return t.cls.indexOf('warn')>=0})", timeout=15), \
            "a saved mention must say why"
        warn = [t for t in json.loads(b.js("JSON.stringify(window.__toasts)")) if "warn" in t["cls"]]
        assert warn[0]["text"] == wakeguard.TOOLS_NO_WAKE, warn
        reply = json.loads(posts(b)[-1]["body"])
        assert reply["mentions"] == ["hermes"], reply
        got = [t for t in server_threads(server, rid, guest) if t["id"] == first["id"]][0]
        assert got["comments"][-1]["text"] == "@Hermes And the total"
        assert len(got["comments"]) == 2
    finally:
        b.js("localStorage.clear()")
