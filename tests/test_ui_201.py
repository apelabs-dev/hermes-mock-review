"""mock-review 2.0.1 board decisions, measured in a real Chrome.

Layout inline is the flowing document (thin rules, no card boxes), layout cards keeps the 2.0.0 look, decide
buttons stand off the canvas in both themes, selects carry the same chevron on the board and Settings, and a
question block carries no answer sticker. Every measurement is a computed style; every wait is a DOM state.
"""

import base64
import json
import os
import re
import time
from pathlib import Path

import pytest

from authkit import account
from mockreview import store
from test_export_browser import Page
from test_live_round_tool import Board, chrome  # noqa: F401  (chrome is the fixture)

pytestmark = pytest.mark.browser

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}
PLAN = Path(store.__file__).resolve().parent.parent / "examples/plan-demo/decisions.json"
QUESTION = {"blocks": [
    {"type": "question", "id": "q1", "title": "Keep retention at 30 days?",
     "text": "Longer history costs storage; shorter history risks losing something.",
     "recommendation": {"answer": "Yes, keep 30 days.", "why": "It matches the plan."}},
]}
WAIT_BOARD = "!!document.querySelector('.doc section.blk')"


def shot_dir():
    value = os.environ.get("MOCKREVIEW_SHOT_DIR")
    return Path(value) if value else None


def shoot(page, name):
    out = shot_dir()
    if out is None:
        return
    out.mkdir(parents=True, exist_ok=True)
    result = page.cdp.call("Page.captureScreenshot", {"format": "png"}, session=page.sid)
    (out / name).write_bytes(base64.b64decode(result["data"]))


def plan_review(tmp_path, layout):
    path = tmp_path / ("plan-" + layout + ".json")
    path.write_text(PLAN.read_text(encoding="utf-8"), encoding="utf-8")
    return store.create_review("Plan probe", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(path)),
                               look={"layout": layout}, wake=False)


def question_review(tmp_path):
    path = tmp_path / "question.json"
    path.write_text(json.dumps(QUESTION), encoding="utf-8")
    return store.create_review("Question probe", "", ORIGIN, "local",
                               store.parse_round_spec(decisions=str(path)),
                               look={"layout": "inline"}, wake=False)


def open_board(chrome, server, data, width=1440, height=900, mobile=False, wait=WAIT_BOARD):
    return Board(chrome, f"{server}/r/{data['id']}/", width=width, height=height, mobile=mobile, wait=wait)


def emulated_media(page, theme):
    page.cdp.call("Emulation.setEmulatedMedia",
                  {"features": [{"name": "prefers-color-scheme", "value": theme}]}, session=page.sid)


def reload(page, wait):
    page.cdp.call("Page.navigate", {"url": page.js("location.href")}, session=page.sid)
    assert page.until(wait, timeout=60), "the page did not come back after reload"
    time.sleep(0.3)


def block_styles(page):
    return json.loads(page.js(
        "JSON.stringify(Array.prototype.map.call(document.querySelectorAll('.doc section.blk'), function (e) {"
        "var s = getComputedStyle(e);"
        "return {cls: e.className, bg: s.backgroundColor, top: s.borderTopWidth, left: s.borderLeftWidth,"
        "right: s.borderRightWidth, bottom: s.borderBottomWidth,"
        "radii: [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomRightRadius, s.borderBottomLeftRadius]};"
        "}))"))


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_inline_layout_is_a_flowing_document(env, server, tmp_path, chrome, width, height, mobile):
    data = plan_review(tmp_path, "inline")
    b = open_board(chrome, server, data, width, height, mobile)
    emulated_media(b, "light")   # the tokens must not depend on the host's appearance
    reload(b, WAIT_BOARD)
    assert b.js("document.body.classList.contains('layout-inline')"), \
        "layout=inline must put layout-inline on the body"
    assert not b.js("document.body.classList.contains('layout-cards')")
    rows = block_styles(b)
    assert rows, "the plan board rendered no sections"
    for row in rows:
        assert row["top"] == "0px", row
        assert row["left"] == "0px", row
        assert row["right"] == "0px", row
        assert row["bottom"] == "1px", row
        assert row["radii"] == ["0px", "0px", "0px", "0px"], row
        assert row["bg"] == "rgba(0, 0, 0, 0)", row
    shoot(b, f"inline-{width}.png")


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_cards_layout_keeps_the_2_0_0_look(env, server, tmp_path, chrome, width, height, mobile):
    data = plan_review(tmp_path, "cards")
    b = open_board(chrome, server, data, width, height, mobile)
    emulated_media(b, "light")   # the tokens must not depend on the host's appearance
    reload(b, WAIT_BOARD)
    assert b.js("document.body.classList.contains('layout-cards')"), \
        "layout=cards must put layout-cards on the body"
    rows = block_styles(b)
    assert rows, "the plan board rendered no sections"
    assert any(row["radii"][0] != "0px" and row["bg"] == "rgb(255, 255, 255)" for row in rows), rows
    shoot(b, f"cards-{width}.png")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_decide_buttons_stand_off_the_canvas(env, server, tmp_path, chrome, theme):
    data = plan_review(tmp_path, "inline")
    b = open_board(chrome, server, data)
    if theme == "dark":
        emulated_media(b, "dark")
        reload(b, WAIT_BOARD)
    rows = json.loads(b.js(
        "JSON.stringify((function () {"
        "var body = getComputedStyle(document.body).backgroundColor, out = [];"
        "document.querySelectorAll('.dbtn, .btn.grp-approve').forEach(function (e) {"
        "var s = getComputedStyle(e), r = e.getBoundingClientRect();"
        "if (!r.width || !r.height || s.display === 'none' || s.visibility === 'hidden') return;"
        "out.push({name: e.className, bg: s.backgroundColor, body: body});"
        "});"
        "return out;"
        "})())"))
    assert rows, "the plan board has no decide buttons"
    for row in rows:
        assert row["bg"] != "rgba(0, 0, 0, 0)" and row["bg"] != "transparent", row
        assert row["bg"] != row["body"], row


CHEVRON_JS = """
JSON.stringify((function () {
  var out = [];
  document.querySelectorAll(%s).forEach(function (e) {
    var r = e.getBoundingClientRect();
    if (!r.width || !r.height || e.getClientRects().length === 0) return;
    var s = getComputedStyle(e);
    if (s.display === 'none' || s.visibility === 'hidden') return;
    out.push({appearance: s.appearance, image: s.backgroundImage, size: s.backgroundSize,
              x: s.backgroundPositionX, y: s.backgroundPositionY, w: e.clientWidth, h: e.clientHeight});
  });
  return out;
})())
"""


def right_gap(q):
    size = float(q["size"].split()[0].replace("px", ""))
    m = re.match(r"calc\(100% - ([\d.]+)px\)", q["x"])
    if m:
        return float(m.group(1))
    if q["x"].endswith("%"):
        return (q["w"] - size) * (1 - float(q["x"].rstrip("%")) / 100.0)
    return q["w"] - float(q["x"].replace("px", "")) - size


def vertical_centre(q):
    size = float(q["size"].split()[1].replace("px", ""))
    if q["y"].endswith("%"):
        return (q["h"] - size) * float(q["y"].rstrip("%")) / 100.0 + size / 2.0
    if q["y"] == "center":
        return q["h"] / 2.0
    return float(q["y"].replace("px", "")) + size / 2.0


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_select_chevrons_match_on_both_pages(env, server, tmp_path, monkeypatch, chrome, theme):
    data = plan_review(tmp_path, "inline")
    b = open_board(chrome, server, data)
    if theme == "dark":
        emulated_media(b, "dark")
        reload(b, WAIT_BOARD)
    b.press(".share-open")
    assert b.until("!!document.querySelector('.share-dialog .share-select')"), "the Share dialog did not open"
    board = json.loads(b.js(CHEVRON_JS % json.dumps(".share-dialog .share-select")))
    assert board, "the Share dialog has no select"

    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    account(name="Theo")
    page = Page(chrome, f"{server}/settings", width=1440, height=900, media=theme,
                script="try{localStorage.clear()}catch(e){}",
                wait="!!document.querySelector('.settings-tab')")
    page.press(".settings-tab:nth-child(1)")
    assert page.until("!!document.querySelector('select.sel')"), "the Members role select did not render"
    settings = json.loads(page.js(CHEVRON_JS % json.dumps("select.sel")))
    assert settings, "the Members pane has no select"
    assert len(settings) >= 2, ("the Members pane renders one role select per account, so the loop below must not be vacuous",
                                settings)
    if theme == "dark":
        shoot(page, "settings-members-dark.png")

    for where, got in (("board", board), ("settings", settings)):
        for q in got:
            assert q["appearance"] == "none", (where, q)
            assert q["image"] != "none", (where, q)
            assert q["size"] == "12px 12px", (where, q)
            assert abs(right_gap(q) - 8.0) <= 1.0, (where, q)
            assert abs(vertical_centre(q) - q["h"] / 2.0) <= 1.0, (where, q)
        gaps = [right_gap(q) for q in got]
        assert max(gaps) - min(gaps) <= 1.0, (where, gaps)
    for board_q in board:
        for settings_q in settings:
            assert abs(right_gap(board_q) - right_gap(settings_q)) <= 1.0, (board_q, settings_q)


def test_question_block_has_no_answer_sticker_and_toggles(env, server, tmp_path, chrome):
    data = question_review(tmp_path)
    b = open_board(chrome, server, data, wait="!!document.querySelector('#dz-q1 .qbtn')")
    text = b.js("document.body.innerText")
    assert "Not answered yet" not in text
    assert "Answer:" not in text
    assert not b.js("!!document.querySelector('.qresult')"), "the question still renders a .qresult sticker"
    assert b.js("document.querySelector('.qbtn.q-recommended').tagName") == "BUTTON"

    b.press(".qbtn.q-recommended")
    assert b.until("document.querySelector('.qbtn.q-recommended').getAttribute('aria-pressed') === 'true'")
    b.press(".qbtn.q-recommended")
    assert b.until("document.querySelector('.qbtn.q-recommended').getAttribute('aria-pressed') === 'false'")

    draft = "JSON.parse(localStorage.getItem('mr:' + %s + ':r1:draft') || 'null')" % json.dumps(data["id"])
    assert b.until(f"(() => {{var d = {draft}; return !!d && !(d.marks && d.marks.q1)}})()", timeout=10), \
        "a second press must clear the stored choice"
    reload(b, "!!document.querySelector('#dz-q1 .qbtn')")
    assert "Not answered yet" not in b.js("document.body.innerText")
    assert b.until(f"(() => {{var d = {draft}; return !!d && !(d.marks && d.marks.q1)}})()", timeout=10)

    b.press(".qbtn.q-custom")
    assert b.until("document.activeElement === document.querySelector('#dz-q1 .qinput')", timeout=10), \
        "Different answer must focus that section's input"


@pytest.mark.parametrize("width,height,mobile", [(390, 844, True), (640, 900, False), (1440, 900, False)],
                         ids=["390", "640", "1440"])
def test_no_horizontal_overflow_on_board_and_settings(env, server, tmp_path, monkeypatch, chrome,
                                                      width, height, mobile):
    data = plan_review(tmp_path, "inline")
    b = open_board(chrome, server, data, width, height, mobile)
    assert b.js("document.body.classList.contains('layout-inline')")
    board = json.loads(b.js(
        "JSON.stringify({scroll: document.documentElement.scrollWidth,"
        "client: document.documentElement.clientWidth})"))
    assert board["scroll"] <= board["client"], (width, board)

    monkeypatch.setenv("MOCKREVIEW_LOOPBACK_OWNER", "true")
    account(name="Theo")
    page = Page(chrome, f"{server}/settings", width=width, height=height, media="light",
                wait="!!document.querySelector('.settings-tab')")
    page.press(".settings-tab:nth-child(1)")
    assert page.until("!!document.querySelector('select.sel')"), "the Members role select did not render"
    got = json.loads(page.js(
        "JSON.stringify({scroll: document.documentElement.scrollWidth,"
        "client: document.documentElement.clientWidth})"))
    assert got["scroll"] <= got["client"], (width, got)
