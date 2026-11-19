# Migrating to 2.0.0

## What 2.0.0 removes

here.now hosting and the here.now board password are removed. Public hosting is now a public URL behind Tailscale
Funnel or any TLS proxy: `hermes mockreview public on` maps a Funnel port, or set `public_url` and `trusted_proxies`
for another proxy, then create the review with `--mode public`. See [public-hosting.md](public-hosting.md).

## Python 3.11-3.14

2.0.0 requires Python 3.11-3.14 (`requires-python = ">=3.11,<3.15"` in `pyproject.toml`); Python 3.9 and 3.10 are no
longer supported. Upgrade the interpreter Hermes runs on before upgrading the plugin, then run `hermes mockreview setup`
(it validates the installed plugin folder) or `hermes plugins validate <plugin folder>`.
The plugin needs one Python dependency, `itsdangerous`, which Hermes installs. The optional `crypto` extra (PyJWT) is only for Cloudflare Access.

## Getting a public URL now

There is no built-in host any more; the board always listens on loopback and a TLS endpoint in front of it publishes
the link. Pick one:

1. **Tailscale Funnel, built in.** On the computer that runs the board, run `hermes mockreview public on`, then create
   the review with `--mode public`. The command maps one Funnel HTTPS port (default 8443) to the board and prints the
   public URL; `hermes mockreview public status` and `off` show and remove it.
2. **Any TLS proxy.** Set `public_url` to the address people open and put the proxy's address in `trusted_proxies`,
   then create the review with `--mode public`. Caddy, nginx, Cloudflare Tunnel, oauth2-proxy and a cloud box are
   covered in [public-hosting.md](public-hosting.md).
3. **Tailnet only.** Keep `default_host: tailnet` (or create with `--mode tailnet`) and expose the loopback
   board with `tailscale serve`, as before.

The full recipes, including the server-sent-events heartbeat for proxies, are in [public-hosting.md](public-hosting.md).

## The new users and roles model

2.0.0 replaces the password with accounts and roles. There are no passwords anywhere in the board.

- **The owner.** With `loopback_owner` on (the default), a request from this computer that did not come through a
  proxy is the install's owner account, an admin. With it off, the owner signs in like anyone else with a one-time
  link: `hermes mockreview login` (single use, 15 minutes).
- **Invite links.** People join with `hermes mockreview invite` (or Settings → Invites). An invite is single use and
  expires after 7 days by default; `--expires` changes that. Opening the link shows a confirmation button, so a link
  preview or mail scanner does not burn it. `--review <id>` or `--all-reviews` also grants the new account a role on
  those reviews, `commenter` by default.
- **One flat user directory.** Accounts belong to the install, not to a workspace. Each account is a member or an
  admin: admins can open and manage every review and see Members and Invites in Settings. The last admin cannot be
  demoted, and the only admin cannot be deleted.
- **Per-review roles.** A person can hold one of three roles on a review, optionally with an end date: `viewer`
  (look), `commenter` (look and comment) or `reviewer` (look, comment and send feedback). Grant one with
  `hermes mockreview users grant <name|id> --review <id> --role reviewer --expires 30d`; the same options with
  `--all-reviews` grant every review, including future ones. That all-reviews grant is a convenience flag, not a
  separate workspace or team object.
- **Guests and share links.** A review has one share link, managed by its owner from the Share dialog or
  `hermes mockreview share <review id>`. A guest opens it with the link's role (`commenter` by default; `viewer` or
  `reviewer` are the other choices) and gets a server-assigned adjective+animal name, unique among that review's live
  guests and still renameable. The link can be limited to signed-in members, carry an expiry, and pin viewer guests
  to one round. Guests never wake Hermes: a guest's Send is stored for the owner. Regenerating or stopping the link
  ends every guest session of the old link.

Roles and grants are read on every request, so a change applies immediately. The complete rules, including proxy
identities, sessions, throttling and audit, are in [security.md](security.md).

## Run `mockreview migrate`

Existing reviews store the old mode. Convert them before using the board with 2.0.0:

    hermes mockreview migrate --dry-run
    hermes mockreview migrate

The command prints one JSON object:

- `data_root`: the store it read (the running board server's store when one is running).
- `dry_run`: whether anything was written.
- `target`: `local` or `tailscale`, from `--mode local|tailnet` when given, else from `default_host`
  (`tailnet` becomes `tailscale`; anything else becomes `local`).
- `changes`: one entry per converted review (`review_id`, `from`, `to`, `url`).
- `changed`: how many reviews were converted.

It rewrites the stored `mode` and deletes the old `herenow` sub-record; no other field is changed except the
bookkeeping `updated_at` timestamp. It is idempotent: a second real run reports `"changed": 0` and writes nothing. A
dry run reads only.

## Existing reviews and pins

Reviews, rounds, feedback and pins are kept as they are. `mockreview migrate` touches only the review's hosting
fields, so a review created before 2.0.0 opens with the same title, rounds and history, and its pin text is still
rendered by the board. Round files and `feedback.json` are not rewritten.

A 1.x pin is a note inside a round's feedback, not a comment thread. After migration it stays a plain-text pin: the
pin list and the mock show its text, and clicking it opens the pin popover with that text. It is not converted into a
thread and gets no reply box, so the migration never invents a
conversation. To start one, add a round and open a new pin there; threads live alongside the round they annotate.
This read path is held by `tests/test_rel_migrate.py`.

## Boards already live on here.now

The local review record switches to the target mode, so the board appears on the board server's local or tailnet link
from then on. The remote here.now site is not deleted for you: delete it at here.now.

## Old config keys

`hermes mockreview setup` removes the obsolete `herenow_credentials`, `subscriptions_file` and
`herenow_publish_script` settings when it finds them.

## Signing in from a phone or laptop

1.x had no accounts, so any device that could reach the board could open it. 2.0 gives every page, comment and pin an
account, and each device signs in once.

- A tailnet or public board answers a device that has not signed in with its sign-in page (HTTP 401), not the board.
- `hermes mockreview setup` prints a sign-in link at the end when it set the board up for the tailnet; `hermes
  mockreview login` prints a fresh one at any time. Open it on the device, press **Sign in**, and that browser stays
  signed in.
- A link works once and expires after 15 minutes, so mint one per device: run the login command again for the second
  phone or laptop.
- A local board needs no sign-in: a request from this computer is the owner.

If a board link opened fine in 1.x and now shows a sign-in page, that is this change and not a broken link.
