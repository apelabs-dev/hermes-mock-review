"""Every board avatar takes a deterministic per-person colour that clears 4.5:1 in both themes.

Drives a real headless Chrome at 1440x900 and 390x844, in light and dark. Colours are read from computed styles and
the WCAG ratio is computed here, so the assertions describe what a reviewer actually sees rather than a CSS grep.
"""

import json
import re

import pytest

from authkit import account, signed_in, token_of
from conftest import http
from mockreview import store, threads, users
from test_live_round_tool import Board, chrome  # noqa: F401

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
MOCK = ('<section><h2>Checkout</h2><p id="copy" style="width:300px;height:50px;margin:20px">'
        'Review this total</p><p id="other" style="width:300px;height:40px;margin:20px">Other line</p></section>')
DECISIONS = {"blocks": [{"type": "mock", "id": "m1", "html": MOCK}]}
ANCHOR = {"block": "m1", "option": None, "w": "desktop", "x": 0.3, "y": 0.4}


def anchor_for(size):
    return dict(ANCHOR, w="phone" if size[0] <= 600 else "desktop")


def open_popover(b):
    is_on = "document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'"
    if not b.js(is_on):
        assert b.until("!!document.querySelector('#comment-toggle')")
        b.press("#comment-toggle")
        assert b.until(is_on)
    assert b.until("!!document.querySelector('.mpin')")
    b.press(".mpin")
    assert b.until("!!document.querySelector('.tpop')")
READY = ("!!(document.querySelector('.mock-host') && document.querySelector('.mock-host').shadowRoot && "
         "document.querySelector('.mock-host').shadowRoot.querySelector('#copy'))")
BODY = {"light": "rgb(250, 250, 250)", "dark": "rgb(11, 11, 14)"}

SIZES = [(1440, 900, False, "desktop"), (390, 844, True, "phone")]


@pytest.fixture
def thread_review(env, tmp_path):
    path = tmp_path / "avatar-colours.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Avatars", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)),
                               wake=False)
    threads.reset_limits()
    return data["id"]


@pytest.fixture(params=SIZES, ids=[s[3] for s in SIZES])
def size(request):
    return request.param[:3]


@pytest.fixture(params=["light", "dark"])
def theme(request):
    return request.param


def open_board(chrome, server, rid, size, theme, url=None):
    width, height, mobile = size
    b = Board(chrome, url or f"{server}/r/{rid}/", wait=READY, width=width, height=height, mobile=mobile)
    set_theme(b, theme)
    return b


def set_theme(b, theme):
    b.js("document.documentElement.setAttribute('data-theme', %s)" % json.dumps(theme))
    assert b.until("getComputedStyle(document.body).backgroundColor === %s" % json.dumps(BODY[theme]))


def as_account(b, server, rid, member):
    b.cdp.call("Network.enable", session=b.sid)
    for key, value in (("mr_session", member["session"]), ("mr_csrf", member["csrf"])):
        assert b.cdp.call("Network.setCookie", {"name": key, "value": value, "url": server, "path": "/"},
                          session=b.sid)["success"]
    b.cdp.call("Page.navigate", {"url": f"{server}/r/{rid}/"}, session=b.sid)
    assert b.until(READY, timeout=60)


def avatars(b, selector):
    """Computed fill, ink and data-av for every element matching selector."""
    return json.loads(b.js(
        "JSON.stringify(Array.prototype.map.call(document.querySelectorAll(%s), function (e) {"
        "  var s = getComputedStyle(e);"
        "  return {key: e.getAttribute('data-av'), fill: s.backgroundColor, ink: s.color, text: e.textContent};"
        "}))" % json.dumps(selector)))


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


def assert_readable(items, where, body="#ffffff"):
    assert items, "no avatars matched %s" % where
    for item in items:
        fill = over(item["fill"], body) if rgb(item["fill"])[3] < 1 else rgb(item["fill"])
        ink = over(item["ink"], "#%02x%02x%02x" % tuple(int(round(v)) for v in fill[:3])) \
            if rgb(item["ink"])[3] < 1 else rgb(item["ink"])
        got = ratio(ink, fill)
        assert got >= 4.5, "%s avatar %r: %.2f:1 ink %s on %s" % (
            where, item["text"], got, item["ink"], item["fill"])


def make_thread(rid, member, name, text, anchor=None):
    return threads.create(rid, f"user:{member['user_id']}", name, "commenter", text,
                          anchor=dict(anchor or ANCHOR))


def add_reply(server, rid, thread, member, text):
    code, _, out = http("POST", f"{server}/r/{rid}/api/threads/{thread['id']}/reply", {"text": text},
                        signed_in(member))
    assert code == 200, out


def test_different_people_get_different_fills(thread_review, server, chrome, size, theme):
    rid = thread_review
    maya = account(review_id=rid, role="commenter", name="Maya")
    theo = account(review_id=rid, role="commenter", name="Theo")
    t = make_thread(rid, maya, "Maya", "Maya's note", anchor=anchor_for(size))
    add_reply(server, rid, t, theo, "Theo's reply")

    b = open_board(chrome, server, rid, size, theme)
    open_popover(b)
    rows = avatars(b, ".tpop .tcomment .av")
    assert len(rows) == 2
    mine = avatars(b, ".av-self")
    assert mine
    assert rows[0]["fill"] != rows[1]["fill"], "two people must not share a fill"
    assert mine[0]["fill"] not in (rows[0]["fill"], rows[1]["fill"]), "the viewer's own fill must differ"


def test_one_person_has_one_fill_everywhere(thread_review, server, chrome, monkeypatch, size, theme):
    rid = thread_review
    owner = users.owner_user()
    maya = account(review_id=rid, role="commenter", name="Maya")
    t = threads.create(rid, f"user:{owner['id']}", owner["name"], "owner", "Pinned", anchor=anchor_for(size))
    add_reply(server, rid, t, maya, "Maya was here")

    b = Board(chrome, f"{server}/r/{rid}/", wait=READY, width=size[0], height=size[1], mobile=size[2])
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    as_account(b, server, rid, maya)
    set_theme(b, theme)

    self_av = avatars(b, ".av-self")
    assert self_av and self_av[0]["key"] is not None, "the header avatar must carry a palette index"
    key = self_av[0]["key"]

    b.press(".more-open")
    assert b.until("!!document.querySelector('.menu')")
    assert b.until("!!document.querySelector('.menu .tacts .av[data-av=\"%s\"]')" % key)
    presence = avatars(b, '.menu .tacts .av[data-av="%s"]' % key)
    b.key("Escape", "Escape", 27)
    assert b.until("!document.querySelector('.menu')")

    open_popover(b)
    own = avatars(b, '.tpop .tcomment .av[data-av="%s"]' % key)
    assert presence, "the viewer's presence avatar must carry the same index"
    assert own, "the viewer's own comment avatar must carry the same index"
    for site in (presence[0], own[0]):
        assert site["fill"] == self_av[0]["fill"]
        assert site["key"] == key


def test_every_avatar_clears_contrast_in_both_themes(thread_review, server, chrome, size, theme):
    rid = thread_review
    maya = account(review_id=rid, role="commenter", name="Maya")
    theo = account(review_id=rid, role="commenter", name="Theo")
    t = make_thread(rid, maya, "Maya", "Maya's note", anchor=anchor_for(size))
    add_reply(server, rid, t, theo, "Theo's reply")

    b = open_board(chrome, server, rid, size, theme)
    body = BODY[theme]
    assert_readable(avatars(b, ".av-self"), "header", body)

    b.press(".more-open")
    assert b.until("!!document.querySelector('.menu')")
    assert b.until("!!document.querySelector('.menu .tacts .av')")
    assert_readable(avatars(b, ".menu .tacts .av"), "More menu presence", body)
    b.key("Escape", "Escape", 27)
    assert b.until("!document.querySelector('.menu')")

    open_popover(b)
    assert_readable(avatars(b, ".tpop .av"), "thread popover", body)
    b.press(".tp-close")
    assert b.until("!document.querySelector('.tpop')")

    b.press(".share-open")
    assert b.until("!!document.querySelector('.share-dialog .share-person .av')")
    assert_readable(avatars(b, ".share-person .av"), "Share dialog", body)


def test_a_guest_rename_keeps_the_id_based_colour(thread_review, server, chrome, monkeypatch, size, theme):
    rid = thread_review
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    token = token_of(users.share("test", rid, "create", "commenter")["path"])
    b = open_board(chrome, server, rid, size, theme, url=f"{server}/s/{token}")
    before = avatars(b, ".av-self")
    assert before and before[0]["key"] is not None, "a guest avatar must carry a palette index"

    b.press(".more-open")
    assert b.until("!!document.querySelector('.menu-input')")
    b.js("(function (i) { i.value = 'Nova Quinn'; i.dispatchEvent(new Event('input')); return 1 })"
         "(document.querySelector('.menu-input'))")
    assert b.until("document.querySelector('.av-self').textContent === 'NQ'")
    after = avatars(b, ".av-self")
    assert after[0]["text"] != before[0]["text"], "the initials must follow the new name"
    assert after[0]["key"] == before[0]["key"], "the id-based colour index must survive a rename"
    assert after[0]["fill"] == before[0]["fill"], "the fill must survive a rename"


def test_a_reload_keeps_the_same_colour(thread_review, server, chrome, monkeypatch, size, theme):
    rid = thread_review
    maya = account(review_id=rid, role="commenter", name="Maya")
    make_thread(rid, maya, "Maya", "Maya's note", anchor=anchor_for(size))

    b = Board(chrome, f"{server}/r/{rid}/", wait=READY, width=size[0], height=size[1], mobile=size[2])
    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "false")
    as_account(b, server, rid, maya)
    set_theme(b, theme)
    before = avatars(b, ".av-self")
    assert before and before[0]["key"] is not None
    open_popover(b)
    head = avatars(b, ".tpop .tp-head .av")
    assert head and head[0]["key"] == before[0]["key"] and head[0]["fill"] == before[0]["fill"]

    b.cdp.call("Page.reload", {}, session=b.sid)
    assert b.until(READY, timeout=60)
    set_theme(b, theme)
    after = avatars(b, ".av-self")
    assert after[0]["key"] == before[0]["key"], "a reload must not reseed the colour"
    assert after[0]["fill"] == before[0]["fill"]
    open_popover(b)
    head2 = avatars(b, ".tpop .tp-head .av")
    assert head2[0]["key"] == head[0]["key"] and head2[0]["fill"] == head[0]["fill"]
