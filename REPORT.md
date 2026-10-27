# mock-review 1.1.0 implementation report

PR: https://github.com/arspesk/hermes-mock-review/pull/1.
Branch: `feat/herenow-password-links`.
Base: `57b1f581d03cb266b89bb4b6239573504a6405a6`.
Implementation commit: `4a4a787a006da70a53f6ba952291f9fe1905cb3a`.

The implementation and report are separate commits. The final report commit SHA and its exact-head CI evidence are
provided in the final handoff, avoiding a commit self-reference loop. The PR workflow started as run `40583069048`
on the implementation commit; pushing this report retriggers CI, and all 16 jobs must be verified on the final head.
No final-head CI result is claimed before that run completes.

## Changed files

- `CHANGELOG.md`
- `README.md`
- `__init__.py`
- `docs/cli.md`
- `docs/decisions-format.md`
- `docs/hosting.md`
- `docs/security.md`
- `docs/settings.md`
- `mockreview/__init__.py`
- `mockreview/api.py`
- `mockreview/board/board.js`
- `mockreview/cli.py`
- `mockreview/herenow.py`
- `mockreview/password_words.txt`
- `mockreview/passwords.py`
- `mockreview/settings.py`
- `mockreview/store.py`
- `plugin.yaml`
- `skills/usage/SKILL.md`
- `tests/board_dom.js`
- `tests/conftest.py`
- `tests/test_board_links.py`
- `tests/test_herenow.py`
- `tests/test_passwords.py`
- `tests/test_release_compliance.py`
- `tests/test_secret_scope_integration.py`
- `tests/test_setup_onboard.py`
- `REPORT.md`

## Features and security rationale

New here.now reviews capture `herenow_password` (default true), with per-create CLI/tool overrides. Passwords use four
independent stdlib `secrets.choice` selections from EFF's bundled 7,776-word list: about 51.7 bits of entropy.
Source and CC BY 3.0 US attribution are in `mockreview/passwords.py` and the hosting documentation. No runtime dependency
or new external command was introduced.

The password stays in local `review.json` under `herenow.password`, with explicit desired/applied state. It is returned
only by explicit create/add-round/publish/password owner operations after confirmed application, never site files,
browser state, list/open output, logs or remote error text. Toggle-off clears it after successful removal; a failed
removal retains the value for recovery. The slug is saved before PATCH. Errors and interrupted requests leave protection
unconfirmed and suppress shareable URLs until reconciled. Publication, password toggles and close share the existing
operation lock. Tests cover toggle/publish concurrency, interrupted changes, recovery and deletion after a gate failure.

The supplied API contract requires first publishing before the slug is known. A short public interval therefore exists
before the initial password PATCH; a failed PATCH can leave the site public. The operation returns an error and records
this state rather than advertising protection. Legacy reviews remain public until explicitly enabled. Owner-key Site
Data reads, unlocked browser inserts, close/delete and housekeeping retain their documented behavior. The real here.now
API was never called; the endpoint contract and unlock behavior remain owner-supplied, not externally integration-tested.

Mock/option anchors retain only absolute HTTP(S) URLs, parsed with `new URL` after rejecting whitespace/controls and
relative forms. Target/rel are replaced with `_blank` and `noopener noreferrer`; tracking/download attributes and SVG
href aliases are removed. Table cells stay text except bare HTTP(S) URLs built through DOM methods. Link clicks cross
shadow roots without creating pins; other mock clicks still pin, and pick controls are unchanged.

## Validation evidence

Final local suites (522 collected tests each):

| Interpreter | Result | Duration |
| --- | --- | --- |
| uv Python 3.9 | 506 passed, 16 skipped | 102.19 s |
| uv Python 3.14 | 506 passed, 16 skipped | 103.94 s |
| Hermes venv with HERMES_AGENT_SRC | 522 passed | 118.90 s |

The standalone interpreters skip 16 tests requiring Hermes source integration; the Hermes venv executed all 522.
`node --check mockreview/board/board.js`, generated reference `--check`, diff whitespace check, and isolated
`hermes plugins validate .` passed. Validator homes were created inside the validator's allowed data boundary and
removed in `finally` blocks. `python3 scripts/check_docs.py` finished with **45 OK, 0 FAIL**, including signed wake-up delivery to a local stand-in receiver and cleanup of its disposable home, work directory and server. Initial full runs found a hard-coded 15-subcommand assertion;
it was updated for the new password command. The documentation checker also required the positional choice to be written
as `<on|off>` so it did not interpret those arguments as slash subcommands. CI and its checks were not modified.

- Old board implementation restored temporarily: **25 new Node/DOM tests failed**, exit 1.
- Old publisher restored temporarily: **1 password regression test failed**, exit 1.
- Both files restored in `finally` blocks. The new focused password/DOM suite passed **40 tests** after restoration (the subsequent three-interpreter suites above also include them).
- Node tests execute production functions using a dependency-free minimal DOM with HTMLParser-decoded attributes.
  They are unit-level DOM tests, not a full-browser navigation or here.now unlock test.
- Identifier positive control: disposable alternate Git object store, random synthetic token, **1 hit**, **21 tokens**.
  The control store was deleted; normal private-token set is **20 tokens**. No private token value was printed.
- Implementation HEAD tree and full-history sweeps before the first push: **0 hits** each, 20 private tokens, 16 generic rules.
- The report commit receives the same tree/history sweeps before its push; final exact-head CI is verified at handoff.

## Upstream catalog inspection

Read `NousResearch/hermes-agent:plugin-catalog/mock-review.yaml` through `gh api` without modifying it.
It currently pins version `1.0.1`, SHA `fb3d07bae45ab7a30f57fdc8cbc0e36df016e996`.
Its banner already references `57b1f581d03cb266b89bb4b6239573504a6405a6`; its screenshot references the older catalog pin.
The current disclosure says boards can be published on here.now and lists four tools; it does not describe password gates.
A NEW upstream PR after merge must pin the merged main SHA and version `1.1.0`, update image URLs to that SHA, and describe
password protection by default, `herenow_password`, per-create opt-out and the live password slash/CLI command. The four-tool
list stays correct: toggling uses the slash command. No new external executable disclosure is needed. Existing here.now
API disclosure should mention the metadata PATCH. Validate the catalog and check both image URLs return HTTP 200.

## Deferred commands — do not run before merge/release authorization

The authorized card overrides older checklist instructions to work directly on main or use a temporary CI branch.
This task only pushes its feature branch and opens a PR. The following are exact follow-up commands for the reviewer;
none of merge, tag, release, catalog mutation, deploy or restart was run in this task.

```bash
PR_NUMBER='<reviewed-pr-number>'
gh pr merge "$PR_NUMBER" --repo arspesk/hermes-mock-review --merge
cd "$HOME/projects/hermes-mock-review"
git fetch origin main
git switch main
git pull --ff-only origin main
MERGED_SHA=$(git rev-parse HEAD)
# Recheck the merged main gates and the release notes before publishing a tag.
uv run -q --no-project --python 3.9 --with pytest --with pyyaml python -m pytest -q -p no:cacheprovider
uv run -q --no-project --python 3.14 --with pytest --with pyyaml python -m pytest -q -p no:cacheprovider
HERMES_AGENT_SRC=$HOME/.hermes/hermes-agent $HOME/.hermes/hermes-agent/venv/bin/python -m pytest -q -p no:cacheprovider
python3 scripts/gen_docs_reference.py --check
python3 scripts/check_docs.py
git tag -a v1.1.0 "$MERGED_SHA" -m 'mock-review 1.1.0'
GITHUB_REPOSITORY=arspesk/hermes-mock-review python3 "$HOME/.hermes/state/mock-review/with_tokens.py" -- python3 scripts/check_identifiers.py
GITHUB_REPOSITORY=arspesk/hermes-mock-review python3 "$HOME/.hermes/state/mock-review/with_tokens.py" -- python3 scripts/check_identifiers.py --history
git push origin refs/tags/v1.1.0
RELEASE_NOTES=$(mktemp "$TMPDIR/mockreview-release.XXXXXX")
python3 - "$RELEASE_NOTES" <<'PY'
from pathlib import Path
import sys
text = Path('CHANGELOG.md').read_text(encoding='utf-8')
section = text.split('## [1.1.0]', 1)[1].split('\n## [', 1)[0]
Path(sys.argv[1]).write_text('## [1.1.0]' + section, encoding='utf-8')
PY
gh release create v1.1.0 --repo arspesk/hermes-mock-review --notes-file "$RELEASE_NOTES" --latest
rm "$RELEASE_NOTES"
```

Create the catalog PR only after the plugin PR is merged and released. Use the merged SHA, not the feature-branch SHA:

```bash
gh repo fork NousResearch/hermes-agent --clone=false
FORK_OWNER=$(gh api user --jq .login)
CATALOG_DIR="$HOME/projects/hermes-agent-catalog-update"
git clone --filter=blob:none --sparse https://github.com/NousResearch/hermes-agent.git "$CATALOG_DIR"
cd "$CATALOG_DIR"
git sparse-checkout set plugin-catalog scripts
git remote add fork "https://github.com/$FORK_OWNER/hermes-agent.git"
git switch -c catalog/mock-review-1.1.0 origin/main
MERGED_SHA=$(gh api repos/arspesk/hermes-mock-review/git/ref/tags/v1.1.0 --jq .object.sha)
MERGED_SHA=$(gh api "repos/arspesk/hermes-mock-review/git/tags/$MERGED_SHA" --jq .object.sha)
python3 - "$MERGED_SHA" <<'PY'
from pathlib import Path
import re, sys
p = Path('plugin-catalog/mock-review.yaml')
s = p.read_text(encoding='utf-8')
s = re.sub(r'^sha:.*$', 'sha: "' + sys.argv[1] + '"', s, flags=re.M)
s = re.sub(r'^version:.*$', 'version: "1.1.0"', s, flags=re.M)
s = re.sub(r'(raw.githubusercontent.com/arspesk/hermes-mock-review/)[0-9a-f]{40}/', r'\g<1>' + sys.argv[1] + '/', s)
s = s.replace('or published on here.now.', 'or published on here.now with a password gate by default. The herenow_password setting controls new reviews; per-create password overrides and the password CLI/slash command control access. Metadata PATCHes apply or remove the gate.')
p.write_text(s, encoding='utf-8')
PY
python3 scripts/validate_plugin_catalog.py
curl --fail --head "https://raw.githubusercontent.com/arspesk/hermes-mock-review/$MERGED_SHA/docs/img/catalog-banner.jpg"
curl --fail --head "https://raw.githubusercontent.com/arspesk/hermes-mock-review/$MERGED_SHA/docs/img/review-board.png"
git add plugin-catalog/mock-review.yaml
RELEASE_AUTHOR_NAME='<approved-public-author-name>'
RELEASE_AUTHOR_EMAIL='<approved-public-noreply-email>'
git -c user.name="$RELEASE_AUTHOR_NAME" -c user.email="$RELEASE_AUTHOR_EMAIL" commit -m 'catalog: pin mock-review 1.1.0'
# Run the catalog checkout's applicable private tree/history sweeps before its public push.
GITHUB_REPOSITORY=NousResearch/hermes-agent python3 "$HOME/.hermes/state/mock-review/with_tokens.py" -- python3 "$HOME/projects/hermes-mock-review/scripts/check_identifiers.py"
GITHUB_REPOSITORY=NousResearch/hermes-agent python3 "$HOME/.hermes/state/mock-review/with_tokens.py" -- python3 "$HOME/projects/hermes-mock-review/scripts/check_identifiers.py" --history
git push fork HEAD:refs/heads/catalog/mock-review-1.1.0
CATALOG_BODY=$(mktemp "$TMPDIR/mockreview-catalog.XXXXXX")
printf 'Pin mock-review 1.1.0 at %s. New here.now reviews default to password protection; disclose controls and metadata PATCHes. Catalog validation and both image HTTP checks passed.\n' "$MERGED_SHA" > "$CATALOG_BODY"
gh pr create --repo NousResearch/hermes-agent --base main --head "$FORK_OWNER:catalog/mock-review-1.1.0" --title 'catalog: mock-review 1.1.0' --body-file "$CATALOG_BODY"
rm "$CATALOG_BODY"
# After that upstream PR merges, delete only this catalog branch/clone as appropriate.
```

Before any owner/setup switch, follow the release checklist's live-machine dry run; fix unexpected route proposals in
code instead of patching configuration. Deployment and verification are separate from PR approval:

```bash
hermes mockreview setup --dry-run --hosting tailnet --origin-platform discord --port 8790 --skip-test-review
DEPLOY_SCRIPT='<approved-deploy-helper-path>'
bash "$DEPLOY_SCRIPT" mock-review
bash "$DEPLOY_SCRIPT" mock-review --check
SKILL_DIR="$HOME/.hermes/skills/creative/mock-review"
MR_DRY=1 MR_OLD_PID=0 MR_VERSION=1.1.0 python3 "$SKILL_DIR/scripts/restart_check.py"
# Positive control: started-after-deploy must fail while the old gateway still runs.
cp "$SKILL_DIR/scripts/restart_check.py" "$HOME/.hermes/scripts/mockreview_restart_check.py"
# Edit MR_OLD_PID, MR_VERSION=1.1.0 and MR_THREAD in that copied script only.
ORIGIN_THREAD='<approved-verification-thread>'
hermes cron create --name 'mock-review restart check (one-shot)' --deliver "discord:$ORIGIN_THREAD" --repeat 1 --script mockreview_restart_check.py --no-agent 2m
```

Obtain explicit restart approval. Then trigger the pre-existing approved restart job as the final action of that turn
(`RESTART_JOB_ID` is the existing job identifier, not the verification cron created above):

```bash
RESTART_JOB_ID='<approved-existing-restart-job-id>'
hermes cron run "$RESTART_JOB_ID"
```

The one-shot must prove a new board PID inside the gateway, started after deployment, version 1.1.0, loopback
`/api/owner` 200, tailnet `/healthz` 200, a resolving public base, and a timestamped local/tailnet review reaching Send 200,
wake 202 and notice `200 delivered`. This task did not perform live verification or create such a review. Afterward,
close only that verification review, remove only its review directory, delete the copied check script and verification cron:

```bash
TEST_REVIEW_ID='<verification-review-id>'
TEST_REVIEW_DIR='<verified-exact-test-review-directory>'
CHECK_CRON_ID='<one-shot-verification-cron-id>'
hermes mockreview close "$TEST_REVIEW_ID"
rm -r -- "$TEST_REVIEW_DIR"
rm "$HOME/.hermes/scripts/mockreview_restart_check.py"
hermes cron remove "$CHECK_CRON_ID"
```

## Cleanup

All validation/docs homes and disposable local servers were removed by their owning commands. The positive-control
object store and temporary PR-body file were removed. Local pytest temporary directories are removed before handoff;
the branch/worktree remain for review. No other PIDs were killed. No live review, profile, runtime configuration,
gateway, catalog, main branch, tag or release was changed.

## Proposed personal SKILL.md diff — not applied

The personal skill was read only. These proposed hunks change sharing and link guidance; headers use a generic path.
No personal skill, profile, runtime configuration or live board data was edited. The required SSD guard exited 0; the pre-created worktree was reused and no main-checkout files were edited.

```diff
--- personal-skill/SKILL.md
+++ personal-skill/SKILL.md
@@ -452 +452 @@
-- In herenow mode a publish failure leaves the review created; retry with `mockreview publish <id>`.
+- In herenow mode a publish failure leaves the review created; retry with `mockreview publish <id>`. New here.now reviews have a password gate by default: send the returned `password` next to the URL. Global `herenow_password` / `MOCKREVIEW_HERENOW_PASSWORD` and create `--password` / `--no-password` or tool `password: bool` control the saved choice. Use `mockreview password <id> on|off` or `/mockreview password <id> <on|off>` for a live toggle. Failed password PATCHes mean protection is unconfirmed: retry publish/toggle or close to delete. Never share a success claim after that error; list/open do not reveal the password. Legacy reviews remain public until explicitly enabled.
@@ -454 +454 @@
-- **Links inside mock/option HTML are stripped** (`cleanFragment` removes every `<a href>`), and table cells are plain text. When he needs to open sites from a board: print the URL visibly under each item AND publish a companion static links page via `~/.hermes/skills/productivity/here-now/scripts/publish.sh . --client hermes` (run with cwd = the site folder; absolute paths are rejected), then put that URL in the board's text block.
+- **Links inside mock/option HTML:** absolute HTTP(S) links open in a new tab with `noopener noreferrer` and do not create pins. Relative, other-protocol and whitespace/control-obfuscated hrefs are removed. Table cells stay text except bare HTTP(S) URLs, which become links. No companion links page is needed.
@@ -635 +635 @@
-- **here.now is public**: the link (and the feedback on earlier rounds) is visible to anyone who has
+- **here.now is internet hosted**: new boards require the returned password by default; explicitly unprotected or legacy boards are visible to anyone who has
```
