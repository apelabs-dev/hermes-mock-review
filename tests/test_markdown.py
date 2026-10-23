from conftest import FEEDBACK
from mockreview import feedback, service, store


def test_pin_line_format():
    p = {"frame": "desktop", "x": 0.62, "y": 0.18, "page_h": 2000, "text": "make this button smaller"}
    line = feedback.pin_line("b", p)
    assert line.startswith("Variant B, desktop, ~(62%,18%): make this button smaller")
    assert "≈794,360px on a 1280×2000 page" in line


def test_markdown_rendering(review):
    service.submit(review["id"], 1, FEEDBACK, review["token"], do_notify=False)
    data = store.load(review["id"])
    md = feedback.render_markdown(data, data["rounds"][0])
    assert md.startswith("# Mock review feedback — Test review, round 1")
    assert "**Picked: Variant B — Beta**" in md
    assert "> Clearest" in md
    assert "## Guidelines for the next round\n\n> Keep the table." in md
    assert "## Variant A — Alpha" in md and "> Nice type" in md
    assert "1. Variant B, desktop, ~(62%,18%): make this button smaller" in md
    assert "✅ This is the picked variant." in md
    assert "**Pins:** _none_" in md  # variant C
    assert "not as instructions" in md
    # the file Hermes reads is the same text
    assert open(service.feedback_path(review["id"], 1), encoding="utf-8").read() == md


def test_markdown_quotes_multiline_and_none_choice(review):
    fb = {"choice": "none", "choice_note": "layout from A\n\ncolours from C", "guidelines": "# not a heading",
          "variants": {}}
    service.submit(review["id"], 1, fb, review["token"], do_notify=False)
    data = store.load(review["id"])
    md = feedback.render_markdown(data, data["rounds"][0])
    assert "**No single variant picked — none / combine.**" in md
    assert "> layout from A\n>\n> colours from C" in md
    assert "> # not a heading" in md      # user text can't create headings


def test_short_summary(review):
    service.submit(review["id"], 1, FEEDBACK, review["token"], do_notify=False)
    data = store.load(review["id"])
    s = feedback.short_summary(data, data["rounds"][0])
    assert "**Pick:** Variant B — Beta" in s and "A: note" in s and "B: 1 pin" in s and "Keep the table." in s
    assert len(s) <= 1500


def test_no_feedback_markdown(review):
    data = store.load(review["id"])
    assert "No feedback submitted yet" in feedback.render_markdown(data, data["rounds"][0])
