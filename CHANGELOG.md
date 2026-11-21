# Changelog

All notable changes to this project are listed here. The format follows Keep a Changelog, and the project follows
semantic versioning.

## [2.1.4](https://github.com/apelabs-dev/hermes-mock-review/compare/v2.1.3...v2.1.4) (2026-11-21)


### Bug Fixes

* **threads:** a delivered @Hermes mention records its working state whatever pin_fix says ([#59](https://github.com/apelabs-dev/hermes-mock-review/issues/59)) ([d18b2e6](https://github.com/apelabs-dev/hermes-mock-review/commit/d18b2e6ca7496c582ef190925d3c8ff752ef8097))

## [2.1.3](https://github.com/apelabs-dev/hermes-mock-review/compare/v2.1.2...v2.1.3) (2026-11-20)


### Bug Fixes

* **board:** show the reviewed project's icon in the header bar ([#56](https://github.com/apelabs-dev/hermes-mock-review/issues/56)) ([2a6efff](https://github.com/apelabs-dev/hermes-mock-review/commit/2a6efff6e0dc2ce38c7793bad6bf59632b9d1261))
* **desktop:** render design-profile mocks styled in the Mocks tab viewer ([#58](https://github.com/apelabs-dev/hermes-mock-review/issues/58)) ([8cdd9b2](https://github.com/apelabs-dev/hermes-mock-review/commit/8cdd9b2444800302441fbb5e262f9dfb65130266))

## [2.1.2](https://github.com/apelabs-dev/hermes-mock-review/compare/v2.1.1...v2.1.2) (2026-11-20)


### Bug Fixes

* a new pin's ghost marker survives a redraw while its note is open ([#46](https://github.com/apelabs-dev/hermes-mock-review/issues/46)) ([ff03966](https://github.com/apelabs-dev/hermes-mock-review/commit/ff03966589f80573b986a27ebbd379e0e6ef0792))
* a second click in an artifact or prototype frame keeps the new pin's ghost ([#50](https://github.com/apelabs-dev/hermes-mock-review/issues/50)) ([e7988ea](https://github.com/apelabs-dev/hermes-mock-review/commit/e7988ea7a1bfeea0c6f2867f2bfbc08d40f0e9b1))

## [2.1.1](https://github.com/apelabs-dev/hermes-mock-review/compare/v2.1.0...v2.1.1) (2026-11-19)


### Documentation

* README and AGENTS.md list nine agent tools, not six ([#42](https://github.com/apelabs-dev/hermes-mock-review/issues/42)) ([961c250](https://github.com/apelabs-dev/hermes-mock-review/commit/961c2505c411df73374589e6f9a6ac1ca08467ac))

## [2.1.0](https://github.com/apelabs-dev/hermes-mock-review/compare/v2.0.0...v2.1.0) (2026-11-19)


### Features

* Desktop Mocks tab, @Hermes thread wake, sign-in fixes and polish ([#40](https://github.com/apelabs-dev/hermes-mock-review/issues/40)) ([9398a8d](https://github.com/apelabs-dev/hermes-mock-review/commit/9398a8d365827724c51868acc2e1b9ce07ef6c51))

## [2.0.0](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.2.0...v2.0.0) (2026-11-18)


### ⚠ BREAKING CHANGES

* here.now hosting and the here.now board password are removed, replaced by accounts with single-use invite links and per-review roles, and public hosting is now `mode: public` behind a TLS endpoint (Tailscale Funnel or any other TLS proxy). Python 3.9 and 3.10 are dropped: 2.0.0 requires Python 3.11 to 3.14, the same range Hermes supports. Run `hermes mockreview migrate` once to convert existing here.now reviews to `local` or `tailnet`.

### Features

* **auth:** accounts without board passwords: single-use invite links, one-time sign-in links, proxy identities, sessions, CSRF tokens, admin and member roles, per-review grants with end dates, throttling, audit, and an `access.sqlite3` store ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** artifact pages can keep their controls and reach Hermes through `window.mockreview.use('data')`, `use('marks')` and `use('clipboard')`, with page state re-validated by the server and carried into feedback ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** page mode fills the board window edge to edge under the header, keeps the page's own background, scrolls inside its frame, and an artifact block can opt into the same frame with `"display": "page"` ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** Settings shows Members, Invites and Devices, guests get a name pill, and the board offers only what the person's role allows ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** the thread popover at its pin, a Comments overlay with unread dots and a Resolved toggle, an Activity feed, a presence avatar stack, and server-read unread state for every frame ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **cli:** `login`, `users`, `invite`, `admin login-link`, `share` and `tokens` commands ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **comments:** a pin can name any word or element on the board with a breadcrumb label or a quoted `TextQuoteSelector` chip, re-resolving after reload and following a Desktop/Mobile switch ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **comments:** a pin is a thread in the data root's SQLite database with plain-text comments, replies, edit and delete, resolve and reopen, and server-enforced limits shared by the REST API, the board and the agent tools ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **comments:** one composer for a new pin and a thread reply, with the Send to Hermes box, the round send button, mention syncing, and a three-icon thread popover with copy-link `?thread=<id>` support ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **export:** PNG, PDF, HTML and Markdown exports, with signed per-round frame tokens ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **guest:** a share-link guest starts with a server-assigned random adjective+animal name, unique among the review's live guests and still renameable ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **hosting:** `mockreview migrate` converts reviews from the removed here.now hosting to `local` or `tailnet` ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **hosting:** `public` mode with `hermes mockreview public status|on|off`, one Tailscale Funnel HTTPS port mapped to the loopback board, and `public_url` plus `trusted_proxies` for any other TLS proxy ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **live:** `GET /r/<id>/events` streams server-sent events with `hello`, `presence` and `typing` frames, persisted thread and comment events, `Last-Event-ID` replay and heartbeats ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **mcp:** comment tools with per-user access tokens: `list_comments`, `reply` and `resolve` act as the account behind `--token` / `MOCKREVIEW_TOKEN` and enforce the same roles as the board ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **mcp:** when a board server runs for the same store, `reply` and `resolve` execute in that server over loopback so an already-open board receives the live frame ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **media:** round assets answer HTTP Range requests with `206` and `Content-Range` and advertise `Accept-Ranges: bytes`, so audio and video get a duration and can be scrubbed ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **pins:** a reviewer can ask Hermes to patch one block of the current round in place instead of sending a new round, with coalesced wakes, revision checks and per-hour and interval caps ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* release 2.0.0 with accounts, live threads and public hosting ([#35](https://github.com/apelabs-dev/hermes-mock-review/issues/35)) ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** `hermes mockreview share --audience {anyone,members} --round {latest,<n>}` and a full URL in its output, built from the review's own host mode ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** a viewer guest's `state.json` carries one round, no submit token and no pins or notes, and viewer links can never submit or wake Hermes ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** one Share dialog for people, invite role and expiry, live grants, link audience, guest role and round, Copy/Regenerate/Stop, live seats and a viewer round picker ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** share links for guests, and one flat user directory with admin and member flags ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **tools:** `mock_review_patch_block` on the stdio MCP server and the Hermes plugin patches one block's HTML in place and marks the cited pin fix requests Done ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **ui:** the desktop Comment toggle is icon-only with the `Comment (C)` / `Exit comment mode (C)` labels, while the phone bottom bar is unchanged ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **ui:** the whole app wears the approved 2.0 design, including the board header and More menu, the phone bottom bar, grouped Home reviews with search, Settings, Share, comments, sign-in and error pages ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))


### Bug Fixes

* **access:** `users` ids are never reused, so a deleted account's comments never become the next invitee's ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **access:** a share-link seat counts a guest who used the link in the last 15 minutes, not every stored guest session, so crawler and preview fetches cannot fill the link for a year ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **access:** a sign-in link opens a session and leaves the device's proxy identity unlinked, so "sign out everywhere" really ends that device ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **access:** an open board stream ends when its grant is revoked, re-granted lower or expires, and so does a stream signed in by a linked proxy identity ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **auth:** a public board no longer signs anyone in from an `X-Forwarded-Email` / `X-Forwarded-User` header, and `hermes mockreview public on` refuses while forwarded identity is on ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **auth:** frame tokens are bound to their viewer ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **auth:** the Funnel identity claim is corrected: Funnel ingress is marked with `Tailscale-Funnel-Request: ?1`, sends no identity headers, and a public host never accepts a Tailscale identity ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** a full-width page artifact measures the layout viewport, so it no longer shifts half a scrollbar left on Linux and Windows ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** a question block refreshes through its own section (no TypeError on load or on answering), and a markable artifact renders its Approve / Change / Skip row ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** every board avatar takes a deterministic per-person colour from an 8-hue palette that clears 4.5:1 in both themes, and the More menu no longer paints its presence avatars with a near-invisible ink ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** keep pins, marks and page notes stable across rounds, page mode and phone widths ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** page mode's docked footer is one row of three zones — round tabs, the decide control with its note, the tally and Send — every control 40 px, wrapping to two rows under 720 px ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** page mode's docked footer keeps a Comment toggle on a phone, keeps its one row of three zones down to 720 px, and stays two rows under 720 px with many rounds ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** pins paint above a mock's own z-indexed content and above a table's sticky headers, so a saved pin never looks like it vanished ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** replayed live events no longer double-count the thread activity badge, and the thread listing reads one consistent snapshot ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** the page-mode footer keeps the round you are viewing scrolled into its round-tab strip ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** the probe captures the DOM methods and getters once at load, so a page cannot hijack a click target or empty its anchor scope ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **board:** the viewed round tab stays fully inside the round strip when the strip has a fractional width, as on the phone layout at 1024 px ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **bundle:** the round's 50 MB budget counts every emitted `data:` URI once at any nesting depth, so a page that repeats one image is refused instead of writing far more than the round allows ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **checkout:** keep the vendored board files byte-exact on every checkout, so a Windows git install serves the pinned DOMPurify and export assets ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **check:** print a test group's failures as soon as it fails, so a CI job stopped by its time limit still shows them ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **cli:** `share <id> update` and `share <id> regenerate` keep the stored deadline when `--expires` is omitted, and `--expires never` clears it ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **deps:** accept PyJWT 2.13+ so the plugin installs next to Hermes core ([#37](https://github.com/apelabs-dev/hermes-mock-review/issues/37)) ([3d733cf](https://github.com/apelabs-dev/hermes-mock-review/commit/3d733cfbf9201f6d97d9af114076eb370aac878d))
* **export:** HTML, Markdown and PDF exports carry the review's real comment threads with replies, resolved state and round scoping instead of an empty "No comments." ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **hosting:** a tailnet Serve address is no longer a public name, so a linked Tailscale identity signs in on the one Host Serve forwards, and a request carrying Serve's `X-Forwarded-For` is still never the loopback owner ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **live:** closing a live stream now also wakes a send stuck on a client that stopped reading on Windows, so a stalled browser can no longer hold a server thread ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **live:** closure completion is linearizable per subscription, so a second successful sign-out, link change or policy change can no longer skip an unfinished close and leak an event from a restricted round ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **mcp:** `reply` and `resolve` report `delivery: "live"` or `"stored"`, so a write no running board could receive is no longer presented as live delivery ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **mcp:** a personal access token used as an HTTP bearer authenticates only the two routes an agent write needs, and every other route refuses it with a 403 that never falls back to the session or the loopback owner ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **mcp:** the `.server.json` descriptor carries one per-start secret and is written 0600, and the MCP process makes the server prove it knows the secret before it sends its token ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **mcp:** the reviewer-only rule for an agent's token is enforced by the server on the status route too, so a commenter's bearer is refused instead of resolving or reopening a thread ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **mcp:** the token for a loopback `reply`/`resolve` goes out on the connection that passed the descriptor challenge, and a write whose connection drops is stored instead of retried on a fresh one ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **migrate:** a tailscale or local review that still carries a stale here.now record keeps its own mode instead of being re-hosted ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **public:** `public on` refuses any HTTPS port that already has a non-board handler instead of publishing it ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **server:** a 404 under a `rounds/<n>/<v>/` path always serves the dead page with the artifact-grade header, so a forbidden review and a missing one answer identically ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **server:** a non-ASCII digit in `?round=`, `Last-Event-ID`, `Content-Length` or an id path now takes the intended answer instead of an `int()` traceback in the 500 log ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **server:** a POST's `Origin` must match the request's scheme and port, not only its host, so `http://localhost:3000` cannot post to a board on `127.0.0.1:8790` even with a planted `mr_csrf` cookie ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **server:** an interrupt that lands after the request thread has started releases its connection slot once, not twice ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **server:** every round file of a review that has an artifact round now carries the artifact-grade header, while the round's own images, media, fonts, styles and scripts still load ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **server:** invite, sign-in and share link tokens are redacted from the log in every percent-encoded spelling the router accepts ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **server:** the one capped parse behind the mention, id, query, header, export and share sites covers every non-ASCII digit and digit runs longer than 18, so they answer 400, 404, 411 or the default instead of a 500 ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** a guest's Send and pin-fix are stored and never wake Hermes; the owner starts the session, and a guest can never replace a member's feedback ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** a person with only an all-reviews grant shows the inherited role and no Remove button, and removing a review-scoped override leaves the inherited access in place ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** a viewer guest whose link is pinned to a round reads exactly that round on every thread surface, and the live stream drops other rounds while keeping its cursor ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** changing a live link's round or guest role closes that review's already-open guest streams, and an in-flight stream request that arrives afterwards is refused ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** opening a live stream re-reads the request's authorization before the read scope and again before registration, so a revocation can never be overtaken by a connect that started earlier ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** share links and invite URLs follow the review's host mode: local, tailnet through `public_base`, or public through `public_url`, then Funnel, then `public_base` ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** switching a link to `members` ends the guest sessions it already issued and closes their live streams ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** the Share button and a viewer's pinned-round notice appear on prototype and decisions rounds as well as the legacy board ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **share:** the Share dialog's invite role and expiry are their own controls, independent of the guest link's role and expiry, and work before any link exists ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **store:** never copy dot entries or symlinked sources into a review ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **store:** read and replace `review.json` under one lock, so a save on Windows no longer fails while a reader has the file open ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **submit:** the round's feedback files are written before the review record says the round was submitted, so a reader never sees a submitted round without its feedback ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **ui:** Comments and Activity close More and its phone backdrop before their modal opens, with focus moved into the overlay, Tab trapped, Escape closing and focus returned; a theme change re-renders every reviewed mock; unread pins stay brand indigo; the section-jump nav is sticky again ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))
* **wakeguard:** a configured `public_url` makes every board that can wake Hermes count as exposed, not only `public` boards ([61127dd](https://github.com/apelabs-dev/hermes-mock-review/commit/61127ddcda8d35c0f70c08b76a1d40c93f192942))

## [1.2.0](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.10...v1.2.0) (2026-11-11)


### Features

* **mcp:** stdio MCP server so Claude Code, Codex and OpenCode can open review boards ([#27](https://github.com/apelabs-dev/hermes-mock-review/issues/27)) ([9fcaf79](https://github.com/apelabs-dev/hermes-mock-review/commit/9fcaf798214c2ec3e3a88d8bf42ac9f2a7da19bb))
* plan boards with Build, question cards, working mock controls and brand enforcement ([#32](https://github.com/apelabs-dev/hermes-mock-review/issues/32)) ([3f74298](https://github.com/apelabs-dev/hermes-mock-review/commit/3f74298b70d89d4becd108df30bd5b2715aa334f))


### Bug Fixes

* **mcp:** pin the MCP server's stdio to UTF-8 so a non-ASCII title cannot kill or mangle it ([#29](https://github.com/apelabs-dev/hermes-mock-review/issues/29)) ([e75f952](https://github.com/apelabs-dev/hermes-mock-review/commit/e75f9527a08f9ad16e586ffb2bd1d2f62fe4cef0))
* **mcp:** resolve the wake secret from the environment outside Hermes ([#30](https://github.com/apelabs-dev/hermes-mock-review/issues/30)) ([708d2ce](https://github.com/apelabs-dev/hermes-mock-review/commit/708d2ce7cda4cc7f861077fc8134ba1044de2718))
* **rounds:** add-round refuses a round that re-asks what the last round settled ([#34](https://github.com/apelabs-dev/hermes-mock-review/issues/34)) ([ec11454](https://github.com/apelabs-dev/hermes-mock-review/commit/ec11454b90ac82b237b7518fe3f0beee9fce188e))
* **secrets:** decide Hermes presence by the top-level package, so a broken parent still fails closed ([#31](https://github.com/apelabs-dev/hermes-mock-review/issues/31)) ([69baeab](https://github.com/apelabs-dev/hermes-mock-review/commit/69baeab20bb8d20082c185892e0f04e3f1591f04))

## [1.1.10](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.9...v1.1.10) (2026-11-05)


### Bug Fixes

* close a here.now review whose site is already gone ([#25](https://github.com/apelabs-dev/hermes-mock-review/issues/25)) ([9188174](https://github.com/apelabs-dev/hermes-mock-review/commit/918817491bfd6c6cb61e0328484cdbb027499a5b))

## [1.1.9](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.8...v1.1.9) (2026-11-05)


### Bug Fixes

* retry a here.now Site Data poll after its outage window and name the Cloudflare error ([#23](https://github.com/apelabs-dev/hermes-mock-review/issues/23)) ([6828350](https://github.com/apelabs-dev/hermes-mock-review/commit/6828350832e0c381ab5d3cd387cc68aab83e74cd))

## [1.1.8](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.7...v1.1.8) (2026-11-05)


### Bug Fixes

* log a transient here.now poll failure as a warning until it repeats ([#21](https://github.com/apelabs-dev/hermes-mock-review/issues/21)) ([c6b8753](https://github.com/apelabs-dev/hermes-mock-review/commit/c6b875311c754f0ec6c41f1634cbd9b6dca0e1bb))

## [1.1.7](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.6...v1.1.7) (2026-11-03)


### Bug Fixes

* open "Try it live" rounds on Use it so clicks work the page ([#16](https://github.com/apelabs-dev/hermes-mock-review/issues/16)) ([e704997](https://github.com/apelabs-dev/hermes-mock-review/commit/e70499710b51b26def5b0ad2ea1c34903418d894))
* retry the review.json rename on Windows while a reader holds the file ([#19](https://github.com/apelabs-dev/hermes-mock-review/issues/19)) ([20b410f](https://github.com/apelabs-dev/hermes-mock-review/commit/20b410f5719dadde3f53e3e345d2d83ad108091a))

## [1.1.6](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.5...v1.1.6) (2026-11-03)


### Bug Fixes

* bundle password-gated here.now prototypes and verify them in a real browser ([#14](https://github.com/apelabs-dev/hermes-mock-review/issues/14)) ([3e099bc](https://github.com/apelabs-dev/hermes-mock-review/commit/3e099bcd7e6d93741c496fed5270ec28d9f7a952))

## [1.1.5](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.4...v1.1.5) (2026-11-02)


### Bug Fixes

* print the wake route's toolsets line instead of editing webhook_subscriptions.json ([#12](https://github.com/apelabs-dev/hermes-mock-review/issues/12)) ([26285a0](https://github.com/apelabs-dev/hermes-mock-review/commit/26285a0ddbb3b5364e8e81fdf38b6d1236bcd669))

## [1.1.4](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.3...v1.1.4) (2026-11-02)


### Bug Fixes

* let password-gated here.now boards wake with wake_toolsets; guard LAN boards and password-off ([#10](https://github.com/apelabs-dev/hermes-mock-review/issues/10)) ([b6a561a](https://github.com/apelabs-dev/hermes-mock-review/commit/b6a561a26233f7dcc35a620dcac41bb8efa3ed97))

## [1.1.3](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.2...v1.1.3) (2026-11-02)


### Bug Fixes

* let the woken session build on opt-in, and stop spurious stalled pings ([#8](https://github.com/apelabs-dev/hermes-mock-review/issues/8)) ([0ed205e](https://github.com/apelabs-dev/hermes-mock-review/commit/0ed205ecaa3758475fe7f8c9c20fad9df1e8b83a))

## [1.1.2](https://github.com/arspesk/hermes-mock-review/compare/v1.1.1...v1.1.2) (2026-10-28)


### Bug Fixes

* make links inside mocks clickable ([#4](https://github.com/arspesk/hermes-mock-review/issues/4)) ([f25c6e9](https://github.com/arspesk/hermes-mock-review/commit/f25c6e916fea74016d0ac1c06a610a71af606ccf))

## [1.1.1](https://github.com/arspesk/hermes-mock-review/compare/v1.1.0...v1.1.1) (2026-10-27)


### Miscellaneous Chores

* release 1.1.1 ([682388a](https://github.com/arspesk/hermes-mock-review/commit/682388a4d2e78c3d7409ca9bb87c130553f1d242))

## [1.1.0] - 2026-10-27

### Added

- New here.now boards default to a server-side password gate. Four random words are stored per review and returned
  with create/add-round/publish results for sharing beside the link. Global, per-create and live password controls
  include CLI flags, the create tool argument and a slash subcommand.
- Password intent/application status, sanitized failures, preserved slugs and serialized publish/toggle/close support
  recovery without claiming unconfirmed protection. Legacy reviews retain their public behavior until explicitly enabled.

### Fixed

- Absolute HTTP(S) links in mock/option HTML remain clickable with safe new-tab attributes, without creating pins.
  Bare HTTP(S) URLs in table cells become DOM-created links, including multiple URLs embedded in prose;
  surrounding text, sentence punctuation and literal HTML stay unchanged.

## [1.0.1] - 2026-10-23

### Fixed

- `setup --hosting tailnet` no longer proposes a route that takes over another app. It reads `tailscale serve status`:
  a route that already reaches the board is used as `public_base` as it is; otherwise the printed `tailscale serve`
  command uses the default HTTPS port only when nothing is served there, and else the board's own port.
- On macOS, when the `tailscale` symlink crashes ("bundleIdentifier is unknown"), the board and setup run the Tailscale
  app's own binary, and setup prints the command with that path. Before, tailnet hosting silently fell back to local
  links.
- The setup summary says `hosting skipped` when tailnet hosting was asked for but Tailscale could not be read; it said
  `ok`.

## [1.0.0] - 2026-10-23

First public release.

### Added

- Review boards drawn from a `decisions.json` file: decision, text, callout, cards, mock, table and recap blocks, with
  inline mocks at desktop, tablet and phone widths in light and dark, and per-project design profiles.
- Per decision marks (Approve, Change, Skip), notes, pins on mocks, and amend after Send.
- A signed webhook wake-up that returns the feedback to the chat the review was created from, with retries and
  duplicate protection.
- An instant "got it, working on round N" ack posted to the origin chat right after the wake-up is accepted (and mirrored
  into that chat's session), plus a `stale_after_minutes` watchdog (default 15, 0 = off) that pings the chat once when a
  sent round gets no new round. Both use a deliver-only `<webhook_route>-<platform>-notice` webhook route that
  `hermes mockreview setup` creates next to each wake route, so an existing install gets it on the next `setup`. A board
  that is sent keeps checking for the next round (every 30 seconds, for up to two hours) and shows the stalled hint and a
  "Round N+1 is ready" button without a reload.
- Four agent tools (`mock_review_create`, `mock_review_add_round`, `mock_review_feedback`, `mock_review_list`), the
  `hermes mockreview` command and the `/mockreview` slash command.
- `hermes mockreview setup`, an onboarding wizard that checks Hermes, creates the signing secret and one wake route per
  platform, picks hosting, enables autostart, validates the plugin and creates a test review. It supports `--dry-run` and
  `--yes`, and it is safe to run again.
- Three hosting modes: `local`, `tailnet` (through `tailscale serve`) and `herenow` (public site with Site Data, published
  through the official here-now skill).
- `autostart`: the board server runs on a background thread inside the Hermes gateway process. It is off until setup turns
  it on. `hermes mockreview serve` runs the same server in the foreground, which is the supported way to run on Windows.
- A settings table with environment overrides and a `hermes mockreview settings` view that shows where each value came
  from.
- Host and Origin checks on the board server, a Content-Security-Policy on every page, sanitized mock HTML, and a
  local-only index page.
- Documentation: README with disclosures, generated settings and command references, and guides for hosting, security,
  troubleshooting and the decisions format.
- A generic bundled `mock-review:usage` skill.
- Continuous integration: the test suite on Linux, macOS and Windows with Python 3.9, 3.11, 3.12 and 3.14,
  `hermes plugins validate` against Hermes 0.22.0, and a secret and identifier sweep of the tree and the full history
  (`scripts/check_identifiers.py`).
- MIT license.

### Security

- Secrets are read only through the Hermes secret scope (`MOCKREVIEW_WEBHOOK_SECRET`, `HERENOW_API_KEY`), and setup never
  prints them.
