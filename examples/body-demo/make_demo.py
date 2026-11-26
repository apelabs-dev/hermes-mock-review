#!/usr/bin/env python3
"""Write four small reviews into a folder, one for each way a board body can be built, all about a made-up notes app.

    python examples/body-demo/make_demo.py OUTDIR
    mockreview create --title "Acme Notes: look" --decisions OUTDIR/rows/decisions.json ...

    rows    decision rows with a mock per option: pick a look (native blocks, no page of your own)
    stage   an artboard: three layouts of one settings screen side by side, marked one by one in the page
    plan    one long page with four parts, each marked in the page's own data
    deck    an artifact with display "canvas": six full-width slides as one long page

Each review is OUTDIR/<name>/decisions.json; a page lives in OUTDIR/<name>/page/page.html, apart from decisions.json (a
sibling assets/ folder would be copied into the round). Nothing here is real: Acme Notes does not exist and every address
is example.test. Standard library only.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGES = HERE / "pages"

# ---------------------------------------------------------------------------------------------------- rows

# Three looks of one screen: the notes window with a list and the open note. The board draws these mocks itself, so the
# look is plain CSS classes in extra_css.
ROWS_CSS = """
.an{width:100%;display:grid;grid-template-columns:minmax(0,290px) minmax(0,1fr);border-radius:var(--an-r2);overflow:hidden;
  font:14px/1.45 system-ui,-apple-system,sans-serif;background:var(--an-bg);color:var(--an-text);border:1px solid var(--an-line)}
.an *{box-sizing:border-box}
.an-list{padding:16px;display:grid;gap:8px;align-content:start}
.an-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:2px}
.an-bar b{font-size:20px;font-weight:650}
.an-plus{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;background:var(--an-accent);color:var(--an-onacc)}
.an-plus svg,.an-find svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.an-find{display:flex;gap:8px;align-items:center;padding:8px 12px;border-radius:var(--an-r);background:var(--an-surface);color:var(--an-muted);
  border:1px solid var(--an-line)}
.an-row{padding:10px 12px;display:grid;gap:2px;background:var(--an-surface);border-radius:var(--an-r);border:1px solid var(--an-line)}
.an-row b{font-size:15px;font-weight:650}
.an-row span{color:var(--an-muted);font-size:13px}
.an-row.an-on{background:var(--an-tint);border-color:var(--an-accent)}
.an-doc{background:var(--an-surface);padding:24px 28px;display:grid;gap:10px;align-content:start;border-left:1px solid var(--an-line)}
.an-doc h5{font-size:24px;font-weight:650;line-height:1.2;margin:0}
.an-doc p{margin:0;color:var(--an-text);max-width:48ch}
.an-pill{justify-self:start;font-size:12px;font-style:normal;padding:2px 10px;border-radius:999px;background:var(--an-tint);color:var(--an-tintink)}
.an-paper{--an-bg:#f6f2e8;--an-surface:#fffdf7;--an-line:#e1d9c6;--an-text:#1f1d18;--an-muted:#5b5649;--an-accent:#1f6f5a;--an-onacc:#fff;
  --an-r:6px;--an-r2:10px;--an-tint:#e3f1ea;--an-tintink:#14604a}
.an-paper .an-bar b,.an-paper .an-row b,.an-paper .an-doc h5{font-family:ui-serif,Georgia,serif}
.an-slate{--an-bg:#151a21;--an-surface:#1c222b;--an-line:#2b3440;--an-text:#e8edf3;--an-muted:#a9b4c1;--an-accent:#e8b04a;--an-onacc:#1c1608;
  --an-r:12px;--an-r2:16px;--an-tint:#2d2615;--an-tintink:#f0c470}
.an-slate .an-row{background:#1f2630;border-color:transparent}
.an-slate .an-row.an-on{background:#2d2615;border-color:var(--an-accent)}
.an-meadow{--an-bg:#e8f2ea;--an-surface:#ffffff;--an-line:#d3e4d7;--an-text:#17261f;--an-muted:#4c6256;--an-accent:#2b8556;--an-onacc:#fff;
  --an-r:18px;--an-r2:26px;--an-tint:#d9efe1;--an-tintink:#1d6a41}
.an-meadow .an-row{box-shadow:0 6px 14px -10px rgba(23,38,31,.35)}
.an-meadow .an-doc{border-left:0}
.an-tight .an-row{padding:6px 12px}.an-tight .an-row span{display:none}.an-tight .an-list{gap:6px}
@media (max-width:620px){.an{grid-template-columns:minmax(0,1fr)}.an-doc{display:none}}
"""

PLUS = '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>'
FIND = '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>'

NOTES = [
    ("Landlord deposit", "Emailed on 3 March. Returned within 14 days.", "Home"),
    ("Tuesday planning", "Launch checklist, owners, open risks.", "Work"),
    ("Bread, simple", "500 g flour, 350 g water, 10 g salt.", "Kitchen"),
]


def mock(look, tight=False, rows=NOTES):
    items = "".join(f'<div class="an-row{" an-on" if i == 0 else ""}"><b>{t}</b><span>{s}</span></div>'
                    for i, (t, s, _) in enumerate(rows))
    title, _, pill = rows[0]
    cls = " an-tight" if tight else ""
    part = f"notes-{look}" + ("-tight" if tight else "")
    return (f'<div class="an an-{look}{cls}" data-mr-part="{part}"><div class="an-list"><div class="an-bar"><b>Notes</b>'
            f'<span class="an-plus">{PLUS}</span></div><div class="an-find">{FIND}Search notes</div>{items}</div>'
            f'<div class="an-doc"><h5>{title}</h5><p>Emailed on 3 March. The deposit is returned within 14 days of the '
            f'move-out date.</p><p>Ask for the photo report before signing off the inventory.</p><i class="an-pill">{pill}</i></div></div>')


def rows_review():
    return {
        "intro": "Pick the look for Acme Notes. Each option is the same notes list, drawn three ways.",
        "next": "Your pick becomes the app's theme; the density choice sets how many notes fit on a screen.",
        "extra_css": ROWS_CSS,
        "blocks": [
            {"type": "decision", "id": "look", "nav": "Look", "layout": "side", "desktop_width": 860,
             "title": "Which look should Acme Notes ship with?",
             "today": "One plain white theme, whatever the time of day.",
             "text": "The three differ in colour, type and corner size. The notes and the layout are the same.",
             "options": [
                 {"label": "Paper", "recommended": True,
                  "text": "Warm off-white, serif titles, small corners. Calm for long reading.", "html": mock("paper")},
                 {"label": "Slate",
                  "text": "Dark and neutral with an amber accent. Easy on the eyes at night.", "html": mock("slate")},
                 {"label": "Meadow",
                  "text": "Light green, large corners and soft shadows. The friendliest of the three.", "html": mock("meadow")},
             ]},
            {"type": "decision", "id": "density", "nav": "Density", "layout": "side", "desktop_width": 860,
             "title": "How many notes should fit on a screen?",
             "text": "Shown in the Paper look. The open note looks the same in both.",
             "options": [
                 {"label": "Comfortable", "recommended": True,
                  "text": "Title and a line of preview. Three notes in view.", "html": mock("paper")},
                 {"label": "Compact",
                  "text": "Titles only. Twice as many notes in view.", "html": mock("paper", tight=True)},
             ]},
        ],
    }


# ---------------------------------------------------------------------------------------------------- pages

def page(name):
    """A page from pages/, with the shared marks script put where it asks for it."""
    html = (PAGES / f"{name}.html").read_text(encoding="utf-8")
    return html.replace("/*MARKS_JS*/", (PAGES / "marks.js").read_text(encoding="utf-8"))


def stage_review(src):
    return {
        "intro": "Three layouts of the same settings screen, side by side on a stage. Try each one, then mark it.",
        "next": "The layout you approve is the one I build; a Change note tells me what to adjust.",
        "blocks": [
            {"type": "artifact", "id": "stage", "title": "Settings screen: three layouts", "src": str(src), "display": "page",
             "markable": False,
             "text": "Each layout has its own Approve, Change and Skip with a note. The marks are kept in the page and arrive with "
                     "your feedback as page state. Fit and 100% zoom the stage."},
        ],
    }


def plan_review(src):
    return {
        "intro": "The launch plan for Acme Notes 2.0, in four parts. Mark each part as you read.",
        "next": "I start on the parts you approve and rewrite the ones you changed.",
        "blocks": [
            {"type": "artifact", "id": "plan", "title": "Acme Notes 2.0 launch plan", "src": str(src), "display": "page",
             "markable": False,
             "text": "Each part has Approve, Change and Skip with a note. The marks are kept in the page and arrive with "
                     "your feedback as page state."},
        ],
    }


def deck_review(src):
    return {
        "intro": "A six-slide overview of Acme Notes, shown as one long page. Scroll through it and pin what you want changed.",
        "next": "Press Send and I fix the pinned slides, then send the deck again.",
        "blocks": [
            {"type": "artifact", "id": "deck", "title": "Acme Notes: product overview", "src": str(src), "display": "canvas",
             "markable": False,
             "text": "Six slides, full width, in order. The page scrolls with the board, so the header and the footer stay in reach."},
        ],
    }


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(out):
    """Write the four reviews under `out`. Returns {name: path of its decisions.json}."""
    out = Path(out).resolve()
    made = {}
    for name, review, source in (("rows", rows_review(), None), ("stage", stage_review, "stage"),
                                 ("plan", plan_review, "plan"), ("deck", deck_review, "deck")):
        folder = out / name
        if source:
            src = folder / "page" / "page.html"   # the page lives apart from decisions.json
            write(src, page(source))
            review = review(src)
        write(folder / "decisions.json", json.dumps(review, indent=2) + "\n")
        made[name] = folder / "decisions.json"
    return made


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    for name, path in build(argv[1]).items():
        print(f"{name:6} {path}")


if __name__ == "__main__":
    main(sys.argv)
