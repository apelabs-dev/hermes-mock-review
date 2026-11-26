# Security

This page states what the board protects, what it leaves open on purpose, and how each hosting mode changes that.
Read it before you set `host` to `0.0.0.0`.

## Model in one paragraph

The board server has accounts. Every request to a review is checked against who is asking: the owner on this computer,
a signed-in member with a grant on that review, or a guest who opened the review's share link. Anyone else gets the
sign-in page, and a signed-in person without access gets the same "not valid" page whether or not the review exists.
There are no passwords: people join with a single-use invite link, a one-time sign-in link from the shell, or an
identity that a trusted reverse proxy vouches for.

## Accounts and sign-in

- Who counts as the owner. With `loopback_owner` on (the default), a request from this computer that did not come
  through a proxy is the install's owner account, an admin. Turn it off and the owner signs in like anyone else, with
  `hermes mockreview login`.
- Invite links (`hermes mockreview invite`, or Settings → Invites). A link works once and expires after 7 days unless
  you pass `--expires`. Opening it shows a confirmation button. The GET never uses the link, so link previews and
  mail scanners do not burn it. The token is signed (itsdangerous) with a random key kept in the database. Only a hash
  of its nonce is stored, and using it, revoking it and expiry are all checked in the database.
- Sign-in links (`hermes mockreview login`, `hermes mockreview admin login-link --user <who>`): single use, 15 minutes.
- Proxy identities, read only from a peer in `trusted_proxies` (default: this computer). Only one identity source
  answers per request. A request that carries the headers of two enabled sources (`Cf-Access-Jwt-Assertion`, any
  `Tailscale-User-*` header in any spelling, `X-Forwarded-User` / `X-Forwarded-Email`), valid or not, gets no proxy
  identity at all and is treated as if it had none (its session cookie, or nobody).
  - Tailscale Serve's `Tailscale-User-Login` (`tailscale_identity`, on by default). At tag `v1.98.1` (commit
    `2f4c02ee68c459aaea92d8b2dd81a38816823bef`), `ipn/ipnlocal/serve.go:1046-1052` deletes every incoming
    `Tailscale-User-*`, `Tailscale-Funnel-Request` and `Tailscale-Headers-Info` header; `:1058-1060` marks public
    Funnel ingress with `Tailscale-Funnel-Request: ?1` and returns before setting any identity header; only private
    tailnet Serve sets them, from `WhoIs` (`:1062-1074`). A Tailscale identity is therefore refused for a request that
    carries the marker and for any public host (the Funnel URL or `public_url`): a public URL never signs anyone in with
    it. `forwarded_identity` is refused on a public host and with the Funnel marker too, because Funnel and Serve
    connect from a trusted loopback proxy and strip only `Tailscale-*` headers; on a public host Cloudflare Access (a
    signed JWT) remains the only header/token-based sign-in source, and everyone else uses an invite or a share link.
    Serve alone is a third case, and the header cannot show it: `tailscale serve` on the tailnet reaches the board over
    loopback and forwards whatever `X-Forwarded-*` the client sent, while a tagged node has no Serve user login to set
    `Tailscale-User-Login`. So the board asks the CLI (`tailscale serve status --json`, once, cached) whether a Web
    mapping proxies its own loopback port, and when one does, `forwarded_identity` signs nobody in and logs why. Turn
    the setting off and use the Tailscale identity, an invite or a share link; a reverse proxy you run yourself, not
    Serve, is what `forwarded_identity` is for.
  - A Tailscale identity also needs the `X-Forwarded-For` header that Serve itself sets: exactly one address, inside the
    tailnet ranges `100.64.0.0/10` or `fd7a:115c:a1e0::/48`. `addProxyForwardedHeaders` in `ipn/ipnlocal/serve.go`
    (`:1036-1044` at tag `v1.98.1`, commit `2f4c02ee68c459aaea92d8b2dd81a38816823bef`) sets it to the tailnet address
    the request came from (`r.Out.Header.Set("X-Forwarded-For", c.SrcAddr.Addr().String())`). A public proxy in front
    puts the real client address there, so its requests get no Tailscale identity.
  - Every other reverse proxy whose address is in `trusted_proxies` (Caddy, cloudflared, oauth2-proxy and so on) must
    strip `Tailscale-User-*` headers from incoming requests, or `tailscale_identity` must be turned off. Otherwise a
    client of that proxy who sends `Tailscale-User-Login` and a tailnet `X-Forwarded-For` could name a tailnet
    account.
  - `X-Forwarded-User` / `X-Forwarded-Email` from oauth2-proxy, Authelia or similar (`forwarded_identity`, off by
    default). Turn it on only when that proxy strips those headers from incoming requests. It is refused on a public
    host and with the Funnel marker (see above); it is for a board on a private host.
  - Cloudflare Access (`cloudflare_team` and `cloudflare_audience`): the `Cf-Access-Jwt-Assertion` JWT must be RS256,
    signed by a key from your team's certs endpoint, for your application's audience and your team's issuer, and not
    expired. This needs the plugin's optional `crypto` extra (PyJWT with cryptography). With the two settings set and
    PyJWT missing, the server refuses to start and says why.

  A proxy identity signs a person in only after it is linked to an account: the first time they open an invite through
  the proxy, the identity is linked to the new account.
- Sessions. The cookie `mr_session` is HttpOnly and SameSite=Lax. It is also Secure when a trusted proxy reports
  HTTPS. Only a hash of the session id is stored. A session lasts 30 days from its last use: when a use pushes its end
  out (at most once a minute), the server sends the cookie again with a fresh 30-day lifetime. A guest's session lasts
  as long as the share link generation it joined through. Settings → Devices lists your sessions, and "Sign out other
  devices" ends all but the current one, live streams included. Signing out ends the session in the browser's cookie
  even when a proxy identity signed the request in.
- Throttling. Ten failed link attempts from one address within a minute block that address for a minute (429). Behind a
  trusted proxy the address is the last `X-Forwarded-For` hop.
- Access tokens (`hermes mockreview tokens`) are stored as SHA-256 hashes and shown once, when created.
- Audit. Every change to a role, a grant, an invite, a share link or a token writes a row to the `audit` table, in the
  same transaction as the change.

The accounts, sessions, grants and audit rows live in `access.sqlite3` under the data root, next to the review folders.

## Roles

- Instance roles: admin or member. Admins can open and manage every review, and see Members and Invites in Settings.
  The last admin cannot be demoted, and the only admin cannot be deleted.
- Review roles, granted per review or for all reviews, optionally with an end date: viewer (look), commenter (look and
  comment), reviewer (look, comment and send feedback). Only an admin manages a review's share link.
- Guests open a review through its share link with the link's guest role (commenter by default). The server gives each
  guest its own id and a random adjective+animal name (for example "Calm Otter"), unique among that review's guests
  that still hold a live session; the guest may rename it, and it stays a label. Regenerating the link signs out every
  guest of the old link and leaves members alone. Stopping the link does the same, and the old URL then answers like
  any invalid link. Switching the link to `members` also ends every guest session of that review and closes its live
  streams; a stale guest cookie then reads as a dead link, and members' sessions are untouched.
- Roles and grants are read again on every request, so a change applies to the next request.

## Share links and viewer guests

- One link per review. It is signed with itsdangerous and carries the review id and its generation. A missing, stopped,
  regenerated, expired or forbidden link answers with the same dead page, byte for byte, as a review that does not
  exist. The same is true of a `members` link opened by nobody or by a guest: only signed-in people who can already read
  the review are let through.
- A viewer guest may read the board and nothing else. `POST` to `api/submit`, `api/pin-fix`, `api/share`, `api/people`
  and `api/guest-name` is refused with 403 before any work is done. Send, Amend and Build (`api/submit`) are the
  owner's alone: anyone else, a reviewer-role guest or a member with the Reviewer role included, gets 403 ("only the
  owner sends feedback to Hermes; your comments and pins stay on the board for them"), and the board shows them no
  Send or Build button. A reviewer's pin fix is stored quietly for the owner and never wakes Hermes. A reviewer's
  `@Hermes` mention, from a guest or a member, may wake Hermes under the request gate (see
  [Who may wake Hermes](#who-may-wake-hermes)); a commenter's or viewer's mention is a plain comment, and a token and a
  board created with wake-up off never wake anyone. An activating mention wakes Hermes once through the Send wake
  route.
- A viewer's `state.json` carries no submit token, no feedback pins or notes, and only the round the link's version
  picker names (or every round while it says `latest`). The board shows one banner line naming the round.
- Seats are capped per review: 25 live seats, one of them the owner's. A full link refuses new guests with a 409 page
  and sets no session cookie. Guests whose sessions have ended free their seat.
- Regenerating or stopping a link ends its guest sessions and closes their live streams; members' sessions are not
  touched. Switching it from `anyone` to `members` does the same, and every later guest request is refused because the
  live link's audience is no longer `anyone`.

## Exports

A round can be exported as PNG, PDF, HTML or Markdown, with or without the review's comments. Every export route
requires viewer access or higher on the review and writes an `export` audit row server-side. All four formats come from
one static document: the server builds it with an allowlisted serializer (`mockreview/export.py`), so no script, event
handler, external URL, `iframe`, form or SVG from a mock or prototype survives; images are inlined as
`data:image/(png|jpeg|webp|gif);base64,` and the document's own CSP allows nothing but inline style and data images. The
PDF is that document on a print page, the Markdown is that document converted in the browser, and the PNG captures
that flattened document (never the live board, whose mocks live in same-origin iframes SnapDOM cannot see into).
Comment bodies are plain text; the comments section is absent when the toggle is off and PNG captures are always built
with it off.

## What is authenticated

- Wake-up requests to Hermes. Send posts to the Hermes webhook with an HMAC-SHA256 signature over the timestamp and the
  body, using `MOCKREVIEW_WEBHOOK_SECRET`. Hermes rejects a request whose signature is wrong or whose timestamp is more
  than 300 seconds off, and it drops a delivery id it saw in the last hour.
- Every board request, as described above. A submit also needs the review's submit token (from its `state.json`) and a
  role that may send feedback.
- Every POST carries a CSRF token: the `X-CSRF-Token` header (or a form's `csrf` field) must equal the `mr_csrf` cookie
  and, when signed in, that session's own token. A POST with a foreign `Origin` or `Sec-Fetch-Site: cross-site` is
  refused before that.
- The index page (`/`) lists only the reviews the person may open. The owner endpoint (`/api/owner`) answers requests
  from this computer without proxy headers and returns 404 to everyone else. A request whose Host is a public name
  (`public_url` or the Tailscale Funnel URL) is never treated as this computer's owner, even when it
  arrives from 127.0.0.1 without a proxy header: a proxy that forwards without setting `X-Forwarded-For` must not hand
  a stranger the owner account.

## What is not authenticated

- Anyone holding a review's share link while it is live: the link is the guest's credential. Stop or regenerate it to
  cut access.
- A guest's display name: anyone can type any name. The server starts a guest with a random default, and access and
  audit use the server-assigned guest id.
- A request from this computer while `loopback_owner` is on. Any local process can act as the owner. Turn the setting
  off on a shared machine.

## Protections in the server

- Host allowlist. A request whose Host header is not loopback, the bind address, or the host of `public_url`, the
  Tailscale Funnel URL, `public_base`, `local_base` or `lan_address` gets 421. This blocks DNS rebinding. With a
  wildcard bind, any IP-literal Host is accepted too. The list is read at server start. The `public_url` and Funnel
  hosts are also public names: a request on one is never the loopback owner, and `/healthz` answers only
  `{"ok": true}` there. `public_base` is not a public name: it is the tailnet Serve address, and Serve keeps the
  tailnet Host, so a tailnet identity still signs in there.
- Origin check. A POST that carries an Origin header must come from an allowed origin, and `null` (a sandboxed frame) is
  refused with 403. The CLI and curl send no Origin and pass.
- Response headers. The board is served with a Content-Security-Policy that allows only same-origin script, style, image
  and frame sources, plus `nosniff` and `no-referrer`. When a trusted proxy reports HTTPS (`X-Forwarded-Proto: https`)
  the response also carries `Strict-Transport-Security: max-age=31536000`; a plain HTTP response never does. Variant and
  prototype files are served with a `sandbox allow-scripts` policy, so they run with an opaque origin and cannot reach
  the board. A round document gets the network-capable prototype policy only with a valid per-round frame token in its
  path; without one it gets the artifact-grade policy described under "Artifact pages".
- Untrusted content. User text is rendered as text. Mock HTML is parsed without running it, and the board removes
  scripts, frames, forms and event-handler attributes, then runs the result through a second pass, the vendored
  [DOMPurify](../mockreview/board/vendor/README.md) 3.4.17, under the same bans. Anchor links retain only absolute HTTP(S) URLs with no whitespace
  or controls; they open in a new tab with `noopener noreferrer`. Relative and other-protocol hrefs are removed.
  Table cells use text/DOM nodes to link bare absolute HTTP(S) URLs, including multiple URLs inside prose.
  Surrounding text, sentence punctuation and literal HTML are preserved; no cell content is parsed as HTML.
- Artifact pages. An `artifact` block is the one place agent-written script runs in front of a reviewer, so it is
  contained twice. The frame is `sandbox="allow-scripts"` with no `allow-same-origin`: an opaque origin with no access to
  the board's DOM, cookies, storage or `api/submit`, and no popups, forms or top navigation. The page's file carries a CSP
  meta (`default-src 'none'`, inline script and style only, `data:` images, media and fonts, `connect-src 'none'`,
  `form-action 'none'`, `frame-src 'none'`). The same policy, after `sandbox allow-scripts`, is the response header of
  every file in a round that holds an artifact (the block file, `assets/`, `design/`, `decisions.json`), so a page that
  navigates its own frame to another file of the round gets a document with no network either; the header holds even if
  the meta is ignored. An artifact's own round keeps `ARTIFACT_ROUND_CSP` even when the request carries a valid frame
  token. Every other round document is served with the same artifact-grade policy plus an allowance for its own
  round's files
  (`img-src`, `media-src`, `font-src`, `style-src`, `script-src`), and no network: a header on an image or video is
  ignored by the browser, so mock media still loads, but a document with no token may not fetch, frame or post. A
  round's normal prototype policy (network allowed) is granted by a **frame token**: the board mints one signed token
  per round the viewer can see into `state.json` and puts it in the path of every frame document URL,
  `/r/<id>/rounds/<n>/~<token>/<variant>/<path>`, so every relative URL a prototype derives (a sibling page, an
  image under the round's own assets folder, a script one folder up) inherits it. The token names the review, the
  round and the viewer, is signed with the install's key under its own salt, and is bound to the viewer's session
  `id_hash`, to `local` for the loopback owner, or to `user:<id>` for a viewer signed in by a trusted proxy with no
  cookie (re-resolved through `users.get_user` and the normal access decision); a request with a session must present
  that session's token, and a sessionless request is re-resolved from the token's session and must still pass the
  normal access decision. Signing out, regenerating the share link or revoking the grant therefore stops the token
  loading anything. A session or account token lives as long as its binding; only a `local` token carries the
  two-hour `max_age`. A `local` token past that clock is no longer a credential — it grants no read and gets no
  network — but because it is still signed for exactly this review and round it still counts as same-review for the
  CORP/cross-site decision, so the page's own scripts and styles keep loading. The `s` it carries is a viewer
  pseudonym the prototype can read from its own URL — a session
  `id_hash` (already a digest, never the cookie), `user:<id>`, or `local` — so an artifact learns that its viewer is
  the viewer of the round, and nothing more. A valid token authorises round files only, for exactly that review and
  round, and never `state.json`, a board file, `/api/*` or another review; it is redacted from the request log
  (`~…`), and round files keep
  `Referrer-Policy: no-referrer` so it never appears in a third-party `Referer`. Every round file also answers
  `Cross-Origin-Resource-Policy`, because a sandboxed frame (the only thing that renders a prototype or an artifact)
  has an opaque origin: a file under this review's and this round's token answers `cross-origin`, so its own media,
  fonts and `rounds/<n>/a/assets/` keep loading inside the review; a file with no token for this review and round
  answers `same-origin`, and a request for it that a frame makes without such a token is a dead end (a cross-site
  `Sec-Fetch-Site`, which page script cannot forge, on anything that could execute, style or fetch — scripts, styles,
  fonts, `fetch`/XHR, nested frames; a navigation, and the images, audio and video that only render, still load as
  before, because a round page served with the `sandbox` CSP keeps an opaque origin even as a top-level document).
  So one review's prototype cannot load another review's scripts, styles or fetchable content even when both reviews
  are served by the same board, and an outside site cannot embed a round file at all. Known edge: a prototype that
  links with a **root-absolute** path such as `/r/<id>/rounds/1/a/page2.html` drops the token and therefore loads
  with no network, and also loses its own CSS and JS, because those derived requests carry no token and a cross-site
  request for them is a dead end; relative links, which Hermes generates, are unaffected. A navigation to a third-party URL is refused by the
  board's own `frame-src 'self'` (no request is made), and a frame that loads anything but its block file (one that does
  not answer the board's ping, which only the block file's probe does) is reset to the block file up to three times and
  then removed. That reset is best effort: a page that forges the probe's reply keeps its frame, and what holds then is the
  header. Media is bundled in as `data:` URIs under the 25 MB per asset and 50 MB per round caps (the round's copied
  `assets/` folder counts against the same 50 MB), the page is capped at 2 MB, and a missing or oversized asset fails
  the create call. The board accepts only a height report, a tap, a pin edit, `data` writes and a `mark` from the
  frame. A frame message may ask for the thread under the marker it drew, and only for a thread the board already
  lays out for that same frame (same block, same device); opening that way never marks it read, because a frame is
  untrusted HTML and can post any message at any moment — read is set only by a tap on the board's own DOM (a thread
  row, a marker the board drew, a pin the board owns). Page state is accepted only from a frame that is one of the board's artifact frames on the round it is showing
  (a message tagged with an earlier round is ignored), only for that block's id, and only within 64 keys and 8 KB per
  value and 256 KB per round; the server re-caps the same limits and drops unknown or foreign block ids. A frame that
  floods the 40-messages-per-second bucket is ignored, not obeyed. Page state is untrusted reviewer-supplied data: it is
  stored in the reviewer's draft, quoted in the feedback markdown under `## Page state`, and never executed or sanitised
  into markup. It counts as content for enabling Send only: it never decides a mark, never resolves the review gate and
  never submits on its own. `use('marks')` can set a mark only on a markable artifact, and the reviewer still presses
  Send. The clipboard is delegated to the frame by `allow="clipboard-write"`, which enables the page's own Clipboard API
  for every script in the frame while the frame has focus; the board is not in the clipboard path and the frame cannot
  read the clipboard. The probe's `use('clipboard').writeText` adds a rule of its own — a real trusted tap in that frame
  no older than 1 second, else the promise rejects — but a direct `navigator.clipboard.writeText` call from the page, or
  from any third-party script it bundles, skips that rule and can overwrite the clipboard whenever its frame has focus
  (pastejacking). The page's HTML is not sanitised (it must run script); its title, text and caption are plain text.
  `tests/test_artifact_browser.py` attacks this from a real browser
  (top navigation, fetch, XHR, WebSocket, EventSource, image, script, stylesheet, frame and media loads, beacon, form
  post, `window.open`, links, the board's own submit endpoint, `parent.document`, cookies, storage, a message flood) and
  asserts a local listener received no request. `tests/test_artifact_data.py` covers the data channel (the caps, a
  flood, a wrong frame, a stale round, a burst that settles every promise) and the clipboard boundary (the tap rule,
  the focus rule and the blocked read) with a real Chrome.
- Size limits. Requests are capped at 2 MB, `decisions.json` at 3 MB, and block counts, pins and text lengths have fixed
  limits (see [decisions-format.md](decisions-format.md)).
- The comment surface. Comments and threads ([comments.md](comments.md)) are the board's second write path and pass the
  same gate as Send: the Host allowlist, the Origin and `Sec-Fetch-Site` checks, the CSRF token and the 64 kB body cap.
  A role without `comment` is refused before its body is read. The 2000-character text cap, the 200-open-thread and
  40-comments-per-thread caps and the 20-writes-per-minute bucket live in `mockreview/threads.py`, which the board, the
  REST API and the agent tools all call. Comment text is untrusted plain text: the board renders it with `textContent`
  and linkifies bare `http(s)` URLs as DOM nodes, never as HTML, and mentions travel as structured ids rather than as
  parsed text. Only an owner's or a reviewer's `@Hermes` (a reviewer-role guest's included) can set a thread's
  `activated` flag and wake Hermes, once per comment and only when the request gate allows it
  ([Who may wake Hermes](#who-may-wake-hermes)); a commenter, a viewer, a token and a wake-off board never wake
  anyone, and mention wakes share the pin-fix caps.
- The live stream. `GET /r/<id>/events` is access-checked like every other read (a JSON 401 for nobody signed in, the
  same 404 as a missing review for no access) and holds one of 25 seats per review: 24 for anyone, one kept for the
  owner. A stream carries only the events of the review its reader may read; `Last-Event-ID` replays from the retained
  window of that review alone, and an older cursor gets `reset` instead of a guess. Signing out other devices and
  regenerating or stopping a share link close the matching sockets at once.
- Feedback is treated as data. The bundled skill tells the model that quoted feedback is design input and not an
  instruction. A hostile reviewer could still write persuasive notes, so keep
  review links to people you trust.

## Tools for the woken session (`wake_toolsets`)

Hermes runs the session that Send wakes as a webhook session. It keeps that session's safe webhook tools (web search
and extract, vision, clarify) and also holds the `mock-review` plugin toolset: Hermes enables plugin toolsets by
default unless they are default-off, and `mock-review` is not. So the woken session has the `mock_review_*` tools,
which act as the install's owner, for every review on this install. It still has no terminal and no file tools unless
`wake_toolsets` grants them, so it cannot build the next round. By default it posts one line ("Round N feedback is
saved; the build continues in this thread's chat session.") and the origin chat's own session does the build.

`wake_toolsets` (empty by default) lets you grant that session more, for example `mock-review, terminal, file` (the list replaces Hermes's webhook default, so
include `mock-review` to keep the mock_review tools). Hermes reads it from the wake route's `toolsets` key in
`$HERMES_HOME/webhook_subscriptions.json`, the per-route override for webhook sessions. `hermes webhook subscribe` has
no flag for it on purpose, so an agent cannot grant itself tools, and granting them is a manual edit, as Hermes's
webhook docs describe. `hermes mockreview setup` never opens that file: it prints the exact line to add to each wake
route (`"toolsets": ["mock-review", "terminal", "file"],`) and you add it by hand. Setup re-subscribes the routes on
every run, which drops the key, so add the line again after each setup run. Hermes has no command that shows a route's
toolsets, so setup cannot check the edit; its summary says `wake toolsets manual`. The notice route runs no agent and
never gets it. To remove it, empty the setting and re-run setup (the re-subscribe drops the key).

The risk: the woken session acts on text a reviewer typed on the board. Even without `wake_toolsets` it can read and
reply on every review on this install with the owner's access, posted as Hermes; with more tools granted it can do
more. The feedback header and the skill tell the model that quoted feedback is design input, not instructions, but a
model can still be persuaded. Grant only the toolsets the build needs, and only for boards whose reviewers you trust.

A waking board with tools is allowed only while the board itself is gated (the board gate, `wakeguard.exposure` in
`mockreview/wakeguard.py`): the board server must listen on loopback, no `public_url` may be configured, and the board
must not be `public`. With `host` beyond loopback (`0.0.0.0`, `--hosting lan`) anyone on the network reaches the
sign-in page; a public board is open to anyone holding the link. While `public_url` is configured, a TLS proxy (Caddy,
nginx, Cloudflare Tunnel, oauth2-proxy) serves the same loopback port on the internet and leaves no marker this server
can trust on each request, so it makes every board exposed too, whatever its mode. A Tailscale Funnel mapping
(`hermes mockreview public on`) does not refuse a board: Tailscale marks every Funnel request with
`Tailscale-Funnel-Request` and strips a client's copy, so the request gate ([Who may wake Hermes](#who-may-wake-hermes))
tells each Funnel request apart from this computer and the tailnet, and a request through Funnel never wakes a board
with tools or a Desktop chat.

A board made in a Hermes Desktop chat wakes that chat inside its own session, so the owner's Send or `@Hermes` starts a
turn with every tool that session has, whatever `wake_toolsets` says. Such a board is therefore always held to the
board gate (refused at create while the board is not gated, and held at Send when it stops being gated later), and
only the owner wakes it, from this computer or the tailnet: a reviewer's mention there is saved. Hermes also requires
the consent `plugins.entries.mock-review.allow_gateway_injection: true` before the plugin may queue that turn, and setup
sets it only on an explicit yes at a terminal: `--yes` prints the command instead, so an agent cannot grant it to itself.

While `wake_toolsets` is set:

- setup refuses to print the line (exit 1, summary row `wake toolsets failed`) when it was run with `--hosting lan` or
  while `host` is beyond loopback;
- `create` and `add-round` refuse a waking board while `host` is beyond loopback, `public_url` is configured, or the
  board is `public` (409, before anything is created);
- the server holds a wake (the submission stays `pending_notify` with `held, not woken: …` as its last error, and is
  retried) while `host` is beyond loopback, `public_url` is configured, or the board is `public`. The feedback is still
  saved; read it with `mockreview feedback`.

A board with `wake` off is always allowed: it wakes nobody.

The guard reads the setting, not the route file: keep `wake_toolsets` matching what you added by hand. After you empty
it, re-run setup so the route loses its tools too.

## Who may wake Hermes

The board gate above decides whether a board may wake Hermes at all. The request gate (`wakeguard.request_gate`) runs
on every Send, Amend, Build, pin fix and `@Hermes` mention on the board server, and decides from who is asking (the
review role) and how the request arrived:

| who | Send, Amend, Build (`api/submit`) | pin fix (`api/pin-fix`) | `@Hermes` mention |
|---|---|---|---|
| owner, from private ingress | wakes | wakes | wakes |
| owner, from anywhere else | saved, not woken | saved quietly, not woken | wakes on a board that wakes a messaging chat while `wake_toolsets` is empty (the reviewer rule below); else saved |
| reviewer who is not the owner (guest or member) | 403 | saved quietly, never woken | wakes only after the owner has set or re-confirmed Reviewer in Share for that link or grant, and only on a board that wakes a messaging chat while `wake_toolsets` is empty, from any ingress (Funnel included); else saved. A Reviewer link or grant that predates this update is saved, not woken, until re-confirmed. The woken session holds the `mock_review_*` tools for every review on this install, so give Reviewer only to someone you trust with all of them |
| commenter, viewer | 403 | 403 | a commenter's is saved, never woken; a viewer cannot comment |
| an agent's token | — | — | never activates |

The owner is an admin account or this computer's loopback owner. Private ingress means a request with no Funnel marker
that comes from this computer (loopback, no proxy headers, not a public Host), from a tailnet address, or through
Tailscale Serve (a trusted proxy whose `X-Forwarded-For` is one tailnet address). A missing Funnel marker alone is never
enough: a request from a public or LAN address, or through a proxy whose `X-Forwarded-For` is public or a chain, is not
private. Funnel and tailnet Serve share the node's `*.ts.net` name, so the Funnel host name does not count against a
request here; the marker does. When the owner's request is saved, the board shows why ("sent from the public link:
saved, and Hermes wasn't woken. Open the board on the tailnet to send it to Hermes", or the same with "sent from outside
this computer and the tailnet" for a request that came another way), and the owner sends it from the tailnet with
Amend. On a board with `public_url` or LAN hosting and wake-up on, an owner who comes through a proxy that is not
Tailscale Serve is therefore saved, not woken. An owner's `@Hermes` mention is the one exception: off private ingress it
follows the reviewer rule below.

A mention that the gate saves says why ("saved; Hermes wasn't woken because this board's chat has tools a
guest may not trigger"). A Reviewer's `@Hermes` mention is saved with "the owner has not confirmed that this Reviewer
may wake Hermes" until the owner sets or re-confirms Reviewer in Share for that link or grant: choosing Reviewer before
this update did not go through that confirmation, so every existing Reviewer link and grant starts saved, and a Reviewer
invite minted before this update grants its role without the confirmation. Picking any other role clears the
confirmation. The server checks the same rules again when it delivers a queued mention wake,
because the settings, the role and the link can change in between: a mention wake whose author's Reviewer confirmation
is gone, whose share link ended (stopped, regenerated, expired or signed-in people only), or whose board now wakes a
Desktop chat, or whose route now has `wake_toolsets`, fails with that reason and nothing is sent. Every record queued
before this update carries no `by_owner` marker, so it has no proof of where it came from, Funnel or not; it stays
saved until the owner sends it again from private ingress. Reviewer mentions share the
pin-fix caps (`pin_fix_max_per_hour`, `pin_fix_max_per_round`), and a board created with wake-up off never wakes anyone.

In the board, only the owner sees Send and Build; anyone else sees "Only the owner sends this round to Hermes. Your
comments and pins are on the board for them." Choosing Reviewer for someone in the Share dialog (the link's guests, a
person, or a new invite) asks first, because a reviewer can wake Hermes with a mention. That confirmation is also what
lets the Reviewer wake by mention: for a Reviewer link, person or invite made before this update, the Share dialog
marks it as unconfirmed ("Waking off") and offers Confirm Reviewer, which asks the same question before it sets
Reviewer again.

## MCP server (stdio)

`mockreview/mcp.py` serves six board tools and three comment tools to a local coding agent over stdin/stdout. It
opens no socket and listens on no port. The board tools need no identity and hold no token: any local process the user
starts can run them, so they are exactly as trusted as that process. They call `mockreview.api` like the CLI, so review
validation, storage and the origin/wake rules are the same code path. Wake stays off unless the agent supplies an
explicit platform and chat ID, and no tool returns a secret.

The comment tools (`list_comments`, `reply`, `resolve`) are scoped by a personal access token that the client
passes as `--token` or `MOCKREVIEW_TOKEN`. Only a hash of it is stored. The token's user must hold a role on the review:
reviewer or commenter to reply, reviewer (or the install's owner) to resolve; a viewer, a token with no grant and an
unknown or revoked token are refused. The server enforces that reviewer-only rule on the status route too: a bearer
whose role on the review is commenter (or below) gets a 403 instead of resolving or reopening a thread, so an agent
holding a commenter token cannot settle a thread by calling the HTTP route behind the tool's back. A reply is a normal
comment — it appears under that user's name, reaches open
boards through the same SSE stream and is audited (`mcp.reply`, `mcp.resolve`, `mcp.reopen`) under the token's account.
An `@Hermes` mention in an agent's reply is dropped and the thread is never activated, so two agents cannot wake each
other in a loop. The plugin's own `mock_review_list_comments`, `mock_review_reply` and `mock_review_resolve` tools run
in process in the woken session, need no token, write under the agent author `agent:hermes` (the audit rows name the
owner account), and reply with `can_activate=False`: they never
activate a thread or wake anyone either.

A personal access token is also an HTTP bearer credential (`Authorization: Bearer mrp_...`), accepted on loopback requests
and only on the two routes an MCP write needs: `POST /r/<id>/api/threads/<tid>/reply` and `.../status`. On the reply
route the token's role must allow commenting; on the status route it must be reviewer or owner, the same rule the
`resolve` tool applies, rather than the board's broader comment capability. Every other
route — account and admin routes included, and every GET — refuses a bearer with a 403 that never falls back to the
session or the loopback owner, so a leaked agent token cannot mint or revoke tokens, manage users or read the API, and
revoking it still evicts whoever holds it. Those two writes are exempt from CSRF because the header is explicit
authority, not an ambient cookie: a page cannot set an `Authorization` header cross-origin without a preflight the
server never grants (no CORS headers, 501), and the Origin, `Sec-Fetch-Site` and Host checks run before the token is
read anyway. An invalid or revoked bearer, or one sent from any other address, is anonymous and never falls back to the
loopback owner. A bearer request never activates `@Hermes` either: `can_activate` is false for a token even where the
role could submit, and a mention it adds to an already-activated thread wakes nobody.

The token is never logged and never written into the server descriptor (`.server.json`). That file holds the address,
the pid, the data root and one per-start secret, is written 0600 by the account that runs the server, and is ignored if
it is a symlink, readable by group or others, or owned by another account. Before it sends any token, the MCP process
makes the server prove it knows that secret on the very connection that will carry the write: the client picks a nonce
and the server answers `HMAC-SHA256(secret, nonce)` at `GET /api/owner/challenge`, which is answered to local requests
only. A process that merely bound the port named in a stale descriptor — after a crash, say — cannot answer it, so a
token never follows a false success, and a listener swapped in after the challenge never sees the token or gets a fresh
connection to be fooled on. The token goes out on the same socket that answered the challenge; if that connection is
gone before the write, the write is stored instead, never retried on a new one. The result says `delivery: "live"` when
the running board took the write and `"stored"` when it was only persisted for the next load, so a write no board could
receive is never presented as live delivery.

## Secrets

Signing keys are never stored in review files. `MOCKREVIEW_WEBHOOK_SECRET` is resolved by `mockreview/secrets.py` from
exactly one of two sources:

- Inside Hermes, the secret scope and only the secret scope. The scoped value of the caller's profile is read; the process
  environment is never consulted, and another profile's value is never used.
- Outside Hermes, when `agent.secret_scope` cannot be imported (the stdio MCP server has no gateway and no profile
  scope), the process environment. There is no profile scope to protect, so the environment is the deployment's secret
  store.

The fallback is never reached while `agent.secret_scope` imports, and the two are never combined. The wake secret is
server-side: the board server signs the POST, and no tool returns a secret.

Setup writes the webhook secret with `hermes config set`, which stores it in the Hermes `.env`. Setup never prints the
secret: command output is redacted before display, and an existing secret is reused instead of replaced. Reviews, tokens
and feedback are plain files under `$HERMES_HOME/workspace/mock-reviews` with the permissions of your account. Treat that
folder like the rest of your Hermes home.

The signing secret is kept off command lines wherever Hermes can answer a route on its own. `hermes webhook subscribe`
keeps an existing route's secret (or generates one) and prints it, so setup subscribes the routes without `--secret`,
reads the reported value back, and reconciles: a route whose own secret differs from the stored one is re-bound with
`--secret`, and only a run that has to change the value supplies it. Two residues remain, both because Hermes 0.22
accepts a value only as an argument - `hermes webhook subscribe --help` and `hermes config set --help` offer no stdin,
environment or file input, and Hermes's own config command reads the value positionally:

- `hermes config set MOCKREVIEW_WEBHOOK_SECRET …` always carries the value in its argv, on the first install and
  whenever the secret is rotated.
- A route Hermes does not have yet is subscribed without a secret, then re-bound with `--secret` to the value the
  plugin signs with, so a first install puts the value in an argv for the duration of that one call.

Both windows are a single short-lived local process; a user who can read `/proc/<pid>/cmdline` or `ps` on the account
that runs Hermes already has the account, and the `.env` and the route record that hold the value are readable by it
too. Re-running setup, which re-subscribes every route, no longer repeats the value on a command line.

## Hosting modes compared

- `local` with the default `host`: reachable from this computer only.
- `local` with `host` at `0.0.0.0`: reachable from your whole network. Devices other than this computer get the sign-in
  page.
- `tailnet`: reachable from devices on your tailnet, through `tailscale serve`. Tailscale's access rules apply. The
  board server itself stays on loopback. Requests through the proxy are never the loopback owner: tailnet people sign
  in with an invite, and later by their linked Tailscale identity.
- `public`: reachable from the internet through Tailscale Funnel or any TLS proxy. The board server still binds
  loopback and carries the same accounts, Host allowlist, body and connection limits and response headers as any other
  mode. A public Host is never the loopback owner, `/healthz` is minimal there, and a Tailscale identity is never
  accepted on a public host, so tailnet people sign in by invite or share link like everyone else. `forwarded_identity`
  is refused there too (Cloudflare Access identities still work). `wake_toolsets` is refused (or the wake is held) for
  a public board because anyone holding the link could press Send into a session with tools. See
  [public-hosting.md](public-hosting.md).

## Reporting a vulnerability

Open a private security advisory on the GitHub repository (Security tab, "Report a vulnerability"). Please do not post
details in a public issue before a fix is out.
