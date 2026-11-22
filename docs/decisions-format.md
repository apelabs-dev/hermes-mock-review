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

`blocks` is required, holds 1 to 60 entries and renders in order. Every block has a `type` and an optional `title`; exports render every block type statically.
(200 characters). `decision`, `question`, `piece`, `mock` and `artifact` blocks need an `id`: lowercase letters, digits and dashes,
starting with a letter or digit, at most 24 characters (`d1`, `q1`, `p1`, `today`). A `defaults` block's `id` follows
the same rule and defaults to `defaults`. Ids are unique across the file. Pins and `notes_answered` refer to them.

The markable blocks collect a mark from the reviewer: `decision`, `question`, `piece`, `defaults` and a `recap` that is
markable. The other types (`text`, `callout`, `cards`, `mock`, `table`, `settled`) are context and never count toward
the tally. An `artifact` is context unless it sets `"markable": true`.

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

`gate`: the final approval. It has no card of its own on the board. The round's footer has one button, picked by the
`footer_button` setting (`--footer-button` per review):

- `build` (the default): the button is "Build". It is locked until nothing is left on Change, every markable block is
  approved or skipped and every question is answered; its tooltip says what is still open ("10 still open"), and a tap
  on a phone shows the same line. While a block is on Change the round cannot be built, so the button is "Send" for
  those notes. Pressing Build sends the review with the action `build`.
- `send`: the button is always "Send" and the board never offers Build.

- `label` is 40 characters and defaults to `Build`. It names the approval in the "pressed" line and in the feedback
  Hermes reads; the footer button itself always reads "Build".
- `text` (600) says what happens when it is pressed. It is the Build button's tooltip once the button unlocks.
- `after` holds 0 to 12 strings of 300 characters: "What I need from you after approval".
- `later` holds 0 to 20 strings of 300 characters: "Later, not in this plan".

With `"preset": "plan"` a missing `gate` (or `null`) becomes the default gate. On a design board the gate is opt-in: add
a `gate` object to get one.

    {"label": "Build", "text": "Approves the whole plan; work starts with the sync engine.", "after": ["Pair two test devices"], "later": ["Accounts and sign-in"]}

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

`artifact`: your own page, with script, shown in a sandboxed frame. Use it where a mock cannot do the job: audio and
video players, a timeline, anything that needs script. Give exactly one of `html` (the page, up to 2 MB) or `src` (an
absolute path to an `.html` file). Optional `text` (2000) and `caption` (300), both plain text; `height` (`"auto"`, the
default, follows the page up to 4000 px, or a fixed 80 to 4000); `desktop_width` as for `mock`; `display` (`"frame"`,
the default, or `"page"`); `markable` (default `false`; `true` adds the keys every markable block takes, and the reviewer
can approve or change it). Reviewers can pin it in comment mode either way.

    {"type": "artifact", "id": "clip", "title": "Click sound", "html": "<audio controls src=\"data:audio/wav;base64,UklGRg==\"></audio><script>document.title = 'ok'</script>", "caption": "Take 3", "markable": true}

`display: "page"` is the same page-mode frame a prototype round gets: the frame is the full width of the window and one
viewport tall (between the board header and the docked decide bar), the page scrolls inside it, and there is no 4000 px
cap and no card frame around it. It ignores `height` and `desktop_width`. The block's title, `text` and `caption` stay
in the document column, and its pins keep working.

With `src` the block is `{"type": "artifact", "id": "film", "src": "/abs/film/page.html"}` and the page's folder supplies the media.

How it runs:

- The page is written to `rounds/<n>/a/blocks/<id>.html` and loaded in an `<iframe sandbox="allow-scripts">`. No
  `allow-same-origin`, so the page has an opaque origin: it cannot read the board, its cookies or storage, cannot post to
  `api/submit`, cannot navigate the board, open windows or submit forms. It is not `srcdoc`: a `srcdoc` frame would inherit
  the board's CSP and its inline script would not run.
- The file starts with a Content-Security-Policy meta: `default-src 'none'; script-src 'unsafe-inline'; style-src
  'unsafe-inline'; img-src data:; media-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; base-uri
  'none'; frame-src 'none'`. Inline script and style run; the page itself loads nothing from the network. Every file of
  the round (the block file, `assets/`, `design/`, `decisions.json`) is also served with `Content-Security-Policy: sandbox
  allow-scripts; ` followed by that same policy, so a page that navigates its own frame to another file of the round
  still gets a document with no network, even if the meta were ignored. A round with no artifact is served with
  `sandbox allow-scripts` alone. A frame that ends up anywhere but its block file (it does not answer the board's ping)
  is reset to the block file up to three times, then removed; the board's own policy already refuses navigation to
  another site.
- Everything the page shows is inside the file. With `src`, the board bundles the page's local files in as `data:` URIs:
  images, audio, video, fonts, stylesheets and scripts (a local `<script src>` becomes an inline script; a local ES module
  `import` is refused). Paths are relative to the page's folder, may not leave it or go through a symbolic link. This
  happens on every board, whatever the hosting mode. Limits: 2 MB for the page itself (inline `html`, or the `src` file),
  25 MB per asset, 50 MB of assets per round across all its artifacts and the round's copied `assets/` folder. A missing
  or oversized asset fails the create or add-round call and names the artifact and the asset.
- The page can keep state and use three board facilities, all through `window.mockreview.use(name)`. The hook is
  synchronous, frozen and defined before the page's own scripts. Inside the frame of an artifact block's own page
  (`rounds/<n>/a/blocks/<id>.html`) `use('data')`, `use('marks')` and `use('clipboard')` return their objects; every
  other name returns `null`, as they all do on a prototype or variant page, whose `mockreview.comments` API still works.
- `use('data')` is a per-review, per-round key/value store for the page. `get(key)` resolves with the value or
  `undefined` (it waits for the board's first state message, so it never answers a stale `undefined`); `set(key, value)`
  resolves `true`; `list()` resolves the sorted keys; `remove(key)` resolves `true`. `await use('data')` is a no-op, so a
  page may write `var data = await window.mockreview.use('data')`. Values must be JSON-serialisable and are capped at
  8 KB each (`JSON.stringify(value)`, UTF-8), 64 keys per block and 256 KB per round across all blocks. A refusal rejects
  the promise with an `Error` naming the limit (`value over 8 KB`, `block has 64 keys`, `round state over 256 KB`).
  The probe also keeps its own outgoing bucket: it posts at most 40 messages a second (its height reports and taps
  included), so a call past that bucket rejects at once with `rate limited` instead of being dropped by the board, and a
  pending call that gets no answer within 3 seconds rejects with the same error, so no call is left pending.
  The store survives a board reload, is scoped to the block's id, and reaches Hermes in the submit under
  `page_state[<block id>]`; the feedback markdown prints it under `## Page state`.
- `use('marks')` is for a markable artifact block: `await use('marks').set({status: 'approve' | 'change' | 'skip', note:
  '…'})` writes the same mark the board's own Approve/Change/Skip row writes and redraws it. A non-markable block is
  refused, and the reviewer still presses Send.
- `use('clipboard').writeText(text)` copies text with the page's own Clipboard API and only within 1 second of a real tap
  in the frame; a call with no recent tap rejects with `no recent tap`. The artifact iframe's `allow="clipboard-write"`
  enables the page's own Clipboard API for every script in the frame while the frame has focus, so a direct
  `navigator.clipboard.writeText` call — the page's own, or one a script it bundles makes — skips both the tap rule and
  the board's wrapper. The frame cannot read the clipboard, and the board is not in the clipboard path.
- The board reads these messages from the frame: its height, its background colour, a tap, a pin edit, `data` set/remove/list, a `mark` and
  nothing else. A message from a frame that is not a block of the round the board is showing, or tagged with an earlier
  round, is ignored. Messages are rate-limited per frame (a frame that floods is ignored, not obeyed) and height changes
  are bounded. A tap can open a pin draft (in comment mode); only the reviewer saves it.
- In comment mode a tap anywhere in the page pins, its own buttons, links, fields and media controls included, and it
  reaches none of the page's handlers; Enter or Space on a focused control does nothing. Mark an element
  `data-mr-live` to keep it working in comment mode: a tap on it, or on anything inside it, is the page's own and never
  pins. For a radio wrapped in a `<label>`, mark the label. On an artifact page the board's listeners run before any of
  the page's; on a prototype or variant page the probe is added at the end, so a `window` capture-phase listener the
  page registers before it still runs first.

A page that uses all three:

    <script>
    var data = window.mockreview.use('data');
    data.get('pick').then(function (pick) { /* restore a control, or undefined */ });
    data.set('pick', 'b').then(function () {}, function (e) { console.log(e.message); });
    data.list().then(function (keys) {});
    // on a markable artifact: the reviewer still presses Send
    window.mockreview.use('marks').set({status: 'approve', note: 'Looks right'});
    // from a real Copy button: no tap in the last second -> a rejected promise
    window.mockreview.use('clipboard').writeText('summary text');
    </script>

- The brand check scans the page's html like any mock (bundled `data:` media is skipped). Colours and fonts the profile
  does not allow fail the call, or pass `off_brand`.
- An artifact counts as interactive for the `interactive_elements` hint: it runs script.

Build it like this when an artifact is the right block: write `page.html` and its media in a folder of their own (not next
to `decisions.json`, whose sibling `assets/` folder is copied into the round too), then point `src` at it.
[examples/artifact-demo/make_demo.py](../examples/artifact-demo/make_demo.py) writes a demo round with audio cards and a
timeline, a video, and a shot-list table:

    python examples/artifact-demo/make_demo.py /tmp/artifact-demo
    mockreview create --title "Artifact demo" --decisions /tmp/artifact-demo/decisions.json ...

`table`: compares options. `columns` holds 2 to 6 headings (60 characters each). `rows` holds 1 to 30 rows, each a list
with one cell per column (240 characters per cell; numbers are allowed). `highlight_col` is an optional column index that
gets emphasis. Bare absolute HTTP(S) URLs inside cells become links opening in a new tab, including multiple URLs
embedded in prose. Surrounding text, sentence punctuation and literal HTML remain unchanged.

    {"type": "table", "columns": ["", "A", "B"], "rows": [["Taps", 3, 2], ["Risk", "low", "medium"]], "highlight_col": 2}

`recap`: a list the reviewer can confirm. `items` is a list of strings (up to 40, 600 characters each). `markable`
defaults to true. `id` defaults to `recap` and must not equal a decision or mock id. With `markable: false` it is drawn
as a plain list with no controls.

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
gets an error naming the open and changed ids. The feedback Hermes reads starts with **Build pressed** after a
`build`, and a custom answer is quoted as typed by the reviewer.

## A full plan round

[examples/plan-demo/decisions.json](../examples/plan-demo/decisions.json) is a plan round that uses every key above:
`status`, `notes_answered`, `settled`, `defaults`, two questions, three pieces in two groups (one with a mock), a decision
with options, the round 2+ keys and a `gate` with `after` and `later`.

## HTML in mocks

The board parses mock, option and piece HTML without running it and removes `script`, `iframe`, `frame`, `object`,
`embed`, `base`, `meta` and `link` elements, `on*` attributes and `javascript:` URLs. Style it with `extra_css`,
with the design profile's `mock.css`, or with inline `style` attributes. Anchor hrefs retain only absolute HTTP(S) URLs
without whitespace/control characters; relative URLs and all other protocols are removed. Safe links open in a new tab
with `target="_blank" rel="noopener noreferrer"` when comment mode is off. In comment mode a click on one pins the link
instead and does not open it.

### Interactive elements

The `interactive_elements` setting is on by default (`--no-interactive` per review turns it off). With it on, build
every control a mock shows so that it responds, using markup that needs no script:

- a dropdown or menu: `<select>`, or `<details><summary>` with the menu inside;
- a switch, checkbox or toggle: `<input type="checkbox">` styled with `:checked`;
- tabs, segmented controls and option pickers: `<input type="radio">` with `:checked` and a sibling selector;
- text fields: `<input>` and `<textarea>`;
- hover and focus states: `:hover` and `:focus-visible`.

The board opens usable: taps reach the mock's own controls, and pressing Comment (or `C`) turns comment mode on, where
a tap on anything in the mock pins a note on its element, controls included. A tap inside a button, link, summary,
field, label or native media player pins that control and does not work it (no navigation, toggle, focus or checkbox
flip), and Enter or Space on a focused control does nothing. Only an element marked `data-mr-live` (and everything
inside it) keeps its clicks in comment mode. Text selection on ordinary content still works, so a drag still makes a
quote pin. Leave comment mode (Esc, `C` or the Comment button) to use the controls. A mock form never navigates the
board, and the board CSP and opaque iframe sandbox still restrict form navigation. A create or add-round whose mocks have no such control returns a `hint` saying so. The setting controls the authoring hint; it does not disable native controls.

### Element and text anchors

Give meaningful elements a stable `data-mr-part`, for example
`<section data-mr-part="order-summary">…</section>`. Keep part names unique within each mock and at most 40 characters.
New rounds automatically stamp `data-mr-id` on anchorable HTML at installation; these IDs are stored with the round,
without changing the source file. Dynamic content gets an ID when first observed. Highlight wrappers get no IDs.

In Comment mode the hovered content has an outline (inside a native control, the control's: the element a click would
pin; none over a `data-mr-live` element), and a pin's composer shows `on <label>`: its nearest heading,
then role/type and up to 40 characters of text (120 characters total). A heading uses its own text. Pins follow their
element on move/resize. Removing it leaves an indigo pin (55% opacity) at the last position with **Element removed**; restoring a
unique matching element reattaches it. Empty-space and older coordinate-only pins retain their positions.

Selecting up to 160 characters on noninteractive content and releasing opens a range comment. The anchor stores W3C
TextQuoteSelector and TextPositionSelector data in addition to the element identity. Reanchoring requires an exact,
uniquely contextual quote; ambiguous matches detach. Highlights use CSS Custom Highlight where supported and text-only
`mark` wrappers otherwise. Recovery tries auto ID, part, finder path, then a unique tag/text signature.

#### Pins on the board itself

Every rendered section of a decisions round is pinnable in Comment mode: the doc header, text, cards, callout, table,
"Already decided", recap items, defaults, a question, an option caption, the decide row and "Anything else". A section
without a block id gets a stable `s<N>` ref. Three reserved refs name the board surfaces: `page` is the header, `else`
is "Anything else", and `doc` is the bare board canvas (empty space between sections). They are reserved, so a block may
not use them as an id. A pin on the decide row pins the section on its padding: its buttons stay live. The markdown line
names the section and the quoted text, and the quote is not printed again in the target suffix.

A board pin remembers its spot in page (document) coordinates: a pin with an element target keeps it in `target.doc`, and
a pin without one (a click on empty canvas) keeps `doc` on the pin. Detached and empty-canvas pins are drawn from that
point, so deleting the anchored element or growing a section's pin list never moves the marker; the outer `x/y`
fractions are kept for older readers and the markdown.

Prototype pages may call `window.mockreview?.comments.openComposer({element})` or `openComposer({range})` while Comment
is active. `window.mockreview?.comments.anchorFor(element)` describes an element without opening UI. A page registers
extra anchors with `window.mockreview.comments.registerAnchors(fn)` (call it after the probe has run, for example on
`load`); `window.mockreview?.comments.customAnchors()` returns the validated list from that function (at most 60, an
empty list when none is registered or it throws). Assigning `window.mockreview.customAnchors` directly is not supported.
These hooks add no network or board-control access. The shell owns the composer and submission. Inline mocks remain
script-free.

### Media in a plain mock

A mock needs no script to show media. A decisions round copies the `assets/` folder next to its `decisions.json` into
the round (50 MB, shared with the media of the round's artifacts, symbolic links skipped), and the board serves it
from its own origin at `rounds/<n>/a/assets/<file>`,
so mock HTML can use `<img>`, `<audio controls>` and `<video controls>` with a path like
`src="rounds/1/a/assets/click.wav"` (`<n>` is the round the block is in, relative to the board's address). The board's
policy allows same-origin media, and the HTML cleaner keeps those tags. The board answers `Range` requests with `206` and
`Content-Range`, and every response carries `Accept-Ranges: bytes`, so audio and video get a duration and can be scrubbed,
and Safari plays video served from a round; a request for several ranges at once is answered with the whole file. One
limit: the media stays beside the round instead of inside the block. Use an `artifact` when the page needs script, or its
media must play everywhere. Page mode (a prototype round, or an artifact with `"display": "page"`) is the framed mode's
full-bleed sibling: the page fills the window and scrolls inside its frame, and its media still seeks because the board
answers `Range` the same way.

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
