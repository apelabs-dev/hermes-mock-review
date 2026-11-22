"""The Share dialog in a real browser: open it, read it, change the audience, close it with Esc.

Modeled on tests/test_live_round_tool.py's Chrome fixture and minimal page driver. Skips when no Chrome is installed.
"""

import base64
import json
import os
import time
from pathlib import Path

import pytest

import browserkit as bc
from authkit import account, token_of
from mockreview import db, store, users

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}


def new_review(tmp_path, name, kind):
    """A review whose newest round uses the modern renderer (prototype or decisions)."""
    if kind == "prototype":
        src = tmp_path / name / "index.html"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("<!doctype html><html><body><h1>prototype</h1></body></html>", encoding="utf-8")
        spec = store.parse_round_spec(prototype=str(src))
    else:
        src = tmp_path / name / "decisions.json"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(json.dumps({"blocks": [{"type": "mock", "id": "m1", "html": "<p>hello</p>"}]}),
                       encoding="utf-8")
        spec = store.parse_round_spec(decisions=str(src))
    return store.create_review(name, "", ORIGIN, "tailscale", spec)


@pytest.fixture
def chrome():
    try:
        bc.find_browser()
    except bc.BrowserUnavailable:
        pytest.skip("no Chrome/Chromium/Edge installed")
    for attempt in (1, 2):
        browser = bc.Chrome(startup=60)
        try:
            host, port, path = browser.__enter__()
            break
        except bc.BrowserCheckError:
            browser.__exit__(None, None, None)
            if attempt == 2:
                raise
    try:
        ws = bc.WebSocket(host, port, path)
        try:
            yield bc.Cdp(ws, bc.Observer())
        finally:
            ws.close()
    finally:
        browser.__exit__(None, None, None)


class Board:
    def __init__(self, cdp, url, wait=None, viewport=(1280, 900)):
        self.cdp = cdp
        tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        for m in ("Page.enable", "Runtime.enable"):
            cdp.call(m, session=self.sid)
        width, height = viewport
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1,
                                                        "mobile": False}, session=self.sid)
        cdp.call("Page.navigate", {"url": url}, session=self.sid)
        assert self.until(wait or "!!document.querySelector('.share-open')", timeout=60), "the board never loaded"

    def js(self, expr):
        return self.cdp.evaluate(expr, session=self.sid)

    def until(self, expr, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = self.js(expr)
            if got:
                return got
            self.cdp.pump(0.1)
        return None

    def press(self, sel):
        assert self.js("(function(e){if(!e)return false;e.click();return true})(document.querySelector(%s))"
                       % json.dumps(sel)), f"{sel} is not on the page"

    def key(self, key, code=None, vk=None):
        down = {"type": "keyDown", "key": key}
        if code:
            down["code"] = code
        if vk:
            down["windowsVirtualKeyCode"] = vk
        self.cdp.call("Input.dispatchKeyEvent", down, session=self.sid)
        up = {"type": "keyUp", "key": key}
        if code:
            up["code"] = code
        if vk:
            up["windowsVirtualKeyCode"] = vk
        self.cdp.call("Input.dispatchKeyEvent", up, session=self.sid)


def test_share_dialog_opens_reads_and_changes_the_audience(server, review, chrome):
    rid = review["id"]
    users.share("test", rid, "create")
    b = Board(chrome, f"{server}/r/{rid}/")

    b.press(".share-open")
    assert b.until("!!document.querySelector('.share-dialog .share-invite-input')"), "the dialog must open"
    assert b.js("document.querySelector('.share-dialog .share-title').textContent").startswith("Share ")
    assert b.js("(function(){var s=document.querySelectorAll('.share-sec');"
                "return Array.prototype.map.call(s,function(e){return e.textContent}).join('|')})()") == "People with access|Link"
    assert b.js("!!document.querySelector('.share-dialog .share-audience')")
    assert b.js("!!document.querySelector('.share-dialog .share-guest-role')")
    assert b.js("!!document.querySelector('.share-dialog .share-round')")
    assert "Live: 1 of 25 seats" in b.js("document.querySelector('.share-dialog .share-seats').textContent")
    assert b.js("document.activeElement === document.querySelector('.share-dialog .share-invite-input')"), \
        "the invite input takes focus on open"

    b.js("(function(){var s=document.querySelector('.share-dialog .share-audience');"
         "s.value='members';s.dispatchEvent(new Event('change',{bubbles:true}));return 1})()")
    end = time.monotonic() + 10
    while time.monotonic() < end and (users.share_link(rid) or {}).get("audience") != "members":
        time.sleep(0.1)
    assert users.share_link(rid)["audience"] == "members"

    b.key("Escape", "Escape", 27)
    assert b.until("!document.querySelector('.share-dialog')"), "Esc must close the dialog"


@pytest.mark.parametrize("kind", ["prototype", "decisions"])
def test_share_opens_on_the_modern_renderer(env, server, tmp_path, chrome, kind):
    data = new_review(tmp_path, kind, kind)
    b = Board(chrome, f"{server}/r/{data['id']}/", wait="!!document.querySelector('#dzbar')")
    assert b.js("!!document.querySelector('#dzbar .share-open')"), "the modern bar must offer Share"
    b.press("#dzbar .share-open")
    assert b.until("!!document.querySelector('.share-dialog .share-invite-input')"), "the dialog must open"
    b.key("Escape", "Escape", 27)
    assert b.until("!document.querySelector('.share-dialog')"), "Esc must close the dialog"


def test_pinned_notice_shows_in_both_renderers(env, server, review, tmp_path, chrome, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    token = token_of(users.share("test", review["id"], "create", "viewer")["path"])
    users.share("test", review["id"], "update", round="1")
    legacy = Board(chrome, f"{server}/s/{token}", wait="!!document.querySelector('.banner')")
    text = legacy.js("(function(){return Array.prototype.map.call(document.querySelectorAll('.banner'),"
                     "function(e){return e.textContent}).join(' ')})()")
    assert "This link shows round 1." in text

    modern = new_review(tmp_path, "pinned", "prototype")
    store.add_round(modern["id"], store.parse_round_spec(prototype=str(tmp_path / "pinned" / "index.html")))
    token = token_of(users.share("test", modern["id"], "create", "viewer")["path"])
    users.share("test", modern["id"], "update", round="1")
    b = Board(chrome, f"{server}/s/{token}", wait="!!document.querySelector('#dzbar')")
    text = b.js("(function(){return Array.prototype.map.call(document.querySelectorAll('.banner'),"
                "function(e){return e.textContent}).join(' ')})()")
    assert "This link shows round 1." in text


def pick(b, selector, value):
    b.js("(function(){var s=document.querySelector(%s);s.value=%s;"
         "s.dispatchEvent(new Event('change',{bubbles:true}));return 1})()"
         % (json.dumps(selector), json.dumps(value)))


def shot(b, name):
    dest = os.environ.get("MR_SHARE_EVIDENCE")
    if dest:
        result = b.cdp.call("Page.captureScreenshot", {"format": "png"}, session=b.sid)
        (Path(dest) / name).write_bytes(base64.b64decode(result["data"]))


def test_invite_permissions_are_independent_of_the_guest_link(env, server, review, chrome):
    rid = review["id"]
    b = Board(chrome, f"{server}/r/{rid}/")
    b.press(".share-open")
    assert b.until("!!document.querySelector('.share-dialog .share-invite-role')"), "the invite controls must exist"
    assert b.js("document.querySelector('.share-invite-role').value") == "commenter"
    assert b.js("document.querySelector('.share-invite-expiry').value") == "never"

    pick(b, ".share-invite-role", "viewer")
    pick(b, ".share-invite-expiry", "24h")
    b.js("(function(){var i=document.querySelector('.share-invite-input');i.value='Maya Kim';return 1})()")
    b.press(".share-invite-btn")
    end = time.monotonic() + 10
    while time.monotonic() < end:
        row = db.connect().execute("SELECT * FROM invites ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            break
        time.sleep(0.1)
    assert row is not None and row["review_id"] == rid
    assert row["review_role"] == "viewer"
    assert abs(row["grant_expires_at"] - users.now() - 86400) < 120

    b.key("Escape", "Escape", 27)
    assert b.until("!document.querySelector('.share-dialog')")
    users.share("test", rid, "create", "viewer", expires="24h")
    b.press(".share-open")
    assert b.until("!!document.querySelector('.share-dialog .share-guest-role')")
    assert b.js("document.querySelector('.share-guest-role').value") == "viewer"
    assert b.js("document.querySelector('.share-expiry').value") == "24h"
    assert b.js("document.querySelector('.share-invite-role').value") == "commenter"
    assert b.js("document.querySelector('.share-invite-expiry').value") == "never"
    assert b.js("document.querySelector('.share-invite-role') !== document.querySelector('.share-guest-role')")
    assert b.js("document.querySelector('.share-invite-expiry') !== document.querySelector('.share-expiry')")

    pick(b, ".share-invite-role", "reviewer")
    assert b.until("!!document.querySelector('.role-confirm-ok')"), "Reviewer asks before it is given"
    b.press(".role-confirm-ok")
    pick(b, ".share-invite-expiry", "7d")
    pick(b, ".share-guest-role", "commenter")
    end = time.monotonic() + 10
    while time.monotonic() < end and users.share_link(rid)["guest_role"] != "commenter":
        time.sleep(0.1)
    assert users.share_link(rid)["guest_role"] == "commenter"
    assert b.until("document.querySelector('.share-invite-role') && "
                   "document.querySelector('.share-invite-role').value === 'reviewer'")
    assert b.js("document.querySelector('.share-invite-expiry').value") == "7d"
    assert b.js("document.querySelector('.share-guest-role').value") == "commenter"

    pick(b, ".share-expiry", "7d")
    end = time.monotonic() + 10
    while time.monotonic() < end:
        link = users.share_link(rid)
        if link["expires_at"] and link["expires_at"] - users.now() > 6 * 86400:
            break
        time.sleep(0.1)
    assert users.share_link(rid)["expires_at"] - users.now() > 6 * 86400
    assert b.until("document.querySelector('.share-expiry') && document.querySelector('.share-expiry').value === '7d'")
    assert b.js("document.querySelector('.share-invite-role').value") == "reviewer"
    assert b.js("document.querySelector('.share-invite-expiry').value") == "7d"

    pick(b, ".share-invite-role", "viewer")
    pick(b, ".share-invite-expiry", "24h")
    time.sleep(0.3)
    assert b.js("document.querySelector('.share-guest-role').value") == "commenter"
    assert b.js("document.querySelector('.share-expiry').value") == "7d"


def test_remove_only_drops_a_direct_grant_and_never_inherited_access(env, server, review, chrome):
    rid = review["id"]
    ivy = account(all_reviews=True, role="viewer", name="Ivy")
    bea = account(review_id=rid, role="commenter", name="Bea")
    users.grant("test", bea["user_id"], "viewer", all_reviews=True)

    b = Board(chrome, f"{server}/r/{rid}/")
    b.press(".share-open")
    assert b.until("!!document.querySelector('.share-person')"), "the people list must load"
    find = ("(function(name){return Array.prototype.find.call(document.querySelectorAll('.share-person'),"
            "function(r){return r.textContent.indexOf(name) >= 0;});})")

    def row(name):
        return f"{find}({json.dumps(name)})"

    assert b.js(f"!!{row('Ivy')} && {row('Ivy')}.querySelector('.share-remove') === null"), \
        "an inherited-only person must have no active Remove"
    assert b.js(f"!!{row('Bea')} && {row('Bea')}.querySelector('.share-remove') !== null")

    b.js(f"({row('Bea')}.querySelector('.share-remove').click(), 1)")
    end = time.monotonic() + 10
    while time.monotonic() < end:
        bea_row = [p for p in users.review_people(rid) if p["user_id"] == bea["user_id"]][0]
        if bea_row["direct_role"] is None:
            break
        time.sleep(0.1)
    assert bea_row["direct_role"] is None and bea_row["role"] == "viewer"

    assert b.until(f"!!{row('Bea')} && {row('Bea')}.querySelector('.share-person-role') && "
                   f"{row('Bea')}.querySelector('.share-person-role').value === 'viewer'"), \
        "the row must re-render with the inherited role"
    assert b.js(f"{row('Bea')}.querySelector('.share-remove') === null")
    assert b.js(f"!!{row('Ivy')} && {row('Ivy')}.querySelector('.share-person-role').value === 'viewer'")


def test_direct_override_removal_is_clear_without_hover(env, server, review, chrome):
    rid = review["id"]
    account(all_reviews=True, role="viewer", name="Ivy")
    bea = account(review_id=rid, role="commenter", name="Bea")
    users.grant("test", bea["user_id"], "viewer", all_reviews=True)
    cal = account(review_id=rid, role="viewer", name="Cal")

    b = Board(chrome, f"{server}/r/{rid}/", viewport=(390, 844))
    b.press(".share-open")
    assert b.until("!!document.querySelector('.share-person')"), "the people list must load"
    shot(b, "share-390-before.png")
    if os.environ.get("MR_SHARE_EVIDENCE"):
        wide = Board(chrome, f"{server}/r/{rid}/", viewport=(1280, 900))
        wide.press(".share-open")
        assert wide.until("!!document.querySelector('.share-person')"), "the people list must load at 1280"
        shot(wide, "share-1280-before.png")

    find = ("(function(name){return Array.prototype.find.call(document.querySelectorAll('.share-person'),"
            "function(r){return r.textContent.indexOf(name) >= 0;});})")

    def row(name):
        return f"{find}({json.dumps(name)})"

    def action_text(name):
        return b.js("(function(r){var a=r&&r.querySelector('.share-remove');return a?a.textContent:null;})(%s)"
                    % row(name))

    def action_fits(name):
        return b.js("(function(r){var a=r&&r.querySelector('.share-remove');if(!a)return false;"
                    "var x=a.getBoundingClientRect(),d=document.querySelector('.share-dialog').getBoundingClientRect();"
                    "return x.width>0&&x.height>0&&x.left>=0&&x.right<=window.innerWidth&&"
                    "x.left>=d.left-0.5&&x.right<=d.right+0.5&&x.top>=d.top-0.5&&x.bottom<=d.bottom+0.5;})(%s)"
                    % row(name))

    dialog_fits = ("(function(){var d=document.querySelector('.share-dialog'),x=d.getBoundingClientRect();"
                   "return x.left>=0&&x.right<=window.innerWidth&&x.top>=0&&x.bottom<=window.innerHeight"
                   "&&d.scrollWidth<=d.clientWidth+1;})()")

    def fits():
        # Deliberately scoped to the dialog: the board header still overflows a 390px document on this
        # branch (a separate header-overflow fix), which is a different surface and not this change.
        return b.js(dialog_fits)

    assert action_text("Bea") == "Clear override", "a direct role beside inherited access must say Clear override"
    assert action_fits("Bea"), "the Clear override action must be fully visible without hover"
    assert action_text("Cal") == "Remove", "a direct-only role keeps Remove"
    assert b.js(f"!!{row('Ivy')} && {row('Ivy')}.querySelector('.share-remove') === null"), \
        "an inherited-only person must not show an action"
    assert b.js(f"{row('Ivy')}.querySelector('.share-inherited').textContent") == "Inherited"
    assert fits(), "the dialog must fit 390x844 with no horizontal overflow"

    b.js(f"({row('Bea')}.querySelector('.share-remove').click(), 1)")
    end = time.monotonic() + 10
    while time.monotonic() < end:
        bea_row = [p for p in users.review_people(rid) if p["user_id"] == bea["user_id"]][0]
        if bea_row["direct_role"] is None:
            break
        time.sleep(0.1)
    assert bea_row["direct_role"] is None and bea_row["role"] == "viewer"

    assert b.until(f"!!{row('Bea')} && {row('Bea')}.querySelector('.share-person-role') && "
                   f"{row('Bea')}.querySelector('.share-person-role').value === 'viewer' && "
                   f"{row('Bea')}.querySelector('.share-remove') === null && "
                   f"!!{row('Bea')}.querySelector('.share-inherited')"), \
        "the row must re-render as inherited-only with no action"
    assert b.js(f"{row('Bea')}.querySelector('.share-inherited').textContent") == "Inherited"
    assert fits(), "the dialog must still fit after clearing the override"
    shot(b, "share-390-after-override.png")

    b.js(f"({row('Cal')}.querySelector('.share-remove').click(), 1)")
    end = time.monotonic() + 10
    while time.monotonic() < end and [p for p in users.review_people(rid) if p["user_id"] == cal["user_id"]]:
        time.sleep(0.1)
    assert not [p for p in users.review_people(rid) if p["user_id"] == cal["user_id"]]
    assert b.until(f"!{row('Cal')}"), "a direct-only person must leave the list when removed"
    assert fits(), "the dialog must still fit after removing a direct-only person"
    shot(b, "share-390-after-remove.png")


# ---------------------------------------------------------------- "Give Reviewer access?"

REVIEWER_TITLE = "Give Reviewer access?"
REVIEWER_BODY = ("People with the Reviewer role can wake Hermes with an @Hermes mention on boards that wake a messaging "
                 "chat. Hermes then runs in that chat with mock-review’s own tools. They can’t press Send or "
                 "Build.")
POSTS = ("window.__posts=[];(function(f){window.fetch=function(u,o){if(o&&o.method==='POST')"
         "window.__posts.push({url:String(u),body:JSON.parse(o.body||'null')});return f.apply(this,arguments)}})"
         "(window.fetch);1")
MODAL = ("JSON.stringify((function(){var d=document.querySelector('.role-confirm');if(!d)return null;"
         "var t=document.getElementById(d.getAttribute('aria-labelledby')),"
         "x=document.getElementById(d.getAttribute('aria-describedby')),r=d.getBoundingClientRect(),"
         "a=document.activeElement;return {role:d.getAttribute('role'),modal:d.getAttribute('aria-modal'),"
         "title:t&&t.textContent,body:x&&x.textContent,titleInside:!!t&&d.contains(t),"
         "buttons:Array.prototype.map.call(d.querySelectorAll('button'),function(e){return e.textContent}),"
         "focus:a&&a.className,left:r.left,right:r.right,top:r.top,bottom:r.bottom,w:innerWidth,h:innerHeight,"
         "fits:d.scrollWidth<=d.clientWidth+1,"
         "smallest:Math.min.apply(null,Array.prototype.map.call(d.querySelectorAll('button'),"
         "function(e){return e.getBoundingClientRect().height}))}})())")


def mouse(b, x, y):
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        p = {"type": kind, "x": x, "y": y, "button": "left" if kind != "mouseMoved" else "none", "clickCount": 1}
        b.cdp.call("Input.dispatchMouseEvent", p, session=b.sid)


def posts(b):
    return json.loads(b.js("JSON.stringify(window.__posts||[])"))


def select_of(field, name="Bea"):
    """A JS expression for the role select under test: the link's guests, Bea's row, or the new invite."""
    if field == "share-person-role":
        return ("Array.prototype.find.call(document.querySelectorAll('.share-person'),function(r){"
                "return r.textContent.indexOf(%s)>=0}).querySelector('.share-person-role')" % json.dumps(name))
    return "document.querySelector('.%s')" % field


def choose(b, sel, value):
    b.js("(function(s){s.value=%s;s.dispatchEvent(new Event('change',{bubbles:true}));return 1})(%s)"
         % (json.dumps(value), sel))


def server_role(rid, field, user_id):
    if field == "share-guest-role":
        return users.share_link(rid)["guest_role"]
    if field == "share-person-role":
        return [p for p in users.review_people(rid) if p["user_id"] == user_id][0]["direct_role"]
    return None


@pytest.mark.parametrize("viewport", [(1280, 900), (390, 844)], ids=["1280", "390"])
@pytest.mark.parametrize("field", ["share-guest-role", "share-person-role", "share-invite-role"])
def test_giving_reviewer_asks_first_and_cancel_keeps_the_old_role(env, server, review, chrome, field, viewport):
    rid = review["id"]
    users.share("test", rid, "create", "commenter")
    bea = account(review_id=rid, role="commenter", name="Bea")
    b = Board(chrome, f"{server}/r/{rid}/", viewport=viewport)
    b.press(".share-open")
    assert b.until("!!document.querySelector('.share-guest-role') && !!document.querySelector('.share-person-role')")
    sel = select_of(field)
    b.js(POSTS)

    def opened():
        choose(b, sel, "reviewer")
        assert b.until("!!document.querySelector('.role-confirm')"), "picking Reviewer must ask first"
        return json.loads(b.js(MODAL))

    def cancelled(how):
        assert b.until("!document.querySelector('.role-confirm')"), f"{how} must close the question"
        assert b.js(f"{sel}.value") == "commenter", f"{how} must put the old role back"
        assert b.js(f"document.activeElement === {sel}"), f"{how} must give focus back to the select"
        assert b.js("!!document.querySelector('.share-dialog')"), f"{how} must leave the Share dialog open"
        assert posts(b) == [], f"{how} must send nothing"
        assert server_role(rid, field, bea["user_id"]) in (None, "commenter")

    # The question: a labelled modal dialog, the exact copy, focus inside on Cancel, fully on screen.
    m = opened()
    assert m["role"] == "dialog" and m["modal"] == "true" and m["titleInside"], m
    assert m["title"] == REVIEWER_TITLE and m["body"] == REVIEWER_BODY, m
    assert m["buttons"] == ["Cancel", "Give Reviewer access"], m
    assert "role-confirm-cancel" in m["focus"], m
    assert m["left"] >= 0 and m["right"] <= m["w"] + 0.5 and m["top"] >= 0 and m["bottom"] <= m["h"] + 0.5, m
    assert m["fits"], m
    if viewport[0] < 640:
        assert m["smallest"] >= 44, "phone-width buttons are full-size tap targets"
    if field == "share-guest-role":
        shot(b, f"reviewer-modal-{viewport[0]}.png")
    b.key("Tab", "Tab", 9)
    assert "role-confirm-ok" in b.js("document.activeElement.className"), "Tab stays inside the question"
    b.key("Tab", "Tab", 9)
    assert "role-confirm-cancel" in b.js("document.activeElement.className"), "Tab stays inside the question"
    b.press(".role-confirm-cancel")
    cancelled("Cancel")

    opened()
    b.key("Escape", "Escape", 27)
    cancelled("Escape")

    opened()
    mouse(b, 6, 6)                                                       # the backdrop, outside the question
    cancelled("a click on the backdrop")

    # Roles below Reviewer never ask. A guest or person change re-renders the dialog: wait for the new select.
    b.js(f"window.__before = {sel}; 1")
    choose(b, sel, "viewer")
    time.sleep(0.3)
    assert not b.js("!!document.querySelector('.role-confirm')"), "Viewer must not ask"
    if field != "share-invite-role":
        assert b.until(f"!!({sel}) && {sel} !== window.__before && {sel}.value === 'viewer'"), "the change must land"
        assert server_role(rid, field, bea["user_id"]) == "viewer"

    # Confirm sends the change (an invite's role rides its Invite press).
    m = opened()
    assert m["title"] == REVIEWER_TITLE
    b.press(".role-confirm-ok")
    assert b.until("!document.querySelector('.role-confirm')")
    if field == "share-invite-role":
        assert b.js(f"{sel}.value") == "reviewer"
        b.js("(function(){var i=document.querySelector('.share-invite-input');i.value='Maya Kim';return 1})()")
        b.press(".share-invite-btn")
    end = time.monotonic() + 10
    want = {"share-guest-role": ("api/share", {"action": "update", "guest_role": "reviewer"}),
            "share-person-role": ("api/people", {"action": "grant", "user_id": bea["user_id"], "role": "reviewer"}),
            "share-invite-role": ("api/people", {"action": "invite", "name": "Maya Kim", "role": "reviewer",
                                                 "expires": None})}[field]
    while time.monotonic() < end and not any(p["url"].endswith(want[0]) and p["body"] == want[1]
                                             for p in posts(b)):
        b.cdp.pump(0.1)
    assert any(p["url"].endswith(want[0]) and p["body"] == want[1] for p in posts(b)), posts(b)
    while time.monotonic() < end:
        if field == "share-invite-role":
            row = db.connect().execute("SELECT * FROM invites ORDER BY id DESC LIMIT 1").fetchone()
            if row is not None and row["review_role"] == "reviewer":
                break
        elif server_role(rid, field, bea["user_id"]) == "reviewer":
            break
        time.sleep(0.1)
    if field == "share-invite-role":
        assert row is not None and row["review_role"] == "reviewer"
    else:
        assert server_role(rid, field, bea["user_id"]) == "reviewer"

    # From Reviewer down never asks either, and Escape closes the Share dialog again once the question is gone.
    assert b.until(f"!!({sel}) && {sel}.value === 'reviewer'")
    choose(b, select_of(field), "commenter")
    time.sleep(0.3)
    assert not b.js("!!document.querySelector('.role-confirm')"), "a lower role must not ask"
    b.key("Escape", "Escape", 27)
    assert b.until("!document.querySelector('.share-dialog')"), "Esc closes the Share dialog when nothing is on top"
