"""GUEST card: server-assigned random guest names, and the three Hermes gates for guests and wake-off boards.

The server half runs against the scratch server like the other unit tests; the surface half drives real headless Chrome
(the `@` picker, the Send-to-Hermes box, the presence stack and the guest name pill).
"""

import json
import re
import time

import pytest

from authkit import account, signed_in, token_of
from conftest import http
from mockreview import db, threads, users
from test_cux_send_to_hermes import make_review, on, open_new_pin, save_new_pin
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

NAME_RE = re.compile(r"^[A-Z][a-z]+ [A-Z][a-z]+$")
BOARD_WAIT = ("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && "
              "document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))")
BLOCK_GUEST_NAME_SAVE = ("window.__origFetch=window.fetch;window.fetch=function(u,o){"
                         "if(String(u).indexOf('api/guest-name')>=0){return Promise.reject(new Error('blocked'))}"
                         "return window.__origFetch.apply(this,arguments)};")

# Holds the activity snapshot (`api/threads?...`) and the mark-read POST for the whole page life, like
# test_board_threads.DELAYED_EVENTS holds the stream back: the fetched feed never lands, so any row the feed shows
# came from the live event path alone.
HOLD_SNAPSHOT = ("window.__origFetch=window.fetch;window.fetch=function(u,o){"
                 "var s=String(u);if(s.indexOf('api/threads?')>=0||s.indexOf('api/activity/read')>=0)"
                 "{return new Promise(function(){})}return window.__origFetch.apply(this,arguments)};")


def join_guest(rid, role="reviewer"):
    if users.share_link(rid) is None:
        made = users.share("test", rid, "create", role)
    else:
        made = users.share("test", rid, "update", role)
    guest = users.join_as_guest(users.resolve_share_token(token_of(made["path"])))
    return made, guest


def board_me(b):
    b.js("window.__mrMe=null;(function(){fetch('state.json',{credentials:'same-origin'})"
         ".then(function(r){return r.json()}).then(function(j){window.__mrMe=j.me})"
         ".catch(function(e){window.__mrMe={error:String(e)}})})()")
    assert b.until("!!window.__mrMe", timeout=15), "state.json never answered"
    return json.loads(b.js("JSON.stringify(window.__mrMe)"))


def initials(name):
    words = str(name or "?").split()
    return "".join(w[0] for w in words[:2]).upper()


def wait_pin(b, tid):
    sel = '.mpin[data-thread="%s"]' % tid
    assert b.until("!!document.querySelector(%s)" % json.dumps(sel), timeout=15), f"no pin for thread {tid}"


def comment_names(b):
    return json.loads(b.js(
        "JSON.stringify(Array.prototype.map.call(document.querySelectorAll('.tpop .cname'),"
        "function(n){return n.textContent}))"))


def mention_names(b):
    return json.loads(b.js(
        "JSON.stringify(Array.prototype.map.call(document.querySelectorAll('.ml .ml-name'),"
        "function(n){return n.textContent}))"))


def mention_picker(b):
    assert b.until("!!document.querySelector('.ml .ml-name')", timeout=10), "the @ picker never rendered"
    return mention_names(b)


def type_at(b, sel):
    b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})"
         "(document.querySelector(%s))" % json.dumps(sel))
    b.cdp.call("Input.insertText", {"text": "@"}, session=b.sid)
    b.cdp.pump(0.3)
    time.sleep(0.2)


MOBILE = {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True}
DESKTOP = {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False}


def at_width(b, metrics):
    """Switch this board's viewport and wait for the browser to take it (and, on a phone, for the phone render).

    The board re-renders 150 ms after the resize event and skips that render while a popover is open, so a helper
    that returned only on a desktop re-render would hang in the tests that switch widths with a composer open.
    A foot toggle exists only in the <=600 render, so its appearance still proves the phone layout landed."""
    b.cdp.call("Page.bringToFront", session=b.sid)   # a background tab does not relayout for the new width
    b.cdp.call("Emulation.setDeviceMetricsOverride", metrics, session=b.sid)
    assert b.until("innerWidth === %d && innerHeight === %d" % (metrics["width"], metrics["height"]), timeout=15), \
        f"the browser never took the {metrics['width']} viewport"
    if metrics["mobile"]:
        assert b.until("!!document.querySelector('.foot-comment')", timeout=15), \
            f"the {metrics['width']} layout never rendered"


def comment_off(b):
    """Leave comment mode, so a fresh `on(b, 390)` really presses the mobile toggle on."""
    b.press(".foot-comment" if b.js("!!document.querySelector('.foot-comment')") else "#comment-toggle")
    assert b.until("document.querySelector('#comment-toggle,#comment-toggle-mobile')"
                   ".getAttribute('aria-pressed') === 'false'"), "comment mode never turned off"


def open_new_pin_at_corner(b):
    """Open the new-pin composer clear of the pin saved earlier in the test.

    A click on a saved pin reopens that thread instead, and the thread popover is also `.popover` (it carries
    `popover tpop`), so the new-pin composer is identified by `.pop-close` and the thread popover is closed first.
    """
    b.js("(function(){var p=document.querySelector('.popover');if(!p)return 1;"
         "var c=p.querySelector('.pop-close,.tp-close');if(c){c.click()}else{p.remove()}return 1})()")
    assert b.until("!document.querySelector('.popover')", timeout=10), "a popover stayed open"
    compose = ("!!(document.querySelector('.popover:not(.tpop) .pop-close') "
               "&& document.querySelector('.popover:not(.tpop) textarea'))")
    for fx, fy in ((0.12, 0.85), (0.88, 0.15), (0.5, 0.92), (0.92, 0.5)):
        point = json.loads(b.js(
            "JSON.stringify((function(){var e=document.querySelector('.mock-host').shadowRoot.querySelector('#copy');"
            "Element.prototype.scrollIntoView.call(e,{block:'center'});"
            "var r=Element.prototype.getBoundingClientRect.call(e);"
            "return [r.left+r.width*%s,r.top+r.height*%s]})())" % (fx, fy)))
        b.click(*point)
        if b.until(compose, timeout=6):
            return
    assert False, "the new-pin composer did not open away from the saved pin"


def presence_initials(b):
    return json.loads(b.js(
        "JSON.stringify(Array.prototype.map.call(document.querySelectorAll('.tavs .av'),"
        "function(n){return n.textContent}))"))


def wait_two_guests(b, names, timeout=15):
    """Both guests' initials in one presence stack, and exactly two entries (initials can collide)."""
    want = sorted(initials(n) for n in names)
    end = time.monotonic() + timeout
    seen = presence_initials(b)
    while time.monotonic() < end and (len(seen) != 2 or sorted(seen) != want):
        time.sleep(0.2)
        seen = presence_initials(b)
    return seen, want


# ---------------------------------------------------------------- A. random guest names


def test_two_live_guests_get_different_server_names(env, review):
    _made, one = join_guest(review["id"])
    _made, two = join_guest(review["id"])
    first = users.guest(one["guest_id"])["display_name"]
    second = users.guest(two["guest_id"])["display_name"]
    assert first not in ("", "Guest"), f"join_as_guest stored no display name: {first!r}"
    assert NAME_RE.match(first), f"the server name is not Adjective Animal: {first!r}"
    assert second not in ("", "Guest"), f"join_as_guest stored no display name: {second!r}"
    assert NAME_RE.match(second), f"the server name is not Adjective Animal: {second!r}"
    assert first != second, f"two live guests of one review share the name {first!r}"


def test_me_payload_carries_the_guest_name(env, server, review):
    _made, guest = join_guest(review["id"])
    code, _headers, body = http("GET", f"{server}/r/{review['id']}/state.json",
                               headers=signed_in(guest, False))
    assert code == 200, body
    me = json.loads(body)["me"]
    assert me["kind"] == "guest"
    assert me["name"] == users.guest(guest["guest_id"])["display_name"]
    assert NAME_RE.match(me["name"]), f"`me` carries no server-assigned guest name: {me['name']!r}"


def test_a_guest_comment_and_its_activity_row_carry_the_guest_kind(env, review):
    """A guest can name themselves like the owner; the payload still says who is a guest."""
    rid = review["id"]
    _made, guest = join_guest(rid)
    owner = users.owner_user()
    users.set_guest_name(guest["guest_id"], owner["name"])
    name = users.guest(guest["guest_id"])["display_name"]
    assert name == owner["name"], "the guest could not take the owner's display name"

    thread = threads.create(rid, f"guest:{guest['guest_id']}", name, "guest", "guest posting as the owner")
    thread = threads.reply(rid, thread["id"], f"user:{owner['id']}", owner["name"], "owner", "owner replying")
    assert [c["author_kind"] for c in thread["comments"]] == ["guest", "member"]

    feed = threads.activity(rid)
    assert feed[0]["actor_kind"] == "member" and feed[0]["data"]["actor_kind"] == "member"
    assert feed[1]["actor_kind"] == "guest" and feed[1]["data"]["actor_kind"] == "guest"


@pytest.mark.browser
def test_a_guest_named_like_the_owner_is_marked_a_guest_on_the_board(env, server, chrome, tmp_path):
    rid = make_review(tmp_path, wake=False)
    made = users.share("test", rid, "create", "reviewer")
    guest = users.join_as_guest(users.resolve_share_token(token_of(made["path"])))
    owner = users.owner_user()
    users.set_guest_name(guest["guest_id"], owner["name"])
    b = Board(chrome, "about:blank", wait="true", width=1440, height=900)
    b.cdp.call("Network.enable", session=b.sid)
    for key, value in [("mr_session", guest["session"]), ("mr_csrf", guest["csrf"])]:
        b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                   session=b.sid)
    b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
    assert b.until(BOARD_WAIT, timeout=60)
    time.sleep(0.4)
    on(b)
    open_new_pin(b)
    save_new_pin(b, "Guest speaking as the owner")
    thread = threads.list_threads(rid)[0]
    assert thread["comments"][0]["author_name"] == owner["name"]

    threads.reply(rid, thread["id"], f"user:{owner['id']}", owner["name"], "owner", "The owner's own reply")
    b.cdp.call("Page.reload", session=b.sid)
    assert b.until(BOARD_WAIT, timeout=60)
    on(b)
    wait_pin(b, thread["id"])
    b.press('.mpin[data-thread="%s"]' % thread["id"])
    assert b.until("document.querySelectorAll('.tpop .tcomment').length === 2", timeout=15)
    rows = json.loads(b.js(
        "JSON.stringify(Array.prototype.map.call(document.querySelectorAll('.tpop .tcomment'),"
        "function(r){return r.querySelector('.guest-mark') ? r.querySelector('.cname').textContent + '|guest'"
        " : r.querySelector('.cname').textContent + '|none'}))"))
    assert rows == [owner["name"] + "|guest", owner["name"] + "|none"], rows
    assert b.js("document.querySelector('.tpop .tcomment .guest-mark').textContent") == "guest"
    assert b.js("document.querySelector('.tpop .tcomment .guest-mark').getAttribute('aria-label')") == "Guest"
    assert b.js("document.querySelectorAll('.tpop .tp-title .guest-mark').length") == 1, \
        "the thread header does not mark its guest author"
    assert b.js("document.querySelectorAll('.tpop .tcomment .guest-mark').length") == 1, \
        "only the guest comment carries the marker"

    b.press(".tactivity")
    assert b.until("!!document.querySelector('.feed .trow')", timeout=10)
    assert b.js("document.querySelectorAll('.feed .trow .guest-mark').length") == 1, \
        "the activity feed does not mark the guest's own row"


@pytest.mark.browser
def test_the_guests_own_live_event_enters_the_feed_before_the_snapshot_lands(env, server, chrome, tmp_path):
    """The stream replays the guest's own thread.created before any snapshot: that row must be in the feed (it is the
    only one that carries the guest mark) and it is the reader's own, so it counts no unread."""
    rid = make_review(tmp_path, wake=False)
    _made, guest = join_guest(rid)
    name = users.guest(guest["guest_id"])["display_name"]
    threads.create(rid, f"guest:{guest['guest_id']}", name, "guest", "The guest's own first comment")
    b = Board(chrome, "about:blank", wait="true", width=1440, height=900)
    b.cdp.call("Network.enable", session=b.sid)
    for key, value in [("mr_session", guest["session"]), ("mr_csrf", guest["csrf"])]:
        b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                   session=b.sid)
    b.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOLD_SNAPSHOT}, session=b.sid)
    b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
    assert b.until(BOARD_WAIT, timeout=60)
    assert b.until("!!document.querySelector('.tactivity')", timeout=15)
    b.press(".tactivity")
    assert b.until("!!document.querySelector('.feed .trow')", timeout=15), \
        "the guest's own live event never reached the feed"
    assert b.js("document.querySelectorAll('.feed .trow').length") == 1
    assert b.js("document.querySelectorAll('.feed .trow .guest-mark').length") == 1, \
        "the feed row of the guest's own event does not carry the guest mark"
    assert b.js("document.querySelectorAll('.feed .trow.unrow').length") == 0, \
        "the reader's own event must not be unread"
    assert not b.js("document.querySelector('.tactivity').classList.contains('has-unread')"), \
        "the reader's own event must not count as unread activity"


@pytest.mark.browser
def test_guest_board_shows_the_name_and_a_rename_persists(env, server, chrome, tmp_path):
    rid = make_review(tmp_path, wake=True)
    made = users.share("test", rid, "create", "reviewer")
    guest = users.join_as_guest(users.resolve_share_token(token_of(made["path"])))
    gid = guest["guest_id"]
    b = Board(chrome, "about:blank", wait="true", width=1440, height=900)
    try:
        b.cdp.call("Network.enable", session=b.sid)
        for key, value in [("mr_session", guest["session"]), ("mr_csrf", guest["csrf"])]:
            assert b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                              session=b.sid)["success"]
        b.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": BLOCK_GUEST_NAME_SAVE}, session=b.sid)
        b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
        assert b.until(BOARD_WAIT, timeout=60)
        time.sleep(0.4)

        me = board_me(b)
        assert me["kind"] == "guest", me
        name = me["name"]
        assert NAME_RE.match(name), f"the board's `me` carries no server-assigned name: {name!r}"
        assert users.guest(gid)["display_name"] == name, \
            f"`me` and the stored guest name disagree: {name!r} != {users.guest(gid)['display_name']!r}"

        seen = json.loads(b.js("JSON.stringify(Array.prototype.map.call("
                               "document.querySelectorAll('.tavs .av'),function(n){return n.textContent}))"))
        assert initials(name) in seen, f"the presence stack does not show {name!r}: {seen}"
        title = b.js("(document.querySelector('.av-self')||{}).title||''")
        assert title.startswith(name + " \u00b7 guest"), f"the identity pill does not show {name!r}: {title!r}"
        b.js("window.fetch=window.__origFetch")

        b.cdp.call("Emulation.setDeviceMetricsOverride",
                   {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True}, session=b.sid)
        assert b.until("!!document.querySelector('.foot-comment')", timeout=15), "the 390 layout never rendered"
        assert b.until("document.querySelectorAll('.tavs .av').length > 0", timeout=15), \
            "the 390 board never painted the presence stack"
        seen = json.loads(b.js("JSON.stringify(Array.prototype.map.call("
                               "document.querySelectorAll('.tavs .av'),function(n){return n.textContent}))"))
        assert initials(name) in seen, f"the 390 presence stack does not show {name!r}: {seen}"

        at_width(b, DESKTOP)
        # No popover is open here, so the 1440 width change does re-render. #comment-toggle exists at every width,
        # while a foot toggle exists only in the <=600 render: its absence plus the decision content (which arrives
        # after the render's own asset fetch) means that render has finished and no queued one can close the menu
        # between the menu poll and the rename write below.
        assert b.until("!document.querySelector('.foot-comment') && " + BOARD_WAIT, timeout=15), \
            "the desktop render never landed"

        on(b)
        open_new_pin(b)
        save_new_pin(b, "Hello from the guest")
        thread = threads.list_threads(rid)[0]
        assert thread["comments"][0]["author_name"] == name, thread["comments"][0]
        b.press('.mpin[data-thread="%s"]' % thread["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        assert comment_names(b)[0] == name, f"the comment author reads {comment_names(b)[0]!r}, not {name!r}"
        b.press(".tpop .tp-close")

        b.press(".more-open")
        assert b.until("!!document.querySelector('.menu-input')", timeout=10)
        # Resolve and write in one page task: a render between a lookup and a later write would leave a null here,
        # and a menu that is open when this runs is the mounted one.
        assert b.js("(function(i){if(!i)return false;i.value='Sunny Badger';"
                    "i.dispatchEvent(new Event('input',{bubbles:true}));return true})"
                    "(document.querySelector('.menu-input'))"), "the rename input was not open"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and users.guest(gid)["display_name"] != "Sunny Badger":
            time.sleep(0.1)
        assert users.guest(gid)["display_name"] == "Sunny Badger", \
            "the rename POST never reached the guest row"
        b.js("document.body.click()")

        b.press('.mpin[data-thread="%s"]' % thread["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        b.js("(function(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);return 1})"
             "(document.querySelector('.tpop .tp-input'))")
        b.cdp.call("Input.insertText", {"text": "After the rename"}, session=b.sid)
        b.press(".tpop .tp-send")
        assert b.until("document.querySelectorAll('.tpop .cname').length === 2", timeout=15)
        assert comment_names(b)[-1] == "Sunny Badger", \
            f"the reply posted after the rename reads {comment_names(b)[-1]!r}"
        assert threads.list_threads(rid)[0]["comments"][-1]["author_name"] == "Sunny Badger"

        b.js("localStorage.clear()")
        b.cdp.call("Page.reload", session=b.sid)
        assert b.until(BOARD_WAIT, timeout=60)
        assert b.until("(document.querySelector('.av-self')||{}).title === 'Sunny Badger \u00b7 guest'", timeout=15), \
            "after a reload the pill reads %r" % b.js('(document.querySelector(".av-self")||{}).title')
        on(b)
        wait_pin(b, thread["id"])
        b.press('.mpin[data-thread="%s"]' % thread["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        assert comment_names(b)[-1] == "Sunny Badger", \
            "after a reload the comment author does not read the new name"
        b.press(".tpop .tp-close")

        b.cdp.call("Emulation.setDeviceMetricsOverride",
                   {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True}, session=b.sid)
        assert b.until("!!document.querySelector('.foot-comment')", timeout=15), "the 390 layout never returned"
        assert b.js("document.querySelector('.foot-comment').getAttribute('aria-pressed')") == "true", \
            "the 390 layout lost comment mode"
        wait_pin(b, thread["id"])
        b.press('.mpin[data-thread="%s"]' % thread["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        assert comment_names(b)[-1] == "Sunny Badger", \
            "the 390 comment author does not read the new name"
        b.js("document.body.click()")
        b.press(".more-open")
        assert b.until("!!document.querySelector('.menu-input')", timeout=10)
        assert b.js("document.querySelector('.menu-input').value") == "Sunny Badger", \
            "the 390 name pill does not show the new name"
    finally:
        b.js("localStorage.clear()")


@pytest.mark.browser
def test_two_guests_are_both_in_the_presence_stack(env, server, chrome, tmp_path):
    rid = make_review(tmp_path, wake=True)
    _made, one = join_guest(rid)
    _made, two = join_guest(rid)
    names = [users.guest(one["guest_id"])["display_name"], users.guest(two["guest_id"])["display_name"]]
    b = Board(chrome, "about:blank", wait="true", width=1440, height=900)
    second = None
    try:
        b.cdp.call("Network.enable", session=b.sid)
        for key, value in [("mr_session", one["session"]), ("mr_csrf", one["csrf"])]:
            assert b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                              session=b.sid)["success"]
        b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
        assert b.until(BOARD_WAIT, timeout=60)
        assert b.until("!!document.querySelector('.tavs .av')", timeout=15), "the first guest never showed up"
        me = board_me(b)
        assert me["kind"] == "guest" and me["name"] == names[0], me

        # The second guest joins from a second tab of the same Chrome, with its own session cookie. Board does not
        # expose its targetId, so the tab is opened with raw CDP and closed by that id in `finally`.
        second = chrome.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid2 = chrome.call("Target.attachToTarget", {"targetId": second, "flatten": True})["sessionId"]
        for method in ("Page.enable", "Runtime.enable", "Network.enable"):
            chrome.call(method, session=sid2)
        for key, value in [("mr_session", two["session"]), ("mr_csrf", two["csrf"])]:
            assert chrome.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                               session=sid2)["success"]
        chrome.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=sid2)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not chrome.evaluate(BOARD_WAIT, session=sid2):
            chrome.pump(0.1)
        assert chrome.evaluate(BOARD_WAIT, session=sid2), "the second guest's board never loaded"

        seen, want = wait_two_guests(b, names)
        assert len(seen) == 2, f"the presence stack does not hold both guests: {seen}"
        assert sorted(seen) == want, f"the presence stack shows {seen}, not {want}"
        there = b.js("(document.querySelector('.tacts .there')||{}).textContent")
        assert there == "2 here", f"the presence count reads {there!r}"

        at_width(b, MOBILE)
        seen, want = wait_two_guests(b, names)
        assert len(seen) == 2 and sorted(seen) == want, f"the 390 presence stack shows {seen}, not {want}"
        there = b.js("(document.querySelector('.tacts .there')||{}).textContent")
        assert there == "2 here", f"the 390 presence count reads {there!r}"
        at_width(b, DESKTOP)
    finally:
        if second is not None:
            try:
                chrome.call("Target.closeTarget", {"targetId": second})
            except Exception:
                pass
        b.js("localStorage.clear()")


# ---------------------------------------------------------------- B. a wake-off board


def test_wake_off_owner_mention_never_activates(env, server, tmp_path):
    rid = make_review(tmp_path, wake=False)
    code, _headers, body = http("POST", f"{server}/r/{rid}/api/threads",
                               {"text": "@Hermes change the copy", "mentions": ["hermes"]})
    assert code == 201, body
    thread = json.loads(body)["thread"]
    assert thread["activated"] is False, "a wake-off board activated @Hermes"
    row = db.connect().execute("SELECT activated FROM threads WHERE id = ?", (thread["id"],)).fetchone()
    assert int(row["activated"]) == 0, "the stored thread row says activated"


@pytest.mark.browser
def test_wake_off_board_picker_never_offers_hermes(env, server, chrome, tmp_path):
    rid = make_review(tmp_path, wake=False)
    b = Board(chrome, f"{server}/r/{rid}/", wait=BOARD_WAIT, width=1440, height=900)
    try:
        on(b)
        open_new_pin(b)
        type_at(b, ".popover textarea")
        names = mention_names(b)
        assert "Hermes" not in names, f"a wake-off board offered Hermes in the @ picker: {names}"
        assert not b.js("!!document.querySelector('.popover .hermes-box')"), \
            "a wake-off board got the Send-to-Hermes box"

        comment_off(b)
        at_width(b, MOBILE)
        on(b, 390)
        open_new_pin(b)
        assert b.js("!!document.querySelector('.popover textarea')"), "the 390 new-pin composer never rendered"
        type_at(b, ".popover textarea")
        names = mention_picker(b)
        assert "Hermes" not in names, f"a 390 wake-off board offered Hermes in the @ picker: {names}"
        assert not b.js("!!document.querySelector('.popover .hermes-box')"), \
            "a 390 wake-off board got the Send-to-Hermes box"
        at_width(b, DESKTOP)
    finally:
        b.js("localStorage.clear()")


@pytest.mark.browser
def test_wake_on_board_picker_still_offers_hermes(env, server, chrome, tmp_path):
    rid = make_review(tmp_path, wake=True)
    b = Board(chrome, f"{server}/r/{rid}/", wait=BOARD_WAIT, width=1440, height=900)
    try:
        on(b)
        open_new_pin(b)
        type_at(b, ".popover textarea")
        names = mention_names(b)
        assert "Hermes" in names, f"a wake-on board stopped offering Hermes: {names}"
        assert b.js("!!document.querySelector('.popover .hermes-box')"), \
            "a wake-on board lost the Send-to-Hermes box"

        comment_off(b)
        at_width(b, MOBILE)
        on(b, 390)
        open_new_pin(b)
        assert b.js("!!document.querySelector('.popover textarea')"), "the 390 new-pin composer never rendered"
        type_at(b, ".popover textarea")
        names = mention_picker(b)
        assert "Hermes" in names, f"a 390 wake-on board stopped offering Hermes: {names}"
        assert b.js("!!document.querySelector('.popover .hermes-box')"), \
            "a 390 wake-on board lost the Send-to-Hermes box"
        at_width(b, DESKTOP)
    finally:
        b.js("localStorage.clear()")


# ---------------------------------------------------------------- C. a share-link guest


@pytest.mark.browser
def test_share_guest_reviewer_has_no_hermes_surface(env, server, chrome, tmp_path, monkeypatch):
    rid = make_review(tmp_path, wake=True)
    made = users.share("test", rid, "create", "reviewer")
    token = token_of(made["path"])
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    b = Board(chrome, f"{server}/s/{token}", wait=BOARD_WAIT, width=1440, height=900)
    try:
        on(b)
        open_new_pin(b)
        type_at(b, ".popover textarea")
        names = mention_names(b)
        assert "Hermes" not in names, f"a share-link guest was offered Hermes in the new-pin picker: {names}"
        assert not b.js("!!document.querySelector('.popover .hermes-box')"), \
            "a share-link guest got the new-pin Send-to-Hermes box"
        b.js("document.querySelector('.popover textarea').blur()")
        time.sleep(0.3)
        save_new_pin(b, "Guest note")
        thread = threads.list_threads(rid)[0]
        b.press('.mpin[data-thread="%s"]' % thread["id"])
        assert b.until("!!document.querySelector('.tpop textarea')")
        assert not b.js("!!document.querySelector('.tpop .hermes-box')"), \
            "a share-link guest got the reply Send-to-Hermes box"
        type_at(b, ".tpop .tp-input")
        names = mention_names(b)
        assert "Hermes" not in names, f"a share-link guest was offered Hermes in the reply picker: {names}"

        b.press(".tpop .tp-close")
        assert b.until("!document.querySelector('.tpop')", timeout=10), "the reply composer did not close"
        comment_off(b)
        at_width(b, MOBILE)
        on(b, 390)
        open_new_pin_at_corner(b)
        assert b.js("!!document.querySelector('.popover textarea')"), "the 390 new-pin composer never rendered"
        type_at(b, ".popover textarea")
        names = mention_picker(b)
        assert "Hermes" not in names, f"a 390 share-link guest was offered Hermes in the new-pin picker: {names}"
        assert not b.js("!!document.querySelector('.popover .hermes-box')"), \
            "a 390 share-link guest got the new-pin Send-to-Hermes box"
        b.press(".popover .pop-close")
        assert b.until("!document.querySelector('.popover')", timeout=10), \
            "the 390 new-pin composer did not close"
        wait_pin(b, thread["id"])
        b.press('.mpin[data-thread="%s"]' % thread["id"])
        assert b.until("!!document.querySelector('.tpop textarea')", timeout=15), \
            "the 390 reply composer never rendered"
        assert not b.js("!!document.querySelector('.tpop .hermes-box')"), \
            "a 390 share-link guest got the reply Send-to-Hermes box"
        type_at(b, ".tpop .tp-input")
        names = mention_picker(b)
        assert "Hermes" not in names, f"a 390 share-link guest was offered Hermes in the reply picker: {names}"
        b.press(".tpop .tp-close")
        at_width(b, DESKTOP)
    finally:
        b.js("localStorage.clear()")


def test_share_guest_reviewer_mention_never_activates(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid, "reviewer")
    code, _headers, body = http("POST", f"{server}/r/{rid}/api/threads",
                               {"text": "@Hermes please build this", "mentions": ["hermes"]},
                               signed_in(guest))
    assert code == 201, body
    thread = json.loads(body)["thread"]
    assert thread["activated"] is False, "a share-link guest activated Hermes"
    comment = thread["comments"][0]
    assert comment["sent_to_hermes"] is False, "a share-link guest's comment was sent to Hermes"
    assert comment["mentions"] == [{"kind": "hermes", "target": "hermes"}], comment["mentions"]


def test_signed_in_reviewer_still_activates_hermes(env, server, review):
    rid = review["id"]
    dana = account(review_id=rid, role="reviewer", name="Dana")
    code, _headers, body = http("POST", f"{server}/r/{rid}/api/threads",
                               {"text": "@Hermes please build this", "mentions": ["hermes"]},
                               signed_in(dana))
    assert code == 201, body
    thread = json.loads(body)["thread"]
    assert thread["activated"] is True, "a signed-in reviewer stopped activating Hermes"
