# AGENTS.md

Guidance for coding agents (and people) changing this repository. mock-review is a Hermes plugin that serves review
boards for UI mockups and wakes the agent when the reviewer presses Send. User documentation is in `README.md` and
`docs/`.

## Layout

- `plugin.yaml`: the Hermes manifest. `config_schema:` is generated from `mockreview/settings.py`.
- `__init__.py`: the plugin entry point, `register(ctx)`. It registers six tools, the `hermes mockreview` command, the
  `/mockreview` slash command, the `usage` skill and the webhook platform handler. It starts nothing itself.
- `skills/usage/SKILL.md`: the skill bundled with the plugin (`mock-review:usage`). It is written for the agent that
  calls the tools and must stay generic.
- `bin/mockreview`: runs the same CLI from a checkout, without Hermes.
- `pyproject.toml`: the dependencies Hermes installs (itsdangerous) and the optional `crypto` extra (PyJWT).
- `mockreview/`: the implementation: the standard library plus itsdangerous.
  - `settings.py`: the `SPEC` table (every setting, its type, range, default, env var) and the resolver
    (call argument, then env, then Hermes config, then default). A strict fallback parser reads `config.yaml` when PyYAML
    is missing.
  - `cli.py`: every subcommand; `setup.py` is the onboarding wizard.
  - `mcp.py`: the stdio MCP server for other coding agents: UTF-8 JSON-RPC on stdout, logs on stderr, no listening
    socket of its own.
  - `runtime.py`: the running board server's descriptor (address plus a per-start secret, owner-readable only), so
    the MCP process can route comment writes through the server for live delivery. The token is sent only after the
    listener proves the secret; never follow a redirect or consult a proxy there.
  - `api.py`: the operations behind the CLI and the tools. `store.py`: review files, `decisions.json` validation, design
    profiles. `feedback.py`: validation and markdown for submitted feedback. `service.py`: submit handling shared by the
    local server and the delivery passes. `migrate.py`: converts reviews from the removed 1.x hosting mode and owns the
    obsolete settings `setup.py` scrubs.
  - `server.py`: the HTTP board server and the background worker. `autostart.py`: runs that server on a thread inside the
    gateway. `owner.py`: decides which store a call writes to and checks the wake route. `public.py`: `mockreview
    public status|on|off`, one Funnel port to the loopback board, refusing a port that is not exclusively the board's
    (any foreign handler, including a TCP forward) before any mutation. `sse.py`: server-sent events plumbing (headers,
    frames, heartbeats) that the live-update layer drives.
  - `notify.py`: the signed wake-up POST. `secrets.py`: the only way to read a secret. `config.py`, `locks.py`: paths,
    URLs and a cross-platform file lock.
  - `db.py`: the SQLite store (`access.sqlite3`): one connection per thread, WAL, `user_version` migrations.
    `users.py`: accounts, invite/login/share links, sessions, grants, tokens, audit. `access.py`: the one access check
    (`resolve`). `auth.py`: who is asking (session, proxy identity, loopback owner), CSRF and throttling. `pages.py`: the
    sign-in, invite and dead-link pages. `streams.py`: the hook that closes live streams when sessions or links end.
  - `board/`: the static board UI (`index.html`, `board.js`, `board.css`) and the Settings page (`settings.js`,
    `settings.css`).
- `docs/`: user docs. `settings.md` and `cli.md` are generated. `img/` holds the README screenshot.
- `scripts/gen_docs_reference.py`: regenerates `docs/settings.md` and `docs/cli.md`. `scripts/check_docs.py`: checks the
  docs against the tree. `scripts/gen_plugin_manifest.py`: rewrites `config_schema:` in `plugin.yaml`.
  `scripts/check_identifiers.py`: the secret and identifier sweep (tree or full history). `scripts/sweep-baseline`: the
  commit after which the history sweep checks private tokens. `scripts/sse_probe.py`: the reproducible
  SSE-through-a-proxy evidence tool.
- `tests/`: pytest suite.
- `.github/workflows/ci.yml`: the test matrix (Linux at Python 3.11 and 3.14; macOS and Windows at 3.14), `hermes plugins
  validate` through Hermes's own `plugin-validate` action, and the sweep over the tree and the history.

## Commands

    python -m pytest -q                       # needs pytest and pyyaml
    python scripts/gen_plugin_manifest.py     # after editing SPEC in settings.py; rewrites plugin.yaml in place
    python scripts/gen_docs_reference.py      # after editing SPEC or any CLI flag or help text
    python scripts/gen_docs_reference.py --check
    python scripts/check_docs.py              # every documented command, path and example against the tree
    python scripts/check_identifiers.py       # secrets and identifiers in the files tracked at HEAD
    python scripts/check_identifiers.py --history   # the same over every blob and commit message (needs full history)
    python scripts/check_identifiers.py --history --since <rev>   # private tokens only after <rev>

The sweep reads extra private tokens, one per line, from `MOCKREVIEW_SWEEP_TOKENS` (CI fills it from a repository secret
of the same name); without it only the generic denylist runs. It prints `path:line rule-id`, never the matched text.
History mode checks private tokens only after the commit recorded in the swept repository's `scripts/sweep-baseline`
(one sha line; `#` comments are allowed). Commit messages of commits listed by `git rev-list --all --not <baseline>` and
blobs not reachable from the baseline get both rule sets; older commit messages and unchanged pre-baseline blobs get the
generic denylist only, so already-published history is not re-flagged. `--since REV` overrides the file and is a usage
error in tree mode; without a baseline file private tokens sweep all history. The stderr mode line names the baseline
or says `no scripts/sweep-baseline`, and still reports the commit and path object counts. Exit codes: 0 clean, 1 hits,
2 usage error (not a git repository, an unknown revision, `--since` without `--history`, a baseline that does not
resolve to a commit or is not reachable from any ref, a baseline file without a sha line, a shallow history); an empty
token list while `MOCKREVIEW_REQUIRE_TOKENS` is truthy also exits 2 and prints on stderr `error: MOCKREVIEW_REQUIRE_TOKENS
is set but MOCKREVIEW_SWEEP_TOKENS is empty: ...`. CI sets that variable on non-fork runs, so a missing repository secret
fails the sweep step there. The 2.0.0 working branches contain pre-SWEEP commits with private names, so the 2.0.0
release must reach GitHub as squashed commit(s) on top of the baseline and never as the raw epic history.

The two docs scripts need the `hermes` CLI (0.22.0 or newer). They build a throwaway Hermes home next to the real one and
delete it afterwards. Hermes refuses a `HERMES_HOME` outside its data directory, so do not point them elsewhere.

## Invariants

- Secrets go through `mockreview/secrets.py` and nowhere else. Inside Hermes that means the secret scope and only the
  secret scope: never read a credentials file, never read `os.environ`, never print a secret, never put one in a log, a
  test fixture name or a doc. Outside Hermes (the stdio MCP server has no gateway) `get_secret` falls back to the process
  environment because there is no profile scope to protect; the fallback is never reached while `agent.secret_scope`
  imports. `setup.py` redacts subprocess output.
- Under multiplexing (`agent.secret_scope.is_multiplex_active()`) every board request and every Worker tick binds the
  owning profile's secret scope through `secrets.owner_scope`, rebuilt on each entry from the home captured when the
  server started (`config.hermes_home()`). Never fall back to `os.environ`, never use another profile's scope (not even
  the one the starting context carried), and never call `set_multiplex_*` from plugin code. Without multiplexing nothing
  is bound.
- `register()` and plugin import start no thread, socket or subprocess. The board server starts only from
  `autostart.gateway_factory` (inside the gateway, when `autostart` is true) or from `hermes mockreview serve`.
- The server binds loopback by default. `make_server` refuses any host outside `ALLOWED_HOSTS`. Every request passes the
  Host allowlist, a POST with an Origin header must match it, and `/api/owner` answers local requests only. Every review
  route asks `auth.decide`, which calls `access.resolve`, and a denied or missing review gets the same bytes. Every POST
  passes `auth.check_csrf`, with one exception: `POST /r/<id>/api/threads/<tid>/reply` and `.../status` also accept a
  personal access token as a bearer (`server.token_route`), which is an explicit credential and so skips CSRF. A bearer
  on any other route is refused before any identity lookup, and the status route still needs the reviewer or owner
  role. The second explicit exception is a round-file request whose path carries `~<token>`
  (`/r/<id>/rounds/<n>/~<token>/<variant>/<path>`): `frames.validate` must accept the signature, the payload's review
  and round, that a request with a session carries that session's `id_hash`, that a sessionless request resolves the
  payload's `s` to a live session whose `auth.decide` still passes `access.can`, that a `user:<id>` token is
  re-resolved through `users.get_user` + `auth.decide` + `access.can`, and that a `local` token comes from this
  computer (it alone carries the two-hour `max_age`; a session or account token lives as long as its binding). A
  valid token authorises round files only, for exactly that review and round — never `state.json`, a board file,
  `/api/*` or another review — and supplies the prototype CSP, except in an artifact variant, which keeps
  `ARTIFACT_ROUND_CSP` even when the request carries a valid token; every other review route still asks
  `auth.decide`. Roles are read from the database on every request, never cached. Keep all of this when you add a
  route.
- The MCP server speaks stdio only: UTF-8 JSON-RPC on stdout (both directions pinned to UTF-8, never the console code
  page), logs on stderr, no listening socket of its own, and it calls `mockreview.api` like the
  CLI — never a second code path.
- Anything a reviewer or a mock can supply is untrusted: text is rendered as text, mock HTML is stripped of scripts and
  handlers, feedback is validated in `feedback.py`, and every submission goes through the same `service.submit` path.
- A review needs an explicit origin (`platform` and `chat_id`). Never guess one for a wake-up.
- Review look settings are stored in the review when it is created, so a later config change never alters an open review.
- One settings table. Add a setting by adding a `Setting` to `SPEC`, then regenerate `plugin.yaml` and the docs. A test
  fails when `plugin.yaml` is out of sync.
- `setup.py` changes Hermes state only by running `hermes` subcommands, and never opens a Hermes file (`config.yaml`,
  `.env`, `auth.json`, `webhook_subscriptions.json`). The wake route's manual `toolsets` key (setting `wake_toolsets`),
  which `hermes webhook subscribe` deliberately cannot set, is the user's edit: setup prints the exact line and refuses
  while a waking board would be ungated (see `wakeguard.py`).
- Python 3.11-3.14, the standard library plus itsdangerous (PyYAML and PyJWT are optional). Import itsdangerous
  through `users.itsdangerous()` and PyJWT only where Cloudflare Access is used, so the plugin still loads in an
  interpreter without them. Code must run on macOS, Linux and Windows; use
  `locks.py` for file locks and avoid POSIX-only calls outside it.
- No identifiers of a person, machine, network or other project in code, docs, tests or fixtures. Use example domains such
  as `example.test` and generic names.
- Docs describe the current behavior. Put history in `CHANGELOG.md`.
