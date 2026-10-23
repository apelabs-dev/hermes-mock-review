# AGENTS.md

Guidance for coding agents (and people) changing this repository. mock-review is a Hermes plugin that serves review
boards for UI mockups and wakes the agent when the reviewer presses Send. User documentation is in `README.md` and
`docs/`.

## Layout

- `plugin.yaml`: the Hermes manifest. `config_schema:` is generated from `mockreview/settings.py`.
- `__init__.py`: the plugin entry point, `register(ctx)`. It registers four tools, the `hermes mockreview` command, the
  `/mockreview` slash command, the `usage` skill and the webhook platform handler. It starts nothing itself.
- `skills/usage/SKILL.md`: the skill bundled with the plugin (`mock-review:usage`). It is written for the agent that
  calls the tools and must stay generic.
- `bin/mockreview`: runs the same CLI from a checkout, without Hermes.
- `mockreview/`: the implementation, stdlib only.
  - `settings.py`: the `SPEC` table (every setting, its type, range, default, env var) and the resolver
    (call argument, then env, then Hermes config, then default). A strict fallback parser reads `config.yaml` when PyYAML
    is missing.
  - `cli.py`: every subcommand; `setup.py` is the onboarding wizard.
  - `api.py`: the operations behind the CLI and the tools. `store.py`: review files, `decisions.json` validation, design
    profiles. `feedback.py`: validation and markdown for submitted feedback. `service.py`: submit handling shared by the
    local server and the here.now poller.
  - `server.py`: the HTTP board server and the background worker. `autostart.py`: runs that server on a thread inside the
    gateway. `owner.py`: decides which store a call writes to and checks the wake route.
  - `notify.py`: the signed wake-up POST. `herenow.py`: here.now publish and Site Data polling. `secrets.py`: the only way
    to read a secret. `config.py`, `locks.py`: paths, URLs and a cross-platform file lock.
  - `board/`: the static board UI (`index.html`, `board.js`, `board.css`).
- `docs/`: user docs. `settings.md` and `cli.md` are generated. `img/` holds the README screenshot.
- `scripts/gen_docs_reference.py`: regenerates `docs/settings.md` and `docs/cli.md`. `scripts/check_docs.py`: checks the
  docs against the tree. `scripts/gen_plugin_manifest.py`: rewrites `config_schema:` in `plugin.yaml`.
  `scripts/check_identifiers.py`: the secret and identifier sweep (tree or full history).
- `tests/`: pytest suite.
- `.github/workflows/ci.yml`: the test matrix (Linux, macOS and Windows; Python 3.9, 3.11, 3.12 and 3.14), `hermes plugins
  validate` through Hermes's own `plugin-validate` action, and the sweep over the tree and the history.

## Commands

    python -m pytest -q                       # needs pytest and pyyaml
    python scripts/gen_plugin_manifest.py     # after editing SPEC in settings.py; rewrites plugin.yaml in place
    python scripts/gen_docs_reference.py      # after editing SPEC or any CLI flag or help text
    python scripts/gen_docs_reference.py --check
    python scripts/check_docs.py              # every documented command, path and example against the tree
    python scripts/check_identifiers.py       # secrets and identifiers in the files tracked at HEAD
    python scripts/check_identifiers.py --history   # the same over every blob and commit message (needs full history)

The sweep reads extra private tokens, one per line, from `MOCKREVIEW_SWEEP_TOKENS` (CI fills it from a repository secret
of the same name); without it only the generic denylist runs. It prints `path:line rule-id`, never the matched text.

The two docs scripts need the `hermes` CLI (0.22.0 or newer). They build a throwaway Hermes home next to the real one and
delete it afterwards. Hermes refuses a `HERMES_HOME` outside its data directory, so do not point them elsewhere.

## Invariants

- Secrets go through `mockreview/secrets.py` (Hermes's secret scope) and nowhere else. Never read a credentials file,
  never print a secret, never put one in a log, a test fixture name or a doc. `setup.py` redacts subprocess output.
- Under multiplexing (`agent.secret_scope.is_multiplex_active()`) every board request and every Worker tick binds the
  owning profile's secret scope through `secrets.owner_scope`, rebuilt on each entry from the home captured when the
  server started (`config.hermes_home()`). Never fall back to `os.environ`, never use another profile's scope (not even
  the one the starting context carried), and never call `set_multiplex_*` from plugin code. Without multiplexing nothing
  is bound.
- `register()` and plugin import start no thread, socket or subprocess. The board server starts only from
  `autostart.gateway_factory` (inside the gateway, when `autostart` is true) or from `hermes mockreview serve`.
- The server binds loopback by default. `make_server` refuses any host outside `ALLOWED_HOSTS`. Every request passes the
  Host allowlist, a POST with an Origin header must match it, and `/` and `/api/owner` answer local requests only. Keep
  all three when you add a route.
- Anything a reviewer or a mock can supply is untrusted: text is rendered as text, mock HTML is stripped of scripts and
  handlers, feedback is validated in `feedback.py`, and here.now records go through the same `service.submit` path.
- A review needs an explicit origin (`platform` and `chat_id`). Never guess one for a wake-up.
- Review look settings are stored in the review when it is created, so a later config change never alters an open review.
- One settings table. Add a setting by adding a `Setting` to `SPEC`, then regenerate `plugin.yaml` and the docs. A test
  fails when `plugin.yaml` is out of sync.
- `setup.py` changes Hermes state only by running `hermes` subcommands. It never edits Hermes files directly.
- Python 3.9 or newer, standard library only (PyYAML is optional). Code must run on macOS, Linux and Windows; use
  `locks.py` for file locks and avoid POSIX-only calls outside it.
- No identifiers of a person, machine, network or other project in code, docs, tests or fixtures. Use example domains such
  as `example.test` and generic names.
- Docs describe the current behavior. Put history in `CHANGELOG.md`.
