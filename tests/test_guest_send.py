"""GUEST-2: Send and Build are the owner's alone; a non-owner's pin fix is stored and never wakes Hermes.

The board server refuses a share-link guest's or a member reviewer's Send with 403 (wakeguard.SUBMIT_OWNER_ONLY) and
stores their pin fix quiet (wakeguard.PIN_FIX_OWNER_ONLY). The store/wake seam runs against the scratch server (the
wake hook is conftest's Receiver, so `.requests`/`.wakes` must stay empty); the board half drives real headless
Chrome at 1440 and 390, because the footer is UI: a non-owner sees one plain line where Send and Build would be.
"""

import base64
import json
import os
import re
from pathlib import Path

import pytest

from authkit import account, signed_in, token_of
from conftest import FEEDBACK, http
from mockreview import api, service, store, users, wakeguard
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)
from test_plan_format import PLAN

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
OWNER_ONLY = "Only the owner sends this round to Hermes. Your comments and pins are on the board for them."
STORED_GUEST_COPY = "Saved by a share-link guest. Hermes was not pinged: tell it in chat."
WAKE_TEXT = re.compile(r"Hermes is on it|notified|Send to Hermes")
FOOTER_READY = "!!document.querySelector('.sb-owner-only, .btn-send, #gate-btn')"
# The owner-only line's footer (the docked #dzfoot/.dzfoot of a plan or prototype round, else the floating .sendbar):
# every other visible element in it that the line overlaps, and every visible summary chip with its natural width
# (a hidden max-content clone next to it) so a chip squeezed below its text shows up.
FOOTER_LAYOUT = """JSON.stringify((function(){
  function box(r){return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,w:r.width,h:r.height};}
  function seen(e){var r=e.getBoundingClientRect();
    return r.width>0&&r.height>0&&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});}
  function name(e){return e.tagName.toLowerCase()+(e.id?'#'+e.id:'')+
    (typeof e.className==='string'&&e.className.trim()?'.'+e.className.trim().split(/\\s+/).join('.'):'')+
    (e.textContent.trim()?' '+JSON.stringify(e.textContent.trim().slice(0,40)):'');}
  var line=document.querySelector('.sb-owner-only'),foot=line.closest('#dzfoot, .dzfoot')||line.closest('.sendbar'),
      l=line.getBoundingClientRect(),hits=[],chips=[];
  [].forEach.call(foot.querySelectorAll('*'),function(e){
    if(e===line||e.contains(line)||line.contains(e)||!seen(e))return;
    var r=e.getBoundingClientRect();
    if(r.left<l.right-0.5&&l.left<r.right-0.5&&r.top<l.bottom-0.5&&l.top<r.bottom-0.5)
      hits.push({el:name(e),rect:box(r)});
  });
  [].forEach.call(foot.querySelectorAll('.sb-summary > *'),function(e){
    if(!seen(e))return;
    var c=e.cloneNode(true);
    c.style.cssText+=';position:absolute;left:0;top:0;visibility:hidden;width:max-content;max-width:none;'+
                     'min-width:0;flex:none';
    e.parentNode.insertBefore(c,e.nextSibling);
    var natural=c.getBoundingClientRect().width;
    c.remove();
    chips.push({el:name(e),w:e.getBoundingClientRect().width,natural:natural,
                scrollWidth:e.scrollWidth,clientWidth:e.clientWidth});
  });
  return {foot:name(foot).split(' ')[0],line:box(l),hits:hits,chips:chips};
})())"""
TOASTS = ("window.__toasts=[];new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach("
          "function(n){if(n.classList&&n.classList.contains('toast'))window.__toasts.push("
          "{text:n.textContent,cls:n.className})})})}).observe(document.body,{childList:true});")

DECISIONS = {"blocks": [
    {"type": "decision", "id": "d1", "title": "Layout", "layout": "side", "options": [
        {"id": "a", "label": "Today", "html": "<div class='a'>A</div>"},
        {"id": "b", "label": "All", "recommended": True, "html": "<div class='b'>B</div>"}]},
    {"type": "mock", "id": "m1", "title": "Today", "html": "<p>today</p>"},
]}


def join_guest(rid, role="reviewer"):
    made = users.share("test", rid, "create", role)
    return made, users.join_as_guest(users.resolve_share_token(token_of(made["path"])))


def guest_submit(rid, guest, token, feedback=FEEDBACK):
    """A guest record as service.submit stores it, for a board where one exists (Send is no longer a guest's)."""
    return service.submit(rid, 1, feedback, token, source="local", no_wake_reason=service.GUEST_NO_WAKE,
                          author=f"guest:{guest['guest_id']}", guest=True)


def shot(b, name):
    """Evidence for the PR: written only when MR_SHARE_EVIDENCE names a directory (tests/test_board_share.py)."""
    dest = os.environ.get("MR_SHARE_EVIDENCE")
    if dest:
        result = b.cdp.call("Page.captureScreenshot", {"format": "png"}, session=b.sid)
        (Path(dest) / name).write_bytes(base64.b64decode(result["data"]))


def visit(b, url, sess=None, headers=None, ready=FOOTER_READY):
    """Open `url` as `sess` (None: the loopback owner), with `headers` added to every request the page makes."""
    b.cdp.call("Page.navigate", {"url": "about:blank"}, session=b.sid)
    assert b.until("location.href === 'about:blank'")
    b.cdp.call("Network.enable", session=b.sid)
    b.cdp.call("Network.clearBrowserCookies", session=b.sid)
    b.cdp.call("Network.setExtraHTTPHeaders", {"headers": headers or {}}, session=b.sid)
    if sess is not None:
        for key, value in [("mr_session", sess["session"]), ("mr_csrf", sess["csrf"])]:
            assert b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": url, "path": "/"},
                              session=b.sid)["success"]
    b.cdp.call("Page.navigate", {"url": url}, session=b.sid)
    assert b.until(ready, timeout=60), "the board footer never rendered"


@pytest.fixture
def pin_board(env, tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    return store.create_review("Guest pin board", "brief", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(path)), look={"pin_fix": True})


# ---------------------------------------------------------------- A. Send


def test_a_reviewer_guests_send_is_refused_and_nothing_is_stored(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    for amend in (False, True):
        code, _headers, out = http("POST", f"{server}/r/{rid}/api/submit", {**body, "amend": amend},
                                   signed_in(guest))
        assert code == 403, out
        assert json.loads(out)["error"] == wakeguard.SUBMIT_OWNER_ONLY

    rnd = store.load(rid)["rounds"][0]
    assert rnd["feedback"] is None and rnd.get("submission") is None
    assert service.retry_pending() == 0
    service.flush_pin_fixes(rid)
    assert env["receiver"].requests == []


# ---------------------------------------------------------------- B. pin fix


def test_a_reviewer_guests_pin_fix_is_stored_and_never_wakes(env, server, pin_board, monkeypatch):
    rid = pin_board["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": pin_board["token"], "pin": {"block": "m1", "text": "make it bigger"}}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/pin-fix", body, signed_in(guest))
    assert code == 200, out
    res = json.loads(out)
    assert res["ok"] is True and res["notified"] is False, res

    requests = store.load(rid)["rounds"][-1]["pin_requests"]
    assert len(requests) == 1
    req = next(iter(requests.values()))
    assert req["status"] == "queued" and req["text"] == "make it bigger"
    assert env["receiver"].wakes == []

    # The §3c regression: a queued request must not ride the worker tick's coalesce into a wake.
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    service.retry_pending()
    service.flush_pin_fixes(rid)
    assert env["receiver"].wakes == []
    assert res["detail"] == wakeguard.PIN_FIX_OWNER_ONLY, res
    assert store.load(rid)["rounds"][-1]["pin_requests"][req["id"]].get("quiet") is True


# ---------------------------------------------------------------- C. a member reviewer is not the owner either


def test_a_signed_in_reviewers_send_is_refused_and_the_owners_still_wakes(env, server, review):
    rid = review["id"]
    reviewer = account(review_id=rid, role="reviewer")
    body = {"round": 1, "token": review["token"], "feedback": FEEDBACK}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/submit", body, signed_in(reviewer))
    assert code == 403 and json.loads(out)["error"] == wakeguard.SUBMIT_OWNER_ONLY, out
    assert env["receiver"].requests == []

    code, _headers, out = http("POST", f"{server}/r/{rid}/api/submit", body)
    assert code == 200, out
    res = json.loads(out)
    assert res["notified"] is True and res["status"] == "notified", res
    assert len(env["receiver"].wakes) == 1
    assert len(env["receiver"].notices) == 1
    assert store.load(rid)["rounds"][0]["submission"]["status"] == "notified"


def test_a_signed_in_reviewers_pin_fix_is_stored_quiet(env, server, pin_board, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PIN_FIX_WINDOW_SECONDS", "0")
    rid = pin_board["id"]
    reviewer = account(review_id=rid, role="reviewer")
    body = {"round": 1, "token": pin_board["token"], "pin": {"block": "m1", "text": "gap"}}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/pin-fix", body, signed_in(reviewer))
    assert code == 200, out
    res = json.loads(out)
    assert res["notified"] is False and res["detail"] == wakeguard.PIN_FIX_OWNER_ONLY, res
    service.flush_pin_fixes(rid)
    service.retry_pending()
    assert env["receiver"].wakes == []
    req = next(iter(store.load(rid)["rounds"][-1]["pin_requests"].values()))
    assert req["status"] == "queued" and req["quiet"] is True


# ---------------------------------------------------------------- D. the footer


@pytest.mark.browser
@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)])
def test_only_the_owner_gets_send_and_build(env, review, server, tmp_path, chrome, width, height, mobile):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(PLAN), encoding="utf-8")
    plan = store.create_review("Plan", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)))
    page = tmp_path / "proto" / "index.html"
    page.parent.mkdir()
    page.write_text("<!doctype html><html><body><h1>prototype</h1></body></html>", encoding="utf-8")
    proto = store.create_review("Proto", "", ORIGIN, "local", store.parse_round_spec(prototype=str(page)))
    b = Board(chrome, "about:blank", wait="true", width=width, height=height, mobile=mobile)
    try:
        for kind, rid in (("variants", review["id"]), ("plan", plan["id"]), ("prototype", proto["id"])):
            people = {"guest reviewer": join_guest(rid)[1],
                      "member reviewer": account(review_id=rid, role="reviewer", name="Rae"),
                      "owner": None}
            for who, sess in people.items():
                visit(b, f"{server}/r/{rid}/", sess)
                if who == "owner":
                    assert b.js("!!document.querySelector('.btn-send, #gate-btn')"), (kind, who)
                    assert not b.js("!!document.querySelector('.sb-owner-only')"), (kind, who)
                    continue
                assert not b.js("!!document.querySelector('.btn-send, #gate-btn')"), \
                    f"{who} on the {kind} board still has Send or Build"
                line = json.loads(b.js(
                    "JSON.stringify((function(){var e=document.querySelector('.sb-owner-only'),"
                    "r=e.getBoundingClientRect();return {text:e.textContent,w:r.width,h:r.height,"
                    "left:r.left,right:r.right,top:r.top,bottom:r.bottom}})())"))
                assert line["text"] == OWNER_ONLY, (kind, who, line)
                assert line["w"] > 0 and line["h"] > 0, f"the owner-only line is hidden for {who} on {kind}"
                assert line["left"] >= 0 and line["right"] <= width + 0.5, (kind, who, line)
                assert line["top"] >= 0 and line["bottom"] <= height + 0.5, (kind, who, line)
                assert not WAKE_TEXT.search(b.js("document.body.innerText") or ""), (kind, who)
                assert b.js("(function(){var f=document.querySelector('#dzfoot, .sendbar');"
                            "return f.scrollWidth <= f.clientWidth + 1})()"), f"the footer overflows for {who} on {kind}"
                # Nothing else in the footer (summary chips, round control, Comment/pin button, any hittable control)
                # sits under or over the line, and no summary chip is squeezed below its own text.
                layout = json.loads(b.js(FOOTER_LAYOUT))
                where = f"{who} on the {kind} board at {width}"
                assert not layout["hits"], f"the owner-only line overlaps the footer for {where}: {layout}"
                for chip in layout["chips"]:
                    assert chip["scrollWidth"] <= chip["clientWidth"] + 1, \
                        f"a summary chip is clipped for {where}: {chip} in {layout}"
                    assert chip["w"] + 0.5 >= chip["natural"], \
                        f"a summary chip is narrower than its text for {where}: {chip} in {layout}"
                if who == "guest reviewer":
                    shot(b, f"footer-non-owner-{kind}-{width}.png")
        assert env["receiver"].requests == []
    finally:
        b.js("localStorage.clear()")


# ---------------------------------------------------------------- E. downstream paths


@pytest.mark.browser
@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)])
def test_the_owner_sees_a_guests_send_was_saved_and_never_pinged(env, review, server, chrome, width, height, mobile):
    rid = review["id"]
    _made, guest = join_guest(rid)
    assert guest_submit(rid, guest, review["token"])["status"] == "stored"

    b = Board(chrome, "about:blank", wait="true", width=width, height=height, mobile=mobile)
    try:
        b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
        assert b.until("!!document.querySelector('.banner')", timeout=60)
        assert b.until("!!document.querySelector('.sb-sub')", timeout=60)
        text = b.js("document.body.innerText") or ""
        assert not WAKE_TEXT.search(text), text
        assert b.js("document.querySelector('.banner').textContent") == STORED_GUEST_COPY
        assert b.js("document.querySelector('.sb-sub').textContent") == STORED_GUEST_COPY
        assert b.js("document.querySelector('.sb-title').textContent") == "Saved"
        assert env["receiver"].requests == []
    finally:
        b.js("localStorage.clear()")


@pytest.mark.browser
@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)])
def test_the_owners_send_through_funnel_says_why_it_was_saved(env, review, server, chrome, width, height, mobile):
    """The owner's own Send from the public link is stored by the request gate: the toast, the send bar and the banner
    say so (wakeguard.FUNNEL_NO_WAKE), and nothing promises that Hermes is on it."""
    rid = review["id"]
    owner = account(instance="admin", name="Owner")
    funnel = {"X-Forwarded-For": "100.64.0.7", "Tailscale-Funnel-Request": "?1"}
    b = Board(chrome, "about:blank", wait="true", width=width, height=height, mobile=mobile)
    try:
        visit(b, f"{server}/r/{rid}/", owner, funnel)
        b.press(".btn-pick")
        assert b.until("(function(){var e=document.querySelector('.btn-send');return !!e && !e.disabled})()")
        b.js(TOASTS)
        b.press(".btn-send")
        assert b.until("(window.__toasts||[]).length > 0", timeout=15), "no toast after Send"
        toast = json.loads(b.js("JSON.stringify(window.__toasts[0])"))
        assert toast["text"] == wakeguard.FUNNEL_NO_WAKE and "toast-warn" in toast["cls"], toast
        assert b.until("!!document.querySelector('.sb-sub')", timeout=15)
        assert b.js("document.querySelector('.sb-sub').textContent") == wakeguard.FUNNEL_NO_WAKE
        assert "Hermes is on it" not in (b.js("document.body.innerText") or "")
        sub = store.load(rid)["rounds"][0]["submission"]
        assert sub["status"] == "stored" and sub["stored_detail"] == wakeguard.FUNNEL_NO_WAKE
        assert env["receiver"].requests == []

        visit(b, f"{server}/r/{rid}/", owner, funnel, ready="!!document.querySelector('.sb-sub')")  # a reload keeps it
        assert b.js("document.querySelector('.sb-sub').textContent") == wakeguard.FUNNEL_NO_WAKE
        assert b.js("document.querySelector('.banner').textContent") == wakeguard.FUNNEL_NO_WAKE
    finally:
        b.js("localStorage.clear()")


def test_a_guests_stored_send_never_triggers_the_stalled_ping(env, server, review):
    rid = review["id"]
    _made, guest = join_guest(rid)
    assert guest_submit(rid, guest, review["token"])["status"] == "stored"
    ts = service._parse_iso(store.load(rid)["rounds"][0]["submission"]["submitted_at"])
    assert service.check_stalled(now=ts + 60 * 60) == 0
    assert env["receiver"].notices == []
    assert "stalled_notice" not in store.load(rid)["rounds"][0]["submission"]


def test_a_guests_pin_fix_survives_the_timeout_pass_and_stays_visible(env, server, pin_board):
    rid = pin_board["id"]
    _made, guest = join_guest(rid)
    body = {"round": 1, "token": pin_board["token"], "pin": {"block": "m1", "text": "make it bigger"}}
    code, _headers, out = http("POST", f"{server}/r/{rid}/api/pin-fix", body, signed_in(guest))
    assert code == 200, out
    before = store.load(rid)["rounds"][-1]["pin_requests"]
    assert len(before) == 1
    req_id = next(iter(before))
    ts = service._parse_iso(before[req_id]["requested_at"])
    assert service.check_stalled(now=ts + 20 * 60) == 0
    after = store.load(rid)["rounds"][-1]["pin_requests"][req_id]
    assert after["status"] == "queued" and after["quiet"] is True, after
    assert req_id in api.get_feedback(rid)["next_step"]
    assert env["receiver"].requests == []
