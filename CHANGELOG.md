# Changelog

All notable changes to this project are listed here. The format follows Keep a Changelog, and the project follows
semantic versioning.

## [1.2.0](https://github.com/apelabs-dev/hermes-mock-review/compare/v1.1.10...v1.2.0) (2026-11-06)


### Features

* **mcp:** stdio MCP server so Claude Code, Codex and OpenCode can open review boards ([#27](https://github.com/apelabs-dev/hermes-mock-review/issues/27)) ([9fcaf79](https://github.com/apelabs-dev/hermes-mock-review/commit/9fcaf798214c2ec3e3a88d8bf42ac9f2a7da19bb))


### Bug Fixes

* **mcp:** pin the MCP server's stdio to UTF-8 so a non-ASCII title cannot kill or mangle it ([#29](https://github.com/apelabs-dev/hermes-mock-review/issues/29)) ([e75f952](https://github.com/apelabs-dev/hermes-mock-review/commit/e75f9527a08f9ad16e586ffb2bd1d2f62fe4cef0))
* **mcp:** resolve the wake secret from the environment outside Hermes ([#30](https://github.com/apelabs-dev/hermes-mock-review/issues/30)) ([708d2ce](https://github.com/apelabs-dev/hermes-mock-review/commit/708d2ce7cda4cc7f861077fc8134ba1044de2718))
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
