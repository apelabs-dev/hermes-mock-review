# MCP server (stdio)

`mockreview/mcp.py` exposes the same review boards to coding agents that do not run inside Hermes. It is a Model Context
Protocol server over stdin/stdout: JSON-RPC 2.0, UTF-8, one object per line in each direction, no listening socket of
its own and no background thread. stdout carries protocol messages only; every log line and traceback goes to stderr.
Read until EOF, then exit 0.

Both streams are pinned to UTF-8 inside the server, so a Windows console code page (cp1252 and friends) cannot kill the
session or mangle a title. It starts no subprocess.

## Run it

From a checkout or an installed plugin directory:

    <abs>/bin/mockreview mcp

Or, with the package importable (`PYTHONPATH` or the repository root as the working directory):

    python -m mockreview.mcp

Inside Hermes, `hermes mockreview mcp` runs the same code. Prefer the `bin/mockreview` or `python -m` form for an MCP
client: it keeps stdout clean even when the Hermes CLI prints its own banner.

Server options may also come from the environment: `--platform` / `MOCKREVIEW_PLATFORM`, `--chat-id` /
`MOCKREVIEW_CHAT_ID`, `--thread` / `MOCKREVIEW_THREAD`, `--token` / `MOCKREVIEW_TOKEN`, and `--wake`. Wake is off
unless `--wake` is passed or a call sets `wake: true`.

`--token` is a personal access token (`mockreview tokens create --user <id> <name>`): the three comment tools act as
that account. The board tools never need it, so an agent that only builds boards starts with no token at all. An MCP
client's server configuration can carry it too, e.g. `{"command": "<abs>/bin/mockreview", "args": ["mcp"],
"env": {"MOCKREVIEW_TOKEN": "<token>"}}`; the token is stored hashed, is read fresh on every call, and a revoked token
stops working at once.

## Tools

The server lists exactly nine tools, six for boards and three for their comment threads. Each has a JSON Schema whose
top-level type is `object`, with `properties` and `required`. Every tool returns one text content block holding a JSON
string; a failure sets `isError: true` and the JSON holds an `error` message. The same `mockreview.api` and
`mockreview.threads` functions run behind these tools, the CLI, the REST API and the Hermes plugin tools, so validation
and storage behave identically.

### Board tools

`mock_review_create` creates a board and its first round. `title` is required. `brief` is the reviewer-facing context.
`decisions` is an absolute path to a `decisions.json` round, `prototype` an absolute path to a clickable
`index.html` round, and `design` a saved design profile name or an absolute profile directory. `project` is the absolute
path of the repo the board is for: the profile bound to it applies on its own (there is no default, so pass it). With saved
profiles, a create that has no `design`, no bound project and no `no_design: true` fails; `off_brand` (a reason, up to 200
characters) skips the check of the mocks' colours and fonts against the profile (see "Brand enforcement" in
[decisions-format.md](decisions-format.md)). `mode` is one of `tailnet`, `local` or `public` (`tailnet` is stored as
`tailscale`; `public` names a public URL behind Tailscale Funnel or any TLS proxy).
The CLI also accepts the `tailscale` and `lan` aliases. `allow_live_demo` permits a prototype round for this call when
the `offer_live_demo` setting is off. The per-review look keys (`devices`, `default_device`, `default_theme`,
`show_theme_toggle`, `pins_enabled`,
`require_note_on_change`, `max_pins`, `hide_resolved_pins`, `mobile_width`, `tablet_width`) come from the same settings table as the plugin
and are stored in the review. The result carries `review_id`, `url`, `mode` and `round`, and `wake: false` for a board
nobody is woken by.

`mock_review_add_round` adds the next round to an open review and returns the same link. `review_id` is required; it
takes `decisions`, `prototype`, `design`, `project`, `no_design`, `off_brand`, `allow_live_demo` and the look keys.

`mock_review_feedback` reads a round's marks, pins and notes plus a markdown rendering. `review_id` is required and
`round` is optional (default: the current round). Reading the newest round records that the agent has the feedback, so
the stalled ping, if any, starts from that read. The result carries `action` (`send`, or `build` after Build).
While a decisions round that ended in a plain Send is the newest round of an open review, it carries `next_round`:
ask only what is still open, and list what was approved or answered in a `settled` block. `mock_review_add_round`
refuses (409) a round that brings back, as markable and without `revised`, a block the last round approved or a
question it answered; the error names the ids. Pass
`allow_reask: true` to ask them again on purpose. A markable `recap` is exempt, and so is the result round after Build.

While a build round is the newest round of an open review, it also carries `next_step`: build now and post the result
with `mock_review_add_round`.

A decisions/prototype pin may include an optional `target` (older pins omit it):

```json
{"block":"summary","option":null,"w":"desktop","x":0.4,"y":0.2,"text":"Shorten this","on":"board",
 "target":{"path":"[data-mr-part=\"order-summary\"]","id":"mr-3","part":"order-summary",
 "x":0.5,"y":0.2,"pin":{"x":0,"y":0},"sig":{"tag":"section","h":"abc123"},
 "label":"Order summary","doc":{"x":400,"y":200},
 "quote":{"type":"TextQuoteSelector","exact":"Shipping","prefix":"","suffix":""},
 "position":{"type":"TextPositionSelector","start":0,"end":8}}}
```

A board pin sets `"on": "board"` and names `block` with a section ref from the round's `sections`, or a reserved board
ref: `page` (the header), `else` ("Anything else") or `doc` (the bare board canvas); its `option` is always `null`. A
mock pin omits `on` and keeps `option` as before (`null`
for a context mock). Outer `x/y` remain compatible mock/page fractions. Target `x/y` are element fractions, `pin` is a fractional offset,
and `doc` is the last document point (finite, bounded to 1,000,000 px); a board pin without a target carries that same
`doc` shape on the pin itself. Strings are plain text: label 120, part 40,
id 80, selector path 300, exact quote 160, quote context 32 characters each. The path accepts finder type/id/class/
attribute/child selectors and numeric nth-child/of-type; malformed or unsupported selectors are dropped. Finite geometry
is clamped to [0,1]. Markdown includes part, label, path and quote under the existing untrusted-feedback banner; selector
and quote backticks are removed. Stored older drafts and pins without targets need no migration.

`mock_review_ack` marks a round's feedback as handled, so no stalled ping is sent for it. `review_id` is required and
`round` is optional. It fails when the round has no submitted feedback.

`mock_review_patch_block` patches one block's HTML in the newest round in place. It is the supported answer to a pin
fix (the feedback's `pin_requests` and `next_step` name it) and replaces "a fix is a new round" for that one block.
`review_id`, `block` (a mock, decision, piece or artifact id), `html`, `round` and `if_rev` are required; `round` must
still be the newest round and `if_rev` the block's current revision (either being stale is refused and nothing is
written, so read the round again and merge), `option` names the option of a decision block (required when the block has
options) and `request_ids` are the pin fix ids the patch closes. Only HTML changes:
a block's id, type, title and option ids stay, and the replaced content is kept under the round's `history/`.
Everything else is still a new round with `mock_review_add_round`.

`mock_review_list` lists reviews. Open reviews only, unless `include_closed` is true.

### Comment tools

These three act as the account behind `--token` / `MOCKREVIEW_TOKEN`, whose role on the review decides what they may
do. Without a token every call fails with an `isError` result that says how to create one; a token that is unknown or
revoked is refused the same way. A conversation started on the board is the same data the agent reads here, and every
write is audited under the token's account.

The write path depends on whether a board server is running for the same store. When one is, it leaves a descriptor
(`.server.json`, no token in it) in the data root; the MCP process ignores a descriptor that is a symlink, readable by
group or others, or not written by its own account, and then makes the server prove it is that process: it sends a
nonce and expects `HMAC-SHA256(secret, nonce)` back at `GET /api/owner/challenge`, together with the descriptor's pid
and store. Only then does it POST the reply or resolve back to it over loopback with the caller's personal access
token as a bearer credential, and the server accepts a bearer on those two routes only. A listener that merely bound
the port named in a stale descriptor — the one a crashed server left behind, for instance — cannot answer the
challenge, so it never receives the token. The server process does the database write and the live publish under its
own publish lock, so an already-open board receives the `thread.replied` / `thread.status` frame immediately, exactly
as if the person had used the board. With no server reachable — or a stale descriptor — the tool writes through
`mockreview.threads` directly: the reply is persisted and appears on the next board load; a board that is open at that
moment does not get the frame. If the HTTP request fails before any answer, the tool falls back to that direct write
with the same idempotency key, so a lost response cannot insert the reply twice; an error answer from the server is
reported as the tool error and never retried. Both `reply` and `resolve` return `delivery`: `"live"` when the running
board took the write (and so its open streams have it), `"stored"` when it was only persisted for the next load. A
stored write is never presented as live delivery.

`list_comments` takes `review_id` and an optional `status`: `open` (default, the same default the board shows),
`resolved`, or `all`. It returns `review_id`, `as` (the token's user, name and role), `counts` (`open` and `resolved`),
`rev` (the review's event cursor) and `threads` — each with its id, round, status, anchor, `detached` flag, comments
(id, author, author name, role, text, timestamps, mentions, `mine`, `sent_to_hermes`) and the thread's `unread` flag.

`reply` takes `thread_id` and `text`: plain text like the board's composer (control characters stripped, newlines kept,
2000 characters, URLs auto-linked by the board). A reviewer, a commenter or the owner may reply; a viewer is refused.
The reply appears under that user's name and fires the same `thread.replied` event a person's reply does. The result
carries the thread and the exact `comment` this call inserted (on an idempotent retry, the comment the same actor
already inserted). Replying to a resolved thread is refused until `resolve` reopens it. An existing thread the token's
user cannot access and a thread that does not exist answer with the same error, so the tools are no existence oracle. An optional `mentions` list takes structured ids
(`user:<id>`, `guest:<id>`); a `hermes` mention is dropped, and `@Hermes` in the text is plain text: an agent's reply
never activates the thread or wakes Hermes, so two agents cannot wake each other in a loop.

`resolve` takes `thread_id` and an optional `reopen` (default false). Only a reviewer or the owner may resolve or reopen
— a commenter may reply but not settle a thread, which is the narrower rule this agent-facing surface uses (the board
offers its own Resolve button to anyone who may comment). The board server enforces the same rule on the status route a
bearer uses, so a commenter's token cannot settle a thread by calling the HTTP route directly. Resolving hides the
thread on the board and fires `thread.status`; a resolved thread takes no replies until it is reopened.

## Origin and wake

An external CLI has no Hermes session, so the server never guesses a destination. `origin_platform`,
`origin_chat_id` and `origin_thread_id` on a `mock_review_create` call override the server's `--platform`,
`--chat-id` and `--thread` options. An origin exists only when both the platform and the chat ID are non-empty; the
thread ID is added only when it is non-empty. Without that pair the board is created with no origin.

`wake` resolves from the call's boolean `wake` argument, else from the server's `--wake`. When it resolves true and
either the platform or the chat ID is missing, the call fails with an `isError` result and the same wording the Hermes
tool uses: explicit `origin_platform` and `origin_chat_id` are required, and a destination must never be guessed.
When wake resolves false, the board is created with `wake=False`: Send stores the feedback and notifies nobody. The
agent reads it with `mock_review_feedback`.

### Wake without Hermes

Wake stays off by default. `wake: true` (or `--wake`) needs an explicit origin and a receiver.

Signing happens in the **board server**: set `MOCKREVIEW_WEBHOOK_SECRET` and `MOCKREVIEW_WEBHOOK_URL` in the environment
of the process that runs `mockreview serve`. The preflight that decides whether a board may have `wake` runs wherever the
call resolves its wake route: normally that is the server's reported status, but with `MOCKREVIEW_OWNER=self`, or `auto`
with no reachable server, it is a local check in the calling process, so that process needs the secret too.

The receiver contract:

- URL: `POST <webhook_url dir>/<webhook_route>-<platform>`. The defaults are `http://localhost:8644/webhooks/mock-review`
  and the route `mock-review`, so the wake for a Claude Code session goes to
  `http://localhost:8644/webhooks/mock-review-claude-code`; a receiver of your own can live anywhere, e.g.
  `http://127.0.0.1:8791/webhooks/mock-review-claude-code`.
- Signature: `X-Webhook-Signature-V2` = `hex(HMAC-SHA256(secret, "<X-Webhook-Timestamp>." + raw_body))`, timestamp in
  unix seconds, reject older than 300 s, compare in constant time.
- Answer `202`, or `200` with `{"status":"accepted"|"coalesced"|"duplicate"}`. Any other status, including `204`, is
  recorded as a failed delivery and retried every `retry_seconds`. Dedupe on `X-Request-ID` yourself; Hermes's 1 h
  filter is not involved.
- Two extra POSTs can arrive on `<route>-<platform>-notice` (the immediate ack and the stalled ping). They carry counts,
  never notes; answer `200 {"status":"delivered"}` or `404` if you do not want them.
- With no session to answer, set `stale_after_minutes` and `stale_after_read_minutes` to `0`.
- mock-review ships no receiver daemon and never runs a command from a board event. The receiver is the user's.

## Protocol era

The server implements the legacy `initialize` handshake. A dual-era client that first probes `server/discover` gets the
normal unknown-method error, `-32601`, which is the signal to fall back to `initialize`. A client that only speaks the
newer per-request era will not work here; capture its requested version and the error before changing anything.

Supported protocol versions are `2025-11-25`, `2025-06-18`, `2025-03-26` and `2024-11-05`. A requested version from
that list is echoed; anything else answers `2025-11-25`. Notifications (requests without an `id`) are never answered.
A malformed JSON line returns `-32700` and the server keeps reading; an unknown method returns `-32601`; a non-object
request or a request without a method returns `-32600`; bad parameters return `-32602`.

## Hosting

The board is served exactly as it is for Hermes: `local` on this computer, `tailnet` through `tailscale serve`, or
`public` behind Tailscale Funnel or any TLS proxy (see [public-hosting.md](public-hosting.md)). An MCP client can create
a `local` board and read its marks without any Hermes process, but the board server still has to run for the link to
load (`hermes mockreview serve`, or the gateway with autostart on). Use `tailnet` when the reviewer is on a phone; a
`local` link with the default bind address opens on this computer only.

## Security

A stdio MCP server has no listening port. Any local process the user starts can run it, with that process's
permissions, and it reaches the same review store and settings as the CLI. Reviews stay protected by their unguessable
ids and by the hosting mode's network boundary. Waking requires an explicit origin, and no tool ever returns a secret.

The comment tools are the exception to "no identity": they act as the user of the access token the server was started
with, so a leaked token is a capability someone else can use from any locally started server. The token is stored
hashed, carries its user's role and nothing more, and can be revoked at any time (`mockreview tokens revoke --user
<user> <token-id>`); each reply and resolve writes an audit row under that account. An agent's `@Hermes` never wakes
Hermes. Read [security.md](security.md) for the full model.

## Troubleshooting

If the tools never appear in the client, run the exact command by hand in a terminal and watch stderr. The server logs
its `initialize` line there (requested protocol version and `clientInfo`) while stdout shows protocol JSON only. A
client that ignores a server usually failed to start it, or the command exits before the first request.

If stdout noise appears, something printed outside the protocol. The only allowed stdout writer is the response line;
diagnostics belong on stderr. Check that the client runs `<abs>/bin/mockreview mcp` or `python -m mockreview.mcp`
rather than a wrapper that prints a banner.

If the server starts but imports fail, the client is using the wrong Python. Point `command` at an interpreter that can
import the `mockreview` package (Python 3.11-3.14), or run the `bin/mockreview` script, which adds its checkout to the
import path itself.
