"""Context sections (text, cards, callout, mock, table) and 4-6 option decisions in decisions.json.

Context sections are never voted on: they stay out of decision_index / the tally / marks / the webhook summary. A
context mock may carry pins (block = its id, option None) and those reach feedback.md like any other pin."""

import copy
import json
import re

import pytest

from conftest import http
from mockreview import config, feedback, service, store
from mockreview.store import ReviewError

ROOT = config.BOARD_DIR

OPT = lambda i: {"id": "abcdef"[i], "label": f"Option {i}", "html": f"<div class='o{i}'>{i}</div>"}  # noqa: E731

GOOD = {"intro": "hi", "blocks": [
    {"type": "text", "title": "Why", "text": "para one\n\npara two"},
    {"type": "callout", "tone": "warn", "title": "Constraint", "text": "Must fit one phone screen."},
    {"type": "cards", "title": "What's changing", "items": [
        {"title": "List", "text": "4 → 8 rows", "pill": "Changed", "tone": "accent"},
        {"title": "Banner"}]},
    {"type": "mock", "id": "today", "title": "Today", "text": "for reference", "caption": "as shipped",
     "html": "<div class='sheet'>today</div>"},
    {"type": "decision", "id": "d1", "title": "Two", "layout": "side", "options": [OPT(0), OPT(1)]},
    {"type": "decision", "id": "d4", "title": "Four", "layout": "side", "options": [OPT(i) for i in range(4)]},
    {"type": "decision", "id": "d5", "title": "Five", "layout": "switch", "options": [OPT(i) for i in range(5)]},
    {"type": "decision", "id": "d6", "title": "Six", "layout": "steps", "options": [OPT(i) for i in range(6)]},
    {"type": "decision", "id": "ba", "title": "BA", "layout": "before-after", "options": [OPT(i) for i in range(4)]},
    {"type": "table", "title": "Compare", "columns": ["Aspect", "A", "B"], "rows": [["Taps", 2, "3"], ["Fits", "yes", "no"]],
     "highlight_col": 2},
]}
DECISIONS = ["d1", "d4", "d5", "d6", "ba"]


def _write(tmp_path, dec, name="dec"):
    f = tmp_path / name / "decisions.json"
    f.parent.mkdir()
    f.write_text(json.dumps(dec), encoding="utf-8")
    return f


def _review(tmp_path, dec=GOOD):
    spec = store.parse_round_spec(decisions=str(_write(tmp_path, dec)))
    return store.create_review("Sections", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, "tailscale", spec)


def _with(block, i=None):
    """GOOD with one block replaced (or appended)."""
    d = copy.deepcopy(GOOD)
    if i is None:
        d["blocks"].append(block)
    else:
        d["blocks"][i] = block
    return d


# ---------- valid ----------

def test_every_block_type_validates_and_is_cleaned():
    out = store.validate_decisions(GOOD)
    by = {b["type"] + (":" + b["id"] if b.get("id") else ""): b for b in out["blocks"]}
    assert [b["type"] for b in out["blocks"]] == [b["type"] for b in GOOD["blocks"]]
    assert by["text"]["text"] == "para one\n\npara two"
    assert by["callout"] == {"type": "callout", "title": "Constraint", "tone": "warn", "text": "Must fit one phone screen."}
    assert by["cards"]["items"] == [{"title": "List", "text": "4 → 8 rows", "pill": "Changed", "tone": "accent"},
                                    {"title": "Banner", "text": "", "pill": "", "tone": "neutral"}]   # tone defaults
    m = by["mock:today"]
    assert m["html"] == "<div class='sheet'>today</div>" and m["caption"] == "as shipped" and m["desktop_width"] == 1280
    assert "options" not in m and "layout" not in m
    t = by["table"]
    assert t["columns"] == ["Aspect", "A", "B"] and t["rows"] == [["Taps", "2", "3"], ["Fits", "yes", "no"]]   # numbers → text
    assert t["highlight_col"] == 2
    assert [len(by[f"decision:{d}"]["options"]) for d in DECISIONS] == [2, 4, 5, 6, 4]
    assert [o["id"] for o in by["decision:d6"]["options"]] == list("abcdef")


def test_text_block_still_works_and_optional_fields_are_optional():
    out = store.validate_decisions({"blocks": [
        {"type": "text", "text": "just prose"},
        {"type": "callout", "tone": "ok", "text": "fine"},
        {"type": "table", "columns": ["a", "b"], "rows": [["1", "2"]]},
        {"type": "mock", "id": "m1", "html": "<p>x</p>", "desktop_width": 640}]})
    assert out["blocks"][0] == {"type": "text", "title": "", "text": "just prose"}
    assert out["blocks"][1]["title"] == "" and out["blocks"][2]["highlight_col"] is None
    assert out["blocks"][3]["desktop_width"] == 640 and out["blocks"][3]["title"] == ""


# ---------- invalid, with clear errors ----------

BAD = [
    ({"type": "cards"}, "cards needs items"),
    ({"type": "cards", "items": []}, "cards needs items, a list of 1-12"),
    ({"type": "cards", "items": [{"title": f"c{i}"} for i in range(13)]}, "1-12"),
    ({"type": "cards", "items": ["just a string"]}, "items[0]: must be an object"),
    ({"type": "cards", "items": [{"text": "no title"}]}, "items[0]: title is required"),
    ({"type": "cards", "items": [{"title": 5}]}, "items[0]: title is required"),
    ({"type": "cards", "items": [{"title": "x", "tone": "red"}]}, "items[0]: tone must be one of"),
    ({"type": "callout", "tone": "warn"}, "callout needs text"),
    ({"type": "callout", "tone": "warn", "text": "   "}, "callout needs text"),
    ({"type": "callout", "text": "no tone"}, "tone must be one of"),
    ({"type": "callout", "tone": "danger", "text": "x"}, "tone must be one of"),
    ({"type": "table", "columns": ["only one"], "rows": [["x"]]}, "columns, a list of 2-6"),
    ({"type": "table", "columns": list("abcdefg"), "rows": [list("abcdefg")]}, "columns, a list of 2-6"),
    ({"type": "table", "columns": "a,b", "rows": [["x", "y"]]}, "columns, a list of 2-6"),
    ({"type": "table", "columns": ["a", {"b": 1}], "rows": [["x", "y"]]}, "columns[1]: must be text"),
    ({"type": "table", "columns": ["a", "b"], "rows": []}, "rows, a list of 1-30"),
    ({"type": "table", "columns": ["a", "b"], "rows": [["x", "y"]] * 31}, "rows, a list of 1-30"),
    ({"type": "table", "columns": ["a", "b"], "rows": [["x"]]}, "rows[0]: must be a list of 2 cells"),
    ({"type": "table", "columns": ["a", "b"], "rows": ["x, y"]}, "rows[0]: must be a list of 2 cells"),
    ({"type": "table", "columns": ["a", "b"], "rows": [["x", True]]}, "rows[0][1]: must be text"),
    ({"type": "table", "columns": ["a", "b"], "rows": [["x", None]]}, "rows[0][1]: must be text"),
    ({"type": "table", "columns": ["a", "b"], "rows": [["x", "y"]], "highlight_col": 2}, "highlight_col must be a column index 0-1"),
    ({"type": "table", "columns": ["a", "b"], "rows": [["x", "y"]], "highlight_col": -1}, "highlight_col"),
    ({"type": "table", "columns": ["a", "b"], "rows": [["x", "y"]], "highlight_col": True}, "highlight_col"),
    ({"type": "table", "columns": ["a", "b"], "rows": [["x", "y"]], "highlight_col": "1"}, "highlight_col"),
    ({"type": "mock", "html": "<p>x</p>"}, "mock id must be short lowercase"),
    ({"type": "mock", "id": "Today", "html": "<p>x</p>"}, "mock id must be short lowercase"),
    ({"type": "mock", "id": "m1"}, "mock needs an html string"),
    ({"type": "mock", "id": "m1", "html": "  "}, "mock needs an html string"),
    ({"type": "mock", "id": "m1", "html": "<p/>", "desktop_width": 100}, "desktop_width must be 320-1920"),
    ({"type": "mock", "id": "m1", "html": "<p/>", "desktop_width": True}, "desktop_width must be 320-1920"),
    ({"type": "mock", "id": "d1", "html": "<p/>"}, "duplicate block id d1"),        # same namespace as decisions
    ({"type": "mock", "id": "today", "html": "<p/>"}, "duplicate block id today"),
    ({"type": "decision", "id": "today", "options": [OPT(0)]}, "duplicate block id today"),
    ({"type": "decision", "id": "d7", "options": [OPT(i) for i in range(6)] + [{"id": "g", "html": "x"}]}, "needs 1-6 options"),
    ({"type": "decision", "id": "d7", "options": []}, "needs 1-6 options"),
    ({"type": "chart"}, "type must be one of"),
]


@pytest.mark.parametrize("block,msg", BAD, ids=[f"{i}-{b.get('type')}" for i, (b, _) in enumerate(BAD)])
def test_invalid_blocks_are_refused_with_a_clear_error(block, msg):
    with pytest.raises(ReviewError) as e:
        store.validate_decisions(_with(block))
    assert msg in e.value.message and e.value.message.startswith(f"blocks[{len(GOOD['blocks'])}]")


def test_errors_name_the_block():
    with pytest.raises(ReviewError) as e:
        store.validate_decisions(_with({"type": "cards", "items": [{"title": "a"}, {"text": "b"}]}))
    assert e.value.message.startswith(f"blocks[{len(GOOD['blocks'])}].items[1]:")


def test_a_recap_id_may_not_clash_with_a_decision_or_mock():
    with pytest.raises(ReviewError, match="used twice"):
        store.validate_decisions(_with({"type": "recap", "id": "today", "items": ["x"]}))
    # two recaps with the default id stay allowed (as before)
    store.validate_decisions(_with({"type": "recap", "items": ["x"]}))
    store.validate_decisions(_with({"type": "recap", "items": ["y"], "markable": False}))


def test_six_options_is_the_cap_and_default_ids_run_a_to_f():
    out = store.validate_decisions({"blocks": [{"type": "decision", "id": "d1", "options": [{"html": str(i)} for i in range(6)]}]})
    assert [o["id"] for o in out["blocks"][0]["options"]] == list("abcdef")
    assert store.MAX_OPTIONS == 6


# ---------- caps ----------

def test_size_caps():
    long = "x" * 5000
    out = store.validate_decisions({"blocks": [
        {"type": "cards", "title": long, "items": [{"title": long, "text": long, "pill": long, "tone": "ok"}] * 12},
        {"type": "callout", "tone": "err", "title": long, "text": long},
        {"type": "table", "title": long, "columns": [long] * 6, "rows": [[long] * 6] * 30},
        {"type": "mock", "id": "m1", "title": long, "text": long, "caption": long, "html": "<p/>"},
        {"type": "text", "text": "y" * 9000}]})
    cards, callout, table, mock, text = out["blocks"]
    assert len(cards["title"]) == 200 and len(cards["items"]) == 12
    assert {len(cards["items"][0][k]) for k in ("title", "text", "pill")} == {120, 600, 40}
    assert len(callout["title"]) == 200 and len(callout["text"]) == 1200
    assert len(table["columns"][0]) == 60 and len(table["rows"][0][0]) == 240 and len(table["rows"]) == 30
    assert len(mock["text"]) == 2000 and len(mock["caption"]) == 300
    assert len(text["text"]) == 6000


# ---------- escaping ----------

def test_text_is_kept_literal_and_control_chars_are_stripped():
    xss = "<img src=x onerror=alert(1)>"
    out = store.validate_decisions({"blocks": [
        {"type": "cards", "items": [{"title": xss + "\x00\x07", "text": "<script>x</script>", "pill": "<b>p</b>"}]},
        {"type": "callout", "tone": "ok", "title": "a\nb", "text": xss},
        {"type": "table", "columns": [xss, "b\x1b"], "rows": [["<i>c</i>", "d\r\ne"]]}]})
    cards, callout, table = out["blocks"]
    assert cards["items"][0]["title"] == xss and cards["items"][0]["pill"] == "<b>p</b>"   # the board shows it as text
    assert callout["title"] == "a b" and callout["text"] == xss
    assert table["columns"] == [xss, "b"] and table["rows"] == [["<i>c</i>", "d e"]]


def test_board_renders_the_new_blocks_with_textContent_only():
    js = (ROOT / "board.js").read_text(encoding="utf-8")
    for fn in ("renderCards", "renderCallout", "renderTable", "renderContextMock"):
        body = js[js.index(f"function {fn}("):]
        body = body[:body.index("\n  }\n")]
        assert "innerHTML" not in body and "insertAdjacentHTML" not in body, fn
    # the only innerHTML in the board: the static icon strings and the inert <template> for mock html
    assert len(re.findall(r"\.innerHTML\s*=", js)) == 2
    assert "root.appendChild(cleanFragment(o.html))" in js


# ---------- not counted ----------

def test_context_blocks_are_not_decisions(env, tmp_path):
    data = _review(tmp_path)
    r = data["rounds"][0]
    assert [d["id"] for d in r["decisions"]] == DECISIONS
    assert r["mocks"] == [{"id": "today", "title": "Today"}]
    st = store.public_state(data)
    assert [d["id"] for d in st["rounds"][0]["decisions"]] == DECISIONS     # what the board's tally / nav count
    assert [len(d["options"]) for d in r["decisions"]] == [2, 4, 5, 6, 4]


def test_marks_on_context_blocks_are_dropped_pins_on_the_context_mock_are_kept(env, tmp_path):
    r = _review(tmp_path)["rounds"][0]
    out = feedback.sanitize_for_round({
        "marks": {"today": {"status": "approve"}, "d6": {"status": "change", "option": "f", "note": "f"}},
        "pins": [{"block": "today", "option": "a", "w": "phone", "x": 0.2, "y": 0.3, "text": "on today"},
                 {"block": "d5", "option": "e", "w": "desktop", "x": 0.1, "y": 0.1, "text": "on E"},
                 {"block": "cards", "x": 0, "y": 0, "text": "no such block"}]}, r)
    assert out["marks"] == {"d6": {"status": "change", "option": "f", "note": "f"}}
    assert out["pins"] == [{"block": "today", "option": None, "w": "phone", "x": 0.2, "y": 0.3, "text": "on today"},
                           {"block": "d5", "option": "e", "w": "desktop", "x": 0.1, "y": 0.1, "text": "on E"}]
    with pytest.raises(ReviewError):     # a mark on a context block alone is "nothing to send"
        feedback.sanitize_for_round({"marks": {"today": {"status": "approve"}}, "pins": []}, r)


def test_context_mock_pins_follow_pins_enabled(env, tmp_path):
    r = _review(tmp_path)["rounds"][0]
    out = feedback.sanitize_for_round({"marks": {"d1": {"status": "skip"}},
                                       "pins": [{"block": "today", "x": 0.2, "y": 0.3, "text": "x"}]}, r, {"pins_enabled": False})
    assert out["pins"] == []


def test_old_rounds_without_a_mock_index_still_drop_unknown_pins(env, tmp_path):
    r = _review(tmp_path)["rounds"][0]
    r.pop("mocks")
    out = feedback.sanitize_for_round({"marks": {"d1": {"status": "skip"}},
                                       "pins": [{"block": "today", "x": 0.2, "y": 0.3, "text": "x"}]}, r)
    assert out["pins"] == []


def test_submit_end_to_end_markdown_and_webhook_have_only_decisions_plus_context_pins(server, env, tmp_path):
    data = _review(tmp_path)
    fb = {"marks": {"d1": {"status": "approve", "option": "b"}, "d6": {"status": "change", "note": "tighter"},
                    "today": {"status": "approve"}},
          "pins": [{"block": "today", "w": "phone", "x": 0.5, "y": 0.25, "text": "already confusing"},
                   {"block": "d4", "option": "d", "w": "desktop", "x": 0.4, "y": 0.6, "text": "D: too tall"}],
          "guidelines": ""}
    code, _, out = http("POST", f"{server}/r/{data['id']}/api/submit", {"round": 1, "token": data["token"], "feedback": fb})
    assert code == 200, out
    payload = json.loads(env["receiver"].wakes[-1]["body"])
    assert "1 approved · 1 to change · 0 skipped · 3 open" in payload["summary"]     # 5 decisions, context not counted
    assert "today" not in payload["summary"] and "Pins:** 2" in payload["summary"]
    md = (config.data_root() / data["id"] / "rounds/1/feedback.md").read_text(encoding="utf-8")
    lines = [ln for ln in md.splitlines() if ln.startswith("- **[")]
    assert [re.match(r"- \*\*\[(\w+)\]", ln).group(1) for ln in lines] == DECISIONS
    assert "1. [today, context mock, phone] ~(50%,25%) of the mock — _Today_: already confusing" in md
    assert "2. [d4, option D, desktop] ~(40%,60%) of the mock — _Four_: D: too tall" in md
    for word in ("Constraint", "What's changing", "Compare", "callout", "cards"):
        assert word not in md
    counts = feedback.decision_counts(store.load(data["id"])["rounds"][0]["feedback"], store.round_index(store.load(data["id"])["rounds"][0]))
    assert counts == {"approve": 1, "change": 1, "skip": 0, "open": 3}
    assert service.build_payload(store.load(data["id"]), store.load(data["id"])["rounds"][0]).keys() == payload.keys()


def test_untitled_context_mock_pin_line_has_no_empty_title(env, tmp_path):
    dec = _with({"type": "mock", "id": "m2", "html": "<p/>"})
    data = _review(tmp_path, dec)
    r = data["rounds"][0]
    r["feedback"] = feedback.sanitize_for_round({"pins": [{"block": "m2", "w": "desktop", "x": 0, "y": 0, "text": "hi"}]}, r)
    md = feedback.render_decisions_markdown(data, r)
    assert "1. [m2, context mock, desktop] ~(0%,0%) of the mock: hi" in md



def test_a_recap_without_the_markable_flag_still_gets_its_buttons():
    """The board reads the round's decisions.json as stored. A hand-fixed round written without validate_decisions has
    no `markable` key; the board must use the same default as validate_decisions (markable) instead of hiding the
    Approve/Change/Skip row (2026-10-21: the recap stayed 'Open' with no way to approve it)."""
    v = store.validate_decisions(_with({"type": "recap", "items": ["y"]}))
    assert [b for b in v["blocks"] if b["type"] == "recap"][0]["markable"] is True
    from pathlib import Path
    js = (Path(store.__file__).parent / "board" / "board.js").read_text(encoding="utf-8")
    assert "if (b.markable !== false) append(el, decideRow(b, false));" in js
    assert "if (b.markable) append(el, decideRow" not in js
