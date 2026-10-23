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

Setup says `no chat platform found in the Hermes config` and exits 2. Setup reads `platforms.*` for the chat you review
on, and skips the platforms that are not chats (`webhook`, `msgraph_webhook`, `api_server`, `local`). Nothing was changed.
Configure your chat platform in Hermes, or name it: `hermes mockreview setup --origin-platform telegram` (repeatable).

Setup says several chat platforms are configured and exits 2. It only asks on a terminal. With `--yes` or `--dry-run`,
choose with `--origin-platform <name>` (repeatable). On a terminal it lists the platforms and asks once; Enter means all.
An explicit `--origin-platform` is used as given, even `webhook`.

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
exist. Run `hermes mockreview setup`. For a demo board that never wakes anyone, pass `--no-wake` (or set `wake_required`
to false).

A create call says `this process's store ... is not the one the board server watches`, or `this process's store ... is
private to this profile`. The process that made the call writes to a different folder than the one the running board
server serves, so the board could never load or wake anyone. Run `hermes mockreview owner` to see both folders. Run the
server for the same Hermes home, or set `owner` to `service` to create in the server's store.

A create call says `Start the board server`. No server is running for a store that needs one. Run
`hermes mockreview serve`, or turn on `autostart` and restart the gateway, then retry.

`A prototype ("try it live") round is turned off`. Prototype rounds need `offer_live_demo` set to true, or
`--allow-live-demo` on the call.

`decisions.json: ...` or `blocks[n]: ...`. The file failed validation. The message names the block. See
[decisions-format.md](decisions-format.md).

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

## Sent but nothing happened

Send does three things, so a chat that stays silent is a real signal:

1. The wake-up is posted, so the woken session builds the next round.
2. Right after the gateway accepts it, an ack is posted to the origin chat (and mirrored into that chat's session):
   "Got your feedback on `<title>`, round N ... Hermes is working on round N+1".
3. If no new round appears within `stale_after_minutes` (default 15), a ping posts to the same chat saying the agent may
   have stalled.

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

## Multiplexing gateways

With `gateway.multiplex_profiles` on, one gateway process serves every profile. The board inside that gateway reads its
signing secret (`MOCKREVIEW_WEBHOOK_SECRET`) and the here.now key (`HERENOW_API_KEY`) from the gateway profile's own
`HERMES_HOME`: its `.env` and its configured secret sources, the same home its settings and reviews come from. It never
uses a routed profile's secrets, even when it starts or answers during that profile's turn.

The board reports the signing secret as `not-set` (`hermes mockreview owner` shows `"secret": "not-set"` under
`server.info.wake`) although the gateway has it. On a multiplexing gateway the value must be in that profile's `.env`,
which is where `hermes mockreview setup` puts it, or in one of its secret sources. A value that exists only in the
process environment (a launchd or systemd `Environment=` entry, `op run`) is not visible to the board there.

A rotated secret is used from the next request and the next retry on, without restarting the gateway. Run
`hermes mockreview setup` again after a rotation so the routes use the new one.

`/api/owner` answers HTTP 500 and the gateway log shows `UnscopedSecretError`. The installed plugin predates the fix
for multiplexing gateways (see [CHANGELOG.md](../CHANGELOG.md)). Update it and run `hermes gateway restart`.

## here.now

`install-here-now-skill`. The here-now skill is not installed in the active Hermes home. Run
`hermes skills install official/productivity/here-now -y`.

`HERENOW_API_KEY is not set in the active Hermes secret scope`. Run `hermes config set HERENOW_API_KEY <key>`.

A published page loads but feedback never comes back with `account_required` in the log. The publish ran without your
key. Check the key, then run `hermes mockreview publish <review id>` again.

## Settings

A setting seems to be ignored. `hermes mockreview settings` shows the source of each value. An environment variable
beats `config.yaml`. An invalid value is skipped with a warning and the next source applies. Look settings are copied into
a review when it is created, so a change affects new reviews only.

## Windows

Run the board with `hermes mockreview serve` and leave the terminal open. Press Ctrl+C or Ctrl+Break to stop it.
