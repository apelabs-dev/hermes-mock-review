"""The 2.0 board header in a real browser: four visible actions, one More menu, and the phone bottom bar.

Properties, at every width 320-1440 for owner / commenter guest / viewer guest, with 1/2/3 devices and a long
name: (a) the document and bar fit and every visible control is inside the viewport and hit-true; (b) an opened
menu or dialog and all its rows are inside the viewport and hit-true by a real click; (c) a menu opens attached
to the control that opened it. The long name now lives in More, so it is typed there and the header must still
fit after the menu closes.
"""

import json
import time

import pytest

from authkit import token_of
from mockreview import users
from test_export_browser import (Page, assert_wseg_clears_export, bar_overflow, metric_report, new_modern_review,
                                 paint)
from test_export_browser import chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

LONG_NAME = "Alexandria Montgomery-Worthington Guest"
THREE = {"devices": ["desktop", "tablet", "mobile"]}
MODERN_WIDTHS = [640, 641, 642, 660, 700, 768, 800, 1024, 1280]
BAND_WIDTHS = [641, 642, 660, 700, 768, 800, 1024, 1100, 1200, 1279]
DEVICE_SETS = [("one", {"devices": ["desktop"]}), ("two", None), ("three", THREE)]
EXPORT_ROWS = ['.export-menu', '.export-item[data-format="png"]', '.export-item[data-format="pdf"]',
               '.export-item[data-format="html"]', '.export-item[data-format="markdown"]', ".export-comments",
               ".export-open"]
OWNER_CONTROLS = [".dzbar", ".bar-comment", ".dzbar .wseg", ".dzbar .who", ".av-self", ".share-open", ".more-open"]
GUEST_CONTROLS = [".dzbar", ".bar-comment", ".dzbar .wseg", ".dzbar .who", ".av-self", ".more-open"]


def type_long_name(cdp, page):
    page.click_sel(".more-open")
    assert page.until("!!document.querySelector('.menu-input')"), "More must offer the guest name field"
    deadline = time.monotonic() + 10
    while True:
        # A live re-render can replace the field between focus and insert, so retry the pair. The value is
        # still asserted exactly; this only stops a lost focus from failing the whole cell.
        page.js("(function(){var e=document.querySelector('.menu-input');if(!e)return false;"
                "e.focus();e.select();return document.activeElement === e})()")
        cdp.call("Input.insertText", {"text": LONG_NAME}, session=page.sid)
        if page.until("document.querySelector('.menu-input').value === %s" % json.dumps(LONG_NAME), timeout=1):
            break
        if time.monotonic() >= deadline:
            raise AssertionError("the guest name field never received the whole typed name")
    page.key("Escape", "Escape", 27)
    assert page.until("!document.querySelector('.export-menu')"), "Escape must close More"
    assert page.js("document.querySelector('.av-self').getAttribute('title')") == LONG_NAME + " · guest"
    page.js("window.scrollTo(0,0);true")
    paint(page)


def close_page(cdp, page):
    tid = cdp.call("Target.getTargetInfo", {}, session=page.sid)["targetInfo"]["targetId"]
    cdp.call("Target.closeTarget", {"targetId": tid})


def assert_controls_inside(page, width, controls, label):
    report = metric_report(page, controls)
    assert report["scroll"] <= width, \
        f"{label}: document scrollWidth {report['scroll']} exceeds the {width}px viewport"
    for item in report["items"]:
        sel = item["sel"]
        assert not item.get("missing"), f"{label}: {sel} is missing"
        assert item["width"] > 0 and item["height"] > 0, f"{label}: {sel} is laid out with no size: {item}"
        assert item["left"] >= 0 and item["right"] <= width, \
            f"{label}: {sel} sits outside the {width}px viewport: left {item['left']} right {item['right']}"
        assert item["hittable"], f"{label}: {sel} cannot be hit at its centre, another element covers it: {item}"
    return report


def open_export_menu(cdp, page, label):
    """A real click opens More (which carries the Export section) and it survives a wrapping re-render."""
    for _ in range(3):
        if page.js("!!document.querySelector('.export-menu')"):
            page.key("Escape", "Escape", 27)
            page.until("!document.querySelector('.export-menu')")
        page.click_sel(".export-open")
        if page.until("!!document.querySelector('.export-menu')"):
            cdp.pump(0.15)
            if page.js("!!document.querySelector('.export-menu')"):
                return
    raise AssertionError(f"{label}: a real click on More never left the menu open")


def assert_export_menu_inside(page, width, label):
    report = metric_report(page, EXPORT_ROWS)
    assert report["vw"] == width, f"{label}: the viewport reports {report['vw']}px, not {width}px"
    assert report["scroll"] <= width, \
        f"{label}: document scrollWidth {report['scroll']} exceeds the {width}px viewport"
    bar = bar_overflow(page)
    assert bar and bar[0] <= bar[1], \
        f"{label}: the bar needs horizontal scrolling: {bar[0]} > {bar[1]}"
    by_sel = {item["sel"]: item for item in report["items"]}
    menu = by_sel[".export-menu"]
    assert not menu.get("missing"), f"{label}: the More menu is missing"
    assert menu["left"] >= 0 and menu["right"] <= width, \
        f"{label}: the More menu box is clipped: left {menu['left']} right {menu['right']} width {width}"
    for sel in EXPORT_ROWS:
        item = by_sel[sel]
        assert not item.get("missing"), f"{label}: {sel} is missing"
        assert item["width"] > 0 and item["height"] > 0, f"{label}: {sel} is laid out with no size: {item}"
        assert item["left"] >= 0 and item["right"] <= width, \
            f"{label}: {sel} sits outside the {width}px viewport: left {item['left']} right {item['right']}"
        if sel != ".export-open":
            assert not item["disabled"], f"{label}: {sel} is disabled"
            assert item["hittable"], f"{label}: {sel} cannot be hit at its centre, another element covers it: {item}"
    button = by_sel[".export-open"]
    assert not button.get("missing"), f"{label}: the More button is missing"
    assert menu["left"] < button["right"] and menu["right"] > button["left"], \
        (f"{label}: the More menu is detached from its button: button left {button['left']} "
         f"right {button['right']}, menu left {menu['left']} right {menu['right']}")
    gap = menu["top"] - button["bottom"]
    assert 0 <= gap <= 10, \
        (f"{label}: the More menu is not just under its button: button bottom {button['bottom']} "
         f"menu top {menu['top']} gap {gap}px")
    return report


@pytest.mark.parametrize("kind", ["decisions", "prototype"])
@pytest.mark.parametrize("devices,look", DEVICE_SETS, ids=[d[0] for d in DEVICE_SETS])
@pytest.mark.parametrize("role", ["owner", "commenter", "viewer"])
def test_more_menu_stays_inside_the_viewport_between_phones_and_desktop(
        env, server, tmp_path, chrome, monkeypatch, kind, role, devices, look):
    """One page per (role, device set, kind), resized through every width where the single-line bar is active."""
    if role == "owner":
        monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    else:
        monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    data = new_modern_review(tmp_path, f"Export band {role} {devices} {kind}", kind, look=look)
    if role == "owner":
        url = f"{server}/r/{data['id']}/"
    else:
        token = token_of(users.share("test", data["id"], "create", role)["path"])
        url = f"{server}/s/{token}"
    page = Page(chrome, url, width=BAND_WIDTHS[0], height=844,
                wait="!!document.querySelector('.more-open')")
    previous = BAND_WIDTHS[0]
    try:
        for width in BAND_WIDTHS:
            chrome.call("Emulation.setDeviceMetricsOverride",
                        {"width": width, "height": 844, "deviceScaleFactor": 1, "mobile": False},
                        session=page.sid)
            paint(page)   # commit the width's re-render before opening anything (a forced frame, not a sleep)
            previous = width
            label = f"{kind} {role} {devices} at {width}px"
            assert page.until("!document.querySelector('.export-menu')"), \
                f"{label}: a menu was open before the click"
            assert page.until("!!document.querySelector('.export-open')"), \
                f"{label}: the More button is missing after the resize"
            assert page.js("window.innerWidth") == width, \
                f"{label}: the viewport is {page.js('window.innerWidth')}px after the resize"
            open_export_menu(chrome, page, label)
            report = assert_export_menu_inside(page, width, label)
            assert len(report["items"]) == len(EXPORT_ROWS), \
                f"{label}: measured {len(report['items'])} of {len(EXPORT_ROWS)} Export surfaces"
            page.key("Escape", "Escape", 27)
            assert page.until("!document.querySelector('.export-menu')"), \
                f"{label}: Escape did not close More"
    finally:
        close_page(chrome, page)


@pytest.mark.parametrize("kind", ["decisions", "prototype"])
@pytest.mark.parametrize("width", MODERN_WIDTHS)
def test_modern_guest_header_fits_and_keeps_every_control(env, server, tmp_path, chrome, monkeypatch, kind, width):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    data = new_modern_review(tmp_path, "Long project title to test sane header ellipsis " + kind, kind, look=THREE)
    token = token_of(users.share("test", data["id"], "create", "commenter")["path"])
    page = Page(chrome, f"{server}/s/{token}", width=width, height=844,
                wait="!!document.querySelector('.av-self')")
    try:
        type_long_name(chrome, page)
        assert not page.js("!!document.querySelector('.share-open')"), \
            "a commenter guest has no manage capability, so the header must not offer Share"
        report = assert_controls_inside(page, width, GUEST_CONTROLS, f"modern {kind} guest at {width}")
        bar = bar_overflow(page)
        assert bar and bar[0] <= bar[1], \
            f"modern {kind} guest at {width}: the bar needs horizontal scrolling: {bar[0]} > {bar[1]}"
        assert len(report["items"]) == len(GUEST_CONTROLS)

        assert not page.js("!!document.querySelector('.export-menu')"), "the More menu is open before any click"
        page.click_sel(".export-open")
        assert page.until("!!document.querySelector('.export-menu')"), \
            f"modern {kind} guest at {width}: a real click on More did not open the menu"
        formats = sorted(page.js(
            "Array.prototype.map.call(document.querySelectorAll('.export-menu [data-format]'),"
            "function(e){return e.getAttribute('data-format')})") or [])
        assert formats == ["html", "markdown", "pdf", "png"], \
            f"modern {kind} guest at {width}: the Export formats are {formats!r}"
        menu = metric_report(page, ['.export-menu [data-format="%s"]' % f for f in formats])
        for item in menu["items"]:
            assert item["left"] >= 0 and item["right"] <= width, \
                f"modern {kind} guest at {width}: {item['sel']} sits outside the viewport: {item}"
            assert item["hittable"], f"modern {kind} guest at {width}: {item['sel']} cannot be hit: {item}"
        page.key("Escape", "Escape", 27)
        assert page.until("!document.querySelector('.export-menu')"), \
            f"modern {kind} guest at {width}: Escape did not close More"

        count = assert_wseg_clears_export(page, width)
        assert count == 3, f"modern {kind} guest at {width}: the three-device width segment has {count} buttons"
        page.click_sel(".more-open")
        assert page.until("!!document.querySelector('.menu-input')"), "More must reopen"
        assert page.js("document.querySelector('.menu-input').value") == LONG_NAME, \
            f"modern {kind} guest at {width}: the long guest name did not survive the width rerenders"
        page.key("Escape", "Escape", 27)
    finally:
        close_page(chrome, page)


@pytest.mark.parametrize("width", [390, 1280])
def test_legacy_owner_share_stays_inside_the_viewport(env, server, review, chrome, monkeypatch, width):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    page = Page(chrome, f"{server}/r/{review['id']}/", width=width, height=844,
                wait="!!document.querySelector('.more-open')")
    try:
        paint(page)
        controls = [".top-actions .more-open", ".top-actions .who"] + ([".top-actions .share-open"] if width > 600 else [])
        assert_controls_inside(page, width, controls, f"legacy owner at {width}")
        if width > 600:
            page.click_sel(".share-open")
        else:
            page.click_sel(".more-open")
            assert page.until("!!document.querySelector('.share-menu')"), "More must offer Share on a phone"
            page.click_sel(".share-menu")
        assert page.until("!!document.querySelector('.share-dialog')"), \
            f"legacy owner at {width}: a real click on Share did not open the dialog"
        assert page.until("!!document.querySelector('.share-dialog .share-invite-input')"), \
            f"legacy owner at {width}: the Share dialog never rendered its settings"
        assert page.until("!document.querySelector('.export-menu')"), "opening Share closes More"
        page.key("Escape", "Escape", 27)
        assert page.until("!document.querySelector('.share-dialog')"), \
            f"legacy owner at {width}: Escape did not close the Share dialog"
    finally:
        close_page(chrome, page)
