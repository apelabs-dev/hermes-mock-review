# Decisions format

A decision round is a JSON file. The agent writes it to disk and passes its absolute path to `mock_review_create`
(`decisions=`) or `mock_review_add_round`. The CLI equivalent is `--decisions /abs/decisions.json`. The board draws the
file itself, so there is no page to build. The file must be under 3 MB.

## Shape

    {
      "intro": "Optional text shown above the first block (3000 characters).",
      "next": "Optional text for the footer: what happens once everything is decided (600 characters).",
      "extra_css": "Optional CSS applied to the mocks, as a string.",
      "blocks": [ ... ]
    }

`blocks` is required, holds 1 to 60 entries and renders in order. Every block has a `type` and an optional `title`
(200 characters). `decision` and `mock` blocks need an `id`: lowercase letters, digits and dashes, starting with a letter
or digit, at most 24 characters (`d1`, `d2`, `today`). Ids are unique across the file. Pins refer to them.

Only `decision` blocks, and a `recap` that is markable, collect a mark from the reviewer. The other types are context
and never count toward the tally.

## Block types

`text`: prose that frames the round.

    {"type": "text", "title": "Background", "text": "Up to 6000 characters."}

`callout`: one constraint or risk. `text` is required (1200 characters). `tone` is `neutral`, `ok`, `warn`, `err` or
`accent`.

    {"type": "callout", "tone": "warn", "title": "Constraint", "text": "Must fit one phone screen."}

`cards`: a short grid for "what is changing". `items` holds 1 to 12 objects. Each needs a `title` (120 characters) and may
add `text` (600), `pill` (40) and `tone` (default `neutral`).

    {"type": "cards", "title": "What changes", "items": [{"title": "Header", "text": "Shrinks to 48 px", "pill": "new"}]}

`mock`: a context mock, for example how the screen looks today. `html` is required. Optional `text` (2000), `caption`
(300) and `desktop_width` (320 to 1920 px, default from the `desktop_width` setting). Reviewers can pin it, but cannot
mark it.

    {"type": "mock", "id": "today", "title": "Today", "html": "<div class=\"card\">...</div>", "caption": "Current checkout"}

`table`: compares options. `columns` holds 2 to 6 headings (60 characters each). `rows` holds 1 to 30 rows, each a list
with one cell per column (240 characters per cell; numbers are allowed). `highlight_col` is an optional column index that
gets emphasis. Bare absolute HTTP(S) URLs inside cells become links opening in a new tab, including multiple URLs
embedded in prose. Surrounding text, sentence punctuation and literal HTML remain unchanged.

    {"type": "table", "columns": ["", "A", "B"], "rows": [["Taps", 3, 2], ["Risk", "low", "medium"]], "highlight_col": 2}

`recap`: a list the reviewer can confirm. `items` is a list of strings (up to 40, 600 characters each). `markable`
defaults to true. `id` defaults to `recap` and must not equal a decision or mock id.

    {"type": "recap", "title": "Agreed so far", "items": ["Dark theme stays", "Header at 48 px"]}

`decision`: the thing being voted on. `options` holds 1 to 6 entries. Prefer two or three.

    {
      "type": "decision", "id": "d1", "title": "Checkout layout", "layout": "side",
      "tag": "Checkout", "nav": "Layout", "today": "One column, 5 steps.", "text": "Which layout ships?",
      "options": [
        {"id": "a", "label": "Single page", "text": "Everything on one screen.", "recommended": true, "html": "<div>...</div>"},
        {"id": "b", "label": "Stepper", "html": "<div>...</div>"}
      ]
    }

- `layout` is `side` (default, options next to each other), `switch`, `steps` or `before-after`.
- Each option needs `html`. `id` defaults to a, b, c in order and must be 1 to 8 lowercase letters or digits, unique within
  the decision. `label` defaults to the id in capitals (80 characters). `text` is 400 characters. `recommended` marks the
  agent's pick.
- `tag` (80), `nav` (24, the label in the jump list), `today` (1200) and `text` (2000) are optional.
- `desktop_width` works as it does for `mock`.

## HTML in mocks

Mock and option HTML is static markup. The board parses it without running it and removes `script`, `iframe`, `frame`,
`object`, `embed`, `base`, `meta`, `link` and `form` elements, `on*` attributes and `javascript:` URLs. Style it with
`extra_css`, with the design profile's `mock.css`, or with inline `style` attributes. Anchor hrefs retain only absolute
HTTP(S) URLs without whitespace/control characters; relative URLs and all other protocols are removed. Safe links open
in a new tab with `target="_blank" rel="noopener noreferrer"`; clicking one does not add a pin.

## Design profiles

A design profile makes the board and its mocks look like your product. It is a folder with a `design.json` and optional
files:

    {
      "name": "acme",
      "label": "Acme",
      "themes": {
        "light": {"bg": "#ffffff", "surface": "#f6f6f4", "text": "#18181b", "accent": "#4f46e5"},
        "dark": {"bg": "#0f0f12", "surface": "#18181d", "text": "#ececf1", "accent": "#a5a4ff"}
      },
      "default_theme": "light",
      "mock_css": "mock.css"
    }

`themes` needs `light`, `dark` or both. Each theme must set `bg`, `surface`, `text` and `accent` as `#hex` or `rgb()`
colours. It may also set `surface2`, `line`, `line2`, `muted`, `faint`, `accentInk`, `ok`, `warn` and `err`. `font`,
`source` and `captured_at` are free text. `icon`, `mock_css` and `fonts_css` name files inside the folder.

    hermes mockreview design save acme /path/to/acme-profile
    hermes mockreview design list
    hermes mockreview design show acme

Then pass `design="acme"` (or `--design acme`) to create. `add-round` inherits the profile.

## Validation errors

A bad file fails the create call with the path of the problem, for example `blocks[2]: tone must be one of ('neutral',
'ok', 'warn', 'err', 'accent')`. Fix the file and call again. Nothing is created on failure.
