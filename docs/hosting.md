# Hosting

Every review is hosted in one of three modes: `tailnet`, `local` or `public`. `default_host` sets the mode used when a
call does not name one. The agent can pass `mode` on each `mock_review_create` call, and the bundled skill asks you
first when `default_host` was never set. `public` means a public URL behind Tailscale Funnel or any TLS proxy; see
[public-hosting.md](public-hosting.md). `hermes mockreview setup --hosting <local|tailnet|lan>` writes the matching
settings. `lan` is local mode with the board bound to `0.0.0.0`.

Whatever the mode, the board server has to be running when you open a link. Either turn on `autostart` and restart the
gateway, or run `hermes mockreview serve`. `hermes mockreview owner` shows whether a server is running and where it
stores reviews.

## Local

The default. The link is `http://<address>:<port>/r/<review id>/`.

- With `host` at its default of `127.0.0.1`, the server accepts connections from this computer only, and the link works
  on this computer only. Create results carry a note saying so.
- With `host` set to `0.0.0.0`, any device on the same network can open the link, and local links use this computer's
  network address (set `lan_address` to override the detected one). Anyone other than this computer meets the sign-in
  page: invite people with `hermes mockreview invite`, or share one review with `hermes mockreview share <review id>`.
  Restart the gateway or the `serve` process after changing `host` or `port`.

      hermes config set plugins.entries.mock-review.settings.host 0.0.0.0

## Tailnet

The board stays on `127.0.0.1` and Tailscale carries it to your other devices. Only devices on your tailnet can open the
link. People other than you sign in once with an invite link; after that their Tailscale login signs them in. Use
`tailscale serve` for a tailnet board, not Funnel: Funnel ingress is public and carries no Tailscale identity, and the
board never accepts one on a public host (see [public-hosting.md](public-hosting.md)).

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

## Public

`public` puts the board on a public HTTPS URL while the server keeps listening on loopback. Tailscale Funnel is built
in: `hermes mockreview public on` maps one Funnel port (default 8443) and prints the URL; `public status` and `public
off` show and remove it. The port must be exclusively the board's; another handler on it is refused before any
Tailscale command runs. Any other TLS proxy works too: set `public_url` to the address people open and
`trusted_proxies` to the proxy's address. Reviewers sign in with an invite or a share link: Funnel carries no Tailscale
identity, the board never accepts one on a public host, and `forwarded_identity` is refused there; Cloudflare Access
still works. The full
recipes, including a cloud box without Tailscale and the SSE heartbeat, are in
[public-hosting.md](public-hosting.md), and the release checks for a real public route are in
[public-hosting-live-checks.md](public-hosting-live-checks.md).

## Share links

A review has one guest link, managed by its owner. The board header's **Share** button (admins only) and
`hermes mockreview share <review id>` both change it. The link is signed with itsdangerous and carries the review id and
its generation: regenerating it makes a new URL, and the old one stops working. The URL is rebuilt from the review's row
and the install's signing key; the database stores the row, not the URL.

- **Who can open it.** `anyone` (the default) means anyone holding the URL joins as a guest. `members` means the link
  only redirects signed-in people who can already read the review; everyone else, and every guest, gets the same dead
  page as a missing link. Switching an `anyone` link to `members` ends every guest session of that review and closes
  their live streams; members' sessions are untouched, and a guest cookie left from before the switch reads as a dead
  link rather than the sign-in page.
- **Guests can.** The guest role is `reviewer`, `commenter` (the default) or `viewer`. It applies to everyone who joins
  through the link.
- **Invite role and expiry.** The invite row has its own role and expiry selects, `commenter` and `never expires` by
  default. They work before any share link exists and never read the guest-link selects, so inviting someone does not
  depend on who may open the link.
- **Expiry.** `Never expires` (the default), `24h` or `7d`. The link, and the guest sessions bound to it, end then.
- **Copy, Regenerate, Stop sharing.** Copy puts the URL on the clipboard. Regenerate makes a new link and signs out every
  guest of the old one. Stop sharing ends the link; the section then reads "Not shared" and offers **Create link**. Both
  buttons ask once more in place ("Regenerate?" / "Stop sharing?") rather than opening a browser dialog.
- **People and inherited access.** A person whose access comes only from an all-reviews grant shows that inherited
  role and no Remove button. A review-scoped grant on top of an all-reviews grant can be removed; the row then
  re-renders with the all-reviews access that remains. Remove never touches another review or the all-reviews grant.
- **Seats.** A review has 25 live seats, and one is the owner's: the dialog shows "Live: N of 25 seats". A new guest is
  refused when the guest seats are full, with a page that asks the owner for a new link.
- **What viewers see.** The version picker (`Latest round` or `Round N`) is stored on the link. It affects viewer guests
  only: their board shows that one round, with no feedback pins or notes and no submit token. Every other role still sees
  every round. If the picked round is removed, the link falls back to the full, latest board.
- **The link base.** The review's own mode decides the address, so a link never points somewhere the review is not
  served: a `local` review uses this computer's local address (the same base as its board URL, following
  `lan_address` when the board is reachable beyond loopback), a `tailnet` review uses `public_base` and never
  `public_url` or the Funnel host, and a `public` review uses `public_url`, then the Tailscale Funnel URL, then
  `public_base` — so a public link stays valid when the owner turns Funnel on or off. A link whose review is gone
  falls back to the public walk and then the local base. Nothing else builds a share URL.

    hermes mockreview share <review id> create --role viewer --expires 7d --audience anyone --round 2

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
  ready." (The counts appear for decision boards only.) After Build it says "`<title>`: Got it — building round
  N's plan now. The result lands as round N+1 on the same link." If the wake-up fails, no ack is sent; it follows right
  after the wake-up lands on a retry. An ack that fails is retried every `retry_seconds` (with a new delivery id after a
  gateway error, because Hermes keeps the failed id as seen for an hour), and is dropped once a newer round exists or the
  review closes.
- The stalled ping. If a sent round gets no new round within `stale_after_minutes` (default 15, 0 turns it off; times
  `build_stale_factor`, default 4, after Build), the board
  server posts "No round N+1 yet ... The agent may have stalled" to the same chat, once per submission (an amend is a new
  submission). Only the board server runs this check, and only for open reviews with wake on. The board shows the same
  hint, and it polls every 30 seconds for up to two hours while a sent round has no successor, so the hint and the next
  round appear without a reload.

Both messages carry the title and counts only, never the reviewer's notes. They reach the chat the same way the wake-up
does (the route's `--deliver-chat-id` is the review's origin chat), so a Discord thread gets them in the thread. Hermes's
command line cannot target a Telegram forum topic for either route, so there both land in the group's general chat.

Re-running `hermes mockreview setup` is safe: it re-subscribes the same route names (updating them, not duplicating).
An install that predates the notice route has to run it once. Until then the gateway answers `404` for the notice route,
the board server logs one warning, marks that notice `no_route` and never retries it; the wake-up is unaffected.

If you only want a board that is viewed and never wakes anyone, set `wake_required` to false or pass `--no-wake` on
`create`.
