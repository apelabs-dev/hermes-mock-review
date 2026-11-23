"""The approved design rules at every width, in both themes, for owner and guest.

Every screen: no horizontal scroll, text on the 12/13/14/16/20 scale, controls at the 36 px desktop / 44 px phone
hit size, WCAG AA contrast, and a menu, dialog and sheet that stay inside the viewport with hittable rows.

The round-2 review regressions live here too: More hands Comments/Activity over before the modal opens, a theme
change re-renders every mock root, unread pins keep the brand indigo with a separate phone hit box, and the
section-jump nav stays sticky under the header. Phone widths enable touch emulation and check that the browser
really reports a coarse pointer before any phone assertion is trusted.
"""

import json

import pytest

from authkit import account, token_of
from test_export_browser import Page, chrome, metric_report, paint, settle, trusted_click  # noqa: F401
from mockreview import server as srv, store, threads, users

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
LOOK = {"devices": ["desktop", "tablet", "mobile"], "default_device": "auto", "default_theme": "auto",
        "show_theme_toggle": True, "pins_enabled": True, "require_note_on_change": False, "max_pins": 40,
        "footer_button": "build", "interactive_elements": False}
MOCK = ('<section><h2>Checkout</h2><p id="copy" style="width:300px;height:50px;margin:20px">'
        'Review this total</p><p id="other" style="width:300px;height:40px;margin:20px">Other line</p></section>')
DECISIONS = {
    "intro": "How the checkout should work after the promo-code field moves. A long enough brief to wrap on a phone.",
    "status": {"label": "Round 1 · awaiting your approval", "milestones": [{"label": "Plan agreed", "date": "Week 1"}]},
    "blocks": [
        {"type": "mock", "id": "m1", "title": "Checkout today", "html": MOCK},
        {"type": "decision", "id": "d1", "title": "Where should the promo code field live?", "tag": "Checkout",
         "text": "Pick the layout that keeps Pay above the fold.", "today": "Always open above the total, 62 px tall.",
         "options": [
             {"id": "a", "label": "Always open", "text": "A field above the total.", "html": MOCK},
             {"id": "b", "label": "Behind a link", "text": "A link above the total.", "recommended": True, "html": MOCK},
         ]},
        {"type": "text", "title": "Anything else", "text": "Direction for the next round."},
    ],
}
WIDTHS = [320, 375, 390, 640, 641, 768, 1280, 1920]
THEMES = ["dark", "light"]

TEXT_SCALE_JS = """
(function(){
  var ok = [12,13,14,16,20], bad = [], seen = [];
  var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    var n = w.currentNode, el = n.parentElement;
    if (!el || !n.textContent.trim() || el.tagName === 'OPTION' || seen.indexOf(el) >= 0) continue;
    if (['SCRIPT','STYLE','TITLE'].indexOf(el.tagName) >= 0) continue;
    seen.push(el);
    var cs = getComputedStyle(el), r = el.getBoundingClientRect();
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0 || !r.width || !r.height) continue;
    var px = parseFloat(cs.fontSize);
    if (ok.indexOf(px) < 0) bad.push(el.tagName + '.' + (el.className || '') + ' ' + cs.fontSize);
  }
  return JSON.stringify(bad.slice(0, 12));
})()
"""

HIT_JS = """
(function(){
  var sel = '.btn,.ctoggle,.icon-btn,.seg-btn,.round-tab,.menu-item,.settings-tab,.share-select,.share-input,' +
            '.inp,.dnote,.qinput,.menu-input,.tbtn,.t-nav a,button,input[type=text],input[type=search],select,textarea';
  var min = window.innerWidth <= 600 ? 44 : 36, bad = [];
  document.querySelectorAll(sel).forEach(function(el){
    if (el.closest('.header-tools') || el.closest('.mock-host') || el.classList.contains('mpin') ||
        el.classList.contains('pin-row')) return;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return;
    var cs = getComputedStyle(el), r = el.getBoundingClientRect();
    if (cs.display === 'none' || cs.visibility === 'hidden' || !r.width || !r.height) return;
    if (r.height + 0.5 < min) bad.push(el.tagName + '.' + (el.className || '') + ' ' + r.height.toFixed(1));
  });
  return JSON.stringify(bad.slice(0, 12));
})()
"""

CONTRAST_JS = """
(function(){
  function parse(c){var m=(c||'').match(/rgba?\\(([^)]+)\\)/);if(!m)return null;
    var p=m[1].split(',').map(function(x){return parseFloat(x)});
    return [p[0],p[1],p[2],p.length>3?p[3]:1];}
  function lum(c){var a=c.slice(0,3).map(function(v){v/=255;return v<=.03928?v/12.92:Math.pow((v+.055)/1.055,2.4)});
    return .2126*a[0]+.7152*a[1]+.0722*a[2];}
  function blend(f,b){return [0,1,2].map(function(i){return f[i]*f[3]+b[i]*(1-f[3])});}
  function bgFor(el){var layers=[];for(var e=el;e;e=e.parentElement){var c=parse(getComputedStyle(e).backgroundColor);
      if(c&&c[3]>0.01)layers.push(c);}var bg=[255,255,255];for(var i=layers.length-1;i>=0;i--)bg=blend(layers[i],bg);return bg;}
  var sels = '.pb-name,.pb-sub,.title,.brief,.blk h2,.blk-text p,.today,.ihead h3,.ihead .tag,.opt-cap,.opt-text,' +
    '.decide .dbtn,.dnote,.tally,.tally-compact,.badge,.menu-label,.menu-item,.menu-row,.row-name,.setting-help,' +
    '.setting-tab,.pane-head h2,.share-title,.share-label,.share-faint,.tp-title strong,.ctext,.hint-line,.dnav a,' +
    '.search input,.muted,.brand b,.top h1,.card p,.card h1,.btn';
  var bad = [];
  document.querySelectorAll(sels).forEach(function(el){
    if (el.closest('.header-tools') || el.closest('.mock-host') || el.closest('.btn[disabled]') ||
        el.disabled || el.getAttribute('aria-disabled') === 'true') return;
    var cs = getComputedStyle(el), r = el.getBoundingClientRect();
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0 || !r.width || !r.height) return;
    if (!el.textContent.trim()) return;
    var fg = parse(cs.color), bg = bgFor(el);
    if (!fg || fg[3] < 0.99) return;
    var l1 = lum(fg), l2 = lum(bg), cr = (Math.max(l1,l2)+.05)/(Math.min(l1,l2)+.05);
    if (cr < 4.5) bad.push(el.tagName + '.' + (el.className || '') + ' ' + cr.toFixed(2));
  });
  return JSON.stringify(bad.slice(0, 12));
})()
"""


def new_review(tmp_path, title="Checkout redesign — a title long enough to truncate on every phone", kind="decisions"):
    src = tmp_path / (kind + ".json")
    src.write_text(json.dumps(DECISIONS if kind == "decisions" else {"blocks": []}), encoding="utf-8")
    proto = tmp_path / "index.html"
    proto.write_text("<!doctype html><html><body><h1>Prototype</h1></body></html>", encoding="utf-8")
    spec = (store.parse_round_spec(decisions=str(src)) if kind == "decisions"
            else store.parse_round_spec(prototype=str(proto)))
    data = store.create_review(title, "A brief long enough to wrap on a phone.", ORIGIN, "tailscale", spec, look=LOOK,
                               wake=False)
    if kind == "decisions":
        threads.create(data["id"], "owner", "Board Owner", "reviewer", "This field pushes Pay below the fold.",
                       anchor={"block": "m1", "x": 0.5, "y": 0.4}, round_n=1, can_activate=True)
    return data


def new_design_review(tmp_path, default_theme):
    d = tmp_path / ("design-" + default_theme)
    d.mkdir()
    (d / "design.json").write_text(json.dumps({
        "name": "probe", "label": "Probe", "default_theme": default_theme, "mock_css": "mock.css",
        "themes": {"dark": {"bg": "#000000", "surface": "#111111", "text": "#eeeeee", "accent": "#8899ff"},
                   "light": {"bg": "#ffffff", "surface": "#ffffff", "text": "#111111", "accent": "#3355dd"}}}),
        encoding="utf-8")
    (d / "mock.css").write_text(
        ".app{background:#000;color:#eee;min-height:160px}.app[data-theme=light]{background:#fff;color:#111}",
        encoding="utf-8")
    src = tmp_path / ("decisions-" + default_theme + ".json")
    src.write_text(json.dumps(DECISIONS), encoding="utf-8")
    return store.create_review("Profile theme probe", "", ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(src), design=str(d)), look=LOOK, wake=False)


def new_var_theme_review(tmp_path):
    dec = json.loads(json.dumps(DECISIONS))
    dec["extra_css"] = ".app{background:var(--bg);color:var(--text)}"
    src = tmp_path / "decisions-vars.json"
    src.write_text(json.dumps(dec), encoding="utf-8")
    return store.create_review("Board theme probe", "", ORIGIN, "tailscale",
                               store.parse_round_spec(decisions=str(src)), look=LOOK, wake=False)


def guest_url(server, data, role):
    link = users.share("test", data["id"], "create", role)
    return f"{server}/s/{token_of(link['path'])}"


def open_page(chrome, url, width, theme, wait=None, script="localStorage.clear();", js=True):
    """One page load per screen. Phone widths get real touch emulation before any phone assertion."""
    page = Page(chrome, url, width=width, height=844 if width < 640 else 900, media=theme,
                script=script, wait=wait, js=js)
    if width < 640:
        page.cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}, session=page.sid)
        page.cdp.call("Emulation.setDeviceMetricsOverride",
                      {"width": width, "height": 844, "deviceScaleFactor": 1, "mobile": True}, session=page.sid)
        assert page.until("matchMedia('(pointer: coarse)').matches"), \
            f"touch emulation did not report a coarse pointer at {width}px"
        settle(page, 0.12)
    return page


def close_page(page):
    page.cdp.call("Target.closeTarget", {
        "targetId": page.cdp.call("Target.getTargetInfo", {}, session=page.sid)["targetInfo"]["targetId"]})


def assert_clean(page, width, label):
    scroll = page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert scroll, f"{label}: the document scrolls horizontally at {width}px"
    bad = json.loads(page.js(TEXT_SCALE_JS))
    assert not bad, f"{label}: text outside the approved 12/13/14/16/20 scale: {bad}"
    small = json.loads(page.js(HIT_JS))
    assert not small, f"{label}: controls below the {'44' if width <= 600 else '36'}px hit size: {small}"
    low = json.loads(page.js(CONTRAST_JS))
    assert not low, f"{label}: text below WCAG AA contrast: {low}"


def menu_inside(page, width, label):
    rows = json.loads(page.js(
        "(function(){var out=[];document.querySelectorAll('.export-menu .menu-item,.export-menu .seg-btn,"
        ".export-menu .menu-input,.export-menu .menu-row').forEach(function(e){var r=e.getBoundingClientRect();"
        "var x=r.left+r.width/2,y=r.top+r.height/2;var h=document.elementFromPoint(x,y);"
        "out.push({sel:e.className,left:r.left,right:r.right,top:r.top,bottom:r.bottom,w:r.width,h:r.height,"
        "hit:!!h&&(h===e||e.contains(h))});});return JSON.stringify(out);})()"))
    assert rows, f"{label}: the More menu has no rows"
    for row in rows:
        assert row["left"] >= 0 and row["right"] <= width and row["top"] >= 0 and row["bottom"] <= 900 + 0.5, \
            f"{label}: menu row outside the viewport: {row}"
        assert row["w"] > 0 and row["h"] > 0 and row["hit"], f"{label}: menu row not hittable: {row}"


def open_more(page, label):
    trusted_click(page, ".more-open")
    assert page.until("!!document.querySelector('.export-menu')"), f"{label}: More did not open"
    paint(page)


def close_more_with_escape(page, label):
    page.key("Escape", "Escape", 27)
    assert page.until("!document.querySelector('.export-menu') && !document.querySelector('.menu-backdrop')"), \
        f"{label}: Escape did not close More and its backdrop"
    assert page.until("document.activeElement === document.querySelector('.more-open')"), \
        f"{label}: Escape must return focus to More"


def overlay_state(page):
    return json.loads(page.js(
        "JSON.stringify({menu:!!document.querySelector('.export-menu'),"
        "backdrop:!!document.querySelector('.menu-backdrop'),"
        "label:(document.querySelector('.toverlay')||{getAttribute:function(){return null}}).getAttribute('aria-label'),"
        "focus:!!(document.activeElement&&document.activeElement.closest&&document.activeElement.closest('.toverlay')),"
        "close:(function(){var e=document.querySelector('.toverlay .tclose');if(!e)return {missing:true};"
        "var r=e.getBoundingClientRect(),h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);"
        "return {hit:!!h&&(h===e||e.contains(h))};})()})"))


def assert_overlay_modal(page, label, opener):
    state = overlay_state(page)
    assert not state["menu"] and not state["backdrop"], f"{label}: the More menu/backdrop survived the handoff: {state}"
    assert state["close"].get("hit"), f"{label}: the overlay Close button is not hittable: {state}"
    assert state["focus"], f"{label}: focus did not move into the overlay: {state}"
    for key, mods in (("Tab", None), ("Tab", 8)):
        page.key(key, key, 9, modifiers=mods)
        settle(page, 0.05)
        assert page.js("!!(document.activeElement && document.activeElement.closest('.toverlay'))"), \
            f"{label}: {'Shift+' if mods else ''}Tab escaped the modal overlay"
    page.key("Escape", "Escape", 27)
    assert page.until("!document.querySelector('.toverlay')"), f"{label}: Escape did not close the overlay"
    assert page.until("document.activeElement === document.querySelector(%s)" % json.dumps(opener)), \
        f"{label}: focus did not return to {opener}"


def theme_state(page):
    return json.loads(page.js(
        "JSON.stringify({outer:document.documentElement.dataset.theme||null,"
        "mocks:[].map.call(document.querySelectorAll('.mock-host'),function(h){"
        "var r=h.shadowRoot.querySelector('.app');return {theme:r.dataset.theme||null,bg:getComputedStyle(r).backgroundColor};})})"))


def wait_for_theme(page, theme, bg):
    expr = ("document.documentElement.dataset.theme === %s && "
            "[].every.call(document.querySelectorAll('.mock-host'),function(h){"
            "var r=h.shadowRoot.querySelector('.app');return r&&r.dataset.theme === %s && "
            "getComputedStyle(r).backgroundColor === %s;})") % (json.dumps(theme), json.dumps(theme), json.dumps(bg))
    assert page.until(expr), f"the mocks never reached {theme}"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("kind", ["decisions", "prototype"])
@pytest.mark.parametrize("role", ["owner", "commenter guest"])
def test_the_board_screen_at_every_width(env, server, tmp_path, chrome, monkeypatch, kind, width, theme, role):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    data = new_review(tmp_path, kind=kind)
    if role == "owner":
        url = f"{server}/r/{data['id']}/"
    else:
        monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
        url = guest_url(server, data, "commenter")
    page = open_page(chrome, url, width, theme, wait="!!document.querySelector('#dzbar')")
    try:
        page.until("!!document.querySelector('.blk, .proto')")
        label = f"{kind} {role} at {width}px ({theme})"
        assert_clean(page, width, label)

        # The approved More contents at every width: theme, Comments, Activity, Share where the role has it,
        # and all four export formats plus Include comments.
        open_more(page, label)
        menu_inside(page, width, label)
        formats = json.loads(page.js(
            "JSON.stringify([].map.call(document.querySelectorAll('.export-menu .export-item'),"
            "function(b){return b.getAttribute('data-format');}))"))
        assert formats == ["png", "pdf", "html", "markdown"], f"{label}: export formats changed: {formats}"
        assert page.js("!!document.querySelector('.export-menu .tcomments') && "
                       "!!document.querySelector('.export-menu .tactivity')"), \
            f"{label}: Comments/Activity are missing from More"
        assert page.js("!!document.querySelector('.export-menu [aria-label=Theme]')"), \
            f"{label}: the theme control is missing from More"
        assert page.js("!!document.querySelector('.export-menu .share-menu')") is (role == "owner"), \
            f"{label}: Share in More does not match the role"
        close_more_with_escape(page, label)

        # Finding 1: More hands over to Comments and Activity without leaving its menu or backdrop behind.
        for action, title in (("tcomments", "Comments"), ("tactivity", "Activity")):
            open_more(page, label)
            trusted_click(page, ".export-menu ." + action)
            assert page.until("!!document.querySelector('.toverlay')"), f"{label}: {title} did not open"
            assert page.until("!!(document.activeElement && document.activeElement.closest && "
                              "document.activeElement.closest('.toverlay'))", timeout=5), \
                f"{label}: focus did not move into the {title} overlay"
            paint(page)
            state = overlay_state(page)
            assert state["label"] == title, f"{label}: {title} overlay has the wrong label: {state}"
            assert_overlay_modal(page, f"{label} More->{title}", ".more-open")

        if role == "owner":
            if width <= 600:
                open_more(page, label)
                trusted_click(page, ".export-menu .share-menu")
                opener = ".more-open"
            else:
                trusted_click(page, ".share-open")
                opener = ".share-open"
            assert page.until("!!document.querySelector('.share-dialog .share-invite-input')"), \
                f"{label}: Share did not open"
            assert page.js("!!(document.activeElement && document.activeElement.closest('.share-dialog'))"), \
                f"{label}: focus did not move into Share"
            for _ in range(3):
                page.key("Tab", "Tab", 9)
                settle(page, 0.03)
                assert page.js("!!(document.activeElement && document.activeElement.closest('.share-dialog'))"), \
                    f"{label}: Tab escaped the Share dialog"
            inside = metric_report(page, [".share-dialog"])
            item = inside["items"][0]
            assert item["left"] >= 0 and item["right"] <= width and item["top"] >= 0 and item["bottom"] <= 900 + 0.5, \
                f"{label}: the Share dialog is outside the viewport: {item}"
            page.key("Escape", "Escape", 27)
            assert page.until("!document.querySelector('.share-dialog')"), f"{label}: Escape did not close Share"
            assert page.until("document.activeElement === document.querySelector(%s)" % json.dumps(opener)), \
                f"{label}: Share focus did not return to {opener}"
        else:
            assert page.js("!document.querySelector('.share-open')"), \
                f"{label}: a commenter guest must not get the Share control (Decision A)"

        # The pin comment popover/sheet on a decision board: real pin click, focus in, Escape back to the pin.
        if kind == "decisions":
            trusted_click(page, ".foot-comment" if width <= 600 else ".bar-comment")
            assert page.until("!!document.querySelector('.mpin')"), f"{label}: Comment mode drew no pin"
            page.js("document.querySelector('.mpin').scrollIntoView({block:'center'});true")
            settle(page, 0.12)
            trusted_click(page, ".mpin")
            assert page.until("!!document.querySelector('.tpop')"), f"{label}: the pin popover did not open"
            assert page.until("!!(document.activeElement && document.activeElement.closest && "
                              "document.activeElement.closest('.tpop'))", timeout=5), \
                f"{label}: focus did not move into the pin popover"
            paint(page)
            assert page.js("!!document.querySelector('.tpop').classList.contains('sheet')") is (width < 640), \
                f"{label}: the comment popover/sheet boundary is wrong"
            assert page.js("!!(document.activeElement && document.activeElement.closest('.tpop'))"), \
                f"{label}: focus did not move into the pin popover"
            page.key("Escape", "Escape", 27)
            assert page.until("!document.querySelector('.tpop')"), f"{label}: Escape did not close the pin popover"
            assert page.until("document.activeElement === document.querySelector('.mpin')"), \
                f"{label}: focus did not return to the pin"

        # The phone bottom bar is the one control strip under 600 px; the header Comment stays at desktop widths.
        # A prototype round's footer carries the round strip, not the header round picker, inside that bar.
        # Send is the owner's alone: a commenter guest's bar has the owner-only line where Send would be.
        if width <= 600:
            round_control = "#dzfoot .rounds .round-tab.on" if kind == "prototype" else ".round-picker"
            controls = [".foot-comment", round_control] + ([".btn-send"] if role == "owner" else [])
            bar = metric_report(page, controls)
            for item in bar["items"]:
                assert not item.get("missing") and item["hittable"], f"{label}: phone bar item is not hittable: {item}"
                assert item["height"] + 0.5 >= 44, f"{label}: phone bar item below 44 px: {item}"
            if role != "owner":
                assert page.js("!document.querySelector('.btn-send') && !document.querySelector('#gate-btn')"), \
                    f"{label}: a commenter guest got Send or Build"
                seen = metric_report(page, [".sb-owner-only"])
                line = seen["items"][0]
                assert not line.get("missing") and line["width"] > 0 and line["height"] > 0 and \
                    line["left"] >= 0 and line["right"] <= width + 0.5 and line["bottom"] <= seen["vh"] + 0.5, \
                    f"{label}: the owner-only line is missing or off screen: {line}"
        else:
            top = metric_report(page, [".bar-comment"])
            assert top["items"][0]["hittable"], f"{label}: the desktop Comment control is not hittable"
    finally:
        close_page(page)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_the_board_is_read_only_for_a_viewer_guest(env, server, tmp_path, chrome, monkeypatch, width, theme):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    data = new_review(tmp_path)
    url = guest_url(server, data, "viewer")
    page = open_page(chrome, url, width, theme, wait="!!document.querySelector('#dzbar')")
    try:
        page.until("!!document.querySelector('.blk')")
        label = f"viewer guest at {width}px ({theme})"
        assert_clean(page, width, label)
        assert page.js("document.body.classList.contains('is-readonly')"), f"{label}: the board is not read-only"
        assert page.js("!document.querySelector('.ctoggle') && !document.querySelector('.foot-comment') && "
                       "!document.querySelector('.bar-comment')"), f"{label}: a viewer guest got the Comment control"
        assert page.js("!document.querySelector('.share-open')"), f"{label}: a viewer guest got Share"
        assert page.js("[].every.call(document.querySelectorAll('.dbtn'),function(b){return b.disabled;})"), \
            f"{label}: an editable decision control is enabled for a viewer"
        open_more(page, label)
        menu_inside(page, width, label)
        assert page.js("!document.querySelector('.export-menu .share-menu')"), f"{label}: viewer got Share in More"
        assert page.js("!!document.querySelector('.export-menu .tcomments')"), f"{label}: viewer lost Comments"
        trusted_click(page, ".export-menu .tcomments")
        assert page.until("!!document.querySelector('.toverlay')"), f"{label}: Comments did not open for a viewer"
        settle(page, 0.12)
        assert overlay_state(page)["label"] == "Comments", f"{label}: wrong overlay label"
        page.key("Escape", "Escape", 27)
        assert page.until("!document.querySelector('.toverlay')"), f"{label}: Escape did not close Comments"
    finally:
        close_page(page)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_home_settings_and_signin_at_every_width(env, server, tmp_path, chrome, monkeypatch, width, theme):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    new_review(tmp_path, title="Checkout redesign")
    screens = [("/", "!!document.querySelector('.search')", "home"),
               ("/?q=no-review-matches-this", "!!document.querySelector('.empty')", "home empty"),
               ("/settings", "!!document.querySelector('.settings-tab')", "settings")]
    for path, wait, name in screens:
        page = open_page(chrome, server + path, width, theme, wait=wait)
        try:
            assert_clean(page, width, f"{name} at {width}px ({theme})")
        finally:
            close_page(page)
    loading = open_page(chrome, server + "/settings", width, theme, js=False,
                        wait="!!document.querySelector('#app') && "
                             "document.querySelector('#app').textContent.indexOf('Loading settings') >= 0")
    try:
        assert_clean(loading, width, f"settings loading at {width}px ({theme})")
    finally:
        close_page(loading)
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    page = open_page(chrome, server + "/signin", width, theme, wait="!!document.querySelector('.card')")
    try:
        assert_clean(page, width, f"sign-in at {width}px ({theme})")
    finally:
        close_page(page)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_link_and_error_pages_at_every_width(env, server, tmp_path, chrome, monkeypatch, width, theme):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    member = account()
    invite = users.create_invite("test", "member")
    login_link = users.create_login_link("test", member["user_id"])
    pages = [(invite["path"], "invite confirm"), (login_link["path"], "sign-in token confirm"),
             ("/invite/not-a-token", "dead link")]
    for path, name in pages:
        page = open_page(chrome, server + path, width, theme, wait="!!document.querySelector('.card')")
        try:
            assert_clean(page, width, f"{name} at {width}px ({theme})")
        finally:
            close_page(page)
    # Throttle this request's peer so the server renders its own "too many attempts" page (status 429).
    monkeypatch.setattr(srv.Handler, "_peer", lambda self: "198.51.100.7")
    for _ in range(users.THROTTLE_FAILURES):
        users.record_failure("198.51.100.7")
    page = open_page(chrome, server + "/invite/not-a-token", width, theme, wait="!!document.querySelector('.card')")
    try:
        assert_clean(page, width, f"throttled at {width}px ({theme})")
        assert page.js("document.querySelector('.card h1').textContent === 'Too many attempts'"), \
            "the throttled page did not render"
    finally:
        close_page(page)


def test_more_menu_keyboard_focus_and_escape(env, server, tmp_path, chrome, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    data = new_review(tmp_path)
    page = open_page(chrome, f"{server}/r/{data['id']}/", 1280, "dark",
                     wait="!!document.querySelector('.blk')")
    try:
        open_more(page, "keyboard")
        assert page.until("document.activeElement && document.activeElement.closest('.export-menu') !== null"), \
            "focus must move into the menu"
        page.key("ArrowDown", "ArrowDown", 40)
        settle(page, 0.05)
        assert page.js("document.activeElement && document.activeElement.closest('.export-menu') !== null"), \
            "ArrowDown must move focus inside the menu"
        close_more_with_escape(page, "keyboard")
    finally:
        close_page(page)


def test_more_hands_over_and_direct_controls_still_work(env, server, tmp_path, chrome, monkeypatch):
    """The extra edges of finding 1: Close returns focus, and the direct header copies keep their handlers."""
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    data = new_review(tmp_path)
    for width, theme in ((390, "dark"), (1280, "light")):
        page = open_page(chrome, f"{server}/r/{data['id']}/", width, theme,
                         wait="!!document.querySelector('.blk')")
        try:
            label = f"handoff edges at {width}px ({theme})"
            open_more(page, label)
            trusted_click(page, ".export-menu .tcomments")
            assert page.until("!!document.querySelector('.toverlay')"), f"{label}: Comments did not open"
            assert page.js("!document.querySelector('.export-menu') && !document.querySelector('.menu-backdrop')"), \
                f"{label}: the menu or backdrop stayed behind the overlay"
            trusted_click(page, ".toverlay .tclose")
            assert page.until("!document.querySelector('.toverlay')"), f"{label}: Close did not close the overlay"
            assert page.until("document.activeElement === document.querySelector('.more-open')"), \
                f"{label}: Close did not return focus to More"
            # A direct click on the header's own Comments control (not through More) still opens the panel.
            page.press(".tcomments")
            assert page.until("!!document.querySelector('.toverlay')"), f"{label}: the direct Comments control broke"
            page.key("Escape", "Escape", 27)
            assert page.until("!document.querySelector('.toverlay')"), f"{label}: Escape did not close the panel"
        finally:
            close_page(page)


@pytest.mark.parametrize("start,other", [("dark", "light"), ("light", "dark")])
@pytest.mark.parametrize("width", [390, 1280])
def test_a_theme_change_rerenders_every_profiled_mock(env, server, tmp_path, chrome, monkeypatch, width, start, other):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    data = new_design_review(tmp_path, start)
    page = open_page(chrome, f"{server}/r/{data['id']}/", width, start,
                     wait="!!document.querySelector('.mock-host')")
    try:
        bg = {"dark": "rgb(0, 0, 0)", "light": "rgb(255, 255, 255)"}
        wait_for_theme(page, start, bg[start])
        before = theme_state(page)
        assert before["outer"] == start and len(before["mocks"]) >= 2, before
        hosts = page.js("document.querySelectorAll('.mock-host').length")
        boxes = page.js("document.querySelectorAll('.mock').length")

        page.js("document.querySelector('.sbtn.s-approve').scrollIntoView({block:'center'});true")
        settle(page, 0.12)
        trusted_click(page, ".sbtn.s-approve")
        draft_key = "mr:%s:r1:draft" % data["id"]
        assert page.until("!!localStorage.getItem(%s)" % json.dumps(draft_key), timeout=10), \
            "the draft mark was not saved"
        page.js("window.scrollTo(0,600);true")
        settle(page, 0.2)

        open_more(page, "theme flip")
        # The baseline is read here, not straight after the scroll: the approve round-trip's own re-render can still
        # move the page in between, and only a move across the theme flip is what this assertion is about.
        y0 = page.js("scrollY")
        trusted_click(page, '[aria-label=Theme] [data-value="%s"]' % other)
        wait_for_theme(page, other, bg[other])
        settle(page, 0.25)
        after = theme_state(page)
        assert after["outer"] == other, after
        assert all(x["theme"] == other and x["bg"] == bg[other] for x in after["mocks"]), after
        assert page.js("!document.querySelector('.export-menu')"), "the menu must close for the re-render"
        assert page.js("document.activeElement === document.querySelector('.more-open')"), \
            "focus must land on the new More button after the re-render"
        draft = json.loads(page.js("localStorage.getItem(%s)" % json.dumps(draft_key)))
        assert draft["marks"]["d1"]["status"] == "approve", draft
        assert page.js('document.querySelector(".sbtn.s-approve").getAttribute("aria-pressed") === "true"'), \
            "the marked decision did not survive the re-render"
        assert abs(page.js("scrollY") - y0) <= 8, "the reading position jumped across the theme change"
        assert page.js("document.querySelectorAll('.mock-host').length") == hosts, "a mock host was lost"
        assert page.js("document.querySelectorAll('.mock').length") == boxes, "a mock container was lost"
        assert page.js("document.querySelectorAll('.pin-layer').length") == hosts, "a pin layer was lost"
    finally:
        close_page(page)


@pytest.mark.parametrize("width", [390, 1280])
def test_a_board_theme_change_moves_the_mocks_with_no_design_profile(env, server, tmp_path, chrome, monkeypatch,
                                                                       width):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    data = new_var_theme_review(tmp_path)
    page = open_page(chrome, f"{server}/r/{data['id']}/", width, "dark",
                     wait="!!document.querySelector('.mock-host')")
    try:
        night, day = "rgb(11, 11, 14)", "rgb(250, 250, 250)"
        page.until("document.documentElement.dataset.theme === undefined || "
                   "document.documentElement.dataset.theme === ''")
        before = theme_state(page)
        assert all(x["theme"] is None and x["bg"] == night for x in before["mocks"]), before
        hosts = page.js("document.querySelectorAll('.mock-host').length")

        for value, theme, bg in (("light", "light", day), ("dark", "dark", night), ("auto", None, night)):
            open_more(page, "board theme")
            trusted_click(page, '[aria-label=Theme] [data-value="%s"]' % value)
            if theme:
                assert page.until("document.documentElement.dataset.theme === %s" % json.dumps(theme)), \
                    f"the board never reached {value}"
            else:
                assert page.until("!document.documentElement.dataset.theme"), "the board never reached auto"
            assert page.until("[].every.call(document.querySelectorAll('.mock-host'),function(h){"
                              "var r=h.shadowRoot.querySelector('.app');"
                              "return getComputedStyle(r).backgroundColor === %s;})" % json.dumps(bg)), \
                f"the mocks did not follow the board theme {value}"
            assert page.js("document.querySelectorAll('.mock-host').length") == hosts, \
                f"a mock host was lost on {value}"
            assert page.js("document.activeElement === document.querySelector('.more-open')"), \
                f"focus fell away from More after the {value} re-render"
    finally:
        close_page(page)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", [320, 390, 1280])
def test_unread_pins_keep_the_brand_indigo_and_a_separate_hit_box(env, server, tmp_path, chrome, monkeypatch,
                                                                   width, theme):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    data = new_review(tmp_path)
    page = open_page(chrome, f"{server}/r/{data['id']}/", width, theme,
                     wait="!!document.querySelector('.blk')")
    try:
        open_more(page, "pin")
        trusted_click(page, '[aria-label=Theme] [data-value="%s"]' % theme)
        assert page.until("document.documentElement.dataset.theme === %s" % json.dumps(theme)), \
            f"the board never reached {theme}"
        trusted_click(page, ".foot-comment" if width <= 600 else ".bar-comment")
        assert page.until("!!document.querySelector('.mpin.un')"), "no unread pin was drawn"
        page.js("document.querySelector('.mpin.un').scrollIntoView({block:'center'});true")
        settle(page, 0.15)
        pin = json.loads(page.js(
            "JSON.stringify((function(){var e=document.querySelector('.mpin.un'),cs=getComputedStyle(e),"
            "after=getComputedStyle(e,'::after'),before=getComputedStyle(e,'::before'),r=e.getBoundingClientRect();"
            "var cx=r.left+r.width/2,cy=r.top+r.height/2,off=[[0,0],[21,0],[-21,0],[0,21],[0,-21],[18,18],[-18,-18]];"
            "var hits=off.map(function(p){var h=document.elementFromPoint(cx+p[0],cy+p[1]);"
            "return !!h&&(h===e||e.contains(h));});"
            "return {theme:document.documentElement.dataset.theme,cls:e.className,token:cs.getPropertyValue('--pin').trim(),"
            "bg:cs.backgroundColor,w:cs.width,h:cs.height,offsetW:e.offsetWidth,offsetH:e.offsetHeight,"
            "beforeW:before.width,beforeH:before.height,afterW:after.width,afterBg:after.backgroundColor,"
            "hits:hits};})())"))
        assert pin["token"] == "#5b5bf0" and pin["bg"] == "rgb(91, 91, 240)", pin
        assert pin["w"] == "26px" and pin["h"] == "26px", pin
        assert pin["offsetW"] == 26 and pin["offsetH"] == 26, pin
        assert pin["afterW"] == "9px" and pin["afterBg"] not in ("rgba(0, 0, 0, 0)", "transparent"), \
            f"the unread dot disappeared: {pin}"
        if width <= 600:
            assert pin["beforeW"] == "44px" and pin["beforeH"] == "44px", pin
            assert all(pin["hits"]), f"the 44 px pin hit box does not resolve to the pin: {pin}"
    finally:
        close_page(page)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", [390, 1280])
def test_section_navigation_stays_sticky_under_the_header(env, server, tmp_path, chrome, monkeypatch, width, theme):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    data = new_review(tmp_path)
    page = open_page(chrome, f"{server}/r/{data['id']}/", width, theme,
                     wait="!!document.querySelector('.dnav')")
    try:
        page.js("window.scrollTo(0,600);true")
        settle(page, 0.2)
        m = json.loads(page.js(
            "JSON.stringify({position:getComputedStyle(document.querySelector('.dnav')).position,"
            "top:document.querySelector('.dnav').getBoundingClientRect().top,"
            "barBottom:document.querySelector('#dzbar').getBoundingClientRect().bottom,"
            "bg:getComputedStyle(document.querySelector('.dnav')).backgroundColor,scrollY:scrollY})"))
        assert m["position"] == "sticky", m
        assert m["top"] >= m["barBottom"] - 2, f"the nav is not under the header after scrolling: {m}"
        assert m["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"), f"the stuck nav is transparent: {m}"
        oq = json.loads(page.js("JSON.stringify({x:scrollX,y:scrollY})"))
        trusted_click(page, ".t-nav a")
        page.until("window.scrollY !== %s" % json.dumps(oq["y"]), timeout=5)
        settle(page, 0.8)
        landed = json.loads(page.js(
            "JSON.stringify({top:document.querySelector('.t-nav a').getAttribute('href')"
            "&&document.querySelector(document.querySelector('.t-nav a').getAttribute('href')).getBoundingClientRect().top,"
            "barBottom:document.querySelector('#dzbar').getBoundingClientRect().bottom})"))
        assert landed["top"] >= landed["barBottom"] - 2, f"a section jump landed under the header: {landed}"
        assert landed["top"] <= landed["barBottom"] + 140, f"a section jump did not land near the header: {landed}"
    finally:
        close_page(page)


def test_share_dialog_traps_focus_and_returns_it(env, server, tmp_path, chrome, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    data = new_review(tmp_path)
    page = open_page(chrome, f"{server}/r/{data['id']}/", 1280, "dark",
                     wait="!!document.querySelector('.share-open')")
    try:
        trusted_click(page, ".share-open")
        assert page.until("!!document.querySelector('.share-invite-input')")
        for _ in range(10):
            page.key("Tab", "Tab", 9)
            settle(page, 0.02)
            assert page.js("document.activeElement && document.activeElement.closest('.share-dialog') !== null"), \
                "Tab must stay inside the Share dialog"
        page.key("Escape", "Escape", 27)
        assert page.until("!document.querySelector('.share-dialog')")
        assert page.until("document.activeElement === document.querySelector('.share-open')"), \
            "Escape must return focus to Share"
    finally:
        close_page(page)


def test_comment_sheet_traps_focus_and_returns_it_to_the_pin(env, server, tmp_path, chrome, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    data = new_review(tmp_path)
    page = open_page(chrome, f"{server}/r/{data['id']}/", 390, "dark",
                     wait="!!document.querySelector('.foot-comment')")
    try:
        trusted_click(page, ".foot-comment")
        assert page.until("document.querySelectorAll('.mpin').length > 0")
        page.js("document.querySelector('.mpin').click();true")
        assert page.until("!!document.querySelector('.popover.sheet')")
        assert page.until("document.activeElement && document.activeElement.closest('.popover') !== null"), \
            "focus must move into the comment sheet"
        for _ in range(6):
            page.key("Tab", "Tab", 9)
            page.cdp.pump(0.1)
            assert page.js("document.activeElement && document.activeElement.closest('.popover') !== null"), \
                "Tab must stay inside the comment sheet"
        page.key("Escape", "Escape", 27)
        assert page.until("!document.querySelector('.popover')")
        assert page.until("document.activeElement === document.querySelector('.mpin')"), \
            "Escape must return focus to the pin"
    finally:
        close_page(page)


def test_settings_tabs_switch_sections_with_a_real_click(env, server, tmp_path, chrome, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    page = open_page(chrome, server + "/settings", 1280, "dark",
                     wait="!!document.querySelector('.settings-tab')")
    try:
        names = json.loads(page.js(
            "JSON.stringify(Array.prototype.map.call(document.querySelectorAll('.settings-tab'),"
            "function(b){return b.textContent}))"))
        assert names == ["Members", "Invites", "Devices"], names
        page.click_sel(".settings-tab:nth-child(3)")
        assert page.until("(document.querySelector('.settings-pane.on h2')||{}).textContent === 'Devices'")
        page.click_sel(".settings-tab:nth-child(2)")
        assert page.until("(document.querySelector('.settings-pane.on h2')||{}).textContent === 'Invites'")
        assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    finally:
        close_page(page)
