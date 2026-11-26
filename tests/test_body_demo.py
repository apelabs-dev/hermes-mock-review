"""examples/body-demo builds four reviews, one per board body, and each of them is a valid round.

The bodies: decision rows, a stage of frames, a page with section marks kept in the page's own data, and an artifact with
display "canvas". No browser here: the round goes through the same parse and create path as `mockreview create`.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from mockreview import store

DEMO = Path(store.__file__).resolve().parent.parent / "examples" / "body-demo" / "make_demo.py"
ORIGIN = {"platform": "discord", "chat_id": "test-chat"}


def _load():
    spec = importlib.util.spec_from_file_location("body_demo_make_demo", DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def built(tmp_path):
    return _load().build(tmp_path / "out")


def _blocks(path):
    return json.loads(path.read_text(encoding="utf-8"))["blocks"]


def test_all_four_reviews_are_written_and_validate(built):
    assert set(built) == {"rows", "stage", "plan", "deck"}
    for name, path in built.items():
        spec = store.parse_round_spec(decisions=str(path))
        assert spec["decisions"]["blocks"], name


def test_each_review_becomes_a_review(env, built):
    for name, path in built.items():
        data = store.create_review(f"Body demo {name}", "", ORIGIN, "local",
                                   store.parse_round_spec(decisions=str(path)), wake=False)
        assert data["id"], name


def test_rows_are_decisions_with_a_mock_per_option(built):
    decisions = [b for b in store.parse_round_spec(decisions=str(built["rows"]))["decisions"]["blocks"] if b["type"] == "decision"]
    assert [d["id"] for d in decisions] == ["look", "density"]
    assert [len(d["options"]) for d in decisions] == [3, 2]
    assert [o["label"] for o in decisions[0]["options"]] == ["Paper", "Slate", "Meadow"]
    for d in decisions:
        assert sum(1 for o in d["options"] if o.get("recommended")) == 1
        assert all(o["html"] for o in d["options"])


@pytest.mark.parametrize("name", ["stage", "plan"])
def test_stage_and_page_keep_their_marks_in_the_page_not_on_the_block(built, name):
    (block,) = store.parse_round_spec(decisions=str(built[name]))["decisions"]["blocks"]
    assert block["type"] == "artifact" and block["display"] == "page" and block["id"] == name
    assert not block.get("markable"), "the marks belong to the page's own data"
    html = (built[name].parent / "page" / "page.html").read_text(encoding="utf-8")
    assert "MARKS_JS" not in html, "the shared marks script was not put into the page"
    assert "use('data')" in html and "hook.set('marks'" in html
    marked = re.findall(r'data-mark-group="([a-z-]+)"', html)
    assert len(marked) == {"stage": 3, "plan": 4}[name] and len(set(marked)) == len(marked)
    for part in marked:   # every mark group sits on a part a pin can name too
        assert f'data-mr-part="{"part-" if name == "plan" else ""}{part}"' in html, part


def test_the_deck_is_a_canvas_of_six_slides(built):
    (block,) = store.parse_round_spec(decisions=str(built["deck"]))["decisions"]["blocks"]
    assert block["type"] == "artifact" and block["display"] == "canvas"
    assert not block.get("markable"), "a markable artifact sits in the document column; a canvas is full width"
    html = (built["deck"].parent / "page" / "page.html").read_text(encoding="utf-8")
    assert len(re.findall(r'<section class="slide', html)) == 6


def test_the_demo_is_generic_and_follows_the_craft_floor(built):
    """Only example.test addresses, no emoji standing in for icons, no uppercase eyebrow labels."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in built.values())
    text += "\n".join(p.read_text(encoding="utf-8") for p in built["rows"].parent.parent.glob("*/page/page.html"))
    for host in re.findall(r"(?:https?://|@|\b)((?:[a-z0-9-]+\.)+(?:test|com|org|net|io|dev|app))\b", text):
        assert host.endswith("example.test"), host
    assert not re.search("[\U0001F300-\U0001FAFF\u2600-\u27BF]", text), "emoji in the demo"
    assert "text-transform:uppercase" not in text.replace(" ", ""), "an eyebrow label"
