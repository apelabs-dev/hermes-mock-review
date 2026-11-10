# Decisions format

A decision round is a JSON file. The agent writes it to disk and passes its absolute path to `mock_review_create`
(`decisions=`) or `mock_review_add_round`. The CLI equivalent is `--decisions /abs/decisions.json`. The board draws the
file itself, so there is no page to build. The file must be under 3 MB.

## Shape

    {
      "preset": "design",
      "intro": "Optional text shown above the first block (3000 characters).",
      "next": "Optional text for the footer: what happens once everything is decided (600 characters).",
      "extra_css": "Optional CSS applied to the mocks, as a string.",
      "status": {"label": "Round 2 · awaiting your approval", "milestones": []},
      "notes_answered": [],
      "blocks": [ ... ]
    }

`blocks` is required, holds 1 to 60 entries and renders in order. Every block has a `type` and an optional `title`
(200 characters). `decision`, `question`, `piece` and `mock` blocks need an `id`: lowercase letters, digits and dashes,
starting with a letter or digit, at most 24 characters (`d1`, `q1`, `p1`, `today`). A `defaults` block's `id` follows
the same rule and defaults to `defaults`. Ids are unique across the file. Pins and `notes_answered` refer to them.

The markable blocks collect a mark from the reviewer: `decision`, `question`, `piece`, `defaults` and a `recap` that is
markable. The other types (`text`, `callout`, `cards`, `mock`, `table`, `settled`) are context and never count toward
the tally.

Text fields longer than their limit are cut to it. A list with too many or too few entries, a missing required field or
a wrong type fails validation.

## Top-level keys

All of these are optional.

`preset`: `design` (the default) or `plan`. Both draw the same skeleton: status line, `Already decided`, the defaults
card, the body, then the final approval. A plan board groups its markable blocks into sections and navigates by their
`group`, and gets a `gate` even when the file has none.

`status`: the status line under the intro. `label` is 120 characters. `milestones` holds 0 to 6 objects, each with a
required `label` (40) and an optional `date` (40).

    {"label": "Round 2 · awaiting your approval · nothing has been built", "milestones": [{"label": "Plan agreed", "date": "Week 1"}]}

`notes_answered`: the round 2+ list "Your notes, answered". It holds 0 to 40 objects. `ref` is required and must be the
id of a block in this file (any block with an id, including a recap). `note` (the reviewer's note, 600 characters) and
`answer` (yours, 600) are required. `status` is `done` (the default), `second-look` or `needs-pick`.

    {"ref": "p2", "note": "What if two devices edit the same note?", "answer": "The newer edit wins; see the revised piece.", "status": "second-look"}

`gate`: the locked final approval. The gate card shows what is still open; the footer's Send button reads "Send notes"
until nothing is left on Change, every markable block is approved or skipped and every question is answered, and then
turns into "Build". Pressing Build sends the review with the action `build`.

- `label` is 40 characters and defaults to `Start building`.
- `text` (600) says what happens when it is pressed.
- `after` holds 0 to 12 strings of 300 characters: "What I need from you after approval".
- `later` holds 0 to 20 strings of 300 characters: "Later, not in this plan".

With `"preset": "plan"` a missing `gate` (or `null`) becomes the default gate. On a design board the gate is opt-in: add
a `gate` object to get one.

    {"label": "Start building", "text": "Approves the whole plan; work starts with the sync engine.", "after": ["Pair two test devices"], "later": ["Accounts and sign-in"]}

## Keys every markable block takes

`decision`, `question`, `piece`, `defaults` and a markable `recap` also accept these optional keys:

- `revised` (40): a badge on the card, for example `Revised` or `Updated from Q3`.
- `asked` (600) and `answer` (600): the "You asked / Answer" strip of a later round.
- `group` (40): the section label. The plan preset groups and navigates by it.

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

`settled`: the "Already decided" section. Context only: it shows answers that are no longer open and the rule each one
sets, with no controls. `items` holds 1 to 30 objects. Each needs a `title` (120 characters) and may add `quote` (600,
what was said) and `rule` (1200, what it decides).

    {"type": "settled", "title": "Already decided", "items": [{"title": "Offline first", "quote": "I write on the train.", "rule": "Every edit is saved on the device first."}]}

`defaults`: "How I read the rest": the assumptions you made where the brief was silent, marked once with
approve / change / skip and a note. `items` holds 1 to 30 strings (600 characters each). `text` is 1200 characters.
`id` defaults to `defaults`.

    {"type": "defaults", "title": "How I read the rest", "text": "Where the brief was silent:", "items": ["Sync runs every 5 minutes"]}

`question`: an open question with your recommendation. The reviewer either goes with the recommendation or types a
different answer (280 characters), and may add a note. `title` is required (200 characters), `text` is 2000.
`recommendation` is required: `answer` (300 characters, required) and `why` (600).

    {"type": "question", "id": "q1", "title": "How long do deleted notes stay recoverable?", "recommendation": {"answer": "30 days", "why": "Keeps storage bounded."}, "group": "Open questions"}

Question or decision? If you can write the answers down, it is a `decision`: two or three concrete alternatives the
reviewer compares and picks with one tap, with or without a mock (the plan preset accepts decisions too). Use a
`question` only when the answer is a value or policy the reviewer owns (a number, a limit, a name) or the range is too
open to list; the recommendation is the one-tap default. Never make the reviewer type out an option you could have
listed.

`piece`: one piece of a plan, marked approve / change / skip with a note. `title` is required. `tag` (80), `text` (2000)
and `items` (0 to 30 strings of 600 characters) are optional. `html` is an optional mock, cleaned exactly like a `mock`
block's html; reviewers can pin it. `desktop_width` works as it does for `mock`.

    {"type": "piece", "id": "p1", "title": "Local change log", "tag": "P0", "items": ["One log per notebook"], "group": "Sync engine", "revised": "Revised"}

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

## Feedback the board sends back

Each markable block gets one mark, keyed by its id. A `decision`, `piece`, `defaults` or markable `recap` mark is
`{"status": "approve" | "change" | "skip", "option": ..., "note": ...}` (`option` only on a decision). A `question` mark
is `{"choice": "recommended" | "custom", "custom": "...", "note": "..."}`: `custom` is required with `"choice": "custom"`
and must be 1 to 280 characters after invisible, bidi and control characters are removed. A question counts as resolved
once it has a choice.

The submit carries an `action`: `send` (the default) or `build`. `build` is accepted only when the round has a `gate`,
every markable block is approved or skipped (or answered, for a question) and nothing is on Change; otherwise the board
gets an error naming the open and changed ids. The feedback Hermes reads starts with **Start building pressed** after a
`build`, and a custom answer is quoted as typed by the reviewer.

## A full plan round

[examples/plan-demo/decisions.json](../examples/plan-demo/decisions.json) is a plan round that uses every key above:
`status`, `notes_answered`, `settled`, `defaults`, two questions, three pieces in two groups (one with a mock), a decision
with options, the round 2+ keys and a `gate` with `after` and `later`.

## HTML in mocks

Mock, option and piece HTML is static markup. The board parses it without running it and removes `script`, `iframe`, `frame`,
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

### Brand enforcement

A saved profile can be bound to the projects it belongs to, so that forgetting `design` cannot produce a generic-looking
board. `design.json` may carry an optional `projects` list of absolute paths (at most 50). Edit it with:

    hermes mockreview design bind acme /path/to/acme-app
    hermes mockreview design unbind acme /path/to/acme-app

`bind` needs an existing directory, stores its real path and refuses a path already bound to another profile (unbind it
there first). Binding the same path twice changes nothing. `design save` keeps the bindings of the profile it replaces,
unless the new `design.json` brings its own `projects`. `design show` and `design list` print them. A round's copy of the
profile never carries `projects`.

**Auto-apply.** `create` and `add-round` take a `project`: an absolute path (the CLI's `--project`, default the current
directory; the MCP and plugin tools have no default, so pass it). The board wears the profile bound to the project, to
the git repository it is in, or to the main checkout when the project is a linked git worktree, so a binding to the main
checkout applies inside its worktrees. The most specific (longest) bound path wins. The result names the profile in
`design_auto`.

**Which design a round gets.** `create`: `design`, else the project's bound profile, else a refusal when any profile is
saved and `no_design` is not set, else no design. `add-round`: `design`, else the review's own design (as before), else
the project's bound profile, else none for a review created with `no_design`, else the same refusal. `design` together
with `no_design` is an error.

The refusal names the project it checked, lists the saved profiles and offers three fixes: pass `design="<name>"`
(`--design <name>`); bind the project with `hermes mockreview design bind <name> <abs path>`; or pass `no_design=true`
(`--no-design`) for a board that deliberately has no profile. Nothing is created when it fires. `no_design` is recorded on
the review, so later rounds need not repeat it.

**Off-brand check.** When a profile is in effect (given, inherited or auto-applied), a decision round is refused if its
mocks use a colour or a font the profile does not allow. It reads the HTML of every block that has an `html` string and of
every option, and `extra_css`. Only CSS is read: `style` attributes, `<style>` text and the colour attributes `fill`,
`stroke`, `stop-color`, `flood-color`, `lighting-color`, `color` and `bgcolor`. Selectors, `url(...)` and quoted strings
are skipped, so `#add {}` or `url(#grad)` is not a colour.

- Colours: `#hex` (3, 4, 6 or 8 digits), `rgb()`/`rgba()` (comma or space syntax), `hsl()`/`hsla()` and CSS colour names.
  Names count only in colour properties, so an animation called `tan` is fine. Custom property definitions
  (`--brand: #ff6b00`) and the fallback of a `var()` (`var(--x, #ff6b00)`) are checked like any other literal.
- Allowed colours: every colour in the profile's themes, every colour in its `mock.css`, and white and black. Alpha is
  ignored, so `rgba(<accent>, .15)` passes, and `hsl()` may differ by one per channel for rounding. `var(--x)` is always
  allowed.
- Fonts: `font-family` and the family part of `font`. Allowed are the families in the profile's `font`, those declared in
  its `mock.css` and `fonts.css`, the generic families (`sans-serif`, `system-ui`, `monospace`, ...), CSS-wide keywords
  and `var(...)`.

A failure lists each value with its path, once each, at most 20 lines and then `... and N more`:

    blocks[3].options[b].html: color #ff6b00 is not in profile acme
    blocks[1].html: font-family "Comic Sans MS" is not in profile acme

The last line names the board's tokens (`--bg --surface --surface-2 --line --line-strong --text --muted --faint --accent
--accent-ink --accent-soft --ok --warn --err --font`): use `var(--accent)` and friends instead of raw values.

**Bypass.** `off_brand="<reason>"` (`--off-brand "<reason>"`), 1 to 200 characters, skips the check for a round that is
off-brand on purpose. The reason is recorded on the round and on the review (the latest one) and echoed in the result. An
empty reason counts as not given.

Prototype ("try it live") rounds are whole pages and are not scanned.

## Validation errors

A bad file fails the create call with the path of the problem, for example `blocks[2]: tone must be one of ('neutral',
'ok', 'warn', 'err', 'accent')`. Fix the file and call again. Nothing is created on failure.
