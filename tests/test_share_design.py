"""The approved Share design in a real browser: centred on desktop, one filled action, phone sheet intact.

Design-pack decisions 6 and 10 and `docs/design.md`: the Share dialog is centred on desktop, stays a bottom
sheet at <= 640 px, scrolls inside its own capped height, and Invite is its only filled primary action in both
the no-link and link states (Create link stays as an outline control beside it).
"""

import json

import pytest

from authkit import account
from test_export_browser import Page, chrome, close_page, new_modern_review, paint, settle, trusted_click  # noqa: F401
from mockreview import users

pytestmark = pytest.mark.browser

WIDTHS = (320, 390, 640, 641, 768, 1280, 1920)
ACTION_WIDTHS = (390, 1280)
THEMES = ("dark", "light")
HEIGHT = 900
PHONE_MAX = 640

MEASURE_JS = """
(function(){
  var d=document.querySelector('.share-dialog'), b=document.querySelector('.share-backdrop'),
      r=d.getBoundingClientRect(), de=document.documentElement, bs=getComputedStyle(b);
  return JSON.stringify({
    width:innerWidth, height:innerHeight,
    scrollWidth:de.scrollWidth, clientWidth:de.clientWidth,
    left:r.left, top:r.top, right:r.right, bottom:r.bottom,
    centerX:(r.left+r.right)/2, centerY:(r.top+r.bottom)/2,
    align:bs.alignItems, justify:bs.justifyItems,
    dialogScrollHeight:d.scrollHeight, dialogClientHeight:d.clientHeight
  });
})()
"""

BUTTONS_JS = """
(function(){
  var rows=[];
  Array.prototype.forEach.call(document.querySelectorAll('.share-dialog button'), function(e){
    var r=e.getBoundingClientRect(), cs=getComputedStyle(e);
    if(!r.width || !r.height || cs.display==='none' || cs.visibility==='hidden') return;
    rows.push({text:e.textContent.trim(), cls:e.className, bg:cs.backgroundColor,
               border:cs.borderTopStyle+' '+cs.borderTopColor,
               primary:e.matches('.btn-primary'), invite:e.matches('.share-invite-btn')});
  });
  return JSON.stringify(rows);
})()
"""


def open_share(page, width):
    """Open Share the way a reviewer does: the visible button on desktop, More -> Share at <= 600 px."""
    assert page.until("!!document.querySelector('.more-open')")
    if width <= 600:
        trusted_click(page, ".more-open")
        assert page.until("!!document.querySelector('.export-menu .share-menu')"), "More must offer Share"
        trusted_click(page, ".export-menu .share-menu")
    else:
        trusted_click(page, ".share-open")
    assert page.until("!!document.querySelector('.share-dialog .share-invite-btn')"), \
        "Share must finish loading its controls"
    settle(page, 0.4)


def click_create_link(page):
    point = json.loads(page.js(
        "(function(){var b=Array.prototype.find.call(document.querySelectorAll('.share-dialog button'),"
        "function(e){return e.textContent.trim()==='Create link';});if(!b)return 'null';"
        "var r=b.getBoundingClientRect();return JSON.stringify([r.left+r.width/2,r.top+r.height/2]);})()"))
    assert point and point != "null", "Create link is not in the dialog"
    page.click_at(*point)


def assert_one_filled_action(page, width, theme, state):
    # Park the real pointer off every control: a :hover fill is not the resting design.
    page.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1, "button": "none"},
                  session=page.sid)
    settle(page, 0.15)
    paint(page)
    buttons = json.loads(page.js(BUTTONS_JS))
    primary = [b for b in buttons if b["primary"]]
    assert len(primary) == 1, \
        f"{width}px {theme} {state}: Share must show exactly one filled primary action, got {primary}"
    invite = primary[0]
    assert invite["invite"] and invite["text"] == "Invite", \
        f"{width}px {theme} {state}: the sole filled action must be Invite, got {invite}"
    assert invite["bg"].startswith("rgb("), \
        f"{width}px {theme} {state}: Invite must be accent-filled, got background {invite['bg']}"
    # 2.0.1 fills .btn with --surface so buttons stand off the canvas. Inside the dialog that fill is the
    # dialog's own --surface, so a secondary control still reads as outline/ghost: it may be transparent or
    # exactly the dialog background, never any other fill.
    dialog_bg = page.js("getComputedStyle(document.querySelector('.share-dialog')).backgroundColor")
    assert dialog_bg.startswith("rgb"), f"{width}px {theme} {state}: no share dialog background, got {dialog_bg}"
    for button in buttons:
        if button is invite:
            continue
        assert not button["primary"], f"{width}px {theme} {state}: {button['text']} must not be a primary action"
        assert button["bg"] in ("rgba(0, 0, 0, 0)", dialog_bg), \
            f"{width}px {theme} {state}: {button['text']} must be an outline/ghost control (transparent or the " \
            f"dialog's {dialog_bg}), got {button['bg']}"
    if state == "no-link":
        create = [b for b in buttons if b["text"] == "Create link"]
        assert len(create) == 1, f"{width}px {theme}: Create link must stay available"
        assert create[0]["cls"] == "btn", f"{width}px {theme}: Create link must be an outline button, got {create[0]}"
        assert create[0]["border"].startswith("solid"), \
            f"{width}px {theme}: Create link must keep a visible outline, got {create[0]['border']}"
    else:
        assert not any(b["text"] == "Create link" for b in buttons), \
            f"{width}px {theme}: the link state must replace Create link"
        assert any(b["text"] == "Copy" for b in buttons), f"{width}px {theme}: the link row must render"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_share_dialog_centres_on_desktop_and_stays_a_phone_sheet(env, server, tmp_path, chrome, width, theme):
    data = new_modern_review(tmp_path, f"share-centre-{width}-{theme}", "decisions")
    page = Page(chrome, f"{server}/r/{data['id']}/", width=width, height=HEIGHT, media=theme)
    try:
        open_share(page, width)
        report = json.loads(page.js(MEASURE_JS))
        report.update(requested_width=width, requested_height=HEIGHT, theme=theme)
        assert report["scrollWidth"] == report["clientWidth"], \
            f"{width}px {theme}: the document scrolls horizontally: {report}"
        assert abs(report["centerX"] - width / 2) <= 1, \
            f"{width}px {theme}: the dialog is not horizontally centred: {report}"
        if width <= PHONE_MAX:
            assert abs(report["bottom"] - report["height"]) <= 1, \
                f"{width}px {theme}: the phone Share sheet must sit flush to the bottom: {report}"
        else:
            assert abs(report["centerY"] - report["height"] / 2) <= 1, \
                f"{width}px {theme}: the desktop Share dialog must be vertically centred: {report}"
        page.key("Escape", "Escape", 27)
        assert page.until("!document.querySelector('.share-dialog')"), f"{width}px {theme}: Escape must close Share"
    finally:
        close_page(page)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", ACTION_WIDTHS)
def test_share_keeps_exactly_one_filled_action_in_the_no_link_state(env, server, tmp_path, chrome, width, theme):
    data = new_modern_review(tmp_path, f"share-no-link-{width}-{theme}", "decisions")
    page = Page(chrome, f"{server}/r/{data['id']}/", width=width, height=HEIGHT, media=theme)
    try:
        open_share(page, width)
        assert_one_filled_action(page, width, theme, "no-link")
        page.key("Escape", "Escape", 27)
        assert page.until("!document.querySelector('.share-dialog')"), f"{width}px {theme}: Escape must close Share"
    finally:
        close_page(page)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", ACTION_WIDTHS)
def test_share_keeps_exactly_one_filled_action_in_the_link_state(env, server, tmp_path, chrome, width, theme):
    data = new_modern_review(tmp_path, f"share-link-{width}-{theme}", "decisions")
    page = Page(chrome, f"{server}/r/{data['id']}/", width=width, height=HEIGHT, media=theme)
    try:
        open_share(page, width)
        assert_one_filled_action(page, width, theme, "no-link")
        click_create_link(page)
        assert page.until("!!document.querySelector('.share-dialog .share-link-row .share-url')"), \
            f"{width}px {theme}: Create link must still create and show the link"
        assert page.until(
            "!Array.prototype.some.call(document.querySelectorAll('.share-dialog button'),"
            "function(e){return e.textContent.trim()==='Create link';})"), \
            f"{width}px {theme}: the link state must replace Create link"
        settle(page, 0.2)
        assert_one_filled_action(page, width, theme, "link")
    finally:
        close_page(page)


def test_a_tall_share_dialog_scrolls_inside_its_own_capped_height(env, server, tmp_path, chrome):
    """Centring must never clip the top of a tall dialog: the dialog caps its height and scrolls inside."""
    data = new_modern_review(tmp_path, "share-tall", "decisions")
    users.share("test", data["id"], "create")
    for i in range(14):
        account(review_id=data["id"], role="commenter", name=f"Person {i:02d}")
    page = Page(chrome, f"{server}/r/{data['id']}/", width=1280, height=520, media="dark")
    try:
        open_share(page, 1280)
        report = json.loads(page.js(MEASURE_JS))
        assert report["scrollWidth"] == report["clientWidth"], f"the document scrolls horizontally: {report}"
        assert report["dialogScrollHeight"] > report["dialogClientHeight"], \
            f"the fixture must be tall enough to need internal scrolling: {report}"
        assert report["top"] >= -0.5 and report["bottom"] <= report["height"] + 0.5, \
            f"the capped dialog must stay inside the viewport: {report}"
        top = json.loads(page.js(
            "(function(){var d=document.querySelector('.share-dialog');d.scrollTop=0;"
            "var h=document.querySelector('.share-head').getBoundingClientRect(),r=d.getBoundingClientRect();"
            "return JSON.stringify({headTop:h.top,dialogTop:r.top,scrollTop:d.scrollTop});})()"))
        assert top["scrollTop"] == 0 and top["headTop"] >= top["dialogTop"] - 1, \
            f"the top of the dialog must be reachable: {top}"
        bottom = json.loads(page.js(
            "(function(){var d=document.querySelector('.share-dialog');d.scrollTop=d.scrollHeight;"
            "var kids=d.children,last=kids[kids.length-1].getBoundingClientRect(),r=d.getBoundingClientRect();"
            "return JSON.stringify({lastBottom:last.bottom,dialogBottom:r.bottom,scrollTop:d.scrollTop,"
            "max:d.scrollHeight-d.clientHeight});})()"))
        assert abs(bottom["scrollTop"] - bottom["max"]) <= 1, f"the dialog must scroll to its end: {bottom}"
        assert bottom["lastBottom"] <= bottom["dialogBottom"] + 1, \
            f"the bottom of the dialog must be reachable: {bottom}"
    finally:
        close_page(page)
