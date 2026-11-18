# Comments, threads and live updates

A comment on the board is a thread, not a note in a bundle. A pin on a block, an option, the bare board or a prototype
opens a conversation with replies, a resolved state and its own unread marker, and the board sees the change on every
open tab within a second. Nothing here needs a build step, a database server or a second process: threads live in the
data root's SQLite database and the live channel is one `EventSource` per page.

This page is the reference for the model, the HTTP surface and the stream protocol. Who may do what is in
[security.md](security.md); the board's own markup and options are in [decisions-format.md](decisions-format.md).

## What a thread is

A thread is one anchored conversation:

- **Anchor.** `{block, option, target, x, y}` with the optional `on: "board"` for a pin that sits on the board canvas
  rather than inside a mock, plus `w` (the device width it was placed on). `target` is the element anchor the board
  writes (a path, a quote, a signature and the pin's offset inside the element) and is what makes a pin follow its
  element. `x` and `y` are the fallback position inside the surface, 0 to 1.
- **Status.** `open` or `resolved`. A resolved thread is hidden on the board; replying to it is refused until it is
  reopened.
- **Comments.** Plain text, in order, each with the author's actor key and the display name at the time of posting.
- **Activation.** `activated` means someone with the `submit` capability mentioned `@Hermes` in it. This release
  stores it, publishes a `mention` event and returns it from `list_comments`; nothing in it wakes Hermes on the flag.

A thread is created on the round it was pinned in. Later rounds still list it (`carried: true`) so a review does not
lose a conversation when the board moves on; the server reports `detached: true` when the anchor's block or option is
not in that round, and the board draws the pin in the brand indigo at 55% opacity, at its last position, with "Element removed".

Deleting the last live comment deletes the thread and publishes `thread.deleted`. Deleting one of several comments
leaves a tombstone (`deleted_at` set, text replaced by "Comment deleted") so the rest of the conversation still reads.
When the author's account is gone the name reads "Former member".

## Text rules

- 2000 characters per comment, after trimming trailing whitespace. Newlines and tabs are kept; every other control
  character is stripped. Empty after trimming is a 400.
- Plain text only. The board renders it with `textContent` and then linkifies bare `http://` and `https://` URLs into
  anchors with `rel="noopener noreferrer"`; no other scheme becomes a link, and no text is ever parsed as HTML or
  markdown.
- Mentions are a list of targets in the POST (`"hermes"`, `"user:<id>"`, `"guest:<id>"`); the server stores and
  broadcasts them as `{kind, target}`. A `hermes` mention sets `activated` only when the author's role on the review
  carries `submit` (owner or reviewer), the author is not a guest, and the board was created with wake-up on; anyone
  else's `@Hermes` is a plain mention. A hand-typed `@Hermes` and the composer's Send to Hermes box produce the same
  `hermes` mention as the `@` picker, and one comment publishes one `mention` event. Send and pin fix are separate: a
  reviewer-role guest may do both, and both are stored for the owner without ever waking Hermes — a guest link never
  pings it, so tell it in chat.

## Limits

| Limit | Value | Answer |
|---|---|---|
| Comment length | 2000 characters | 400 |
| Open threads per review | 200 | 400 on create |
| Comments per thread | 40 | 400 |
| Anchor JSON | 4 kB | 400 |
| Comment writes per person per review | 20 per 60 s | 429 `{"ok": false, "error": "slow down"}` |
| Live streams per review | 25 in total, one kept for the owner (owner up to 5) | 429 `{"ok": false, "error": "too many live connections"}` |
| Replayed events kept per review | 500 | older cursors get `reset` |

The write bucket is in memory and per `(actor, review)`: a restart forgets it, and it never blocks a read.

## HTTP surface

Everything is under `/r/<review-id>/api/` and passes the same checks as Send: the Host allowlist, the Origin check,
`Sec-Fetch-Site`, and the CSRF token (`X-CSRF-Token` matching the `mr_csrf` cookie and the session's own token). Every
failure is JSON, never an HTML page, and every POST body is capped at 64 kB.

| Route | Method | Needs | Body | Answer |
|---|---|---|---|---|
| `threads` | GET | `read` | `?round=N` optional | `{ok, threads, counts, rev, activity, people, me}` |
| `threads` | POST | `comment` | `{anchor, text, mentions?, round?, idem?}` | 201 `{ok, thread}` |
| `threads/<tid>/reply` | POST | `comment` | `{text, mentions?, idem?}` | `{ok, thread}` |
| `threads/<tid>/status` | POST | `comment` | `{status: "open"\|"resolved"}` | `{ok, thread}` |
| `threads/<tid>/read` | POST | `read` | `{}` | `{ok}` |
| `comments/<cid>` | POST | `comment` | `{text}` | `{ok, thread, comment}` (own comment only) |
| `comments/<cid>/delete` | POST | `comment` | `{}` | `{ok, thread?}` (own comment only) |
| `typing` | POST | `comment` | `{thread}` | `{ok}` |
| `activity/read` | POST | `read` | `{}` | `{ok}` |
| `/r/<id>/events` | GET | `read` | — | `text/event-stream` |

`idem` is a client-chosen string (letters, digits, `_ . : -`): repeating a create or a reply with the same `idem` from
the same author returns the comment that already exists instead of posting a second one, which is what an offline or
double-clicked board needs. The board sends one per composer session.

A viewer gets 403 on every writing route. A guest posts with the share link's role; a guest viewer cannot post at all.
Commenting needs only the `comment` capability: a commenter can open the Comment toggle and post on any round, including
a round whose feedback was already sent. Sending feedback still needs `submit`, the current round and an unsubmitted (or
amending) board. A reviewer-role guest may Send and request a pin fix: both are stored for the owner and neither ever
wakes Hermes (tell it in chat).
Resolving and reopening are open to anyone who may comment: a comment is input, and which threads are done is a
judgement call its readers make.

### `state.json`

Watch `state.json` instead of the stream when a page only polls: it carries `threads: {"open": n, "rev": <last event
id>}`. It never carries comment text, an anchor or an author.

### Read scope

One effective read scope is computed per request and applied at the query layer of every thread surface. A viewer
guest whose share link picks a round reads exactly that round: `api/threads` (the default listing and `?round=`, which
is intersected with the pinned exact allowlist, never replaces it, so another round yields nothing), `activity`,
`counts`, `state.json`'s `threads` summary, and the `events` stream, both its `Last-Event-ID` replay and its live
frames. Members, the owner and a guest through a link left on `latest` read every round the board shows; `?round=`
still narrows their thread listing. A pinned round that no longer exists falls back to the full view, the same
fallback `state.json` uses. Hidden events still advance a restricted stream's cursor, so a reconnect after them
neither replays them nor stalls. Changing a link's round or guest role closes that review's open guest streams at
once; their `EventSource` reconnects and computes the new scope, while owner and member streams stay open. The same
holds for a request that was already in flight when the policy changed: if its subscription is admitted after the
change, it is refused rather than registered under the old scope, and the client reconnects. A signed-out device is
treated the same way: opening a stream re-reads the request's authorization, so a request that was authorized before
the sign-out cannot take a seat. A closed subscription writes nothing at all — not `hello`, not `reset`, not a replay
row, not a live frame that was already queued, and not a frame that had already reached the socket write.

## The live stream

`GET /r/<id>/events` answers `text/event-stream` and then blocks on that client's own queue. One thread per stream, so a
slow reader never holds another's.

Frames, in order:

1. `event: hello` — `{actor, me, rev, presence}`. `me` is the same object `state.json` carries.
2. Either the missed frames or `event: reset` — see replay below.
3. `event: presence` — `{people: [{actor, name, kind}], count}`.

Then one frame per change, plus presence and typing:

| Event | Id | Payload |
|---|---|---|
| `thread.created` | yes | `{thread, actor, name}` |
| `thread.replied` | yes | `{thread, actor, name, comment}` |
| `thread.status` | yes | `{thread, actor, name, status}` |
| `thread.deleted` | yes | `{thread_id, actor, name}` (after the last `comment.deleted`) |
| `comment.edited` | yes | `{thread, actor, name, comment}` |
| `comment.deleted` | yes | `{thread, actor, name, comment}` |
| `mention` | yes | `{thread, actor, name, targets}` |
| `review.reset` | yes | reserved for a round-level reset |
| `presence` | no | people and count; never replayed |
| `typing` | no | `{thread, actor, name, until}` |

Every `presence` and `typing` frame has no id on purpose: they are ephemeral, and a reconnecting client must not replay
a stale "is typing".

- **Replay.** Send `Last-Event-ID` and the stream replays every retained event with a higher id for that review, in
  order, before switching to live frames. A cursor older than the oldest retained event (500 per review) gets
  `event: reset` with `{rev}` and the client refetches `state.json` and `api/threads` instead of pretending it saw
  everything.
- **Heartbeat.** A comment line (`: ping <iso time>`) after `sse_heartbeat_seconds` (default 15) of silence, so a proxy
  in front of the board never drops an idle stream.
- **Slow consumer.** Each stream owns a queue of 200 frames. A publisher never waits for a socket: when a queue is
  full the stream is marked slow, closed, and the client's `EventSource` reconnects by itself and replays from
  `Last-Event-ID`. A dead subscriber costs a writer nothing.
- **Seats.** 25 streams per review in total, one of them kept for the owner (who may hold 5). The refusal happens before
  the response headers, so a client sees a plain 429 instead of an empty stream.
- **Sign-out and link changes cut the stream immediately.** Signing out other devices, regenerating or stopping a
  share link, or changing its round or guest role closes the matching streams at once instead of letting the socket
  live until its next write.
- **Admission is re-authorized, and closure is a socket fact.** Opening a stream re-reads the request's authorization
  from its own cookies twice — before the read scope and again at the last moment before registration — and compares
  a revocation revision snapshotted before the first read under the same lock that registers the seat. A request that
  was authorized before a sign-out, a regenerated or stopped link or a `members` switch therefore cannot become live:
  it gets no subscription, no seat and no presence lease, just a clean EOF, so `EventSource` reconnects. Closing a
  subscription fences its output: the closer sets the flag, shuts the socket down (which wakes a write parked on a
  stalled peer) and waits for the frame in flight, so nothing more is written — not `hello`, not `reset`, not a replay
  row, not a live frame that was already dequeued, and not a frame that had already reached the write path. Completion
  is linearizable: a closer that finds a close already in progress waits for that fence or finishes it itself, and a
  subscription closed but not yet fenced stays registered so a later restriction cannot skip it. A slow consumer is
  only *stopped* by the publisher; its own loop, or a restriction closer, performs the socket fence.

Anonymous gets 401 with `{"ok": false, "error": "sign in first", "signin": "/signin"}`; somebody without access gets the
same 404 JSON as any other missing review.

## Unread and activity

Unread is server-side and per principal (`user:<id>`, or `guest:<id>` for a guest, which is already per browser), never
a browser guess for a signed-in person. Opening a thread posts `threads/<tid>/read` and clears its dot; the activity
feed has its own cursor, moved by `activity/read`. More shows the open-thread and unread-activity counts.

## On the board

- The Comment toggle decides everything: markers, the composer and the pin list appear only in comment mode. It is
  there for anyone with the `comment` role, on every round and whether or not feedback was sent. Press Comment (or `C`),
  click anywhere on the board, and the composer opens at that point; saving posts the thread immediately (the pin is
  drawn locally first and removed again, with a toast, if the server refuses it).
- An anchor names the element under the pointer, on every surface: the board header and intro, a text block, a callout,
  a card, a table cell (header or body), a settled, recap or defaults item, a question, a decision option caption, mock
  content and an artifact frame. Hovering in comment mode outlines that element and shows its breadcrumb label. A
  drag-selected word becomes a `TextQuoteSelector` on the same element: the composer shows the word in a removable chip
  (`×` drops the quote and leaves a plain element pin), and the board paints it with the same yellow treatment as a mock
  (`CSS.highlights` where available, `<mark data-mr-highlight>` otherwise). Both kinds of anchor re-resolve after a
  reload; board anchors also follow a Desktop/Mobile switch, at their element or word rather than at the old
  coordinates. A mock pin stays with the width it was made at. A round
  stored without a section index (or whose block count changed after it was created) gets its block refs derived at
  render time: the block id, else `s<N>` in file order, with the reserved `page`, `else` and `doc` refs excluded, so
  nothing about the stored round changes. Controls stay live in comment mode: approve/change/skip, links, fields,
  native media and the pin markers themselves are never turned into a new pin.
- A pin opens its thread in a popover next to it. The header row is the first commenter's avatar, name and time on the
  left, and three ghost icon buttons on the right, on desktop and in the phone sheet alike: Resolve (a pressed, green
  check means the thread is resolved; the same slot becomes Reopen), More (Edit, Copy link and Delete for the root
  comment when you own it; Delete is last and in the danger colour, and the menu is Copy link alone on someone
  else's thread) and Close. The words live in each
  button's `title` and `aria-label`; there is no status chip. Every own comment carries a `⋯` menu on row hover or focus
  with Edit and Delete. The reply box keeps its own composer: Enter sends, Shift+Enter adds a newline, `@` completes
  people with access plus Hermes, and a live update patches the open thread in place, so an unsent reply keeps its
  text, caret and mentions.
- Copy link copies an absolute URL with `?thread=<id>`. Opening it starts the board and opens that thread's popover; the
  round hash keeps working as before, and a missing or foreign thread id is ignored.
- The composer under a new pin and under a thread reply carries a **Send to Hermes** box, checked every time it opens.
  Checked, it puts `@Hermes ` at the start of the text and posts a real `hermes` mention (the same mention the `@`
  picker inserts); unchecked, it removes that token and its mention and posts a plain comment. Typing or deleting
  `@Hermes` by hand keeps the box in sync, and accepting Hermes from the `@` picker checks it. The picker offers Hermes
  only to someone who may activate (the owner or a signed-in reviewer, on a wake-on board), and the box is hidden, with
  no mention added, for a guest as well as on a board created with wake-up off and for anyone whose role lacks
  `submit`.
- Prototype and artifact frames receive the review's server threads too: a pin made in another browser is drawn there
  and opens its thread on click, not a new empty composer.
- The **Comments** button opens the full list as an overlay: open threads first, a Resolved toggle, an unread dot per
  row, and a click jumps to the pin. **Activity** opens the feed ("Maya replied to thread 2 · 2m") and marks it read.
- More shows an avatar stack of who is here, with neutral initials per actor.
- Live updates patch the page in place: no reload on a new comment, and a "Live: reconnecting" chip appears only after
  two failed reconnects.

## What this release does not do

Nothing in this release wakes Hermes on `@Hermes`: a person's mention sets the thread's activated flag and
publishes the event; agents reach the same threads through the MCP comment tools (`list_comments`, `reply`, `resolve`);
when the board server runs for the same store those writes execute in the server, so an open board sees them live, and
their replies never activate a thread, so an agent cannot wake another agent. Mentions of people notify nobody yet.
There is no carriage of a thread into a new review, only into a later round of the same one.
