"""Export in a real browser: Turndown Markdown, the SnapDOM PNG and the print page. One Chrome for the file."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import browserkit as bc
from authkit import token_of
from conftest import http
from mockreview import export, server as srv, store, threads, users

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}

PNG_HOOK = """
(function () {
  var desc = Object.getOwnPropertyDescriptor(window, 'snapdom');
  Object.defineProperty(window, 'snapdom', {
    configurable: true,
    get: function () { return desc && desc.value; },
    set: function (v) {
      try {
        var orig = v.toPng;
        v.toPng = function (el, o) {
          return orig.call(v, el, o).then(function (img) {
            window.__mrPng = { src: img && img.src, w: img && img.naturalWidth, h: img && img.naturalHeight };
            return img;
          });
        };
      } catch (e) { window.__mrPng = { error: String(e) }; }
      Object.defineProperty(window, 'snapdom', { value: v, writable: true, configurable: true });
    }
  });
})();
"""

PRINT_HOOK = "window.__prints = 0; window.print = function () { window.__prints += 1; };"


class SharedChrome:
    """One Chrome and its DevTools connection, shared by this module's pages.

    `Page`'s bring-up runs through `browserkit.with_page_bring_up`, which calls `replace()` on a transport-class
    failure; the module keeps using the new process and connection. Everything else delegates to the current Cdp, so
    a page created after a replacement talks to the live launch."""

    def __init__(self):
        self.chrome = None
        self.ws = None
        self.cdp = None

    def start(self):
        for attempt in (1, 2):
            chrome = bc.Chrome(startup=60)
            try:
                host, port, path = chrome.__enter__()
                break
            except bc.BrowserCheckError:
                chrome.__exit__(None, None, None)
                if attempt == 2:
                    raise
        ws = bc.WebSocket(host, port, path)
        try:
            cdp = bc.Cdp(ws, bc.Observer())
            try:
                cdp.call("Browser.setDownloadBehavior", {"behavior": "deny"})
            except bc.BrowserCheckError:
                pass
        except BaseException:
            ws.close()
            chrome.__exit__(None, None, None)
            raise
        self.chrome, self.ws, self.cdp = chrome, ws, cdp

    def replace(self):
        self.close_launch()
        self.start()

    def close_launch(self):
        ws, chrome = self.ws, self.chrome
        self.ws = self.chrome = self.cdp = None
        if ws is not None:
            ws.close()
        if chrome is not None:
            chrome.__exit__(None, None, None)

    def close(self):
        self.close_launch()

    def __getattr__(self, name):
        cdp = self.__dict__.get("cdp")
        if cdp is None:
            raise AttributeError(name)
        return getattr(cdp, name)


@pytest.fixture(scope="module")
def chrome():
    try:
        bc.find_browser()
    except bc.BrowserUnavailable:
        pytest.skip("no Chrome/Chromium/Edge installed")
    browser = SharedChrome()
    browser.start()
    try:
        yield browser
    finally:
        browser.close()


HEADED_EVIDENCE_ENV = "MOCKREVIEW_HEADED_EVIDENCE"
HEADED_EVIDENCE_DEFAULT = Path(__file__).resolve().parent.parent / ".dispatch" / "evidence" / "headed"


def window_metrics(page):
    """The real window, screen and visibility measurements a headed run must trust."""
    return json.loads(page.js(
        "JSON.stringify({innerWidth:window.innerWidth,innerHeight:window.innerHeight,"
        "outerWidth:window.outerWidth,outerHeight:window.outerHeight,"
        "screenX:window.screenX,screenY:window.screenY,"
        "screenWidth:screen.width,screenHeight:screen.height,"
        "availWidth:screen.availWidth,availHeight:screen.availHeight,"
        "dpr:window.devicePixelRatio,visibilityState:document.visibilityState,"
        "hidden:document.hidden})"))


def fit_headed_window(page, target_id, width, height):
    """Size the real OS window until innerWidth is exactly width; height is best effort inside the work area.

    The browser chrome deltas are measured from the page, never assumed. innerHeight is recorded as achieved and is
    legitimately smaller than height when the work area clamps the window.
    """
    window_id = page.cdp.call("Browser.getWindowForTarget", {"targetId": target_id})["windowId"]
    page.cdp.call("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})

    def resize(w, h):
        page.cdp.call("Browser.setWindowBounds", {"windowId": window_id,
                                                  "bounds": {"left": 0, "top": 25, "width": int(w), "height": int(h)}})

    metrics = window_metrics(page)
    for _ in range(5):
        delta_w = metrics["outerWidth"] - metrics["innerWidth"]
        delta_h = metrics["outerHeight"] - metrics["innerHeight"]
        resize(min(width + delta_w, metrics["availWidth"]), min(height + delta_h, max(100, metrics["availHeight"])))
        time.sleep(0.2)
        metrics = window_metrics(page)
        if metrics["innerWidth"] == width:
            break
    if metrics["innerWidth"] != width:
        raise bc.BrowserCheckError(
            f"the headed window could not reach innerWidth {width}: measured innerWidth {metrics['innerWidth']}, "
            f"outerWidth {metrics['outerWidth']}, screen.availWidth {metrics['availWidth']}", report=metrics)
    return window_id, metrics


def console_lock_state():
    """(locked, evidence) from the macOS console, never raising. Other platforms are never considered locked."""
    if sys.platform != "darwin":
        return False, {"platform": sys.platform}
    try:
        ioreg = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, timeout=15)
        plutil = subprocess.run(["plutil", "-p", "-"], input=ioreg.stdout, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, {"error": str(exc)[:200]}
    text = plutil.stdout.decode("utf-8", "replace")
    flags = {name: f'"{name}" => true' in text for name in ("CGSSessionScreenIsLocked", "IOConsoleLocked")}
    return any(flags.values()), flags


def require_headed_visibility(metrics):
    """Raise unless the headed window is really visible; a hidden or locked-console window may not be trusted."""
    measured = ", ".join(f"{key} {value!r}" for key, value in sorted(metrics.items()))
    locked, lock = console_lock_state()
    if locked:
        raise bc.BrowserCheckError(
            "the headed browser window cannot be trusted: the macOS console session is locked "
            f"({lock}), even if Chrome reports document.visibilityState {metrics['visibilityState']!r} and "
            f"document.hidden {metrics['hidden']!r}; measured {measured}. "
            "Unlock the console and run the headed check again.", report=dict(metrics, console=lock))
    if metrics["visibilityState"] == "visible" and metrics["hidden"] is False:
        return
    raise bc.BrowserCheckError(
        f"the headed browser window is not visible: document.visibilityState is "
        f"{metrics['visibilityState']!r}, document.hidden is {metrics['hidden']!r} ({measured}). "
        "The macOS console session may be locked: unlock it and run the headed check again.",
        report=dict(metrics, console=lock))


def write_headed_evidence(page, window_id, metrics, width, height):
    """A receipt plus a desktop capture, only after the visibility gate passed. No pixel assertion is made here."""
    out = Path(os.environ.get(HEADED_EVIDENCE_ENV) or HEADED_EVIDENCE_DEFAULT)
    out.mkdir(parents=True, exist_ok=True)
    shot = out / f"desktop-{width}x{height}.png"
    capture = {"path": str(shot)}
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(["screencapture", "-x", str(shot)], capture_output=True, timeout=30)
            capture.update(returncode=proc.returncode, stderr=proc.stderr.decode("utf-8", "replace")[-400:])
        except (OSError, subprocess.SubprocessError) as exc:
            capture["error"] = str(exc)[:400]
    else:
        capture["error"] = "desktop capture is macOS-only"
    receipt = dict(metrics)
    receipt.update(requestedWidth=width, requestedHeight=height,
                   windowBounds=page.cdp.call("Browser.getWindowBounds", {"windowId": window_id}).get("bounds") or {},
                   browserProduct=page.cdp.call("Browser.getVersion").get("product", ""),
                   browserExecutable=bc.find_browser(), desktopCapture=capture)
    (out / f"receipt-{width}x{height}.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Page:
    def __init__(self, cdp, url, script=None, wait=None, width=1280, height=900, media=None, js=True):
        self.cdp = cdp
        self.headed_metrics = None

        def bring_up():
            tid = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            self.sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
            for m in ("Page.enable", "Runtime.enable"):
                cdp.call(m, session=self.sid)
            if script:
                cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": script}, session=self.sid)
            if not js:
                cdp.call("Emulation.setScriptExecutionDisabled", {"value": True}, session=self.sid)
            headed = bc.headed_requested()
            if not headed:
                cdp.call("Emulation.setDeviceMetricsOverride",
                         {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}, session=self.sid)
            if media:
                cdp.call("Emulation.setEmulatedMedia",
                         {"features": [{"name": "prefers-color-scheme", "value": media}]}, session=self.sid)
            cdp.call("Page.navigate", {"url": url}, session=self.sid)
            return tid, headed

        tid, headed = bc.with_page_bring_up(cdp, bring_up)
        if headed:
            self.window_id, self.headed_metrics = fit_headed_window(self, tid, width, height)
            require_headed_visibility(self.headed_metrics)
            write_headed_evidence(self, self.window_id, self.headed_metrics, width, height)
        if wait:
            assert self.until(wait, timeout=60), "the page never reached its ready state"

    def js(self, expr):
        return self.cdp.evaluate(expr, session=self.sid)

    def until(self, expr, timeout=20.0):
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

    def click_at(self, x, y):
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            p = {"type": kind, "x": x, "y": y,
                 "button": "none" if kind == "mouseMoved" else "left", "clickCount": 1}
            if kind != "mouseMoved":
                p["buttons"] = 1
            self.cdp.call("Input.dispatchMouseEvent", p, session=self.sid)

    def click_sel(self, sel):
        point = json.loads(self.js(
            "(function(){var e=document.querySelector(%s);if(!e)return 'null';"
            "var r=e.getBoundingClientRect();return JSON.stringify([r.left+r.width/2,r.top+r.height/2]);})()"
            % json.dumps(sel)))
        assert point, f"{sel} is not on the page"
        self.click_at(*point)

    def key(self, key, code=None, vk=None, modifiers=None):
        down = {"type": "keyDown", "key": key}
        if code:
            down["code"] = code
        if vk:
            down["windowsVirtualKeyCode"] = vk
        if modifiers is not None:
            down["modifiers"] = modifiers
        self.cdp.call("Input.dispatchKeyEvent", down, session=self.sid)
        up = {"type": "keyUp", "key": key}
        if code:
            up["code"] = code
        if vk:
            up["windowsVirtualKeyCode"] = vk
        if modifiers is not None:
            up["modifiers"] = modifiers
        self.cdp.call("Input.dispatchKeyEvent", up, session=self.sid)


def metric_report(page, selectors):
    """Viewport width, document scroll width and each selector's rect plus a real hit-test at its centre."""
    return json.loads(page.js(
        "(function(sels){var out={vw:window.innerWidth,vh:window.innerHeight,"
        "scroll:document.documentElement.scrollWidth,items:[]};"
        "sels.forEach(function(s){var e=document.querySelector(s);"
        "if(!e){out.items.push({sel:s,missing:true});return;}"
        "var r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;"
        "var hit=(x>=0&&y>=0&&x<window.innerWidth&&y<window.innerHeight)?document.elementFromPoint(x,y):null;"
        "out.items.push({sel:s,left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,"
        "disabled:!!e.disabled,hittable:!!hit&&(hit===e||e.contains(hit))});});"
        "return JSON.stringify(out);})(" + json.dumps(selectors) + ")"))


def bar_overflow(page):
    return page.js("(function(){var b=document.querySelector('.dzbar');"
                   "return b?[b.scrollWidth,b.clientWidth]:null})()")


def new_modern_review(tmp_path, name, kind, look=None):
    """A review whose round uses the modern renderer, the way tests/test_board_share.py builds one."""
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
    return store.create_review(name, "", ORIGIN, "tailscale", spec, look=look, wake=False)


# Long unbroken text in every consumer the export document emits. The title is the reviewer's exact control.
LONG_WORD = "Migration-" + "x" * 120
LONG_URL = "https://example.test/" + "a" * 179
LONG_CARD = "Card-" + "y" * 115
EXPORT_WIDTHS = [320, 375, 390, 640, 641, 768, 1280, 1920]
EXPORT_THEMES = ["dark", "light"]
EXPORT_CONSUMERS = [
    "h1", "p", "li", "th", "td",
    ".mr-meta", ".mr-brief", ".mr-block-title", ".mr-text", ".mr-callout",
    ".mr-card", ".mr-card-title", ".mr-card-text",
    ".mr-table", ".mr-list li", ".mr-quote", ".mr-rule", ".mr-caption",
    ".mr-long-link", ".mr-long-link a",
]
COMMENT_CONSUMERS = [".mr-comment", ".mr-comment-anchor", ".mr-comment-body"]


def long_text_review(tmp_path, name="long-text"):
    """A review whose title and every body consumer carry a 120 character unbroken word."""
    decisions = {
        "blocks": [
            {"type": "text", "title": "Long word", "text": LONG_WORD},
            {"type": "callout", "tone": "warn", "title": "Long callout", "text": LONG_WORD},
            {"type": "piece", "id": "p1", "title": "Long link",
             "html": '<p class="mr-long-link"><a href="%s">%s</a></p>' % (LONG_URL, LONG_URL)},
            {"type": "cards", "title": "Cards", "items": [{"title": LONG_CARD, "text": LONG_WORD}]},
            {"type": "table", "title": "Table", "columns": ["Name", "Note"],
             "rows": [[LONG_WORD, LONG_WORD]]},
            {"type": "defaults", "title": "Defaults", "text": "Where silent:", "items": [LONG_WORD]},
            {"type": "settled", "title": "Settled",
             "items": [{"title": LONG_WORD, "quote": LONG_WORD, "rule": LONG_WORD}]},
            {"type": "mock", "id": "m1", "title": "Mock", "html": "<p>mock</p>", "caption": LONG_WORD},
            {"type": "question", "id": "q1", "title": "Question", "text": "Pick.",
             "recommendation": {"answer": LONG_WORD, "why": LONG_WORD}},
        ]
    }
    src = tmp_path / name / "decisions.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps(decisions), encoding="utf-8")
    data = store.create_review(LONG_WORD, "Brief-" + "b" * 120, ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(src)), wake=False)
    threads.create(data["id"], "owner", "Owner", "reviewer", "Comment-" + "m" * 120,
                   anchor={"block": "m1", "x": 0.5, "y": 0.4}, round_n=1)
    return data


def set_document(page, html):
    """Render a fetched response body in the page without a download navigation."""
    frame_id = page.cdp.call("Page.getFrameTree", session=page.sid)["frameTree"]["frame"]["id"]
    page.cdp.call("Page.setDocumentContent", {"frameId": frame_id, "html": html}, session=page.sid)


def export_fit_report(page, selectors=EXPORT_CONSUMERS):
    """Per-consumer scroll/client widths and computed overflow-wrap, plus the document's own fit."""
    return json.loads(page.js(
        "(function(sels){var d=document.documentElement,out={width:innerWidth,client:d.clientWidth,"
        "scroll:d.scrollWidth,items:[]};sels.forEach(function(s){var e=document.querySelector(s);"
        "if(!e){out.items.push({sel:s,missing:true});return;}var r=e.getBoundingClientRect();"
        "out.items.push({sel:s,scroll:e.scrollWidth,client:e.clientWidth,left:r.left,right:r.right,"
        "wrap:getComputedStyle(e).overflowWrap});});return JSON.stringify(out);})("
        + json.dumps(selectors) + ")"))


def assert_export_fits(report):
    """The document never grows wider than its viewport and every probed text consumer fits its own box."""
    assert report["scroll"] == report["client"], \
        f"the export document scrolls horizontally: scrollWidth {report['scroll']} vs clientWidth {report['client']}"
    for item in report["items"]:
        assert not item.get("missing"), f"{item['sel']} is missing from the export document"
        assert item["scroll"] <= item["client"] + 1, \
            f"{item['sel']} overflows its box: scrollWidth {item['scroll']} vs clientWidth {item['client']}"
        assert item["left"] >= -1 and item["right"] <= report["client"] + 1, \
            f"{item['sel']} sits outside the {report['client']}px viewport: {item}"
    wraps = {item["sel"]: item["wrap"] for item in report["items"] if item["sel"] in ("h1", "p", "li", "th", "td")}
    assert wraps == dict.fromkeys(wraps, "anywhere"), wraps


# The 2.0 header: four visible actions and one More menu. At phone widths Comment moves to the bottom bar,
# the width switch and Share move into More, and the avatar plus More stay in the header.
HEADER_CONTROLS = [".dzbar", ".bar-comment", ".dzbar .wseg", ".dzbar .who", ".export-open", ".share-open", ".av-self"]
GUEST_CONTROLS = [".dzbar", ".bar-comment", ".dzbar .wseg", ".dzbar .who", ".export-open", ".av-self"]
PHONE_OWNER_CONTROLS = [".dzbar", ".foot-comment", ".more-open", ".av-self", ".dzbar .pb-title"]
PHONE_GUEST_CONTROLS = [".dzbar", ".foot-comment", ".more-open", ".av-self", ".dzbar .pb-title"]


def assert_header_inside(page, width, controls=HEADER_CONTROLS):
    report = metric_report(page, controls)
    assert report["scroll"] <= width, \
        f"document scrollWidth {report['scroll']} exceeds the {width}px viewport"
    bar = bar_overflow(page)
    assert bar and bar[0] <= bar[1], f"the header needs horizontal scrolling: scrollWidth {bar[0]} > clientWidth {bar[1]}"
    for item in report["items"]:
        sel = item["sel"]
        assert not item.get("missing"), f"{sel} is missing from the modern header"
        assert item["width"] > 0 and item["height"] > 0, f"{sel} is laid out with no size: {item}"
        assert item["left"] >= 0 and item["right"] <= width, \
            f"{sel} sits outside the {width}px viewport: left {item['left']} right {item['right']}"
        assert item["hittable"], f"{sel} cannot be hit at its centre, another element covers it: {item}"
    return report


def rects_intersect(a, b):
    return a["left"] < b["right"] and b["left"] < a["right"] and a["top"] < b["bottom"] and b["top"] < a["bottom"]


def assert_wseg_clears_export(page, width):
    """The width segment is reachable and every segment button is really hittable: in the header on desktop, in
    More on a phone. A real click activates exactly that button and never leaves the Export menu open."""
    phone = width <= 600
    if phone:
        page.click_sel(".export-open")
        assert page.until("!!document.querySelector('.export-menu')"), "More must open on a phone"
        paint(page)
        root = ".export-menu .wseg"
    else:
        root = ".dzbar .wseg"
        assert not page.js("!!document.querySelector('.export-menu')"), "the Export menu is open before any click"
    count = page.js("document.querySelectorAll('%s .seg-btn').length" % root)
    assert count >= 2, "the header must offer the width segment"
    sels = ([root] + [root + " .seg-btn:nth-child(%d)" % (i + 1) for i in range(count)])
    if not phone:
        sels.append(".export-open")
    report = metric_report(page, sels)
    by_sel = {item["sel"]: item for item in report["items"]}
    assert report["scroll"] <= width, \
        f"document scrollWidth {report['scroll']} exceeds the {width}px viewport"
    for sel in sels:
        item = by_sel[sel]
        assert not item.get("missing"), f"{sel} is missing"
        assert item["left"] >= 0 and item["right"] <= width, \
            f"{sel} sits outside the {width}px viewport: left {item['left']} right {item['right']}"
        assert item["hittable"], f"{sel} cannot be hit at its centre, another element covers it: {item}"
    if not phone:
        seg, export = by_sel[root], by_sel[".export-open"]
        assert not rects_intersect(seg, export), \
            f"the width segment {seg} intersects Export {export} at {width}px"
    for i in range(count):
        if phone:
            page.click_sel(root + " .seg-btn:nth-child(%d)" % (i + 1))
        else:
            page.click_sel(".dzbar .wseg .seg-btn:nth-child(%d)" % (i + 1))
        ok = page.until(
            "(function(){var b=document.querySelectorAll('.dzbar .wseg .seg-btn');"
            "if(b.length!==%d||!b[%d].classList.contains('on'))return false;"
            "for(var j=0;j<b.length;j++){if(j!==%d&&b[j].classList.contains('on'))return false;}"
            "return !!document.querySelector('.dzbar .pb-name');})()" % (count, i, i))
        assert ok, f"a real click on segment button {i + 1} at {width}px did not activate exactly that button"
        if phone:
            page.click_sel(".export-open")
            assert page.until("!!document.querySelector('.export-menu')"), "More must reopen on a phone"
            paint(page)
    return count


THREE_WIDTHS = {"devices": ["desktop", "tablet", "mobile"]}


@pytest.mark.parametrize("width", [375, 390])
@pytest.mark.parametrize("kind", ["decisions", "prototype"])
@pytest.mark.parametrize("who,look", [("owner", None), ("owner", THREE_WIDTHS),
                                      ("guest", None), ("guest", THREE_WIDTHS)],
                         ids=["owner-two-widths", "owner-three-widths", "guest-two-widths", "guest-three-widths"])
def test_export_controls_fit_narrow_modern_header(env, server, tmp_path, chrome, monkeypatch, kind, width, who, look):
    data = new_modern_review(tmp_path, who + " " + kind + (" 3w" if look else ""), kind, look=look)
    if who == "guest":
        monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
        token = token_of(users.share("test", data["id"], "create", "commenter")["path"])
        page = Page(chrome, f"{server}/s/{token}", width=width, height=844,
                    wait="!!document.querySelector('.av-self')")
        # Commenting needs only the `comment` capability, which a commenter guest has; Share still needs `manage`.
        assert page.js("!!document.querySelector('%s') && !document.querySelector('.share-open')"
                       % (".foot-comment" if width <= 600 else ".bar-comment")), \
            "a commenter guest page keeps the Comment control and has no Share button"
        assert_header_inside(page, width, PHONE_GUEST_CONTROLS if width <= 600 else GUEST_CONTROLS)
    else:
        page = Page(chrome, f"{server}/r/{data['id']}/", width=width, height=844,
                    wait="!!document.querySelector('#dzbar')")
        assert_header_inside(page, width, PHONE_OWNER_CONTROLS if width <= 600 else HEADER_CONTROLS)
    assert_wseg_clears_export(page, width)


@pytest.mark.parametrize("look", [None, THREE_WIDTHS], ids=["two-widths", "three-widths"])
def test_guest_controls_clear_export_at_320(env, server, tmp_path, chrome, monkeypatch, look):
    data = new_modern_review(tmp_path, "guest 320" + (" 3w" if look else ""), "decisions", look=look)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    token = token_of(users.share("test", data["id"], "create", "commenter")["path"])
    page = Page(chrome, f"{server}/s/{token}", width=320, height=844,
                wait="!!document.querySelector('.av-self')")
    assert_header_inside(page, 320, PHONE_GUEST_CONTROLS)
    assert_wseg_clears_export(page, 320)


def test_export_menu_stays_within_phone_viewport(env, server, tmp_path, chrome):
    data = new_modern_review(tmp_path, "phone menu", "decisions")
    page = Page(chrome, f"{server}/r/{data['id']}/", width=390, height=844,
                wait="!!document.querySelector('#dzbar')")
    assert page.js("document.querySelectorAll('.pin, .mpin').length") == 0, "a fresh board has no pins"

    trusted_click(page, ".foot-comment")
    assert page.until("document.body.classList.contains('tool-comment')"), "Comment mode must turn on"
    assert page.js("document.querySelector('.foot-comment').getAttribute('aria-pressed')") == "true"

    trusted_click(page, ".export-open")
    assert page.until("!!document.querySelector('.export-menu')"), "the Export menu must open"
    paint(page)
    assert page.js("document.querySelector('.export-open').getAttribute('aria-expanded')") == "true"
    assert page.js("document.querySelectorAll('.pin, .mpin').length") == 0, \
        "opening Export in Comment mode must not create a pin"

    items = [".export-menu", '.export-item[data-format="png"]', '.export-item[data-format="pdf"]',
             '.export-item[data-format="html"]', '.export-item[data-format="markdown"]', ".export-comments"]
    report = metric_report(page, items)
    for item in report["items"]:
        sel = item["sel"]
        assert not item.get("missing"), f"{sel} is missing from the Export menu"
        assert item["width"] > 0 and item["height"] > 0, f"{sel} is laid out with no size: {item}"
        assert item["left"] >= 0 and item["right"] <= report["vw"], \
            f"{sel} sits outside the {report['vw']}px viewport: left {item['left']} right {item['right']}"
    for item in report["items"][1:]:
        assert not item["disabled"], f"{item['sel']} is disabled"
        assert item["hittable"], f"{item['sel']} cannot be hit at its centre: {item}"

    checked = page.js("document.querySelector('.export-comments').getAttribute('aria-checked')")
    trusted_click(page, ".export-comments")
    assert page.until("document.querySelector('.export-comments').getAttribute('aria-checked') !== %s"
                      % json.dumps(checked)), "the Include comments toggle must flip with a real click"

    page.key("Escape", "Escape", 27)
    assert page.until("!document.querySelector('.export-menu')"), "Esc must close the Export menu"
    assert page.js("document.body.classList.contains('tool-comment')"), "Esc must leave Comment mode on"
    assert page.js("document.querySelector('.foot-comment').getAttribute('aria-pressed')") == "true"
    assert page.js("document.querySelectorAll('.pin, .mpin').length") == 0, "Esc must not create a pin"

    trusted_click(page, ".foot-comment")
    assert page.until("!document.body.classList.contains('tool-comment')"), "Comment mode must turn off"
    assert_header_inside(page, 390, PHONE_OWNER_CONTROLS)


def test_markdown_export_converts_the_same_document(server, review, chrome):
    b = Page(chrome, f"{server}/r/{review['id']}/", wait="!!document.querySelector('.export-open')")
    b.js("window.__md=null;window.__mdErr=null;var __orig=URL.createObjectURL;"
         "URL.createObjectURL=function(blob){blob.text().then(function(t){window.__md=t;},"
         "function(e){window.__mdErr=String(e);});return __orig.call(URL,blob);};")
    b.press(".export-open")
    assert b.until("!!document.querySelector('.export-menu')")
    assert b.js("document.querySelector('.export-comments').getAttribute('aria-checked')") == "true"
    b.press('.export-item[data-format="markdown"]')
    assert b.until("!!window.__md", timeout=30), "the Markdown download must be produced"
    md = b.js("window.__md")
    assert md.startswith("# Test review")
    assert "## Comments" in md
    assert "<script" not in md.lower()

    b.press(".export-open")
    assert b.until("!!document.querySelector('.export-menu')")
    b.press(".export-comments")
    assert b.until("document.querySelector('.export-comments').getAttribute('aria-checked') === 'false'")
    b.js("window.__md=null")
    b.press('.export-item[data-format="markdown"]')
    assert b.until("!!window.__md", timeout=30)
    md = b.js("window.__md")
    assert "# Test review" in md
    assert "## Comments" not in md


def test_markdown_export_carries_real_threads_and_replies(server, review, chrome):
    owner = users.owner_user()
    actor = f"user:{owner['id']}"
    thread = threads.create(review["id"], actor, owner["name"], "owner", "Markdown thread body.",
                            anchor={"block": "a", "x": 0.4, "y": 0.6})
    threads.reply_comment(review["id"], thread["id"], actor, owner["name"], "owner", "Markdown reply body.")
    b = Page(chrome, f"{server}/r/{review['id']}/", wait="!!document.querySelector('.export-open')")
    b.js("window.__md=null;window.__mdErr=null;var __orig=URL.createObjectURL;"
         "URL.createObjectURL=function(blob){blob.text().then(function(t){window.__md=t;},"
         "function(e){window.__mdErr=String(e);});return __orig.call(URL,blob);};")
    b.press(".export-open")
    assert b.until("!!document.querySelector('.export-menu')")
    b.press('.export-item[data-format="markdown"]')
    assert b.until("!!window.__md", timeout=30), "the Markdown download must be produced"
    md = b.js("window.__md")
    assert "Markdown thread body." in md
    assert "Markdown reply body." in md


def test_png_capture_renders_the_flattened_document_at_the_requested_width(server, review, chrome):
    url = f"{server}/r/{review['id']}/export/capture?round=1&width=390"
    b = Page(chrome, url, script=PNG_HOOK, wait="!!document.querySelector('.mr-header')")
    got = b.until("window.__mrPng || (document.querySelector('.mr-capture-status') && "
                  "document.querySelector('.mr-capture-status').textContent.indexOf('Could not') === 0)", timeout=60)
    assert got
    result = b.js("window.__mrPng")
    if not result or result.get("error"):
        status = b.js("document.querySelector('.mr-capture-status') && "
                      "document.querySelector('.mr-capture-status').textContent")
        pytest.fail(f"SnapDOM did not produce the PNG: {result or status}")
    assert result["src"].startswith("data:image/png;base64,")
    assert len(result["src"]) > 1000
    assert result["w"] == 390, result
    assert result["h"] > 100, result
    assert b.js("!document.querySelector('.pin, .mpin, .mr-comments, [data-comment-toggle]')")


def test_the_print_page_calls_print_once_with_the_print_stylesheet(server, review, chrome):
    b = Page(chrome, f"{server}/r/{review['id']}/export/print?round=1&comments=1",
             script=PRINT_HOOK, wait="!!document.querySelector('.mr-header')")
    assert b.until("window.__prints >= 1")
    time.sleep(0.3)
    assert b.js("window.__prints") == 1
    css = b.js("(function(){var s=document.querySelector('style');return s?s.textContent:''})()")
    assert "@media print" in css
    assert b.js("!!document.querySelector('.mr-comments')")


def test_the_print_page_carries_real_threads_and_replies(server, review, chrome):
    owner = users.owner_user()
    actor = f"user:{owner['id']}"
    thread = threads.create(review["id"], actor, owner["name"], "owner", "Printed thread body.",
                            anchor={"block": "a", "x": 0.4, "y": 0.6})
    threads.reply_comment(review["id"], thread["id"], actor, owner["name"], "owner", "Printed reply body.")
    b = Page(chrome, f"{server}/r/{review['id']}/export/print?round=1&comments=1",
             script=PRINT_HOOK, wait="!!document.querySelector('.mr-header')")
    assert b.until("!!document.querySelector('.mr-comments')")
    text = b.js("document.querySelector('.mr-comments').textContent")
    assert "Printed thread body." in text
    assert "Printed reply body." in text


@pytest.mark.parametrize("width", EXPORT_WIDTHS)
@pytest.mark.parametrize("theme", EXPORT_THEMES)
def test_export_print_contains_long_text_at_every_width_and_theme(env, server, tmp_path, chrome, width, theme):
    """The print route wraps every text consumer at every width, in both themes, without clipping content."""
    data = long_text_review(tmp_path, "print-%s-%s" % (theme, width))
    page = Page(chrome, f"{server}/r/{data['id']}/export/print?round=1&comments=1",
                width=width, height=844, media=theme, wait="!!document.querySelector('.mr-header h1')")
    try:
        assert page.js("document.querySelector('.mr-header h1').textContent") == LONG_WORD
        report = export_fit_report(page)
        assert_export_fits(report)
    finally:
        close_page(page)


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("theme", EXPORT_THEMES)
def test_an_exported_comment_body_contains_long_text(env, tmp_path, chrome, width, theme):
    """The comment consumer fits too when the export carries a thread (the builder's explicit threads path)."""
    data = long_text_review(tmp_path, "comment-%s-%s" % (theme, width))
    thread = {
        "anchor": {"label": LONG_WORD}, "author": {"name": "Owner", "kind": "member"},
        "at": "", "body": "Comment-" + "m" * 120, "resolved": False, "replies": [],
    }
    html = export.build_export_html(store.load(data["id"]), 1, True, threads=[thread])
    page = Page(chrome, "about:blank", width=width, height=844, media=theme)
    try:
        set_document(page, html)
        assert page.until("!!document.querySelector('.mr-comment-body')")
        assert page.js("document.querySelector('.mr-comment-body').textContent") == thread["body"]
        assert_export_fits(export_fit_report(page, EXPORT_CONSUMERS + COMMENT_CONSUMERS))
    finally:
        close_page(page)


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("theme", EXPORT_THEMES)
def test_the_html_export_route_contains_long_text(env, server, tmp_path, chrome, width, theme):
    """The downloaded HTML route carries the same containment, without comments on that route."""
    data = long_text_review(tmp_path, "html-%s-%s" % (theme, width))
    status, _, body = http("GET", f"{server}/r/{data['id']}/export.html?comments=0")
    assert status == 200
    html = body.decode("utf-8")
    assert '<section class="mr-comments"' not in html
    page = Page(chrome, "about:blank", width=width, height=844, media=theme)
    try:
        set_document(page, html)
        assert page.until("!!document.querySelector('.mr-header h1')")
        assert page.js("document.querySelector('.mr-header h1').textContent") == LONG_WORD
        assert_export_fits(export_fit_report(page))
    finally:
        close_page(page)


def test_css_escaped_urls_are_gone_after_the_browser_parses_the_export(server, review, chrome, tmp_path):
    source = r'<div id="css-escape" style="background-image:\75rl(\2f\2f export-leak.example.test/pixel)">safe prose</div>'
    css = r'.x{background-image:\75rl(\2f\2f export-leak.example.test/pixel)}'
    fixture = tmp_path / "hostile-prototype.html"
    fixture.write_text("<!doctype html><html><head><style>" + css + "</style></head><body>" + source + "</body></html>",
                       encoding="utf-8")
    created = store.create_review("CSS export attack", "", ORIGIN, "local",
                                  store.parse_round_spec(prototype=str(fixture)), wake=False)
    status, _, body = http("GET", f'{server}/r/{created["id"]}/export.html?comments=0')
    assert status == 200
    exported = body.decode("utf-8")
    page = Page(chrome, f'{server}/r/{review["id"]}/', wait='!!document.querySelector(".export-open")')
    decl = export._escape_css(css).split("{", 1)[1].rsplit("}", 1)[0]
    result = page.js(
        "(function(){var doc=new DOMParser().parseFromString(" + json.dumps(exported) + ',"text/html");'
        'var d=document.createElement("div");d.style.cssText=' + json.dumps(decl) + ";"
        'return {inline:doc.querySelector("#css-escape").style.backgroundImage,'
        "stylesheet:d.style.backgroundImage};})()")
    assert all("export-leak" not in value for value in result.values()), result


# --- a trusted mouse click on the prototype board's Export menu ---------------------------------------------------
HOVERED = ("(function(s){var e=document.querySelector(s);if(!e)return false;"
           "return Array.prototype.some.call(document.querySelectorAll(':hover'),"
           "function(n){return n===e||e.contains(n)})})(%s)")

PROTO_STATE = """
JSON.stringify({
  menu: !!document.querySelector('.export-menu'),
  expanded: document.querySelector('.export-open') ? document.querySelector('.export-open').getAttribute('aria-expanded') : null,
  checked: document.querySelector('.export-comments')
    ? document.querySelector('.export-comments').getAttribute('aria-checked') : null,
  popover: (document.querySelector('.popover') || {}).textContent || '',
  pins: document.querySelectorAll('.pin, .mpin, #pins-proto li').length,
  draft: (document.querySelector('.sendbar') || {}).textContent || ''
})"""


def settle(page, seconds):
    """A real wall-clock wait: Cdp.pump(timeout) returns as soon as ONE websocket message arrives, so pumping alone
    is not a wait. Returns the measured elapsed seconds."""
    start = time.monotonic()
    deadline = start + seconds
    while time.monotonic() < deadline:
        page.cdp.pump(0.02)
        time.sleep(0.005)
    return time.monotonic() - start


def paint(page):
    """Commit one frame and let it settle for a measured 50 ms. A real browser paints continuously; a headless one
    only paints on demand, and until it does, its input hit-test data can still answer for the sandboxed prototype
    frame under the bar rather than for a popup that was inserted in the same frame -- the click is then routed into
    the frame, not into the menu."""
    page.cdp.call("Page.captureScreenshot", {"format": "png"}, session=page.sid)
    return settle(page, 0.05)


def point_of(page, sel):
    point = json.loads(page.js(
        "(function(){var e=document.querySelector(%s);if(!e)return 'null';"
        "var r=e.getBoundingClientRect();return JSON.stringify([r.left+r.width/2,r.top+r.height/2]);})()"
        % json.dumps(sel)))
    assert point and point != "null", f"{sel} is not on the page"
    return point


def frame_click(page, sel, fx=0.5, fy=0.5):
    """A trusted click inside a sandboxed prototype frame, sent to that frame's own CDP target.

    Headless Chrome runs a sandboxed prototype in its own target; a top-session Input event at the iframe's
    coordinates is not always routed across the process boundary. A page click is still a real trusted click."""
    info = json.loads(page.js(
        "(function(){var e=document.querySelector(%s);if(!e)return 'null';"
        "var r=e.getBoundingClientRect();return JSON.stringify({w:r.width,h:r.height,src:e.src});})()" % json.dumps(sel)))
    assert info and info != "null", f"{sel} is not on the page"
    targets = page.cdp.call("Target.getTargets")["targetInfos"]
    frames = [t for t in targets if t["type"] == "iframe" and t.get("url") == info["src"]]
    assert frames, "the prototype frame is not its own target"
    rect = {"w": info["w"], "h": info["h"]}
    sid = page.cdp.call("Target.attachToTarget", {"targetId": frames[-1]["targetId"], "flatten": True})["sessionId"]
    try:
        x, y = rect["w"] * fx, rect["h"] * fy
        page.cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left",
                                                   "clickCount": 1, "buttons": 1}, session=sid)
        settle(page, 0.03)
        page.cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left",
                                                   "clickCount": 1, "buttons": 0}, session=sid)
        settle(page, 0.12)
    finally:
        page.cdp.call("Target.detachFromTarget", {"sessionId": sid})


def click_here(page, x, y):
    page.cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left",
                                               "clickCount": 1, "buttons": 1}, session=page.sid)
    settle(page, 0.03)
    page.cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left",
                                               "clickCount": 1, "buttons": 0}, session=page.sid)
    settle(page, 0.08)


def trusted_click(page, sel):
    """A deterministic pointer click on sel for headless runs: the page settles first (so a menu inserted in the
    previous frame is committed), a forced paint() commits one frame, the pointer moves there, the :hover chain must
    really reach sel, and only then press and release, each pause measured by settle(). The forced paint is a
    headless-determinism measure, not a model of a human: the same toggles also pass on base 3021984 for that reason.
    Real user click delivery is what the headed round checks; this helper does not claim to prove it."""
    settle(page, 0.06)
    paint(page)
    x, y = point_of(page, sel)
    page.cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
                  session=page.sid)
    assert page.until(HOVERED % json.dumps(sel), timeout=5), \
        f"the real pointer never reached {sel}: another element covers it, or the page never painted it"
    settle(page, 0.05)
    page.cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left",
                                               "clickCount": 1, "buttons": 1}, session=page.sid)
    settle(page, 0.03)
    page.cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left",
                                               "clickCount": 1, "buttons": 0}, session=page.sid)
    return x, y


def close_page(page):
    tid = page.cdp.call("Target.getTargetInfo", {}, session=page.sid)["targetInfo"]["targetId"]
    page.cdp.call("Target.closeTarget", {"targetId": tid})


def proto_state(page):
    return json.loads(page.js(PROTO_STATE))


def assert_nothing_commented(page, where):
    state = proto_state(page)
    assert not state["popover"].strip(), f"{where} started a comment composer: {state['popover']}"
    assert state["pins"] == 0, f"{where} left a preview or saved pin on the board"
    assert "pin" not in state["draft"].lower(), f"{where} saved a pin in the draft: {state['draft']}"


def html_export_problem(path, title):
    """Why path is not this page's finished HTML export; empty when it is."""
    if path.suffix != ".html" or path.name.endswith(".crdownload"):
        return f"{path.name} is not a finished .html download"
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return f"{path.name} does not decode as UTF-8"
    if not text.rstrip().endswith("</html>"):
        return f"{path.name} does not end with </html>: {text[-40:]!r}"
    if '<header class="mr-header">' not in text:
        return f'{path.name} has no <header class="mr-header">'
    if f"<h1>{title}</h1>" not in text:
        return f"{path.name} is not this page's review: <h1>{title}</h1> is missing"
    return ""


def wait_for_html_export(page, downloads, title, timeout=20.0):
    """Poll with measured settles for the exact filename this review's export produces; accept it only when it is a
    complete UTF-8 HTML document for this review."""
    expected = downloads / export.export_filename({"title": title}, 1, "html")
    start, problem = time.monotonic(), "the expected file never appeared"
    while time.monotonic() - start < timeout:
        if expected.exists():
            problem = html_export_problem(expected, title)
            if not problem:
                return expected
        settle(page, 0.1)
    listing = sorted(p.name for p in downloads.iterdir())
    raise AssertionError(f"the HTML item did not download a fresh export of {title!r} within {timeout:.0f}s: "
                         f"{problem}; download directory: {listing}")


def check_menu_formats(page, label, downloads, title):
    """Each format is a real click on the item that closes the menu and runs that format."""
    page.js("window.__opened=[];window.open=function(u){window.__opened.push(u);return null};"
            "window.__md=null;var __orig=URL.createObjectURL;"
            "URL.createObjectURL=function(b){b.text().then(function(t){window.__md=t},function(){});"
            "return __orig.call(URL,b)};true")
    for fmt, path in (("png", "/export/capture?round=1&width="),
                      ("pdf", "/export/print?round=1&comments="),
                      ("markdown", None)):
        trusted_click(page, ".export-open")
        assert page.until("!!document.querySelector('.export-menu')"), f"the Export menu must reopen for {fmt}"
        trusted_click(page, '.export-item[data-format="%s"]' % fmt)
        assert page.until("!document.querySelector('.export-menu')"), \
            f"a real click on the {fmt} item at {label} must close the menu"
        if fmt == "markdown":
            assert page.until("!!window.__md", timeout=30), "the Markdown item must produce the download"
            assert page.js("window.__md").startswith("# ")
        else:
            opened = page.js("window.__opened[window.__opened.length-1]")
            assert opened and path in opened, f"the {fmt} item opened {opened!r}"
        assert_nothing_commented(page, f"the {fmt} item at {label}")
    # HTML is served as an attachment, so the board hands it to the download manager; it goes last
    trusted_click(page, ".export-open")
    assert page.until("!!document.querySelector('.export-menu')"), "the Export menu must reopen for html"
    trusted_click(page, '.export-item[data-format="html"]')
    assert page.until("!document.querySelector('.export-menu')"), \
        f"a real click on the html item at {label} must close the menu"
    wait_for_html_export(page, downloads, title)
    assert_nothing_commented(page, f"the html item at {label}")


def test_prototype_export_menu_consumes_real_mouse_input(env, server, tmp_path, chrome, monkeypatch):
    """Real trusted input on the prototype board's Export menu acts on the menu and never starts a comment.

    Owner in Comment mode at 640 and a guest at 641: Include comments flips both ways, no composer or pin appears,
    every format item works, and a click that reaches the page while the menu is open dismisses the menu instead of
    stacking a composer on it. Commenting on the page itself still works once the menu is closed.
    """
    downloads = tmp_path / "downloads-owner"
    downloads.mkdir()
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    owner = new_modern_review(tmp_path, "export menu owner", "prototype")
    page = Page(chrome, f"{server}/r/{owner['id']}/", width=640, height=844,
                script="localStorage.clear();", wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    chrome.call("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(downloads)},
                session=page.sid)
    trusted_click(page, ".ctoggle")
    assert page.until("document.body.classList.contains('tool-comment')"), "Comment mode must turn on"

    trusted_click(page, ".export-open")
    assert page.until("!!document.querySelector('.export-menu')"), "the Export menu must open"
    state = proto_state(page)
    assert state["expanded"] == "true"
    assert_nothing_commented(page, "opening Export in Comment mode")

    checked = state["checked"]
    trusted_click(page, ".export-comments")
    assert page.until("document.querySelector('.export-comments') && "
                      "document.querySelector('.export-comments').getAttribute('aria-checked') !== %s"
                      % json.dumps(checked)), "a real click on Include comments must flip it"
    assert proto_state(page)["menu"], "Include comments must not close the menu"
    assert_nothing_commented(page, "the Include comments item")
    trusted_click(page, ".export-comments")
    assert page.until("document.querySelector('.export-comments').getAttribute('aria-checked') === %s"
                      % json.dumps(checked)), "a second real click must flip Include comments back"

    # a click that reaches the page while the menu is open is an outside click: the menu goes, nothing is commented
    frame_click(page, ".proto iframe", 0.2)
    assert page.until("!document.querySelector('.export-menu')"), \
        "a real click on the page must dismiss the open Export menu"
    assert_nothing_commented(page, "a click on the page under the open menu")

    # commenting on the page still works once the menu is closed
    frame_click(page, ".proto iframe", 0.5)
    assert page.until("!!document.querySelector('.popover')"), \
        "a real click on the page must still start a prototype comment once the menu is closed"
    assert page.js("(document.querySelector('.popover') || {}).textContent || ''").strip().startswith("New pin")
    page.key("Escape", "Escape", 27)
    assert page.until("!document.querySelector('.popover')"), "Esc must close the pin composer"
    assert_nothing_commented(page, "cancelling the composer")

    check_menu_formats(page, "640", downloads, owner["title"])

    # guest prototype at 641: the same menu; this guest is a commenter and keeps the Comment toggle
    # (commenting is gated on the capability), and there is no Share button
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    guest = new_modern_review(tmp_path, "export menu guest", "prototype")
    token = token_of(users.share("test", guest["id"], "create", "commenter")["path"])
    view = Page(chrome, f"{server}/s/{token}", width=641, height=844,
                script="localStorage.clear();", wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    guest_downloads = tmp_path / "downloads-guest"
    guest_downloads.mkdir()
    chrome.call("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(guest_downloads)},
                session=view.sid)
    assert view.js("!!document.querySelector('.ctoggle') && !document.querySelector('.share-open')")
    trusted_click(view, ".export-open")
    assert view.until("!!document.querySelector('.export-menu')"), "a guest must be able to open Export"
    checked = proto_state(view)["checked"]
    trusted_click(view, ".export-comments")
    assert view.until("document.querySelector('.export-comments').getAttribute('aria-checked') !== %s"
                      % json.dumps(checked)), "a real click on Include comments must flip it for a guest too"
    assert_nothing_commented(view, "the guest Include comments item")

    # This guest is a commenter, but Comment mode is off and the page probe only reports a click while Comment mode
    # is on, so this click is not a comment. The frame click itself still dismisses the menu for every role (HDR).
    frame_click(view, ".proto iframe", 0.2)
    assert view.until("!document.querySelector('.export-menu')"), \
        "a guest's click in the page must dismiss the open Export menu"
    assert_nothing_commented(view, "a guest click on the page under the open menu")

    # the Export button and Escape keep their behavior: click opens, a second click closes, Escape closes
    trusted_click(view, ".export-open")
    assert view.until("!!document.querySelector('.export-menu')")
    trusted_click(view, ".export-open")
    assert view.until("!document.querySelector('.export-menu')"), \
        "a second real click on Export must close the menu"
    trusted_click(view, ".export-open")
    assert view.until("!!document.querySelector('.export-menu')")
    view.key("Escape", "Escape", 27)
    assert view.until("!document.querySelector('.export-menu')"), "Esc must close the Export menu"
    assert proto_state(view)["expanded"] == "false"

    check_menu_formats(view, "641", guest_downloads, guest["title"])


BURST_LISTENER = """
window.__burstEvents = {pointerdown: 0, mousedown: 0, mouseup: 0, click: 0};
['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(function (k) {
  document.addEventListener(k, function () { window.__burstEvents[k] += 1; }, true);
});
true
"""

BURST_READ = """
JSON.stringify({
  after: document.querySelector('.export-comments')
    ? document.querySelector('.export-comments').getAttribute('aria-checked') : null,
  menu: !!document.querySelector('.export-menu'),
  popover: (document.querySelector('.popover') || {}).textContent || '',
  pins: document.querySelectorAll('.pin, .mpin, #pins-proto li').length,
  events: window.__burstEvents
})
"""


@pytest.mark.parametrize("guest,width", [(False, 640), (True, 641)], ids=["owner-640", "guest-641"])
def test_prototype_export_menu_burst_click_witness(env, server, tmp_path, chrome, monkeypatch, guest, width):
    """The reviewer's original sequence: open Export, click Include comments as one burst, no paint or wait.

    This witness pins "never a composer on the menu": whatever Chrome does with the click, no composer, popover,
    preview pin or saved pin may appear. Whether a sub-frame click is delivered to the menu at all is Chrome's input
    routing; it is recorded per rep, not asserted. A menu that stays open with no effect and a menu that closes
    either by the toggle or by the dismissal guard are all acceptable.
    """
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false" if guest else "true")
    data = new_modern_review(tmp_path, f"burst witness {'guest' if guest else 'owner'} {width}", "prototype")
    token = token_of(users.share("test", data["id"], "create", "commenter")["path"]) if guest else None
    url = f"{server}/s/{token}" if guest else f"{server}/r/{data['id']}/"
    for rep in range(3):
        page = Page(chrome, url, width=width, height=844, script="localStorage.clear();",
                    wait="!!document.querySelector('#dz-proto[data-ready=true]')")
        try:
            if not guest:
                page.click_sel(".ctoggle")
                assert page.until("document.body.classList.contains('tool-comment')"), "Comment mode must turn on"
            page.click_sel(".export-open")
            assert page.until("!!document.querySelector('.export-menu')"), "the Export menu must open"
            page.js(BURST_LISTENER)
            before = page.js("document.querySelector('.export-comments').getAttribute('aria-checked')")
            page.click_sel(".export-comments")
            settle(page, 0.3)
            state = json.loads(page.js(BURST_READ))
            row = {"width": width, "guest": guest, "rep": rep, "before": before, "after": state["after"],
                   "toggled": state["after"] is not None and state["after"] != before,
                   "menu": state["menu"], "popover": state["popover"], "pins": state["pins"],
                   "events": state["events"]}
            print(json.dumps(row), flush=True)
            assert_nothing_commented(page, f"the burst click on Include comments at {width} rep {rep}")
        finally:
            close_page(page)


def test_export_menu_html_download_must_be_a_fresh_export_of_this_page(env, server, tmp_path, chrome, monkeypatch):
    """A failed guest HTML export must fail even when a stale file with this review's exact expected name is present.

    The injection only touches the HTML download route (format=html); Markdown shares that route and must keep
    working, which the helper checks before the HTML item fails.
    """
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    guest = new_modern_review(tmp_path, "fresh guest html", "prototype")
    token = token_of(users.share("test", guest["id"], "create", "commenter")["path"])
    page = Page(chrome, f"{server}/s/{token}", width=641, height=844,
                script="localStorage.clear();", wait="!!document.querySelector('#dz-proto[data-ready=true]')")
    downloads = tmp_path / "downloads-guest-negative"
    downloads.mkdir()
    stale = downloads / export.export_filename(guest, 1, "html")
    stale.write_text('<!doctype html><html><body><header class="mr-header"><h1>stale owner export</h1></header>'
                     "</body></html>", encoding="utf-8")
    real, failed = srv.Handler._export, []

    def broken(self, who, rid, kind, decision):
        if kind == "html" and "format=md" not in self.path:
            failed.append((rid, kind, self.path))
            self.send_error(503, "injected HTML export failure")
            return
        return real(self, who, rid, kind, decision)

    monkeypatch.setattr(srv.Handler, "_export", broken)
    try:
        with pytest.raises(AssertionError) as excinfo:
            check_menu_formats(page, "641", downloads, guest["title"])
        assert "did not download a fresh export" in str(excinfo.value), str(excinfo.value)
        assert failed and all(kind == "html" and "format=html" in path for _, kind, path in failed), failed
        assert sorted(p.name for p in downloads.iterdir()) == [stale.name], \
            f"a failed HTML export must not be accepted or create a file: {sorted(p.name for p in downloads.iterdir())}"
    finally:
        close_page(page)


def test_browser_args_are_headless_unless_the_headed_switch_is_set(monkeypatch):
    """The default argv is byte-identical to the headless-only harness; only MOCKREVIEW_TEST_HEADED=1 drops it."""
    monkeypatch.delenv(bc.HEADED_ENV, raising=False)
    default = bc.browser_args("/fake/chrome", "/tmp/profile", bc.headed_requested())
    expected = ["/fake/chrome", "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-dev-shm-usage", "--mute-audio", "--use-mock-keychain",
                "--password-store=basic", "--remote-debugging-port=0", "--disable-features=MacAppCodeSignClone",
                "--user-data-dir=/tmp/profile", "about:blank"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        expected.insert(1, "--no-sandbox")
    assert default == expected
    monkeypatch.setenv(bc.HEADED_ENV, "1")
    headed = bc.browser_args("/fake/chrome", "/tmp/profile", bc.headed_requested())
    assert "--headless=new" not in headed and "--window-position=0,25" in headed
    monkeypatch.setenv(bc.HEADED_ENV, "TrUe")
    assert bc.headed_requested() is True
    monkeypatch.setenv(bc.HEADED_ENV, "0")
    assert bc.headed_requested() is False


CONTRAST_JS = """
(function(){
  function rgb(s){var m=s.match(/[\\d.]+/g).map(Number);return {r:m[0],g:m[1],b:m[2],a:m.length>3?m[3]:1};}
  function lum(c){return [c.r,c.g,c.b].map(function(v){v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);})
    .reduce(function(a,v,i){return a+v*[0.2126,0.7152,0.0722][i];},0);}
  var el=document.querySelector('.mr-mock .probe'), fg=rgb(getComputedStyle(el).color), n=el, bg=null;
  while(n && n.nodeType===1){var c=rgb(getComputedStyle(n).backgroundColor);if(c.a>0.5){bg=c;break;}n=n.parentElement;}
  if(!bg) bg=rgb(getComputedStyle(document.documentElement).backgroundColor);
  var a=lum(fg)+0.05,b=lum(bg)+0.05;
  return JSON.stringify({fg:getComputedStyle(el).color, ratio:Math.max(a,b)/Math.min(a,b)});
})()
"""


@pytest.mark.parametrize("theme", EXPORT_THEMES)
@pytest.mark.parametrize("mock_html", [
    '<p class="probe">Plain mock text</p>',
    '<div style="background:#ffffff;padding:12px"><p class="probe">Text on the mock\'s own white panel</p></div>',
], ids=["plain", "own-light-panel"])
def test_mock_text_stays_readable_in_the_export_in_both_themes(env, tmp_path, chrome, theme, mock_html):
    """A mock is a light design the board shows as authored. The export (and the Desktop viewer, which shows the same
    document) must not paint the dark theme's light text onto it: mock text keeps WCAG AA contrast against whatever
    background it actually sits on, in light and in dark."""
    src = tmp_path / ("contrast-" + theme) / "decisions.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps({"blocks": [{"type": "mock", "id": "m1", "html": mock_html}]}), encoding="utf-8")
    data = store.create_review("contrast-" + theme, "", ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(src)), wake=False)
    html = export.build_export_html(store.load(data["id"]), 1, False)
    page = Page(chrome, "about:blank", width=1280, height=900, media=theme)
    try:
        set_document(page, html)
        assert page.until("!!document.querySelector('.mr-mock .probe')"), "the mock fragment lost its text"
        got = json.loads(page.js(CONTRAST_JS))
        assert got["ratio"] >= 4.5, f"{theme}: mock text {got['fg']} has contrast {got['ratio']:.2f} (< 4.5)"
    finally:
        close_page(page)
