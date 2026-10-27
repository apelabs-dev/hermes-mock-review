# Hosting

Every review is hosted in one of three modes. `default_host` sets the mode used when a call does not name one
(`local`, `tailnet` or `herenow`). The agent can pass `mode` on each `mock_review_create` call, and the bundled skill
asks you first when `default_host` was never set. `hermes mockreview setup --hosting <local|tailnet|lan|herenow>` writes
the matching settings. `lan` is local mode with the board bound to `0.0.0.0`.

Whatever the mode, the board server has to be running when you open a link. Either turn on `autostart` and restart the
gateway, or run `hermes mockreview serve`. `hermes mockreview owner` shows whether a server is running and where it
stores reviews.

## Local

The default. The link is `http://<address>:<port>/r/<review id>/`.

- With `host` at its default of `127.0.0.1`, the server accepts connections from this computer only, and the link works
  on this computer only. Create results carry a note saying so.
- With `host` set to `0.0.0.0`, any device on the same network can open the link, and local links use this computer's
  network address (set `lan_address` to override the detected one). There is no login, so use it only on a network you
  trust. Restart the gateway or the `serve` process after changing `host` or `port`.

      hermes config set plugins.entries.mock-review.settings.host 0.0.0.0

## Tailnet

The board stays on `127.0.0.1` and Tailscale carries it to your other devices. Only devices on your tailnet can open the
link.

1. Check that Tailscale is running (`tailscale status`).
2. Run `hermes mockreview setup --hosting tailnet`. It reads your address with `tailscale status --json` and your
   existing routes with `tailscale serve status --json`, then saves the board's address as `public_base`:
   - a route that already sends an HTTPS port to the board's port is used as it is, and there is nothing to run;
   - otherwise setup prints one `tailscale serve` command to run once. It uses the default HTTPS port (443) only when
     nothing is served there, and otherwise the board's own port (`--https=8790`), so the routes of your other apps stay
     as they are.

   The plugin never starts or changes Tailscale itself. On macOS, when the `tailscale` command crashes ("bundleIdentifier
   is unknown", a known issue with the symlink the app installs), setup runs the app's own binary,
   `/Applications/Tailscale.app/Contents/MacOS/Tailscale`, and prints the command with that path.
3. Run the printed command, if any. `tailscale serve status` then lists the address saved as `public_base`.
4. Restart the gateway (or the `serve` process). The server only accepts requests whose Host header matches
   `public_base`, `local_base`, `lan_address` or a loopback name, and it reads those settings at start.

If you run Tailscale under a different name or port, set `public_base` yourself, for example
`hermes config set plugins.entries.mock-review.settings.public_base https://<machine>.<tailnet>.ts.net:8790`.

## here.now

Publishing calls the official skill's `publish.sh` through `bash`; it needs Bash and the helper's Unix tools.
On Windows, use Git Bash on `PATH` with paths that its tools can read, or run Hermes and the plugin inside WSL.
Native Windows without that environment cannot publish here.now reviews. The Bash integration tests are skipped
on Windows; site building, feedback validation and polling are still tested there. Local hosting does not need Bash.

New here.now boards have a server-side password gate by default. Send the returned `password` next to the URL;
the reviewer unlocks the site before viewing the board and sending feedback. Keep secrets and private values out of mocks. Feedback is collected through the site's Site Data and read back by your
board server, which polls every `poll_seconds`.

1. Install the official skill: `hermes skills install official/productivity/here-now -y`. The plugin looks for its
   `publish.sh` under `$HERMES_HOME/skills`, and nowhere else.
2. Store your key: `hermes config set HERENOW_API_KEY <key>`. The key goes through Hermes's secret scope and is passed
   to the publish script in the child process environment.
3. Create the review with `mode="herenow"` (or set `default_host` to `herenow`).

The server has to be running for feedback to come back, because the here.now page cannot reach your computer. Closing the
review deletes the public site and archives its Site Data. `hermes mockreview publish <review id>` republishes a review,
and `hermes mockreview poll` reads Site Data once.

## Waking the agent

Send posts a signed request to the Hermes webhook route `<webhook_route>-<platform>` (for example `mock-review-telegram`)
at `http://localhost:8644/webhooks/<route>`. `hermes mockreview setup` creates one route for each chat platform you
choose (`--origin-platform`, repeatable; with one chat platform configured it uses that one) and stores the shared secret
as `MOCKREVIEW_WEBHOOK_SECRET`. When the Hermes webhook platform listens on another port (`platforms.webhook.extra.port`)
and `webhook_url` was never set, setup saves a `webhook_url` that points at that port. A review is tied to the platform and
chat it was created from, so the wake lands in the right place. To test the path without a browser, use `hermes
mockreview notify-test <review id> --to <url>` on a review that has an origin, which sends the same payload.

A woken session only posts its final answer, so without more the chat would look dead while Hermes works. Setup
therefore creates a second route per platform, `<webhook_route>-<platform>-notice`. It is created with `--deliver-only
--mirror-to-session`: Hermes posts the message to the origin chat without starting an agent, and mirrors it into that
chat's session so the chat's own agent knows Hermes is working on it. It carries two messages, both signed with the same
secret and sent to the same local webhook:

- The ack. Send posts the wake-up first, and once the gateway has accepted it, posts the ack: "Got your feedback on
  `<title>`, round N (approved X, change Y, skip Z). Hermes is working on round N+1; the link lands here when it's
  ready." (The counts appear for decision boards only.) If the wake-up fails, no ack is sent; it follows right after the
  wake-up lands on a retry. An ack that fails is retried every `retry_seconds` (with a new delivery id after a gateway
  error, because Hermes keeps the failed id as seen for an hour), and is dropped once a newer round exists or the review
  closes.
- The stalled ping. If a sent round gets no new round within `stale_after_minutes` (default 15, 0 turns it off), the board
  server posts "No round N+1 yet ... The agent may have stalled" to the same chat, once per submission (an amend is a new
  submission). Only the board server runs this check, and only for open reviews with wake on. The board shows the same
  hint, and it polls every 30 seconds for up to two hours while a sent round has no successor, so the hint and the next
  round appear without a reload. On a here.now board the server republishes after the ping, and the hosted board shows the
  hint the next time its state is read.

Both messages carry the title and counts only, never the reviewer's notes. They reach the chat the same way the wake-up
does (the route's `--deliver-chat-id` is the review's origin chat), so a Discord thread gets them in the thread. Hermes's
command line cannot target a Telegram forum topic for either route, so there both land in the group's general chat.

Re-running `hermes mockreview setup` is safe: it re-subscribes the same route names (updating them, not duplicating).
An install that predates the notice route has to run it once. Until then the gateway answers `404` for the notice route,
the board server logs one warning, marks that notice `no_route` and never retries it; the wake-up is unaffected.

If you only want a board that is viewed and never wakes anyone, set `wake_required` to false or pass `--no-wake` on
`create`.

### Password controls and recovery

`herenow_password` defaults to `true` in `plugins.entries.mock-review.settings` (environment override:
`MOCKREVIEW_HERENOW_PASSWORD`). Create stores the choice in `review.json`; later global changes do not affect an
existing review. Override it with `--password` / `--no-password` on create, or the `mock_review_create` tool's boolean
`password` argument. Create, add-round and publish JSON return `password`, `password_enabled` and `password_status`.
The password is four independent words from the bundled [EFF large wordlist](https://www.eff.org/files/2016/07/18/eff_large_wordlist.txt)
(7,776 choices each, about 51.7 bits of entropy; [CC BY 3.0 US](https://creativecommons.org/licenses/by/3.0/us/)).
It lives only in the local review record and explicit owner results, never the site files, browser state or list output.

Use `hermes mockreview password <review id> on` to enable the gate (reuses an existing password), or
`hermes mockreview password <review id> off` to remove it and clear the local password after success.
The same commands are available as `/mockreview password <review id> <on|off>`. Turning it on again after removal generates
a new password. Closed reviews reject password changes and publication.

Publishing first obtains a slug, saves it, then PATCHes `/api/v1/publish/<slug>/metadata` with `{"password":"..."}`.
Removal PATCHes `{"password":null}`. The gate survives republishing; protected reviews reassert it on each publish.
Owner Bearer-key Site Data reads are unaffected, and same-origin browser inserts work after unlock.
There is a brief interval between first publication and the password PATCH when the site can be reached without the gate.
A failed PATCH returns an error, records `password_status: error` and `password_applied: null`, and suppresses the URL in
list/open results. The remote gate is then **unconfirmed**; do not share the board. The slug and password are preserved:
retry `hermes mockreview publish <review id>` or the password command, or close the review to delete the site.
Retries also reconcile interrupted removals. Publish, toggle and close share a per-review operation lock.
An unpublished toggle saves the choice but returns an error until publication can apply it.

Legacy reviews without a stored password choice stay public on republish. Enable their gate explicitly with the
password command. Closing still archives feedback and deletes the site; housekeeping behavior is unchanged.
