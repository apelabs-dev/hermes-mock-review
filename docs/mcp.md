# MCP server (stdio)

`mockreview/mcp.py` exposes the same review boards to coding agents that do not run inside Hermes. It is a Model Context
Protocol server over stdin/stdout: JSON-RPC 2.0, UTF-8, one object per line in each direction, no listening socket of
its own and no background thread. stdout carries protocol messages only; every log line and traceback goes to stderr.
Read until EOF, then exit 0.

Both streams are pinned to UTF-8 inside the server, so a Windows console code page (cp1252 and friends) cannot kill the
session or mangle a title. The only subprocess is the here.now publish helper a `mode=herenow` round runs, exactly as
the CLI runs it.

## Run it

From a checkout or an installed plugin directory:

    <abs>/bin/mockreview mcp

Or, with the package importable (`PYTHONPATH` or the repository root as the working directory):

    python -m mockreview.mcp

Inside Hermes, `hermes mockreview mcp` runs the same code. Prefer the `bin/mockreview` or `python -m` form for an MCP
client: it keeps stdout clean even when the Hermes CLI prints its own banner.

Server options may also come from the environment: `--platform` / `MOCKREVIEW_PLATFORM`, `--chat-id` /
`MOCKREVIEW_CHAT_ID`, `--thread` / `MOCKREVIEW_THREAD`, and `--wake`. Wake is off unless `--wake` is passed or a call
sets `wake: true`.

## Tools

The server lists exactly five tools. Each has a JSON Schema whose top-level type is `object`, with `properties` and
`required`. Every tool returns one text content block holding a JSON string; a failure sets `isError: true` and the JSON
holds an `error` message. The same `mockreview.api` functions run behind these tools, the CLI and the Hermes plugin
tools, so validation, storage and publishing behave identically.

`mock_review_create` creates a board and its first round. `title` is required. `brief` is the reviewer-facing context.
`decisions` is an absolute path to a `decisions.json` round, `prototype` an absolute path to a clickable
`index.html` round, and `design` a saved design profile name or an absolute profile directory. `mode` is one of
`tailnet`, `tailscale`, `local`, `lan` or `herenow` (the aliases resolve like the CLI's). `allow_live_demo` permits a
prototype round for this call when the `offer_live_demo` setting is off. `password` controls the here.now password gate.
The per-review look keys (`devices`, `default_device`, `default_theme`, `show_theme_toggle`, `pins_enabled`,
`require_note_on_change`, `max_pins`, `mobile_width`, `tablet_width`) come from the same settings table as the plugin
and are stored in the review. The result carries `review_id`, `url`, `mode` and `round`, and `wake: false` for a board
nobody is woken by.

`mock_review_add_round` adds the next round to an open review and returns the same link. `review_id` is required; it
takes `decisions`, `prototype`, `design`, `allow_live_demo` and the look keys.

`mock_review_feedback` reads a round's marks, pins and notes plus a markdown rendering. `review_id` is required and
`round` is optional (default: the current round). Reading the newest round records that the agent has the feedback, so
the stalled ping, if any, starts from that read.

`mock_review_ack` marks a round's feedback as handled, so no stalled ping is sent for it. `review_id` is required and
`round` is optional. It fails when the round has no submitted feedback.

`mock_review_list` lists reviews. Open reviews only, unless `include_closed` is true.

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
`herenow` on the internet with a password gate by default. An MCP client can create a `local` board and read its marks
without any Hermes process, but the board server still has to run for the link to load (`hermes mockreview serve`, or
the gateway with autostart on). Use `herenow` (password protected) or `tailnet` when the reviewer is on a phone; a
`local` link with the default bind address opens on this computer only.

## Security

A stdio MCP server has no listening port and no token of its own. Any local process the user starts can run it, with
that process's permissions, and it reaches the same review store and settings as the CLI. Reviews stay protected by
their unguessable ids and by the hosting mode's network boundary; here.now reviews stay password gated by default.
Waking requires an explicit origin, and no tool ever returns a secret. Read [security.md](security.md) for the full
model.

## Troubleshooting

If the tools never appear in the client, run the exact command by hand in a terminal and watch stderr. The server logs
its `initialize` line there (requested protocol version and `clientInfo`) while stdout shows protocol JSON only. A
client that ignores a server usually failed to start it, or the command exits before the first request.

If stdout noise appears, something printed outside the protocol. The only allowed stdout writer is the response line;
diagnostics belong on stderr. Check that the client runs `<abs>/bin/mockreview mcp` or `python -m mockreview.mcp`
rather than a wrapper that prints a banner.

If the server starts but imports fail, the client is using the wrong Python. Point `command` at an interpreter that can
import the `mockreview` package (Python 3.9 or newer), or run the `bin/mockreview` script, which adds its checkout to the
import path itself.
