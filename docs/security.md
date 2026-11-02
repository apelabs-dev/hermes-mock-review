# Security

This page states what the board protects, what it leaves open on purpose, and how each hosting mode changes that.
Read it before you set `host` to `0.0.0.0` or host a review on here.now.

## Model in one paragraph

A review link is a capability. The review id is random (at least 64 bits), and anyone who can open the board can read the
review and send feedback for it. New here.now reviews additionally require a shared password by default. The board has no reviewer accounts. What protects a review is that the link is hard
to guess and that the network path to the server is limited: loopback by default, your tailnet with Tailscale, a password gate on here.now (unless explicitly disabled, or a legacy review).

## What is authenticated

- Wake-up requests to Hermes. Send posts to the Hermes webhook with an HMAC-SHA256 signature over the timestamp and the
  body, using `MOCKREVIEW_WEBHOOK_SECRET`. Hermes rejects a request whose signature is wrong or whose timestamp is more
  than 300 seconds off, and it drops a delivery id it saw in the last hour.
- Feedback submits. Each review has a random submit token, and a submit without the right token is refused with 403. The
  token is part of the review's `state.json`, so whoever can open the board holds it. It stops a blind request to a known
  review id. It does not separate one reviewer from another.
- The index page (`/`) and the owner endpoint (`/api/owner`). They answer requests that come from this computer without
  proxy headers, and return 404 to everyone else. A visitor over the LAN or tailnet needs a review's own link.

## What is not authenticated

- Opening a local/tailnet review. Anyone with access and the link sees the title, the brief, the mocks, earlier marks and notes.
- Submitting feedback, for anyone who opened the board. Reviewer identity is not recorded or checked.
- Anything on a server bound to `0.0.0.0`: every device that reaches the port can send requests. The index stays
  local-only, but a device that learns a review id can use it.
- Reviewer identity on here.now. A shared password gates the site by default, but does not identify individual reviewers.
  After unlock, Site Data accepts inserts from the page; only your API key can read them back.

## Protections in the server

- Host allowlist. A request whose Host header is not loopback, the bind address, or the host of `public_base`,
  `local_base` or `lan_address` gets 421. This blocks DNS rebinding. With a wildcard bind, any IP-literal Host is accepted
  too. The list is read at server start.
- Origin check. A POST that carries an Origin header must come from an allowed origin, and `null` (a sandboxed frame) is
  refused with 403. The CLI and curl send no Origin and pass.
- Response headers. The board is served with a Content-Security-Policy that allows only same-origin script, style, image
  and frame sources, plus `nosniff` and `no-referrer`. Variant and prototype files are served with a `sandbox allow-scripts` policy, so they
  run with an opaque origin and cannot reach the board.
- Untrusted content. User text is rendered as text. Mock HTML is parsed without running it, and the board removes
  scripts, frames, forms and event-handler attributes. Anchor links retain only absolute HTTP(S) URLs with no whitespace
  or controls; they open in a new tab with `noopener noreferrer`. Relative and other-protocol hrefs are removed.
  Table cells use text/DOM nodes to link bare absolute HTTP(S) URLs, including multiple URLs inside prose.
  Surrounding text, sentence punctuation and literal HTML are preserved; no cell content is parsed as HTML.
- Size limits. Requests are capped at 2 MB, `decisions.json` at 3 MB, here.now records at 15 KB, and block counts, pins and
  text lengths have fixed limits (see [decisions-format.md](decisions-format.md)).
- Feedback is treated as data. The bundled skill tells the model that quoted feedback is design input and not an
  instruction. A hostile reviewer could still write persuasive notes, so keep
  review links to people you trust.

## Tools for the woken session (`wake_toolsets`)

Hermes runs the session that Send wakes as a webhook session with its safe webhook tools only (web search and extract,
vision, clarify): no terminal, no files, no mock_review tools. That session therefore cannot build the next round. By
default it posts one line ("Round N feedback is saved; the build continues in this thread's chat session.") and the
origin chat's own session does the build.

`wake_toolsets` (empty by default) lets you grant that session more, for example `mock-review, terminal, file` (the list replaces Hermes's webhook default, so
include `mock-review` for the mock_review tools). `hermes mockreview
setup` writes the list into the wake route's `toolsets` key in `$HERMES_HOME/webhook_subscriptions.json`, the per-route
override Hermes reads for webhook sessions. `hermes webhook subscribe` has no flag for it on purpose, so an agent cannot
grant itself tools; setting it is your explicit step. The notice route runs no agent and never gets it. Empty the setting
and re-run setup to remove it.

The risk: with tools granted, the woken session acts on text a reviewer typed on the board. The feedback header and the
skill tell the model that quoted feedback is design input, not instructions, but a model can still be persuaded. Grant
only the toolsets the build needs, and only for boards whose reviewers you trust.

here.now is refused. A here.now board is a public link (password protected by default), and every board of a platform
wakes the same route, so its feedback would reach the same tools. While `wake_toolsets` is set:

- setup refuses to write it (exit 1, summary row `wake toolsets failed`) when it was run with `--hosting herenow`, when
  `default_host` is `herenow`, or while any open here.now review wakes Hermes, and it then leaves the route without tools;
- `create` refuses a here.now board that wakes Hermes (409), before anything is created or published, and
  `add-round` refuses to add a round to one. A here.now board with `wake` off is allowed: it wakes nobody.

The guard reads the setting, not the route file: after you empty `wake_toolsets`, re-run setup before you host on here.now
so the route loses its tools too.

## Secrets

API and signing keys are never stored in review files. `MOCKREVIEW_WEBHOOK_SECRET` and `HERENOW_API_KEY` come from the Hermes secret scope.
Setup writes the webhook secret with `hermes config set`, which stores it in the Hermes `.env`. Setup never prints the
secret: command output is redacted before display, and an existing secret is reused instead of replaced. Per-review here.now passwords, reviews, tokens
and feedback are plain files under `$HERMES_HOME/workspace/mock-reviews` with the permissions of your account. Treat that
folder like the rest of your Hermes home.

## Hosting modes compared

- `local` with the default `host`: reachable from this computer only.
- `local` with `host` at `0.0.0.0`: reachable from your whole network, no login.
- `tailnet`: reachable from devices on your tailnet, through `tailscale serve`. Tailscale's access rules apply. The
  board server itself stays on loopback, and requests through the proxy count as non-local for the index and owner
  endpoint.
- `herenow`: internet hosting with a password gate by default for new reviews. Explicit opt-outs and legacy reviews
  are public. The initial publish precedes the metadata PATCH, so a short public interval exists; failed PATCHes leave
  protection unconfirmed and must be retried or the site deleted. See [recovery details](hosting.md#password-controls-and-recovery).

## Reporting a vulnerability

Open a private security advisory on the GitHub repository (Security tab, "Report a vulnerability"). Please do not post
details in a public issue before a fix is out.
