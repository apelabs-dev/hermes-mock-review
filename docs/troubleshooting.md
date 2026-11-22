# Troubleshooting

Start with two commands. `hermes mockreview settings` prints every setting and where its value came from, with a
warning under any value it had to ignore. `hermes mockreview owner` reports whether a board server is running, where it
keeps reviews, and whether a new review would pass the wake-route check. It sends nothing.

## Setup

Setup prints "not a terminal: re-run with --yes" and exits 2. It only asks questions on a terminal. In a script, pass
`--yes`, plus `--hosting` and `--origin-platform <name>` (repeatable) so it does not need to ask. Nothing was changed.

Setup says `hermes --version did not answer within 120 s`, or `hermes <command> did not answer`. On the first run in a Hermes
home, Hermes checks for updates before it prints its version, which can be slow. Nothing was changed by the version check,
so re-run setup; a later timeout names the command (never its arguments) and setup is safe to repeat. `cannot run hermes`
means the `hermes` binary was not found: install Hermes or set `MOCKREVIEW_HERMES_BIN`.

Setup says `no messaging platform found in the Hermes config` and sets up Hermes Desktop chats only. Setup reads
`platforms.*` for the chats you review in, and skips the ones that are not messaging chats (`webhook`, `msgraph_webhook`,
`api_server`, `a2a`, `raft`, and `local`, which is always set up as Desktop chats). To be woken in a messaging chat too,
configure it in Hermes and run setup again, or name it: `hermes mockreview setup --origin-platform telegram` (repeatable).

Setup sets up every messaging platform it finds, plus Desktop chats. Under `--yes` or `--dry-run` it takes them all and
says so. On a terminal with several, it lists them and asks once; Enter means all. `--origin-platform <name>`
(repeatable) narrows the list to exactly what you name; add `--origin-platform local` to keep Desktop chats. Setup stops
before changing anything when you name a platform that is not a chat (`origin platform webhook cannot be woken: ...`).

Setup asks whether Desktop chats may be woken. Hermes lets a plugin queue a turn into a session only with
`plugins.entries.mock-review.allow_gateway_injection: true`, so setup asks before it sets that and needs an explicit `y`
(Enter means no). `--yes` never sets it: the summary shows `desktop wake skipped (needs your yes)` and setup prints the
command to run. `--dry-run` only prints the command. If you said no, the summary shows `desktop wake skipped (declined)`;
grant it later with `hermes config set plugins.entries.mock-review.allow_gateway_injection true`. Setup only sets the key
through `hermes config set`. It never opens a Hermes file. With `--no-autostart` it warns that Desktop chats cannot be
woken, because only the board server inside the gateway can wake them.

The test review ends as `pending (board not running)`. The review was saved, and no link is printed because no board of
this Hermes home answered. It loads by itself once the board runs. The `next:` lines at the end of the setup output say
how to start it for your setup: restart or start the gateway (`hermes gateway restart`, `hermes gateway run`,
`hermes gateway start`), or run `hermes mockreview serve`. Run `hermes mockreview setup` again for a link you can open: it
closes the earlier test review and creates a new one. It is safe to repeat.

Setup says `board: already running on http://127.0.0.1:<port> (this plugin's board, pid N)`. That is not a problem: the
port is held by this home's own board, so setup does not warn. If it warns that the port is held by a mock-review board
for another Hermes home, another profile or install runs a board on that port. Give this home its own with
`--port <n>`. A plain `port <n> is busy` warning means some other program uses the port.

Setup lists a self-delivering route such as `mock-review-webhook` and offers to remove it. An earlier setup took the
webhook ingress for a chat and created a route that delivers to `webhook` itself, so wake-ups never reached a chat.
Answer yes on a terminal and setup runs `hermes webhook remove <name>` for it. With `--yes` or `--dry-run` it only prints
the command; run it yourself.

Setup says the `webhook_url` port differs from the webhook platform's port. `webhook_url` was set explicitly, to a port
the Hermes webhook platform does not listen on (`platforms.webhook.extra.port`), so wake-ups would miss it. Set
`webhook_url` to the printed value. When `webhook_url` was never set, setup saves it for you.

Setup says the webhook platform is disabled, or no route was created. Hermes only creates routes while the webhook
platform is on. Accept the prompt to enable it, or run `hermes config set platforms.webhook.enabled true`, then run setup
again.

Setup says Hermes is too old. The plugin needs Hermes 0.22.0 or newer. Check with `hermes --version`.

`hermes plugins validate` fails inside setup. Setup validates the installed plugin folder. If you pointed it at a
development checkout with `--plugin-dir`, validate the folder you actually install, or use `--skip-validate`.

## Creating reviews

`no wake route: Send would store feedback but never wake Hermes`. The signing secret is missing or the route does not
exist. Inside Hermes, run `hermes mockreview setup`; outside Hermes, set `MOCKREVIEW_WEBHOOK_SECRET` and point
`MOCKREVIEW_WEBHOOK_URL` at a receiver (see [mcp.md](mcp.md#wake-without-hermes)). For a demo board that never wakes
anyone, pass `--no-wake` (or set `wake_required` to false).

A create call says `this process's store ... is not the one the board server watches`, or `this process's store ... is
private to this profile`. The process that made the call writes to a different folder than the one the running board
server serves, so the board could never load or wake anyone. Run `hermes mockreview owner` to see both folders. Run the
server for the same Hermes home, or set `owner` to `service` to create in the server's store.

A create call says `Start the board server`. No server is running for a store that needs one. Run
`hermes mockreview serve`, or turn on `autostart` and restart the gateway, then retry.

`Wake is enabled, but the calling chat could not be read from this session (...)`. The create tool reads the chat to
wake from the calling session's own context and never guesses one. The reason in brackets says what was missing: no
chat at all (a CLI or cron caller), or a session in a process of its own (a terminal UI or a standalone `hermes
dashboard`), which the board server in the gateway cannot reach. Create the board from the chat that should be woken,
pass `origin_platform` and `origin_chat_id` exactly as your session context gives them, or pass `wake=false` for a demo.

`this chat cannot be woken: origin platform '<name>' is not a chat mock-review can wake (...)`. The origin is a Hermes
surface that is not a chat (`api_server`, `webhook`, `msgraph_webhook`, `a2a`, `raft`), or a platform this Hermes does
not deliver webhook routes to (is its platform plugin installed and enabled?). Nothing was written. See
[which chats can be woken](hosting.md#which-chats-can-be-woken).

`cannot wake this Desktop chat: ...`. A board made in a Hermes Desktop chat is woken by the board server inside the
gateway. The reason says what is missing:

- `allow_gateway_injection is off`: run `hermes mockreview setup` and accept the Desktop prompt, or run `hermes config
  set plugins.entries.mock-review.allow_gateway_injection true`. Hermes reads it on the next wake; no restart is needed.
- `the board server is not running inside the Hermes gateway`: turn on `autostart` with the webhook platform enabled,
  and restart the gateway. A board server run with `hermes mockreview serve` cannot wake a Desktop chat.
- `the board server predates Desktop wakes`: the running board server is older than the plugin. Restart the gateway.

`a Desktop (local) origin is woken inside its own session and needs that session's key`. A `local` origin was passed by
hand (the CLI's `--platform local`, or `origin_platform` for another chat). Only the Desktop chat itself has its key:
create the board from that chat without `origin_*`.

`refusing a board that wakes a Desktop chat: ...`. A Desktop chat is woken inside its own session with every tool it
has, so its board must keep Send gated: server on loopback, no `public_url`, not a `public` board. Keep it local, or pass
`wake=false`. See [hosting](hosting.md#desktop-chats).

`A prototype ("try it live") round is turned off`. Prototype rounds need `offer_live_demo` set to true, or
`--allow-live-demo` on the call.

`decisions.json: ...` or `blocks[n]: ...`. The file failed validation. The message names the block. See
[decisions-format.md](decisions-format.md).

A create or add-round is refused as off-brand. A design profile is in effect (given, inherited by the round, or bound to
the project) and the mocks use a colour or font it does not allow. Nothing was written. The message starts
`Off-brand mock: N value(s) outside the design profile <name>. Nothing was written.` and lists each value with its exact
location, once each:

    blocks[3].options[b].html: color #ff6b00 is not in profile acme
    blocks[1].html: font-family "Comic Sans MS" is not in profile acme

Fix the mock to use the profile's tokens (`var(--accent)`, `var(--text)`, `var(--surface)`, `var(--font)`, ...), add the
value to the profile's `mock.css` or `fonts.css`, or, when the round is off-brand on purpose, pass
`--off-brand "<reason>"` (tools and MCP: `off_brand`). That is the only bypass: the reason (1 to 200 characters) is
recorded on the round and on the review. See
[decisions-format.md](decisions-format.md#brand-enforcement).

## Server and links

`mock-review: http://127.0.0.1:8790 is already in use`. Another board server holds the port. It is often the one inside
the gateway. Run `hermes mockreview owner`. If you want a different port, set `port` (or `setup --port <n>`) and restart.
`hermes mockreview serve` exits with code 2 in this case.

The board does not start inside the gateway. `autostart` must be true (on Windows the board runs with
`hermes mockreview serve`), and the gateway has to be restarted after any change to `autostart`, `host` or `port`. The gateway log says why it did not start, in lines beginning
`mock-review autostart`. Kanban workers never start a board.

A link works on this computer but not on a phone or another device. With the default `host` of `127.0.0.1` the server is
reachable from this computer only. Use tailnet hosting, or set `host` to `0.0.0.0` on a network you trust (see
[hosting.md](hosting.md)). Restart afterwards.

The browser shows `421 Misdirected Request`. The address in the browser is not one the server expects. Hosts come from
`public_base`, `local_base`, `lan_address` and the bind address. Set the one that matches the address you use, then
restart the server.

Send fails with `403 ... submit token is not valid`. The page is from a different review or an old copy. Reload the link.
`409 ... closed` means the review was closed, so it no longer takes feedback.

## Waking the agent

The board says the feedback was sent, but nothing arrives in the chat. Check the pieces in order:

1. `hermes webhook list` shows a route named `<webhook_route>-<platform>` for the platform the review came from.
2. The gateway is running, with the webhook platform on.
3. `hermes mockreview notify-test <review id> --to http://localhost:8644/webhooks/<route>` sends a signed test request and
   prints the gateway's answer. `HTTP 202` means Hermes accepted it. `ignored` means the route filtered the event out.
4. If the secret was rotated, run `hermes mockreview setup` again so the routes use the new one.

Undelivered wake-ups are retried every `retry_seconds` while the board server runs.

A Desktop chat has no route; the stored submission says what happened. `queued in the Desktop chat` means Hermes took
the turn (it runs after the chat's current turn, if one is running). `Hermes refused the wake: the Desktop chat is not
live in this profile's gateway` means the chat was closed, or another profile serves it: the wake is retried and goes
through once the chat is open again. `allow_gateway_injection is off` means the consent was taken back: see "Creating
reviews" above. A Desktop chat gets no ack or stalled ping (both are stored as `skipped`): the wake shows as a turn in
the chat itself.

`cannot wake: this chat cannot be woken: ...` in a stored submission means the board's chat stopped being reachable after
the board was made, for example because its platform plugin was disabled. Nothing is posted. Turn the platform back on;
a mention wake that failed this way is not retried.

### The woken session says the build continues in the thread

Hermes runs the session Send wakes with its safe webhook tools only, so by default it cannot write files or call the
mock_review tools. It posts one line, "Round N feedback is saved; the build continues in this thread's chat session.", and
the origin chat's own session builds the round (ask in that chat if it has not started). To let the woken session build
by itself, set `wake_toolsets` (for example `hermes config set plugins.entries.mock-review.settings.wake_toolsets
"mock-review,terminal,file"`; include `mock-review` so it can call the mock_review tools) and run `hermes mockreview setup` again. Setup
prints the `"toolsets": [...]` line and the route to add it to in `webhook_subscriptions.json`; add it by hand (the
summary shows `wake toolsets manual`). The gateway reads the route file again on its next request. Every setup run
re-subscribes the routes and drops the key, so add the line again after each run. Setup refuses while a waking board
would be ungated (`host` beyond loopback): see [security](security.md#tools-for-the-woken-session-wake_toolsets).

## Sent but nothing happened

Send does three things, so a chat that stays silent is a real signal:

1. The wake-up is posted, so the woken session builds the next round.
2. Right after the gateway accepts it, an ack is posted to the origin chat (and mirrored into that chat's session):
   "Got your feedback on `<title>`, round N ... Hermes is working on round N+1".
3. If no new round appears within `stale_after_minutes` (default 15), a ping posts to the same chat saying the agent may
   have stalled. Once the session has read the feedback (`mock_review_feedback`), the ping waits
   `stale_after_read_minutes` (default 60) from that read instead and says Hermes read it and posted no round. After
   Build both waits are multiplied by `build_stale_factor` (default 4), and the ack says the build started.

Check them in order:

- No wake-up, no ack. The ack is only sent once the wake-up was accepted, so start with "Waking the agent" above. When the
  wake-up lands on a retry, the ack follows it.
- The ack and the ping use the route `<webhook_route>-<platform>-notice` (for example `mock-review-telegram-notice`).
  `hermes webhook list` should show it next to the wake route. If it is missing, the server log has one warning
  (`the gateway has no notice route ... run hermes mockreview setup`) and the board's stored submission shows the notice
  as `no_route`: run `hermes mockreview setup` again. It re-subscribes both routes and reuses the secret.
- The notice route is `--deliver-only`, so it needs a connected adapter for the origin platform. Without one the gateway
  answers `502 Delivery failed` and the board server retries every `retry_seconds` (the ack is dropped once a newer round
  exists or the review is closed).
- The ping only fires while the board server runs, for open reviews with wake on, once per submission, and only if no
  newer round exists. `stale_after_minutes` 0 turns it off. The board shows the same hint.
- No ping is expected (the stored submission's `stalled_notice` shows `skipped`) when the round asks for no more work:
  every decision Approved or Skipped, every question on its recommendation, with no pins, no general notes and no
  Build. A typed answer that overrides a recommendation means the plan changes, so it keeps the ping. Close
  the review when you are done.
- The session can mark a submission as handled with `mockreview ack <review id> [--round N]` (tool `mock_review_ack`),
  for example when it answered in chat: no ping for that submission. An amend is a new submission with its own clock.
- Closing a review, by hand or by `auto_close_idle_days`, drops any ping not yet sent (`dropped`, reason
  `review closed`). A closed review never pings.
- A notice that can never be delivered goes `dropped` with one warning in the server log and is not retried: a review
  whose origin has no chat id (created before chat ids were required), or a gateway answer of 400, 413 or 422. 401, 403,
  429 and 5xx answers are retried every `retry_seconds`.

### add-round says the round asks again what was already settled

The round brings back a block the last round approved, or a question it answered, with the same id, still markable and
without `revised`. Only blocks marked Change, skipped or left open come back; move the settled ones into a `settled`
block, or set `revised` on a block that really changed to a short string label such as `Revised`, not a flag.
`--allow-reask` (`allow_reask: true` for the tools) asks them again on purpose.

### A round's files were edited after it was added

The board checks marks and Build against the block list stored in `review.json` when the round was added (`decisions`,
`mocks`, `preset`, `gate`). Editing `rounds/<n>/a/decisions.json` afterwards leaves that record stale, so Build can be
checked against blocks that are no longer on the page. Add a new round instead.

### Build pressed but nothing came back

Build is a Send with `action: "build"`, so the three steps above apply: check the wake route first with
`hermes mockreview owner` (it reports whether the board server runs and whether a create would pass the wake-route
check, and sends nothing), then the ack, then the ping. The ack reads "Got it — building round N's plan now".

The build gets longer clock than a feedback round: the stalled ping waits `stale_after_minutes x build_stale_factor` after
the submit (defaults: 15 x 4 = 60 minutes), and once the session has read the feedback (`mock_review_feedback`) it waits
`stale_after_read_minutes x build_stale_factor` from that read (defaults: 60 x 4 = 240 minutes). The feedback result
carries `action: "build"` and a `next_step`; an ack only silences the ping, so a session that acknowledges instead of
building posts no round. See [settings.md](settings.md) for the three settings.

## Multiplexing gateways

With `gateway.multiplex_profiles` on, one gateway process serves every profile. The board inside that gateway reads its
signing secret (`MOCKREVIEW_WEBHOOK_SECRET`) from the gateway profile's own `HERMES_HOME`: its `.env` and its configured
secret sources, the same home its settings and reviews come from. It never uses a routed profile's secrets, even when it
starts or answers during that profile's turn.

The board reports the signing secret as `not-set` (`hermes mockreview owner` shows `"secret": "not-set"` under
`server.info.wake`) although the gateway has it. On a multiplexing gateway the value must be in that profile's `.env`,
which is where `hermes mockreview setup` puts it, or in one of its secret sources. A value that exists only in the
process environment (a launchd or systemd `Environment=` entry, `op run`) is not visible to the board there.

A rotated secret is used from the next request and the next retry on, without restarting the gateway. Run
`hermes mockreview setup` again after a rotation so the routes use the new one.

`/api/owner` answers HTTP 500 and the gateway log shows `UnscopedSecretError`. The installed plugin predates the fix
for multiplexing gateways (see [CHANGELOG.md](../CHANGELOG.md)). Update it and run `hermes gateway restart`.

## Settings

A setting seems to be ignored. `hermes mockreview settings` shows the source of each value. An environment variable
beats `config.yaml`. An invalid value is skipped with a warning and the next source applies. Look settings are copied into
a review when it is created, so a change affects new reviews only.

## Windows

Run the board with `hermes mockreview serve` and leave the terminal open. Press Ctrl+C or Ctrl+Break to stop it.
