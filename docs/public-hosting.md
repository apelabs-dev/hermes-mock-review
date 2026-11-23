# Public hosting

The `public` mode gives a review a public HTTPS link while the board server keeps listening on loopback. A TLS
endpoint in front of it — Tailscale Funnel, or any reverse proxy — terminates HTTPS and forwards to
`http://127.0.0.1:<port>`. People then sign in with an invite or a share link: a public board never has a password
and never treats a public Host as the owner of this computer.

A public review's link is the `public_url` setting when set, else the Tailscale Funnel URL when Funnel serves the
board, else `public_base`. That order lets a public review move to Funnel, or to a proxy, without changing the mode.
Only `public` reviews use it: a `local` review's link is the local base, and a `tailnet` review's link is
`public_base`, never `public_url` or the Funnel host.

## Tailscale Funnel, built in

On the computer that runs the board:

    hermes mockreview public on

The command maps one Funnel HTTPS port to this board. It prints the public URL and the `commands` it ran; the setting
`funnel_port` chooses the port (default 8443). To use another allowed port:

    hermes mockreview public on --port 10000

Check or remove the mapping with:

    hermes mockreview public status
    hermes mockreview public off

What `on` changes is exactly one Funnel route: `--https=<port>` to `http://127.0.0.1:<port>`. It never runs `funnel
reset`, never touches port 443 unless you pass it, and refuses (exit 1) when the chosen port already has any handler
that is not this board (another app's proxy, a text, path or redirect handler, or a TCP forward), so routes such as a
dashboard or a media server stay as they are. `off` removes only this board's route. Tailscale Funnel accepts HTTPS
ports 443, 8443 and 10000 only; 443 is usually already taken on a node that serves other apps, which is why 8443 is the
default. The release checks for a real public route are in
[public-hosting-live-checks.md](public-hosting-live-checks.md).

Funnel ingress is public ingress and carries no proxy identity: at tag `v1.98.1`, `ipn/ipnlocal/serve.go:1046-1075`
deletes every incoming `Tailscale-User-*` and `Tailscale-Funnel-Request` header, marks a Funnel request with
`Tailscale-Funnel-Request: ?1`, and returns before setting any identity header. Only private tailnet Serve on the same
node sets `Tailscale-User-*` (from `WhoIs`) and signs people in. The board refuses a Tailscale identity on any public
host (the Funnel URL or `public_url`) and on a request that carries the marker. It refuses `forwarded_identity`
(`X-Forwarded-User` / `X-Forwarded-Email`) on a public host and with the marker too, because Funnel and Serve connect
from a trusted loopback proxy and Tailscale strips only `Tailscale-*` headers. Cloudflare Access (a signed JWT) remains
the only header/token-based public sign-in source; everyone else signs in with an invite (`hermes mockreview invite`)
or the review's share link. `public on` refuses while `forwarded_identity` is on.

`public on` needs the chosen HTTPS port to be exclusively this board's. Every handler on the port is inspected first:
another app's proxy, a `Text`, `Path` or `Redirect` handler (including a `/private/` route), or a TCP forward is refused
(exit 1) before any Tailscale command runs, because Funnel applies to the whole port. The board keeps binding loopback;
public mode changes no `host`, `public_url` or other setting.

`hermes mockreview public status` reports `available: false` when the Tailscale CLI is missing or its JSON cannot be
read; it still exits 0 and says why in `detail`.

## Any TLS proxy

Set `public_url` to the address people will open, and put the proxy's address in `trusted_proxies` so its
`X-Forwarded-Proto` / `X-Forwarded-For` headers are believed:

    hermes config set plugins.entries.mock-review.settings.public_url https://reviews.example.com
    hermes config set plugins.entries.mock-review.settings.trusted_proxies '["127.0.0.1", "::1"]'

The board still listens on `127.0.0.1:8790`; the proxy connects there. `host` stays at its default.

- Caddy. Caddy sets `X-Forwarded-Proto` and `X-Forwarded-For` itself:

      reviews.example.com {
          reverse_proxy 127.0.0.1:8790
      }

- nginx. Forward the host and scheme, and turn buffering off so server-sent events are not held back:

      location / {
          proxy_pass http://127.0.0.1:8790;
          proxy_set_header Host $host;
          proxy_set_header X-Forwarded-Proto $scheme;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_buffering off;
      }

- Cloudflare Tunnel. Point a public hostname at `http://127.0.0.1:8790` in `cloudflared`. `cloudflared` connects from
  loopback, which is already in `trusted_proxies`. For Cloudflare Access sign-in, also set `cloudflare_team` and
  `cloudflare_audience` (the optional `crypto` extra is required).

- oauth2-proxy or Authelia. For a board on a private host (loopback, LAN or a tailnet Serve address): put the proxy in
  `trusted_proxies`, turn on `forwarded_identity` when it supplies `X-Forwarded-User` / `X-Forwarded-Email`, and make it
  strip those headers from incoming requests. A public host refuses `forwarded_identity`; use Cloudflare Access or
  invites there.

Every non-Tailscale proxy listed in `trusted_proxies` must strip `Tailscale-User-*` headers from incoming requests (or
`tailscale_identity` must be off). Otherwise a client of that proxy could name a tailnet account. Set `public_url`
only to the scheme, host and optional port: a path, query, fragment or credentials are refused when the setting is
read.

## A cloud box

No Tailscale is needed. Keep the board on loopback, put nginx, Caddy or another TLS proxy on the same machine (or a
private network), and set:

    hermes config set plugins.entries.mock-review.settings.public_url https://reviews.example.com
    hermes config set plugins.entries.mock-review.settings.trusted_proxies '["127.0.0.1", "::1"]'

`host` stays `127.0.0.1`; only the proxy reaches the board. `hermes mockreview public status` says `available: false`
there because there is no Funnel, which is expected: the proxy, not Tailscale, publishes the link. If the proxy
connects from another address, list that address or network in `trusted_proxies` instead of loopback.

## Server-sent events through a proxy

The board sends server-sent events on a live stream (the live-stream plumbing). A quiet stream would look idle to a
proxy, so the server writes a comment heartbeat every `sse_heartbeat_seconds` (default 15):

    hermes config set plugins.entries.mock-review.settings.sse_heartbeat_seconds 15

Reproduce the evidence with the probe in the repository (the probe server, not the board, answers `/events`). It runs
the same `mockreview.sse` code and verifies TLS certificates normally:

    python3 scripts/sse_probe.py serve --port 18902
    python3 scripts/sse_probe.py client --url https://board.example.ts.net:10000/events --seconds 300

The client prints one line per frame with the monotonic gap since the previous frame, then a
`RESULT ok=<bool> frames=<n> max_gap=<s> seconds=<s>` line, and exits non-zero when no frame arrives within three
heartbeats, a gap exceeds that, or the stream closes early. The heartbeat must stay well under the proxy's idle
timeout (commonly 60 seconds; some proxies close an idle stream sooner), which is why the default is 15. The framing
also sends `Cache-Control: no-store` and `X-Accel-Buffering: no` so a proxy does not buffer it.

A five-minute stream through a real public route has **not** been verified on this install. The release gate for that
check is [public-hosting-live-checks.md](public-hosting-live-checks.md); it requires `RESULT ok=true` and
`seconds >= 300` from a device off the tailnet, with the log kept outside the repository.

## Security checklist for a public board

- Accounts only. Public request handling follows the accounts layer: viewers sign in with an invite or a share
  link. A request on a public Host is never the loopback owner, even from 127.0.0.1 and even when the proxy forwards
  without `X-Forwarded-For`. A Tailscale identity and `forwarded_identity` are never accepted on a public host or with
  the Funnel marker.
- Host allowlist. Only loopback names, the bind address, and the hosts of `public_url`, the Funnel URL, `public_base`,
  `local_base` and `lan_address` are accepted; anything else is 421 (DNS-rebinding guard). The public names are the
  `public_url` host and the Funnel host only; loopback names are never public names.
- Response headers. CSP with `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: no-referrer`, and
  `Strict-Transport-Security: max-age=31536000` when a trusted proxy reports HTTPS (never on plain HTTP).
- Body and connection limits. Feedback is capped at 2 MiB and API bodies at 64 KiB; a silent connection is dropped
  after 30 seconds and at most 64 connections are served at once.
- `/healthz` is `{"ok": true}` on a public Host; the service name appears only on loopback and private requests.
- Share links are credentials. Anyone holding a live link can open its review; regenerate or stop it from Settings or
  `hermes mockreview share <review id> regenerate|stop`, and check the audit trail.
- Wake-ups. A board that wakes a session with tools (`wake_toolsets`) or a Desktop chat is refused (409) or held while
  it is `public` or `public_url` is set, because such a proxy leaves no marker the server can trust on each request. A
  Tailscale Funnel mapping alone does not refuse it: Tailscale marks every Funnel request, and a request through it
  never wakes such a board. On every board Send and Build are the owner's, and the owner wakes Hermes only from this
  computer or the tailnet: through Funnel or another proxy the owner's Send, pin fix or mention is saved with the
  reason, and Amend from the tailnet sends it. A reviewer's `@Hermes` mention wakes a messaging chat with no
  `wake_toolsets` from any ingress. See [security.md](security.md#who-may-wake-hermes).
- The proxy holds the certificate and private key; the board keeps speaking plain HTTP on loopback only.
