# Design system (2.0)

The whole app wears one design: graphite with the mock-review indigo accent, dark and light. The approved source is
the approved 2.0 design pack; this page records the rules that live in the code and the tests.

## Tokens

`mockreview/board/board.css` defines the palette as custom properties. A per-review design profile may override the
colour tokens and the font, so a reviewed project still lends its own colours to the chrome. `--pin` is the one
exception: it is the brand indigo in every theme and cannot be overridden by a profile.

| Token | Dark | Light | Meaning |
| --- | --- | --- | --- |
| `--b` | `#0b0b0e` | `#fafafa` | page background |
| `--s` | `#141418` | `#ffffff` | surface (bar, cards, menus) |
| `--s2` | `#1c1c22` | `#f1f1f4` | raised surface, tracks, quiet chips |
| `--l` | `#2a2a33` | `#e2e2e8` | hairline |
| `--t` | `#ededf2` | `#17171c` | text |
| `--m` | `#a0a0ac` | `#666672` | muted text |
| `--a` | `#8585ff` | `#4f46e5` | accent |
| `--ai` | `#0b0b0e` | `#ffffff` | ink on accent |
| `--as` | `rgba(133,133,255,.16)` | `rgba(79,70,229,.10)` | accent soft |
| `--ok` / `--oks` | `#4ade80` / soft | `#15803d` / soft | approved, done |
| `--wn` / `--wns` | `#fbbf24` / soft | `#a16207` / soft | waiting, needs a note |
| `--err` | `#f87171` | `#dc2626` | errors |
| `--pin` | `#5b5bf0` | `#5b5bf0` | every pin and pin badge, always |
| `--r` | `10px` | `10px` | controls |
| `--rc` | `12px` | `12px` | cards, dialogs, menus |
| `--sh` | deep shadow | soft shadow | dialogs and menus |

`mockreview/board/tw.css` is built from `mockreview/board/src/board.src.css` with Tailwind (`npm run build:css`) and committed, so a
release compiles the utilities once and installing the plugin still needs no Node. The Tailwind theme maps the same
variables (`bg-surface`, `text-muted`, `border-line`, `rounded-card`, …). `tests/test_tw_css.py` rebuilds the file when
the local CLI is present and fails when it is stale.

## Type, spacing, shape

- Text sizes are only **12, 13, 14, 16 and 20 px**. Body copy is 14; secondary copy is 13; labels and metadata are
  12; section headings and card titles are 20. Nothing is smaller than 12.
- Spacing follows a 4 px grid. Components use 8, 12, 16 and 24 px gaps and padding.
- Two radii: controls (`--r`, 10 px) and cards, dialogs and menus (`--rc`, 12 px). Pills and avatars are full-round.
- Icons are Lucide, 16 px, stroke 1.5, `currentColor`, inline SVG. An icon sits next to a label unless its meaning is
  universal (close, more, send). `mockreview/board/board.js` holds the paths; prototype pins use the same indigo.
- Exactly one filled accent button per screen: Send, Invite or Save. Everything else is an outline or ghost button.
- Long titles and names truncate with an ellipsis and keep a `title` attribute. No screen scrolls horizontally from
  320 px up.

## Hit sizes and focus

- Desktop controls are at least 36 px high; phone controls are at least 44 px. Segmented controls, switches and menu
  rows follow the same floor. The switch is drawn 40 × 24 inside a 44 px row.
- `:focus-visible` draws a 2 px accent ring with a 2 px offset.
- Menus open attached to the control that opened them. Arrow keys move through a menu, Home/End jump, Escape closes
  and returns focus to the opener; Tab is trapped.
- Dialogs and bottom sheets trap Tab, close on Escape and return focus to the control that opened them. Comment
  sheets return focus to the pin.
- Colour pairs meet WCAG AA (4.5:1 for normal text) in both themes. `tests/test_ui_matrix.py` measures it.

## Components

The implementation is plain JavaScript; shadcn/ui is the reference for look and behaviour, not a dependency. The
browser never runs a framework.

| Component | Class | Notes |
| --- | --- | --- |
| Button | `.btn`, `.btn-primary`, `.btn-ghost`, `.icon-btn` | outline default, one filled primary per screen |
| Segmented control | `.seg`, `.seg-btn` | `role="radiogroup"`, arrow/Tab friendly |
| Dropdown menu | `.menu`, `.menu-item`, `.menu-label`, `.menu-sep` | fixed, attached, focus-trapped; bottom sheet on a phone |
| Dialog | `.share-dialog` / `.dialog` | centred on desktop, bottom sheet under 640 px |
| Bottom sheet | `.menu-sheet`, `.popover.sheet` | grab handle, safe-area padding |
| Tabs | `.settings-nav`, `.settings-tab`, `.t-nav a` | side list on desktop, horizontal row on phone; the section-jump `.dnav` stays sticky under the header with an opaque background |
| Switch | `.switch`, `.switch-knob` | 40 × 24 visual, 44 px row on a phone |
| Badge | `.badge`, `.badge.ac`, `.badge.wn` | 22 px pill, 12 px label |
| Input | `.inp`, `.field`, `.share-input`, `.menu-input` | 36 / 44 px, accent focus ring |
| Avatar | `.av` | accent-soft with accent initials; `.av-self` in the header |
| Tooltip | `title` + `aria-label` | every icon-only control names itself |

## Screens

- **Board header.** Logo, title and round line; then Comment, the Desktop/Tablet/Mobile switch, Share and the avatar,
  with one More menu (`ellipsis`). A phone header keeps the logo, title, avatar and More. More holds appearance
  (Light / Dark / Auto), the view switch on a phone, Comments and Activity with their counts, presence, Share on a
  phone, Export (PNG, PDF, HTML, Markdown and Include comments), the hosting mode and, for an account, Settings and
  Sign out. A guest starts with a server-assigned random adjective+animal name, unique among the review's live guests,
  and can edit the name others see in More.
- **Phone bottom bar.** One bar: Comment (icon button), the round picker, the tally (`3 of 5 done`) and Send, the only
  filled button.
- **Decision card and footer.** The board canvas is the review: each block is a card with its tag, title, Today line,
  option tiles, the mock, Approve / Change / Skip, the picker and a note field. The sticky footer carries the round
  segment, the tally and the one Send or Build button. On a phone the footer becomes the bottom bar above.
- **Comments.** A popover anchored at the pin on desktop, a bottom sheet on a phone. Header: pin number, anchor name,
  state, Resolve and Close. Comments keep their avatars, edit/delete and typing state; the reply row is an input plus
  a filled send button. There is no separate Comments panel in the board chrome; the all-comments overlay and the
  activity feed stay in More.
- **Share.** Centred dialog on desktop, bottom sheet on a phone. Invite row on top, people with access below, link
  controls last. Every existing control is kept: invite name, role and expiry, the invite link, people and their
  roles, Clear override/Remove, audience, guest role, link expiry, copy, regenerate, stop sharing and the version the
  viewer sees; seats stay visible.
- **Home.** Reviews grouped by what needs the reader: **Needs you**, **Waiting on Hermes**, **Done**. Search filters
  by title; each row keeps the status badge, round and age. The title, link and Settings link are unchanged.
- **Settings.** Sections are a side list on desktop and a horizontal tab row on a phone. Each setting is a row with a
  label, one line of help and the control on the right. The existing sections (Members, Invites, Devices) and every
  control are kept.
- **Export.** The menu lives in More; PNG, PDF, HTML and Markdown, plus the Include comments toggle, all behave as
  before. The exported HTML uses the same tokens and type scale.
- **Sign-in, link confirmation, dead-link, throttled, link-full and error states.** Same words and actions, in the
  same system: logo, 12/13/14/16/20 px type, a 12 px card, 36 px desktop / 44 px phone controls and the accent ring.
  There is no separate setup or token web page: setup and login are CLI commands.

## The board shell

Every board keeps the shell: a header, a footer and the essential controls in both, drawn in the review's design
profile, or the default look when the review has none. Everything between them is the agent's decision for each task;
nothing in the plugin picks it.

## The pin

Pins and pin badges are always mock-review indigo (`--pin: #5b5bf0`), white numeral, a 2 px surface-coloured ring so
they read on any mock underneath. A design profile cannot change `--pin` or its ring (`applyDesign` removes both and
`DESIGN_VARS` omits them). A detached pin keeps the same indigo at 55% opacity and says **Element removed**.

The marker is 26 × 26 px in every theme and at every width; the blanket phone control floor must not stretch it. On a
phone the touch target is a separate transparent 44 × 44 px box centred on the marker (`::before`), so the visual
stays 26 px while the hit area meets the 44 px floor. Unread is a small red dot on the pin (`::after`), never a
recolour of the pin body; the 7 px red dot in the comments list is a separate `un` element scoped away from pins.

## Control inventory (2.0 regrouping)

Nothing was removed. “More” means the control moved into the header's More menu with a plain label.

| Screen | On the base | In 2.0 |
| --- | --- | --- |
| Board header | brand, title, brief, identity (guest name pill / Settings link), Comments, Activity, presence, mode badge, Export, Share, theme, width switch, Comment | logo, title + round line, Comment, width switch, Share, avatar, More. More: theme, view switch (phone), Comments, Activity, presence, Share (phone), Export + Include comments, hosting mode, Settings, Sign out, guest name (the pill starts with the server-assigned random name) |
| Legacy board header | brand, identity, Comments, Activity, mode, Export, Share, theme; toolbar rounds, device switch, Comment | identity avatar, Share, More; toolbar unchanged. More carries the same controls |
| Footer | rounds, tally, Send/Build; send-bar summary/status | desktop: the same. Phone: Comment, round picker, `N of M done`, Send/Build |
| Decision block | Pick, Approve, Change, Skip, note | identical, with icons; the card and footer are restyled |
| Comments | pin popover/bottom sheet, resolve, reply, edit/delete, typing, unread | identical, restyled; focus returns to the pin |
| Share | invite (name, role, expiry), people and roles, clear override/remove, audience, guest role, expiry, copy, regenerate, stop, seats, round picker | identical, restyled and ordered invite → people → link |
| Export | Export menu: PNG, PDF, HTML, Markdown, Include comments | identical items inside More; exported HTML restyled |
| Home | flat review list, title + meta, Settings link | grouped list, search, status badge, round and age; same links |
| Settings | Members, Invites, Devices; role, invite and revoke, sign out others, sign out account | identical controls, side list / tab row, row + help + control |
| Pages | sign-in, confirm, dead/throttled/full | identical words and actions, restyled |

## Not drawn in the pack (extended in the same system)

Sign-in and the other server-rendered pages, the exported HTML, tablet (768–820), prototype and artifact rounds,
presence, unread and activity, toasts, empty and loading states, and the option tiles' Recommended badge. The content
inside a mock frame is the reviewed project's own UI and is never restyled. An artifact with `display: "page"` fills the
window and scrolls inside its frame; one with `display: "canvas"` runs the page's full length and scrolls with the board
between the same header and footer.

## Tests that hold the rules

- `tests/test_design_system.py`: the token values, the 12/13/14/16/20 scale, the two radii, the hit-size floors and
  the brand pin.
- `tests/test_ui_matrix.py`: every screen at 320–1920, dark and light, owner and guest: no horizontal scroll, text
  scale, hit sizes, WCAG AA contrast, menus and dialogs inside the viewport, keyboard focus and Escape.
- `tests/test_header_integration.py`: the header and More menu at 641–1279 for owner, commenter and viewer guests,
  with one, two and three device widths and a long guest name.
- `tests/test_tw_css.py`: the committed stylesheet is current.
