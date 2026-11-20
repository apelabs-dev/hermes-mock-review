"""HDR header regressions in a real browser.

(a) Export stays usable on a phone: at 320/375/390/640 the opened menu's format rows and the comments row are
inside the viewport and hittable, and Escape closes it — modern (decisions, prototype) and legacy, owner and
commenter guest.
(b) A real mouse click outside the open More menu dismisses it without creating a comment, with comment mode off and
on — on desktop the click is inside the sandboxed prototype frame, on a phone it is on the More sheet's backdrop,
which covers the frame; a focus move into the frame does the same at both widths.
(c) The brand mark is the real SVG mark, never the text `mr`, on the sign-in and home pages, the board header and
Settings, in both themes; the page's icon link answers 200.
(d) The board header shows the reviewed project's icon (the round's design/icon.svg, labelled by the design) and not
the mock-review mark; a round with no design icon keeps the mock-review mark.
"""

import json

import pytest

from authkit import token_of
from conftest import http
from mockreview import store, users
from test_export_browser import (Page, assert_nothing_commented, chrome, metric_report,  # noqa: F401
                                 new_modern_review, paint, proto_state, settle, trusted_click)
from test_store import _design

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
PHONE_WIDTHS = [320, 375, 390, 640]
EXPORT_ROWS = ['.export-item[data-format="png"]', '.export-item[data-format="pdf"]',
               '.export-item[data-format="html"]', '.export-item[data-format="markdown"]', ".export-comments"]


def fresh_page(chrome, url, width, height, wait, theme=None):
    """A page with an empty cookie jar and, optionally, one emulated colour scheme."""
    page = Page(chrome, "about:blank", width=width, height=height, script="localStorage.clear();")
    chrome.call("Network.enable", session=page.sid)
    chrome.call("Network.clearBrowserCookies", session=page.sid)
    if theme:
        chrome.call("Emulation.setEmulatedMedia",
                    {"features": [{"name": "prefers-color-scheme", "value": theme}]}, session=page.sid)
    chrome.call("Page.navigate", {"url": url}, session=page.sid)
    assert page.until(wait, timeout=60), f"the page never reached its ready state: {url}"
    return page


def close_page(chrome, page):
    tid = chrome.call("Target.getTargetInfo", {}, session=page.sid)["targetInfo"]["targetId"]
    chrome.call("Target.closeTarget", {"targetId": tid})


def legacy_review(tmp_path, name):
    base = tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    specs = []
    for variant in ("alpha", "beta"):
        d = base / variant
        d.mkdir()
        (d / "index.html").write_text("<!doctype html><html><body><h1>%s</h1></body></html>" % variant,
                                      encoding="utf-8")
        specs.append(store.parse_variant_spec("%s|stance %s|%s" % (variant.title(), variant, d / "index.html")))
    return store.create_review(name, "", ORIGIN, "tailscale", specs, wake=False)


def review_url(server, data, role):
    if role == "owner":
        return f"{server}/r/{data['id']}/"
    return f"{server}/s/{token_of(users.share('test', data['id'], 'create', role)['path'])}"


def click_here(page, x, y):
    page.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
                  session=page.sid)
    settle(page, 0.03)
    page.cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left",
                                               "clickCount": 1, "buttons": 1}, session=page.sid)
    settle(page, 0.03)
    page.cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left",
                                               "clickCount": 1, "buttons": 0}, session=page.sid)
    settle(page, 0.25)


def frame_point(page):
    """A real viewport point inside the prototype iframe's rect, never on one of the board's menus."""
    point = json.loads(page.js(
        "(function(){var f=document.querySelector('.proto iframe');if(!f)return 'null';"
        "var r=f.getBoundingClientRect(),xs=[0.08,0.15,0.25,0.5],ys=[0.12,0.2,0.3,0.5];"
        "for(var j=0;j<ys.length;j++){for(var i=0;i<xs.length;i++){"
        "var x=r.left+r.width*xs[i],y=r.top+r.height*ys[j];"
        "if(x<0||y<0||x>=innerWidth||y>=innerHeight)continue;"
        "var e=document.elementFromPoint(x,y);"
        "if(!e||!e.closest('.menu'))return JSON.stringify({x:x,y:y,hit:e?e.tagName+'.'+e.className:null});"
        "}}return 'null'})()"))
    assert point and point != "null", "no real point over the prototype frame is clear of the open menu"
    return point


def phone_dismiss_point(page):
    """A real viewport point on the phone More sheet's backdrop.

    The sheet is capped at 88vh and its top edge sits above the frame's, so while it is open no point over the
    prototype frame is clear of it: the phone's dismiss tap is on the backdrop strip above the sheet."""
    point = json.loads(page.js(
        "(function(){var b=document.querySelector('.menu-backdrop');if(!b)return 'null';"
        "var xs=[0.25,0.5,0.75];"
        "for(var i=0;i<xs.length;i++){var x=Math.round(innerWidth*xs[i]),y=8;"
        "var e=document.elementFromPoint(x,y);"
        "if(e&&e.closest&&e.closest('.menu-backdrop'))return JSON.stringify({x:x,y:y,hit:e.tagName+'.'+e.className});}"
        "return 'null'})()"))
    assert point and point != "null", "no real point on the phone More sheet's backdrop is clear of the sheet"
    return point


def assert_phone_round_control(page, label):
    """The footer round strip, not the header round picker, is the phone's round control on a prototype round."""
    strip = metric_report(page, ["#dzfoot .rounds .round-tab.on"])["items"][0]
    assert not strip.get("missing"), f"{label}: the footer round strip is missing: {strip}"
    assert strip["hittable"], f"{label}: the footer round tab is not hittable: {strip}"
    assert strip["height"] + 0.5 >= 44, f"{label}: the footer round tab is below the phone hit size: {strip}"
    assert page.js("!document.querySelector('.round-picker')"), \
        f"{label}: a prototype round must not render the header round picker on a phone"


@pytest.mark.parametrize("width", PHONE_WIDTHS)
@pytest.mark.parametrize("kind", ["decisions", "prototype", "legacy"])
@pytest.mark.parametrize("role", ["owner", "commenter"])
def test_export_rows_stay_inside_the_viewport_on_a_phone(env, server, tmp_path, chrome, monkeypatch,
                                                         kind, role, width):
    data = legacy_review(tmp_path, f"hdr legacy {role}") if kind == "legacy" \
        else new_modern_review(tmp_path, f"hdr {kind} {role}", kind)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true" if role == "owner" else "false")
    page = fresh_page(chrome, review_url(server, data, role), width, 700,
                      "!!document.querySelector('.export-open')")
    try:
        paint(page)
        trusted_click(page, ".export-open")
        assert page.until("!!document.querySelector('.export-menu')"), "a real click on Export must open the menu"
        paint(page)
        assert page.js("document.querySelector('.export-open').getAttribute('aria-expanded')") == "true"
        report = metric_report(page, [".export-menu"] + EXPORT_ROWS)
        vh = page.js("window.innerHeight")
        by_sel = {item["sel"]: item for item in report["items"]}
        assert not by_sel[".export-menu"].get("missing"), "the Export menu is missing"
        for sel in EXPORT_ROWS:
            item = by_sel[sel]
            assert not item.get("missing"), f"{kind} {role} at {width}: {sel} is missing"
            assert item["width"] > 0 and item["height"] > 0, \
                f"{kind} {role} at {width}: {sel} is laid out with no size: {item}"
            assert item["left"] >= 0 and item["right"] <= width and item["top"] >= 0 and item["bottom"] <= vh, \
                f"{kind} {role} at {width}: {sel} is outside the {width}x{vh} viewport: {item}"
            assert not item["disabled"], f"{kind} {role} at {width}: {sel} is disabled"
            assert item["hittable"], f"{kind} {role} at {width}: {sel} cannot be hit at its centre: {item}"
        page.key("Escape", "Escape", 27)
        assert page.until("!document.querySelector('.export-menu')"), \
            f"{kind} {role} at {width}: Escape did not close the Export menu"
        assert page.js("document.querySelector('.export-open').getAttribute('aria-expanded')") == "false", \
            f"{kind} {role} at {width}: Escape left aria-expanded true"
    finally:
        close_page(chrome, page)


@pytest.mark.parametrize("width,height", [(1024, 700), (390, 844)], ids=["desktop", "phone"])
@pytest.mark.parametrize("role", ["commenter", "viewer"])
def test_a_click_in_the_prototype_frame_closes_the_open_menu(env, server, tmp_path, chrome, monkeypatch,
                                                            role, width, height):
    """More at both widths: a prototype round renders the footer round strip, not the header round picker, and the
    strip is not a popup (a .round-tab switches rounds directly). On a phone More is a bottom sheet whose backdrop
    covers the whole viewport and whose top edge sits above the frame, so the dismiss tap is on the backdrop strip
    above the sheet, not on the frame itself — that is the phone's outside-click dismissal. The footer round strip
    is asserted below as the phone round control."""
    data = store.create_review("HDR frame click", "", ORIGIN, "tailscale",
                               store.parse_round_spec(prototype=str(write_proto(tmp_path))), wake=False)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    url = review_url(server, data, role)
    opener, menu = ".export-open", ".export-menu"
    for comment in (("off", "on") if role == "commenter" else ("off",)):
        page = fresh_page(chrome, url, width, height, "!!document.querySelector('#dz-proto[data-ready=true]')")
        try:
            paint(page)
            if width <= 600:
                assert_phone_round_control(page, f"{role} {width} comment {comment}")
            if comment == "on":
                toggle = ".bar-comment" if width > 600 else ".foot-comment"
                assert page.js("!!document.querySelector(%s)" % json.dumps(toggle)), "the Comment toggle is missing"
                trusted_click(page, toggle)
                assert page.until("document.body.classList.contains('tool-comment')"), "Comment mode must turn on"
            paint(page)
            trusted_click(page, opener)
            assert page.until("!!document.querySelector(%s)" % json.dumps(menu)), "the menu must open"
            paint(page)
            assert page.js("document.querySelector(%s).getAttribute('aria-expanded')" % json.dumps(opener)) == "true"
            point = frame_point(page) if width > 600 else phone_dismiss_point(page)
            click_here(page, point["x"], point["y"])
            assert page.until("!document.querySelector(%s)" % json.dumps(menu)), \
                f"{role} {width} comment {comment}: a real outside click left the menu open"
            assert page.js("document.querySelector(%s).getAttribute('aria-expanded')" % json.dumps(opener)) == "false", \
                f"{role} {width} comment {comment}: aria-expanded stayed true"
            state = proto_state(page)
            assert not state["menu"], f"{role} {width} comment {comment}: the Export menu is still in the page"
            assert_nothing_commented(page, f"{role} {width} comment {comment}: the outside click")
        finally:
            close_page(chrome, page)


@pytest.mark.parametrize("width,height", [(1024, 700), (390, 844)], ids=["desktop", "phone"])
def test_a_focus_move_into_the_prototype_frame_closes_the_open_menu(env, server, tmp_path, chrome, monkeypatch,
                                                                    width, height):
    """More at both widths; on a phone the sheet and its backdrop stay open to pointer taps over the frame, but a
    focus move into the frame itself still dismisses them. The footer round strip is the phone round control."""
    data = store.create_review("HDR frame focus", "", ORIGIN, "tailscale",
                               store.parse_round_spec(prototype=str(write_proto(tmp_path))), wake=False)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    url = review_url(server, data, "commenter")
    opener, menu = ".export-open", ".export-menu"
    page = fresh_page(chrome, url, width, height, "!!document.querySelector('#dz-proto[data-ready=true]')")
    try:
        paint(page)
        if width <= 600:
            assert_phone_round_control(page, f"{width}px focus move")
        trusted_click(page, opener)
        assert page.until("!!document.querySelector(%s)" % json.dumps(menu)), "the menu must open"
        paint(page)
        assert page.js("(function(){var f=document.querySelector('.proto iframe');"
                       "if(!f)return false;f.focus();return true})()"), "the prototype frame must be focusable"
        assert page.until("!document.querySelector(%s)" % json.dumps(menu)), \
            "a focus move into the frame must close the open menu"
        assert page.js("document.querySelector(%s).getAttribute('aria-expanded')" % json.dumps(opener)) == "false"
        assert_nothing_commented(page, "a focus move into the frame")
    finally:
        close_page(chrome, page)


SCROLLED_DEVICE = '.dzbar .wseg .seg-btn[data-value="phone"]'
SCROLLED_CELLS = [
    ("commenter", ".bar-comment", "comment"),
    ("owner", ".bar-comment", "comment"),
    ("owner", ".share-open", "share"),
    ("commenter", SCROLLED_DEVICE, "device"),
    ("owner", SCROLLED_DEVICE, "device"),
]


def scroll_under_the_bar(page, limit=120):
    """Page mode keeps the frame below the sticky bar, so the page that scrolls is the frame's own document.

    It used to be the outer board page, and the scrolled iframe slid under the header, which is what let the frame
    shield cover the controls. Return the frame/bar geometry and the frame's own scroll offset."""
    info = json.loads(page.js(
        "(function(){var f=document.querySelector('.proto iframe'),b=document.querySelector('.dzbar');"
        "if(!f||!b)return 'null';var fr=f.getBoundingClientRect(),br=b.getBoundingClientRect();"
        "return JSON.stringify({max:Math.max(0,document.documentElement.scrollHeight-window.innerHeight),"
        "y:window.scrollY,top:fr.top,barBottom:br.bottom})})()"))
    assert info and info != "null", "the prototype frame or the bar is missing"
    assert info["top"] >= info["barBottom"] - 0.5, \
        f"page mode must keep the frame below the sticky bar: {info}"
    frame = json.loads(page.js(
        "(function(){var e=document.querySelector('.proto iframe'),r=e.getBoundingClientRect();"
        "return JSON.stringify({src:e.src,w:r.width,h:r.height})})()"))
    targets = page.cdp.call("Target.getTargets")["targetInfos"]
    target = next(t for t in targets if t["type"] == "iframe" and t["url"] == frame["src"])
    sid = page.cdp.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["sessionId"]
    try:
        page.cdp.call("Runtime.evaluate", {"expression":
            "(function(){if(document.documentElement.scrollHeight<=innerHeight){"
            "var d=document.createElement('div');d.style.height='3000px';d.id='mr-test-tall';"
            "document.body.appendChild(d);}window.scrollTo(0,%d);return 1})()" % limit}, session=sid)
        settle(page, 0.1)
        inside = json.loads(page.cdp.call("Runtime.evaluate", {
            "expression": "JSON.stringify({y:scrollY,h:document.documentElement.scrollHeight,vh:innerHeight})",
            "returnByValue": True}, session=sid)["result"]["value"])
    finally:
        page.cdp.call("Target.detachFromTarget", {"sessionId": sid})
    assert inside["y"] > 0, f"the page inside the frame did not scroll: {inside}"
    assert inside["h"] > inside["vh"], inside
    info.update(y=inside["y"], frameMax=inside["h"] - inside["vh"])
    return info


def top_element_at(page, sel):
    """What a real click at sel's centre would land on, the reviewer's measurement for the frame shield."""
    return json.loads(page.js(
        "(function(){var e=document.querySelector(%s);if(!e)return 'null';"
        "var r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;"
        "var t=(x>=0&&y>=0&&x<innerWidth&&y<innerHeight)?document.elementFromPoint(x,y):null;"
        "return JSON.stringify({x:Math.round(x),y:Math.round(y),ok:!!t&&(t===e||e.contains(t)),"
        "hit:t?t.tagName+'.'+String(t.className||'').trim():null})})()" % json.dumps(sel)))


def assert_control_effect(page, kind, sel, where):
    if kind == "comment":
        assert page.until("document.body.classList.contains('tool-comment')"), \
            f"{where}: the click did not turn Comment mode on"
    elif kind == "share":
        assert page.until("!!document.querySelector('.share-backdrop') && "
                          "!!document.querySelector('.share-dialog')"), \
            f"{where}: the click did not open the Share dialog"
    else:
        assert page.until("(function(){var b=document.querySelector(%s);return !!b && "
                          "b.getAttribute('aria-checked')==='true' && b.classList.contains('on')})()"
                          % json.dumps(sel)), f"{where}: the click did not make that device width the checked one"


@pytest.mark.parametrize("width,height", [(1024, 700), (1280, 800)], ids=["desktop", "wide"])
@pytest.mark.parametrize("role,control,kind", SCROLLED_CELLS,
                         ids=["commenter-comment", "owner-comment", "owner-share",
                              "commenter-device", "owner-device"])
def test_a_scrolled_frame_does_not_shield_the_header_controls(env, server, tmp_path, chrome, monkeypatch,
                                                              role, control, kind, width, height):
    """A plain outside click still dismisses More, but the clicked header control must also do its job.

    Reviewer comment 549: on a scrolled prototype the frame shield sat above the sticky bar, so a real click on
    Comment, Desktop/Mobile, Share or More only closed the menu. In page mode the frame stays below the header and
    the page inside it scrolls; after that scroll, open More and assert the control is the top element at its centre
    before clicking it for real.
    """
    data = store.create_review("HDR scrolled frame", "", ORIGIN, "tailscale",
                               store.parse_round_spec(prototype=str(write_proto(tmp_path))), wake=False)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true" if role == "owner" else "false")
    url = review_url(server, data, role)
    page = fresh_page(chrome, url, width, height, "!!document.querySelector('#dz-proto[data-ready=true]')")
    try:
        paint(page)
        scroll = scroll_under_the_bar(page)
        paint(page)
        trusted_click(page, ".export-open")
        assert page.until("!!document.querySelector('.export-menu')"), "a real click on Export must open the menu"
        paint(page)
        top = top_element_at(page, control)
        assert top and top != "null", f"{role} {width}: {control} is missing"
        assert top["ok"], (f"{role} {width}: {control} is not the top element at its centre "
                           f"({top['x']}, {top['y']}) after scrolling {scroll['y']}px: {top['hit']} covers it")
        trusted_click(page, control)
        assert page.until("!document.querySelector('.export-menu')"), \
            f"{role} {width}: a real click on {control} left the Export menu open"
        assert page.js("document.querySelector('.export-open').getAttribute('aria-expanded')") == "false", \
            f"{role} {width}: a real click on {control} left aria-expanded true"
        assert_control_effect(page, kind, control, f"{role} {width} {control}")
        assert_nothing_commented(page, f"{role} {width}: the click on {control}")
    finally:
        close_page(chrome, page)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1280])
def test_the_brand_mark_is_the_svg_mark_everywhere(env, server, tmp_path, chrome, monkeypatch, theme, width):
    data = store.create_review("HDR brand", "", ORIGIN, "tailscale",
                               store.parse_round_spec(prototype=str(write_proto(tmp_path))), wake=False)

    def mark(page, sel):
        return json.loads(page.js(
            "(function(){var e=document.querySelector(%s);if(!e)return 'null';"
            "var r=e.getBoundingClientRect(),cs=getComputedStyle(e);"
            "return JSON.stringify({tag:e.tagName,text:(e.textContent||'').trim(),"
            "svg:!!e.querySelector('svg'),src:e.getAttribute('src'),alt:e.getAttribute('alt'),"
            "hidden:e.getAttribute('aria-hidden'),w:r.width,h:r.height,bg:cs.backgroundColor})})(%s)"
            % (json.dumps(sel), json.dumps(sel))))

    def assert_logo(page, where, board):
        m = mark(page, ".brand-mark" if board else ".logo")
        assert m and m != "null", f"{where}: the brand mark is missing"
        assert m["w"] > 0 and m["h"] > 0, f"{where}: the mark has no size: {m}"
        if board:
            assert m["tag"] == "IMG" and (m["src"] or "").endswith("/_/favicon.svg"), \
                f"{where}: the mark is not the shared favicon image: {m}"
            assert m["hidden"] == "true" and m["alt"] == "", f"{where}: the image must stay decorative: {m}"
        else:
            assert m["svg"], f"{where}: the mark does not inline the SVG: {m}"
            assert m["hidden"] == "true", f"{where}: the mark must stay aria-hidden: {m}"
        assert m["text"] != "mr", f"{where}: the mark still renders the text 'mr': {m}"
        assert m["text"] == "", f"{where}: the mark carries text: {m}"
        assert 27 <= m["w"] <= 29 and 27 <= m["h"] <= 29, f"{where}: the mark is not the 28px box: {m}"
        return m

    def assert_icon(page, page_url, where):
        href = page.js("(function(){var e=document.querySelector('link[rel~=icon]');"
                       "return e?e.getAttribute('href'):null})()")
        assert href, f"{where}: the page has no icon link"
        from urllib.parse import urljoin
        status, headers, body = http("GET", urljoin(page_url, href))
        assert status == 200, f"{where}: {href} answered {status}"
        assert body.lstrip().startswith(b"<svg"), f"{where}: {href} is not an SVG"
        return href

    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    page = fresh_page(chrome, server + "/signin", width, 800, "!!document.querySelector('.card')", theme)
    try:
        assert_logo(page, f"sign-in at {width} ({theme})", board=False)
        assert page.js("(function(){var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);"
                       "while(w.nextNode()){if(w.currentNode.textContent.trim()==='mr')return false}return true})()")
        assert_icon(page, server + "/signin", f"sign-in at {width} ({theme})")
    finally:
        close_page(chrome, page)

    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    for path, wait, sel, board, name in [
            ("/", "!!document.querySelector('.search')", ".logo", False, "home"),
            ("/settings", "!!document.querySelector('.settings-tab')", ".settings-bar .brand-mark", True, "settings"),
            (f"/r/{data['id']}/", "!!document.querySelector('.dzbar')", ".dzbar .brand-mark", True,
             "board header")]:
        url = server + path
        page = fresh_page(chrome, url, width, 800, wait, theme)
        try:
            assert_logo(page, f"{name} at {width} ({theme})", board=board)
            assert_icon(page, url, f"{name} at {width} ({theme})")
        finally:
            close_page(chrome, page)


BAR_PROBE = (
    "(function(){var i=document.querySelector('.dzbar .proj-icon'),m=document.querySelector('.dzbar .brand-mark');"
    "var out={icon:null,mark:m?m.getAttribute('src'):null};"
    "if(i){var r=i.getBoundingClientRect(),cs=getComputedStyle(i);"
    "out.icon={tag:i.tagName,src:i.src,alt:i.getAttribute('alt'),loaded:i.complete&&i.naturalWidth>0,"
    "w:r.width,h:r.height,display:cs.display,visibility:cs.visibility};}"
    "return JSON.stringify(out)})()")


@pytest.mark.parametrize("width", [390, 1280])
def test_the_board_bar_shows_the_project_icon_not_the_mock_review_mark(env, server, tmp_path, chrome,  # noqa: F811
                                                                      monkeypatch, width):
    """A round with a design icon shows that icon in the bar, labelled by the design; the mock-review mark does not
    show beside it. A round with no design icon still carries the mock-review mark."""
    branded_dir = tmp_path / "branded"
    branded_dir.mkdir()
    design = _design(branded_dir)
    (design / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30 30"><rect width="30" height="30" fill="#090b13"/>'
        '<rect x="7" y="12" width="4" height="12" fill="#6a91fc"/></svg>', encoding="utf-8")
    branded = store.create_review("HDR project icon", "", ORIGIN, "tailscale",
                                  store.parse_round_spec(prototype=str(write_proto(tmp_path)), design=str(design)),
                                  wake=False)
    plain = store.create_review("HDR no icon", "", ORIGIN, "tailscale",
                                store.parse_round_spec(prototype=str(write_proto(tmp_path))), wake=False)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")

    page = fresh_page(chrome, server + f"/r/{branded['id']}/", width, 800, "!!document.querySelector('.dzbar')")
    try:
        assert page.until("(function(){var i=document.querySelector('.dzbar .proj-icon');"
                          "return !!i && i.complete && i.naturalWidth > 0})()"), \
            f"{width}: the round's design icon never loaded in the board bar"
        state = json.loads(page.js(BAR_PROBE))
        icon = state["icon"]
        assert icon, f"{width}: the board bar has no project icon: {state}"
        assert icon["tag"] == "IMG" and icon["src"] == server + f"/r/{branded['id']}/rounds/1/a/design/icon.svg", \
            f"{width}: the bar icon is not the round's design/<icon> file: {icon}"
        assert icon["alt"] == "Proj", f"{width}: the bar icon alt is not the design label: {icon}"
        assert icon["w"] > 0 and icon["h"] > 0 and icon["display"] != "none" and icon["visibility"] != "hidden", \
            f"{width}: the bar icon is not visible: {icon}"
        assert state["mark"] is None, f"{width}: the mock-review mark shows beside a branded round: {state}"
    finally:
        close_page(chrome, page)

    page = fresh_page(chrome, server + f"/r/{plain['id']}/", width, 800, "!!document.querySelector('.dzbar')")
    try:
        state = json.loads(page.js(BAR_PROBE))
        assert state["icon"] is None, f"{width}: a round without a design icon has a project icon: {state}"
        assert state["mark"] and state["mark"].endswith("/_/favicon.svg"), \
            f"{width}: a round without a design icon must keep the mock-review mark: {state}"
    finally:
        close_page(chrome, page)


def write_proto(tmp_path):
    src = tmp_path / "hdr-proto"
    src.mkdir(parents=True, exist_ok=True)
    path = src / "index.html"
    path.write_text("<!doctype html><html><body><h1>prototype</h1><p>page</p></body></html>", encoding="utf-8")
    return path
