"""Settings-page avatars use the board's shared per-person palette in both themes.

Drives a real headless Chrome through one board and /settings per theme. The eight fills are read from the board's
own CSS (the canonical palette); every Members row must paint the same index -> fill pair and clear 4.5:1, and the
loopback owner's row must match the board header avatar for that person.
"""

import json
import re

import pytest

from authkit import account
from test_export_browser import Page, chrome  # noqa: F401
from mockreview import store

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
DECISIONS = {"blocks": [{"type": "mock", "id": "m1", "html": "<p>hello</p>"}]}
LOOK = {"devices": ["desktop", "tablet", "mobile"], "default_device": "auto", "default_theme": "project",
        "show_theme_toggle": True, "pins_enabled": True, "require_note_on_change": False, "max_pins": 40,
        "footer_button": "build", "interactive_elements": False}
BODY = {"light": "rgb(250, 250, 250)", "dark": "rgb(11, 11, 14)"}
CLEAR = "try{localStorage.clear()}catch(e){}"

BOARD_AVATAR_JS = """
(function () {
  var e = document.querySelector('.av-self');
  if (!e) return null;
  var s = getComputedStyle(e);
  return JSON.stringify({key: e.getAttribute('data-av'), fill: s.backgroundColor, ink: s.color, text: e.textContent});
})()
"""

PALETTE_JS = """
(function () {
  var out = [];
  for (var i = 0; i < 8; i++) {
    var e = document.createElement('span');
    e.className = 'av';
    e.setAttribute('data-av', String(i));
    e.textContent = 'X';
    document.body.appendChild(e);
    var s = getComputedStyle(e);
    out.push({key: e.getAttribute('data-av'), fill: s.backgroundColor, ink: s.color});
    e.remove();
  }
  return JSON.stringify(out);
})()
"""

ROWS_JS = """
(function () {
  var rows = Array.prototype.map.call(document.querySelectorAll('.setting-list .setting-row'), function (r) {
    var av = r.querySelector('.row-title .av');
    var name = r.querySelector('.row-name');
    var badge = r.querySelector('.badge');
    var s = av ? getComputedStyle(av) : null;
    return {key: av ? av.getAttribute('data-av') : null,
            fill: s ? s.backgroundColor : null,
            ink: s ? s.color : null,
            text: av ? av.textContent : null,
            name: name ? name.textContent : null,
            badge: badge ? badge.textContent : null,
            warn: !!(av && av.classList.contains('warn'))};
  });
  return JSON.stringify(rows);
})()
"""

SURFACE_JS = "getComputedStyle(document.querySelector('.setting-list')).backgroundColor"


def rgb(css):
    m = re.match(r"rgba?\(([^)]*)\)", css or "")
    assert m, "not an rgb colour: %r" % (css,)
    parts = [p for p in re.split(r"[,\s/]+", m.group(1).strip()) if p]
    r, g, b = (float(p) for p in parts[:3])
    a = float(parts[3]) if len(parts) > 3 else 1.0
    return r, g, b, a


def over(fg, bg):
    r, g, b, a = rgb(fg)
    br, bgc, bb, _ = rgb(bg)
    return (r * a + br * (1 - a), g * a + bgc * (1 - a), b * a + bb * (1 - a), 1.0)


def luminance(colour):
    def channel(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in colour[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def solid(css, bg):
    return over(css, bg) if rgb(css)[3] < 1 else rgb(css)


def assert_readable(items, where, surface):
    assert items, "no avatars matched %s" % where
    for item in items:
        fill = solid(item["fill"], surface)
        ink = over(item["ink"], "#%02x%02x%02x" % tuple(int(round(v)) for v in fill[:3])) \
            if rgb(item["ink"])[3] < 1 else rgb(item["ink"])
        got = ratio(ink, fill)
        assert got >= 4.5, "%s avatar %r: %.2f:1 ink %s on %s" % (
            where, item["text"], got, item["ink"], item["fill"])


def close_page(page):
    page.cdp.call("Target.closeTarget", {
        "targetId": page.cdp.call("Target.getTargetInfo", {}, session=page.sid)["targetInfo"]["targetId"]})


@pytest.fixture
def avatar_review(env, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    path = tmp_path / "avatar-settings.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Avatar settings", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(path)), look=LOOK, wake=False)
    account(name="Theo")
    return data["id"]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_settings_avatars_share_the_board_palette(avatar_review, server, chrome, theme):
    rid = avatar_review
    board = Page(chrome, f"{server}/r/{rid}/", width=1280, height=900, media=theme, script=CLEAR,
                 wait="!!document.querySelector('.av-self')")
    try:
        assert board.js("getComputedStyle(document.body).backgroundColor") == BODY[theme]
        mine = json.loads(board.js(BOARD_AVATAR_JS))
        palette = json.loads(board.js(PALETTE_JS))
        other = "dark" if theme == "light" else "light"
        board.cdp.call("Emulation.setEmulatedMedia",
                       {"features": [{"name": "prefers-color-scheme", "value": other}]}, session=board.sid)
        other_palette = json.loads(board.js(PALETTE_JS))
    finally:
        close_page(board)

    assert mine and mine["key"] in [str(i) for i in range(8)], "the header avatar carries no palette index"
    assert len(palette) == 8
    assert [p["fill"] for p in palette] != [p["fill"] for p in other_palette], \
        "the emulated theme did not switch the palette"

    page = Page(chrome, f"{server}/settings", width=1280, height=900, media=theme, script=CLEAR,
                wait="!!document.querySelector('.setting-row .row-title .av')")
    try:
        assert page.js("getComputedStyle(document.body).backgroundColor") == BODY[theme]
        surface = page.js(SURFACE_JS)
        rows = json.loads(page.js(ROWS_JS))
    finally:
        close_page(page)

    assert rows, "the Members pane has no rows"
    for row in rows:
        assert row["key"] is not None, "a Member row has no data-av: %r" % row
        index = int(row["key"])
        assert 0 <= index <= 7, "a Member row has data-av %r outside 0..7" % row["key"]
        assert row["fill"] == palette[index]["fill"], \
            "%s row index %s painted %s, the board palette says %s" % (
                row["name"], row["key"], row["fill"], palette[index]["fill"])
    assert_readable(rows, "settings members (%s)" % theme, surface)

    you = [r for r in rows if (r["badge"] or "").strip() == "You"]
    assert len(you) == 1, "the Members pane must mark exactly one row as You: %r" % rows
    assert you[0]["key"] == mine["key"], "the You row and the board header must use the same index"
    assert solid(you[0]["fill"], BODY[theme]) == solid(mine["fill"], BODY[theme]), \
        "the You row and the board header must use the same fill"

    theo = [r for r in rows if r["name"] == "Theo"]
    assert len(theo) == 1, "Theo's member row is missing: %r" % rows
    assert theo[0]["warn"], "Theo is a member, so the row keeps the warn class"
    assert theo[0]["fill"] == palette[int(theo[0]["key"])]["fill"], \
        "a non-admin row must not keep the ok-soft role fill"
