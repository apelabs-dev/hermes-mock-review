"""CUX item 4: the desktop header Comment toggle is icon-only, with the state still readable without a label.

Real headless Chrome at 1280: no visible text, the tooltip and aria-label are the same words in both states, the box is
the same size as the other header icon buttons, and toggling never moves the header or its neighbours.
"""

import json

import pytest

from mockreview import store
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
DECISIONS = {"intro": "A board with a header and one text block.", "blocks": [
    {"type": "text", "id": "t1", "title": "Why this round", "text": "A paragraph to review."}]}


@pytest.fixture
def button_review(env, tmp_path):
    path = tmp_path / "button.json"
    path.write_text(json.dumps(DECISIONS), encoding="utf-8")
    data = store.create_review("Comment button", "", ORIGIN, "local", store.parse_round_spec(decisions=str(path)),
                               wake=False)
    return data["id"]


def open_board(chrome, server, rid, width=1280, height=900, mobile=False):
    return Board(chrome, f"{server}/r/{rid}/", width=width, height=height, mobile=mobile,
                 wait="!!document.querySelector('#comment-toggle') && !!document.querySelector('.blk-text')")


STATE = """(function(){var b=document.querySelector('#comment-toggle');
  var lb=b.querySelector('.lb'), r=b.getBoundingClientRect(), bar=document.querySelector('#dzbar');
  function pos(sel){var e=document.querySelector(sel);if(!e)return null;var x=e.getBoundingClientRect();
    return [Math.round(x.left*100)/100,Math.round(x.top*100)/100,Math.round(x.width*100)/100,Math.round(x.height*100)/100];}
  var cs=getComputedStyle(b);
  return JSON.stringify({pressed:b.getAttribute('aria-pressed'),label:b.getAttribute('aria-label'),title:b.title,
    inner:b.innerText,labelText:lb&&lb.textContent,labelShown:!!lb&&getComputedStyle(lb).display!=='none',
    box:[Math.round(r.width*100)/100,Math.round(r.height*100)/100],
    bar:[Math.round(bar.getBoundingClientRect().width*100)/100,Math.round(bar.getBoundingClientRect().height*100)/100],
    bg:cs.backgroundColor,border:cs.borderColor,color:cs.color,
    neighbours:{wseg:pos('.dzbar .wseg'),share:pos('.share-open'),more:pos('.more-open'),who:pos('.dzbar .who')}})})()"""


def state(b):
    return json.loads(b.js(STATE))


def test_the_desktop_comment_button_is_icon_only_and_never_moves_the_header(button_review, server, chrome):
    rid = button_review
    b = open_board(chrome, server, rid)
    try:
        off = state(b)
        assert off["pressed"] == "false"
        assert off["inner"].strip() == "", "the desktop Comment button still shows text: %r" % (off["inner"],)
        assert off["labelText"] == "Comment" and not off["labelShown"], off
        assert off["title"] == "Comment (C)" and off["label"] == off["title"], off
        assert off["box"][0] == off["box"][1], "the Comment button is not square: %r" % (off["box"],)
        more = b.js("JSON.stringify((function(){var r=document.querySelector('.more-open').getBoundingClientRect();"
                    "return [Math.round(r.width*100)/100,Math.round(r.height*100)/100]})())")
        assert json.loads(more) == off["box"], (more, off["box"])

        b.press("#comment-toggle")
        assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
        on = state(b)
        assert on["pressed"] == "true"
        assert on["inner"].strip() == "", "the desktop Comment button shows text in the on state: %r" % (on["inner"],)
        assert on["labelText"] == "Exit comment mode" and not on["labelShown"], on
        assert on["title"] == "Exit comment mode (C)" and on["label"] == on["title"], on
        assert on["box"] == off["box"], (on["box"], off["box"])
        assert on["bg"] != off["bg"], "the on state lost its accent fill: %r" % (on,)
        assert "rgb" in on["bg"], on
        for key in ("wseg", "share", "more", "who"):
            if off["neighbours"].get(key):
                assert on["neighbours"][key] == off["neighbours"][key], \
                    f"toggling moved the header's {key}: {off['neighbours'][key]} -> {on['neighbours'][key]}"
        assert abs(on["bar"][0] - off["bar"][0]) <= 1, (off["bar"], on["bar"])

        # C still toggles from the page, and Escape leaves comment mode as it did.
        b.js("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
        b.key("c", "KeyC", 67, text="c")
        assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'false'")
        assert state(b)["title"] == "Comment (C)"
        b.key("c", "KeyC", 67, text="c")
        assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'true'")
        b.key("Escape", "Escape", 27)
        assert b.until("document.querySelector('#comment-toggle').getAttribute('aria-pressed') === 'false'")
    finally:
        b.js("localStorage.clear()")


def test_the_phone_bottom_bar_comment_button_is_unchanged(button_review, server, chrome):
    rid = button_review
    b = open_board(chrome, server, rid, width=390, height=844, mobile=True)
    try:
        b.cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}, session=b.sid)
        got = json.loads(b.js(
            "JSON.stringify((function(){var b=document.querySelector('.foot-comment');if(!b)return {missing:1};"
            "var r=b.getBoundingClientRect();return {pressed:b.getAttribute('aria-pressed'),box:[r.width,r.height],"
            "title:b.title,inner:b.innerText,bar:!!document.querySelector('.dzfoot')}})())"))
        assert not got.get("missing"), "the phone bottom bar lost its Comment button"
        assert got["pressed"] == "false" and got["title"] == "Comment (C)"
        assert got["inner"].strip() == "", got
        assert got["box"][0] >= 44 and got["box"][1] >= 44, got
        b.press(".foot-comment")
        assert b.until("document.querySelector('.foot-comment').getAttribute('aria-pressed') === 'true'")
        assert b.js("document.querySelector('.foot-comment').title") == "Exit comment mode (C)"
    finally:
        b.js("localStorage.clear()")
