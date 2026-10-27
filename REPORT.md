# mock-review 1.1.0 implementation report

PR: https://github.com/arspesk/hermes-mock-review/pull/1.
Branch: `feat/herenow-password-links`.
Base: `57b1f581d03cb266b89bb4b6239573504a6405a6`.
Implementation commit: `4a4a787a006da70a53f6ba952291f9fe1905cb3a`.

The original implementation and historical report are separate commits. The embedded-table-URL correction completed at
[`a40e98917a500f83ab41cefc88b95744669ab962`](https://github.com/arspesk/hermes-mock-review/commit/a40e98917a500f83ab41cefc88b95744669ab962),
with [CI run 40585596181](https://github.com/arspesk/hermes-mock-review/actions/runs/40585596181) **16/16 jobs successful**
at that exact head: 12 test jobs, three plugin validators and the identifier sweep.

This is a **report-only followup** on the same branch and PR. It changes only `REPORT.md`, restores the required exact
deferred deployment/restart/cleanup templates, and records the parent's independent validation below. Production code,
tests and CI are unchanged; full suites are not rerun for this documentation correction. The PR report link identifies
this followup's final commit. The parent will supervise and verify all 16 CI jobs at that final head; the completed run
above belongs to the earlier correction SHA and is not claimed as final-followup CI evidence.

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
href aliases are removed. Bare HTTP(S) URLs inside table cells, including multiple URLs embedded in prose, become
DOM-created anchors using the same URL validator and forced target/rel. Text before, between and after URLs, sentence
punctuation, balanced URL brackets and literal HTML remain unchanged. No table content is parsed as HTML. Link clicks cross
shadow roots without creating pins; other mock clicks still pin, and pick controls are unchanged.

## Validation evidence

Parent independent validation of the completed correction (reported by the parent before this report-only followup):

- Python 3.9: **524 passed, 16 skipped in 111.79 s**.
- Python 3.14: **524 passed, 16 skipped in 113.07 s**.
- Hermes integration environment: **540 passed in 123.62 s**.
- Documentation smoke check: **45 OK, 0 FAIL**.
- Node syntax and generated-reference checks **passed**; private tree/history sweeps: **0 hits** each.

Report-only followup validation: Markdown paths and diff whitespace **passed**. The report snapshot tree and existing
full-history private sweeps found **0 hits** each, with **20 private tokens** and **16 generic rules**. The final
committed-tree and full-history gates are repeated before pushing only `feat/herenow-password-links`.
No deferred command below is executed.

Correction validation (540 collected tests per full suite):

- uv Python 3.9: **524 passed, 16 skipped in 98.02 s**.
- uv Python 3.14: **524 passed, 16 skipped in 100.78 s**.
- Hermes venv with `HERMES_AGENT_SRC`: **540 passed in 111.12 s**.
- The standalone interpreters skip 16 Hermes integration tests; the Hermes venv executed all 540 tests.
- Focused executed Node/DOM suite: **43 passed in 1.73 s**. Coverage includes whole-cell URLs, multiple URLs inside
  prose, text before/between/after, surrounding sentence punctuation and quotes/brackets, literal `<script>`/HTML text,
  numeric cells, forced target/rel, and relative/other-scheme strings staying text.
- Regression proof: copied `a38cab51b794afc05f4eeedf342c12eb505af528:mockreview/board/board.js` into a disposable
  scratch fixture with the corrected tests. `test_table_multiple_embedded_urls_preserve_all_text` **failed**, exit 1,
  in **0.05 s**: actual anchors `[]`, expected the two HTTP(S) links. The production working file was never replaced.
- `node --check mockreview/board/board.js`, generated references `--check`, diff whitespace check and isolated
  `hermes plugins validate .` **passed**. The validator used a disposable home within the allowed data boundary and
  removed it in a `finally` block. CI and its checks are unchanged.
- `python3 scripts/check_docs.py`: **45 OK, 0 FAIL**, including signed wake delivery to a local stand-in receiver
  and confirmed cleanup of its disposable home, work directory and board server.
- Fresh identifier positive control: disposable Git object store, random synthetic token, **1 hit**, **21 private tokens**,
  **16 generic rules**. The control store was deleted. No private token value was printed.
- Correction snapshot tree and existing full-history private sweeps: **0 hits** each, **20 private tokens**,
  **16 generic rules**. The committed tree/history gates are repeated before public push, with final output and
  exact-head CI evidence recorded in the PR/final handoff.
- Node tests execute production functions using a dependency-free minimal DOM with HTMLParser-decoded attributes.
  These are unit-level DOM tests, not a full-browser navigation or here.now unlock test.

Historical validation before the embedded-URL correction (522 collected tests per suite; these are earlier results):

- uv Python 3.9: **506 passed, 16 skipped in 102.19 s**.
- uv Python 3.14: **506 passed, 16 skipped in 103.94 s**.
- Hermes venv with `HERMES_AGENT_SRC`: **522 passed in 118.90 s**.
- Earlier `check_docs`: **45 OK, 0 FAIL**. Initial runs found a hard-coded 15-subcommand assertion, which was updated
  for the password command, and required the positional choice to be written as `<on|off>`.
- Before the first implementation, temporarily restoring the older board produced **25 new Node/DOM failures**;
  restoring the older publisher produced **1 password regression failure**. Both were restored in `finally` blocks;
  the focused password/DOM suite then passed **40 tests**. That proof did not cover embedded table URLs; the correction
  regression above specifically proves the acceptance failure on `a38cab5`.
- Earlier private tree/history sweeps: **0 hits** each, **20 private tokens**, **16 generic rules**.

## Upstream catalog inspection

Re-read the live `NousResearch/hermes-agent:plugin-catalog/mock-review.yaml` and catalog validator through `gh api`
read-only during this correction; neither was modified.
It currently pins version `1.0.1`, SHA `fb3d07bae45ab7a30f57fdc8cbc0e36df016e996`.
Its banner already references `57b1f581d03cb266b89bb4b6239573504a6405a6`; its screenshot references the older catalog pin.
The current disclosure says boards can be published on here.now and lists four tools; it does not describe password gates.
A NEW upstream PR after merge must pin the merged main SHA and version `1.1.0`, update image URLs to that SHA, and describe
password protection by default, `herenow_password`, per-create opt-out and the live password slash/CLI command. The four-tool
list stays correct: toggling uses the CLI/slash command. No new external executable disclosure is needed. Existing here.now
API disclosure should mention the metadata PATCH. The live validator requires a catalog path and `ruamel.yaml`;
the deferred command below includes both. Validate the catalog and check both image URLs return HTTP 200 after
the update. Those future checks were not executed here.

## Deferred commands — do not run before merge/release authorization

The authorized card overrides older checklist instructions to work directly on main or use a temporary CI branch.
This correction only pushes its existing feature branch and updates the existing PR. All commands in this section
are non-executed templates for later authorized work, including the complete deployment/restart/cleanup sequence.
Explicit restart approval is required in addition to merge/release authorization. None of merge, tag, release,
catalog mutation, live setup, deploy, restart or live verification was run in this task.

```bash
PR_NUMBER=1
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
uv run -q --no-project --python 3.14 --with ruamel.yaml python scripts/validate_plugin_catalog.py plugin-catalog/mock-review.yaml
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
printf 'Pin mock-review 1.1.0 at %s. New here.now reviews default to password protection; disclose controls and metadata PATCHes. Attach the actual catalog validation and image HTTP check results after running them.\n' "$MERGED_SHA" > "$CATALOG_BODY"
gh pr create --repo NousResearch/hermes-agent --base main --head "$FORK_OWNER:catalog/mock-review-1.1.0" --title 'catalog: mock-review 1.1.0' --body-file "$CATALOG_BODY"
rm "$CATALOG_BODY"
# After that upstream PR merges, delete only this catalog branch/clone as appropriate.
```

After merge/release authorization, run the release checklist's live-machine setup dry run before any owner/setup switch.
Treat any proposed address or command needing a manual override as a plugin bug; fix it before switching rather than
patching live configuration. Then deploy and check the installed copy. These commands are recorded only, not executed.

The canonical deployment script path triggered the private identifier sweep. Its basename must therefore remain
`DEPLOY_SCRIPT_NAME`: set that variable to the approved deployment script's basename from the installed release
checklist before running the two templates. The literal matched value is omitted; the sweep is not bypassed.

```bash
hermes mockreview setup --dry-run --hosting tailnet --origin-platform discord --port 8790 --skip-test-review
bash "$HOME/.hermes/scripts/${DEPLOY_SCRIPT_NAME:?}" mock-review
bash "$HOME/.hermes/scripts/${DEPLOY_SCRIPT_NAME:?}" mock-review --check
```

Use the installed release checklist's `scripts/restart_check.py` as the source below. `OLD_BOARD_PID` is the board's
current pre-restart PID, `THREAD_ID` is the approved delivery thread, and `RESTART_JOB_ID` is the existing approved
gateway-restart job supplied privately by the parent. It is distinct from `CHECK_JOB_ID`, returned when creating the
new verification job. No private identifier is embedded here. Capture the old board PID before restarting; do not
guess a PID or a delivery thread. Run the old-process positive control after deploy/check:

```bash
RESTART_CHECK_SOURCE="$HOME/.hermes/skills/creative/mock-review/scripts/restart_check.py"
OLD_BOARD_PID='<current-board-pid>'
THREAD_ID='<approved-delivery-thread-id>'
RESTART_JOB_ID='<existing-approved-restart-job-id>'
MR_DRY=1 MR_OLD_PID=0 MR_VERSION=1.1.0 python3 "$RESTART_CHECK_SOURCE"
cp "$RESTART_CHECK_SOURCE" "$HOME/.hermes/scripts/mockreview_restart_check.py"
```

The positive control must show the "started after the deploy" check failing while the old process still runs.
Before scheduling, edit only the copied `mockreview_restart_check.py`: set its top-level `MR_OLD_PID` to the numeric
`OLD_BOARD_PID`, `MR_VERSION` to `"1.1.0"` and `MR_THREAD` to the quoted `THREAD_ID`. Leave the source script untouched.
Create the one-shot verification job and retain the returned job ID for cleanup:

```bash
hermes cron create --name "mock-review restart check (one-shot)" --deliver "discord:$THREAD_ID" --repeat 1 --script mockreview_restart_check.py --no-agent 2m
CHECK_JOB_ID='<verification-job-id-returned-by-create>'
```

Obtain explicit restart approval before the following command. Run the existing approved restart job as the last
action of that later turn, because restarting the gateway ends the agent session. This command is deferred here:

```bash
hermes cron run "$RESTART_JOB_ID"
```

Post-restart verification must prove a new board PID inside the gateway, started after deployment, version 1.1.0,
loopback `/api/owner` 200, tailnet `/healthz` 200, a resolving public base, and a timestamped local/tailnet review reaching
Send 200, wake 202 and notice `200 delivered`. None of these live checks was performed in this task. After the owner
verifies them, take `REVIEW_ID` and its exact local directory from that check's output. Confirm the directory belongs
to that timestamped verification review before removal. These exact cleanup templates are also deferred; preserve
unrelated reviews, scripts and cron jobs, including the existing restart job:

```bash
REVIEW_ID='<verification-review-id>'
REVIEW_DIR='<absolute-directory-of-that-verification-review>'
hermes mockreview close "$REVIEW_ID"
rm -r -- "${REVIEW_DIR:?}"
rm -- "$HOME/.hermes/scripts/mockreview_restart_check.py"
hermes cron remove "$CHECK_JOB_ID"
```

## Cleanup

All validation/docs homes and disposable local servers were removed by their owning commands. The positive-control
object store and temporary PR-body file were removed. Correction scratch fixtures, pytest directories and temporary
PR files are removed before handoff; the branch/worktree remain for review. No other PIDs were killed. No live review, profile, runtime configuration,
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
+- **Links inside mock/option HTML:** absolute HTTP(S) links open in a new tab with `noopener noreferrer` and do not create pins. Relative, other-protocol and whitespace/control-obfuscated hrefs are removed. Bare HTTP(S) URLs inside table cells become links, including multiple URLs embedded in prose; surrounding text, punctuation and literal HTML are preserved. No companion links page is needed.
@@ -635 +635 @@
-- **here.now is public**: the link (and the feedback on earlier rounds) is visible to anyone who has
+- **here.now is internet hosted**: new boards require the returned password by default; explicitly unprotected or legacy boards are visible to anyone who has
```
