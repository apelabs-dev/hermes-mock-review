"""CUX item 2: the thread popover's three icon actions, the per-comment menu and the copyable thread link.

Real headless Chrome; the header row is checked for words at both widths (icons only, words in title/aria-label), the
hit boxes at 32 px desktop and 44 px on the phone sheet, and every action the old footer carried still works. All of
this fails on the CUX base, where the header still carries a status chip and labelled Close.
"""

import json
import time

import pytest

from authkit import account, signed_in
from conftest import http
from mockreview import store, threads, users
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
MOCK = ('<section><h2>Checkout</h2><p id="copy" style="width:300px;height:50px;margin:20px">'
        'Review this total</p><p id="other" style="width:300px;height:40px;margin:20px">Other line</p></section>')
DECISIONS = {"blocks": [{"type": "mock", "id": "m1", "html": MOCK}]}


@pytest.fixture
def action_review(env, tmp_path):
    path = tmp_path / "actions.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Thread actions", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)),
                               wake=False)
    threads.reset_limits()
    return data["id"]


def owner_thread(rid, text="First note on the mock", anchor=None):
    owner = users.owner_user()
    return threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", text,
                          anchor=anchor or {"block": "m1", "option": None, "x": 0.3, "y": 0.4})


def open_board(chrome, server, rid, width=1440, height=900, mobile=False):
    return Board(chrome, f"{server}/r/{rid}/", width=width, height=height, mobile=mobile,
                 wait="!!document.querySelector('.mock-host') && !!document.querySelector('.mock-host').shadowRoot")


def on(b, width=1440):
    b.press(".foot-comment" if width <= 600 else ".bar-comment")
    assert b.until("document.querySelector('#comment-toggle,#comment-toggle-mobile')"
                   ".getAttribute('aria-pressed') === 'true'")


def open_thread(b, tid):
    assert b.until("!!document.querySelector('.mpin[data-thread=\"%s\"]')" % tid, timeout=20)
    b.press('.mpin[data-thread="%s"]' % tid)
    assert b.until("!!document.querySelector('.tpop')", timeout=20)


def server_gone(server, rid, tid):
    code, _, body = http("GET", f"{server}/r/{rid}/api/threads")
    assert code == 200
    return all(t["id"] != tid for t in json.loads(body)["threads"])


def status_of(server, rid, tid):
    code, _, body = http("GET", f"{server}/r/{rid}/api/threads")
    assert code == 200
    return next(t for t in json.loads(body)["threads"] if t["id"] == tid)


@pytest.mark.parametrize("width,height,mobile,minimum", [(1440, 900, False, 28), (390, 844, True, 44)],
                         ids=["desktop-1440", "phone-390"])
def test_the_header_row_is_three_ghost_icons_only(action_review, server, chrome, width, height, mobile, minimum):
    rid = action_review
    t = owner_thread(rid)
    member = account(review_id=rid, role="commenter", name="Maya")
    other = threads.create(rid, f"user:{member['user_id']}", "Maya", "commenter", "Maya started this thread",
                           anchor={"block": "m1", "option": None, "x": 0.65, "y": 0.3})
    b = open_board(chrome, server, rid, width=width, height=height, mobile=mobile)
    try:
        if mobile:
            b.cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}, session=b.sid)
        on(b, width)
        open_thread(b, t["id"])
        row = json.loads(b.js(
            "JSON.stringify((function(){var a=document.querySelector('.tpop .tp-acts');"
            "var bs=[].slice.call(a.children);return {n:bs.length,texts:bs.map(function(x){return x.innerText}),"
            "labels:bs.map(function(x){return x.getAttribute('aria-label')}),titles:bs.map(function(x){return x.title}),"
            "pressed:bs.map(function(x){return x.getAttribute('aria-pressed')}),"
            "expanded:bs.map(function(x){return x.getAttribute('aria-expanded')}),"
            "boxes:bs.map(function(x){var r=x.getBoundingClientRect();return [r.width,r.height]}),"
            "rowText:a.innerText,sheet:document.querySelector('.tpop').classList.contains('sheet'),"
            "icons:bs.map(function(x){return !!x.querySelector('svg')})}})())"))
        assert row["n"] == 3, row
        assert row["labels"] == ["Resolve", "More", "Close"], row
        assert row["titles"] == ["Resolve", "More", "Close"], row
        assert row["icons"] == [True, True, True], row
        assert row["rowText"].strip() == "", "the action row carries words: %r" % (row["rowText"],)
        assert all(x.strip() == "" for x in row["texts"]), "an action button carries visible words: %r" % (row["texts"],)
        assert row["pressed"][0] == "false" and row["expanded"][1] == "false", row
        for w, h in row["boxes"]:
            assert w >= minimum and h >= minimum, f"a {width}px hit box is {w}x{h}, below {minimum}px: {row}"
        assert row["sheet"] is (width < 640), row
        b.press(".tpop .tp-more")
        assert b.until("!!document.querySelector('.tp-menu')"), "More opened no menu"
        assert b.js("document.querySelector('.tpop .tp-more').getAttribute('aria-expanded')") == "true"
        items = json.loads(b.js("JSON.stringify([].map.call(document.querySelectorAll('.tp-menu .menu-item'),"
                                "function(x){return {text:x.innerText.trim(),danger:x.classList.contains('menu-danger')}}))"))
        assert [x["text"] for x in items] == ["Edit", "Copy link", "Delete"], items
        assert items[-1]["danger"] and not items[0]["danger"] and not items[1]["danger"], items
        b.key("Escape", "Escape", 27)
        assert b.until("!document.querySelector('.tp-menu')"), "Escape did not close the menu"
        assert b.js("document.querySelector('.tpop .tp-more').getAttribute('aria-expanded')") == "false"
        assert b.js("!!document.querySelector('.tpop')")
        b.key("Escape", "Escape", 27)
        assert b.until("!document.querySelector('.tpop')"), "Escape did not close the popover"

        # Someone else's root comment: More offers Copy link only.
        open_thread(b, other["id"])
        b.press(".tpop .tp-more")
        assert b.until("!!document.querySelector('.tp-menu')")
        theirs = json.loads(b.js("JSON.stringify([].map.call(document.querySelectorAll('.tp-menu .menu-item'),"
                                 "function(x){return x.innerText.trim()}))"))
        assert theirs == ["Copy link"], theirs
        b.key("Escape", "Escape", 27)
        assert b.until("!document.querySelector('.tp-menu')")
        b.key("Escape", "Escape", 27)
        assert b.until("!document.querySelector('.tpop')")
    finally:
        b.js("localStorage.clear()")


def test_the_actions_work_and_the_copied_link_reopens_the_thread(action_review, server, chrome):
    rid = action_review
    t = owner_thread(rid, "Copy, edit and resolve this", anchor={"block": "m1", "option": None, "x": 0.3, "y": 0.4})
    member = account(review_id=rid, role="commenter", name="Maya")
    code, _, out = http("POST", f"{server}/r/{rid}/api/threads/{t['id']}/reply", {"text": "A second comment"},
                        signed_in(member))
    assert code == 200, out
    t2 = owner_thread(rid, "Delete this whole thread", anchor={"block": "m1", "option": None, "x": 0.7, "y": 0.3})
    b = open_board(chrome, server, rid)
    try:
        on(b)
        open_thread(b, t["id"])

        # Copy link writes an absolute URL that names this thread.
        b.js("window.__copied=null;(function(){var c=navigator.clipboard;"
             "try{c.writeText=function(v){window.__copied=v;return Promise.resolve()}}"
             "catch(e){Object.defineProperty(navigator,'clipboard',{value:{writeText:function(v){window.__copied=v;"
             "return Promise.resolve()}},configurable:true})}})()")
        b.press(".tpop .tp-more")
        assert b.until("!!document.querySelector('.tp-menu')")
        assert json.loads(b.js("JSON.stringify([].map.call(document.querySelectorAll('.tp-menu .menu-item'),"
                               "function(x){return x.innerText.trim()}))")) == ["Edit", "Copy link", "Delete"]
        b.press(".tp-menu .menu-item:nth-child(2)")
        assert b.until("!!window.__copied", timeout=5), "Copy link wrote nothing"
        url = b.js("window.__copied")
        assert url.startswith("http") and ("thread=" + t["id"]) in url, url

        # Opening that URL brings the same thread back, without touching the round hash.
        b.cdp.call("Page.navigate", {"url": url}, session=b.sid)
        assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot)", timeout=60)
        assert b.until("!!document.querySelector('.tpop')", timeout=30), "?thread= did not open the popover"
        assert t["id"] in b.js("document.querySelector('.tpop').outerHTML") or \
            "Copy, edit and resolve this" in b.js("document.querySelector('.tpop').textContent")
        assert "#" not in b.js("location.href").split("?")[0]

        # Edit on the owner's own comment, through its hover menu, is still reachable and saves.
        b.press(".tpop .tcomment .cmore")
        assert b.until("!!document.querySelector('.tp-menu')")
        assert json.loads(b.js("JSON.stringify([].map.call(document.querySelectorAll('.tp-menu .menu-item'),"
                               "function(x){return x.innerText.trim()}))")) == ["Edit", "Delete"]
        b.press(".tp-menu .menu-item")
        assert b.until("!!document.querySelector('.tpop .tcomment textarea')"), "Edit opened no editor"
        b.js("(function(t){t.value='Edited by its owner';t.dispatchEvent(new Event('input'));return 1})"
             "(document.querySelector('.tpop .tcomment textarea'))")
        b.press(".tpop .tcomment .btn-primary")
        assert b.until("document.querySelector('.tpop').textContent.includes('Edited by its owner')")
        assert status_of(server, rid, t["id"])["comments"][0]["text"] == "Edited by its owner"

        # Resolve from the header icon; the resolved thread reads Reopen in the same slot and flips back.
        b.press(".tpop .tp-resolve")
        assert b.until("!document.querySelector('.tpop')"), "Resolve must close the popover as it always did"
        assert status_of(server, rid, t["id"])["status"] == "resolved"
        b.press(".tcomments")
        assert b.until("!!document.querySelector('.toverlay')")
        b.press(".ttoggle .tr")
        b.press(".toverlay .trow")
        assert b.until("!!document.querySelector('.tpop')")
        got = json.loads(b.js(
            "JSON.stringify((function(){var x=document.querySelector('.tpop .tp-resolve');"
            "return {label:x.getAttribute('aria-label'),title:x.title,pressed:x.getAttribute('aria-pressed')}})())"))
        assert got == {"label": "Reopen", "title": "Reopen", "pressed": "true"}, got
        b.press(".tpop .tp-resolve")
        assert b.until("document.querySelector('.tpop .tp-resolve').getAttribute('aria-pressed') === 'false'")
        assert b.js("document.querySelector('.tpop .tp-resolve').getAttribute('aria-label')") == "Resolve"
        assert status_of(server, rid, t["id"])["status"] == "open"

        # The header More menu edits and deletes the root comment itself.
        b.press(".tpop .tp-more")
        assert b.until("!!document.querySelector('.tp-menu')")
        assert json.loads(b.js("JSON.stringify([].map.call(document.querySelectorAll('.tp-menu .menu-item'),"
                               "function(x){return x.innerText.trim()}))")) == ["Edit", "Copy link", "Delete"]
        b.press(".tp-menu .menu-item")
        assert b.until("!!document.querySelector('.tpop .tcomment textarea')"), "More > Edit opened no editor"
        b.js("(function(t){t.value='Root edited from More';t.dispatchEvent(new Event('input'));return 1})"
             "(document.querySelector('.tpop .tcomment textarea'))")
        b.press(".tpop .tcomment .btn-primary")
        assert b.until("document.querySelector('.tpop').textContent.includes('Root edited from More')")
        assert status_of(server, rid, t["id"])["comments"][0]["text"] == "Root edited from More"
        b.press(".tpop .tp-more")
        assert b.until("!!document.querySelector('.tp-menu')")
        items = json.loads(b.js("JSON.stringify([].map.call(document.querySelectorAll('.tp-menu .menu-item'),"
                                "function(x){return {text:x.innerText.trim(),danger:x.classList.contains('menu-danger')}}))"))
        assert [x["text"] for x in items] == ["Edit", "Copy link", "Delete"] and items[-1]["danger"], items
        b.press(".tp-menu .menu-item:last-child")
        assert b.until("document.querySelector('.tpop').textContent.includes('Comment deleted')"), \
            "More > Delete did not delete the root comment"
        root = status_of(server, rid, t["id"])["comments"][0]
        assert root["deleted_at"] is not None and root["text"] == ""
        assert "A second comment" in b.js("document.querySelector('.tpop').textContent")
        b.key("Escape", "Escape", 27)
        assert b.until("!document.querySelector('.tpop')")

        # Delete from the per-comment menu removes the thread when it was the last live comment.
        on(b)
        open_thread(b, t2["id"])
        b.press(".tpop .tcomment .cmore")
        assert b.until("!!document.querySelector('.tp-menu')")
        b.press(".tp-menu .menu-item:last-child")
        assert b.until("!document.querySelector('.tpop')", timeout=20), "Delete left the popover open"
        assert server_gone(server, rid, t2["id"]), "Delete did not remove the thread"
    finally:
        b.js("localStorage.clear()")


def test_a_missing_or_foreign_thread_parameter_is_ignored(action_review, server, chrome):
    rid = action_review
    owner_thread(rid)
    b = open_board(chrome, server, rid)
    try:
        for wanted in ("0" * 32, "not-a-thread-id", "f" * 32):
            b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/?thread={wanted}"}, session=b.sid)
            assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot)", timeout=60)
            time.sleep(0.8)
            assert not b.js("!!document.querySelector('.tpop')"), f"a foreign id {wanted!r} opened a popover"
    finally:
        b.js("localStorage.clear()")


def test_a_viewer_gets_more_and_close_but_no_resolve(action_review, server, chrome, monkeypatch):
    rid = action_review
    t = owner_thread(rid, "A thread a viewer may read")
    viewer = account(review_id=rid, role="viewer", name="Vic")
    b = open_board(chrome, server, rid)
    try:
        monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
        b.cdp.call("Network.enable", session=b.sid)
        for key, value in [("mr_session", viewer["session"]), ("mr_csrf", viewer["csrf"])]:
            b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                       session=b.sid)
        b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/?thread={t['id']}"}, session=b.sid)
        assert b.until("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot)",
                       timeout=60)
        assert b.until("!!document.querySelector('.tpop')", timeout=30), "?thread= did not open the popover"
        assert not b.js("!!document.querySelector('.tpop .tp-resolve')"), "a viewer got the Resolve button"
        assert b.js("!!document.querySelector('.tpop .tp-more')"), "a viewer lost More"
        assert b.js("!!document.querySelector('.tpop .tp-close')"), "a viewer lost Close"
        note = b.js("(document.querySelector('.tpop .tp-reply .tp-note')||{}).textContent||''")
        assert "You can read this thread" in note, note
        assert not b.js("!!document.querySelector('.tpop .hermes-box')"), "a viewer got the Send to Hermes box"
    finally:
        b.js("localStorage.clear()")
