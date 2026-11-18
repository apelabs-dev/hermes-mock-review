#!/usr/bin/env python3
"""Sweep the repository for secrets and for identifiers of a person, machine or network.

    python3 scripts/check_identifiers.py                      # tree mode: every file tracked at HEAD
    python3 scripts/check_identifiers.py --rev v1.0.0         # tree mode at another revision
    python3 scripts/check_identifiers.py --history            # every blob and commit message of `git rev-list --all`
    python3 scripts/check_identifiers.py --history --since v1.0.0   # private tokens only after v1.0.0

Two rule sets run:

  * The generic denylist, always on. Secrets: private key blocks and the common credential prefixes (GitHub, Slack,
    AWS, OpenAI/Anthropic-style `sk-` keys, Google API keys, Stripe, npm, PyPI, Hugging Face, Discord webhooks).
    Identifiers: absolute home directories (under /Users, /home or C:\\Users, except generic placeholder names), Tailscale
    `*.ts.net` host names (except ones with an `example` label), email addresses outside example, test and noreply
    domains, and 17 to 20 digit ids (Discord-style snowflakes) that decode to a plausible creation time.
  * Private tokens from the environment variable MOCKREVIEW_SWEEP_TOKENS, one per line, matched case-insensitively as
    plain text (a line that starts with `re:` is a regular expression instead; blank lines and `#` lines are ignored).
    CI feeds it from a repository secret of the same name. When it is empty, as on forks, only the generic denylist
    runs and the script says so. Set MOCKREVIEW_REQUIRE_TOKENS to 1, true or yes to make an empty list a usage error
    instead (CI does this on non-fork runs, so a missing repository secret fails the job).

History mode checks private tokens only after a recorded baseline. `<repo>/scripts/sweep-baseline` holds one commit sha
on its own line; blank lines and `#` lines are ignored. Commit messages of commits that `git rev-list --all --not
<baseline>` lists get both rule sets; every older commit message gets the generic denylist only. A blob gets both rule
sets when its object id is not reachable from the baseline, so a blob a post-baseline commit adds or rewrites is
checked while an unchanged pre-baseline blob is not re-flagged. The generic denylist always covers every commit and
blob. With no baseline file private tokens sweep all history; `--since REV` overrides the file. A baseline that does
not resolve to a commit or is not reachable from any ref is a usage error, never an empty sweep.

The 2.0.0 working branches contain pre-SWEEP commits with private names, so the 2.0.0 release must reach GitHub as
squashed commit(s) on top of the baseline, never as the raw epic history.

Not a leak:
  * The repository's own address, current or former. `owner/repo` (from --repo-slug, else GITHUB_REPOSITORY, else the
    `origin` remote) and the former addresses in FORMER_SLUGS are removed from a line before the line is checked, so
    `github.com/owner/repo` and an install command naming the slug pass.
  * A hit listed in KNOWN_HITS, keyed by rule id and commit, path or path:line, with a written reason. It is reported
    as `known:` on stderr and does not fail the sweep.
  * The copyright line. LICENSE files at the root are exempt from the environment tokens only, never from the
    generic denylist.
  * Commit identities. History mode checks commit messages but not author or committer names and addresses: public
    contributor identities are normal in a public repository. Review them separately before publishing.

History mode refuses a shallow clone (a sweep of missing history proves nothing) and never limits the number of
commits. Binary files are skipped, the way `git grep -I` skips them.

Output is one line per hit, `path:line rule-id`, where a token rule is `token-<n>` (its line number in
MOCKREVIEW_SWEEP_TOKENS). The matched text is never printed. In history mode a blob hit ends with `@<blob>` and a
commit message hit reads `commit <sha>:line rule-id`. A mode line on stderr names the baseline when there is one and
reports the commit and path object counts.

Exit codes: 0 clean (no hit, or only known ones), 1 hits, 2 usage error (not a git repository, unknown revision,
`--since` without `--history`, a baseline that is not a reachable commit or a `scripts/sweep-baseline` without a sha,
or a shallow clone in history mode). An empty token list while MOCKREVIEW_REQUIRE_TOKENS is truthy also exits 2,
printing on stderr `error: MOCKREVIEW_REQUIRE_TOKENS is set but MOCKREVIEW_SWEEP_TOKENS is empty: ...`.
Standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Pattern, Tuple

ENV_VAR = "MOCKREVIEW_SWEEP_TOKENS"
REQUIRE_VAR = "MOCKREVIEW_REQUIRE_TOKENS"
TRUTHY = {"1", "true", "yes"}
LICENSE_FILES = {"LICENSE", "LICENSE.md", "LICENSE.txt"}
BASELINE_FILE = "scripts/sweep-baseline"

# Release-please wrote the repository's former home into CHANGELOG.md and older READMEs. Its address is the
# repository's own address too, so it is removed like the current slug before the rules run.
FORMER_SLUGS = ("arspesk/hermes-mock-review",)

# Hits this sweep accepts, keyed by (rule id, location), each with the reason it is accepted. A location is
# `commit <12-hex>` for a commit message, `<path>` for every hit in one file, or `<path>:<line>` for one line;
# the most specific match wins. Every suppressed hit is still reported as `known:` so none vanishes silently.
KNOWN_HITS = {
    ("token-20", "commit 4b5dfe1c928d"): "the commit that announced the move to the new org; the "
                                         "message names the org, which is the repository's own address",
    ("email", "mockreview/board/vendor/turndown.min.js:331"): "the public upstream MIT attribution of the pinned, "
                                                              "hash-verified Turndown 7.2.5 build",
}

SECRET_RULES: Dict[str, str] = {
    "private-key": r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----",
    "github-token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "slack-token": r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    "slack-webhook": r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+",
    "aws-access-key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "sk-api-key": r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}",
    "google-api-key": r"\bAIza[0-9A-Za-z_-]{35}",
    "stripe-key": r"\b[rs]k_live_[0-9A-Za-z]{16,}",
    "npm-token": r"\bnpm_[A-Za-z0-9]{36}\b",
    "pypi-token": r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{40,}",
    "hf-token": r"\bhf_[A-Za-z0-9]{34,}\b",
    "discord-webhook": r"discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+",
}

PLACEHOLDER_NAMES = {"user", "username", "you", "yourname", "your-name", "your_name", "yourusername", "name", "me",
                     "someone", "example", "runner", "alice", "bob"}
HOME_PATH = re.compile(r"(?:(?<![\w.])/(?:Users|home)/|\b[A-Za-z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/))"
                       r"([A-Za-z0-9._-]+)")
TAILNET_HOST = re.compile(r"\b((?:[a-z0-9-]+\.)+)ts\.net\b", re.I)
EMAIL = re.compile(r"(?<![\w.%+-])([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+([A-Za-z]{2,}))\b")
EMAIL_OK_DOMAINS = re.compile(r"(?:^|\.)(?:example(?:\.(?:com|org|net))?|test|invalid|localhost|"
                              r"users\.noreply\.github\.com)$", re.I)
EMAIL_OK_LOCAL = {"noreply", "no-reply", "git"}
FILE_SUFFIXES = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "js", "mjs", "css", "py", "md", "json",
                 "html", "txt", "yaml", "yml"}
SNOWFLAKE = re.compile(r"(?<![0-9A-Za-z_])([0-9]{17,20})(?![0-9A-Za-z_])")
DISCORD_EPOCH_MS = 1420070400000


def plausible_snowflake(digits: str) -> bool:
    """A Discord-style id: its top bits are milliseconds since 2015-01-01, so it decodes to a creation time between the
    epoch and next year. Fixture numbers (one or two repeated digits, a counting run, a run of six zeros) are not ids:
    the low bits of a real id are a worker and a counter, which almost never print six zeros in a row."""
    if len(set(digits)) <= 2 or digits in "12345678901234567890" * 2 or digits in "98765432109876543210" * 2 \
            or "000000" in digits:
        return False
    ms = (int(digits) >> 22) + DISCORD_EPOCH_MS
    limit = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=366)).timestamp() * 1000
    return DISCORD_EPOCH_MS + 86400000 < ms < limit


def _home_path(line: str) -> bool:
    return any(m.group(1).lower() not in PLACEHOLDER_NAMES for m in HOME_PATH.finditer(line))


def _tailnet(line: str) -> bool:
    return any("example" not in m.group(1).lower().split(".") for m in TAILNET_HOST.finditer(line))


def _email(line: str) -> bool:
    for m in EMAIL.finditer(line):
        local, domain, tld = m.group(1), m.group(2), m.group(3)
        if local.lower() in EMAIL_OK_LOCAL or EMAIL_OK_DOMAINS.search(domain) or tld.lower() in FILE_SUFFIXES:
            continue
        return True
    return False


def _snowflake(line: str) -> bool:
    return any(plausible_snowflake(m.group(1)) for m in SNOWFLAKE.finditer(line))


IDENTIFIER_RULES: Dict[str, Callable[[str], bool]] = {
    "home-path": _home_path,
    "tailnet-host": _tailnet,
    "email": _email,
    "snowflake-id": _snowflake,
}


class Rules:
    """Everything one sweep checks: the generic denylist, the private tokens and the repository's own slug."""

    def __init__(self, tokens: Iterable[Tuple[int, str]] = (), repo_slug: Optional[str] = None):
        self.secrets = [(rid, re.compile(rx)) for rid, rx in SECRET_RULES.items()]
        self.tokens: List[Tuple[str, Pattern[str]]] = []
        for index, token in tokens:
            rx = token[3:] if token.startswith("re:") else re.escape(token)
            self.tokens.append((f"token-{index}", re.compile(rx, re.I)))
        self.slugs = [self._slug_pattern(slug) for slug in FORMER_SLUGS]
        if repo_slug:
            self.slugs.append(self._slug_pattern(repo_slug))

    @staticmethod
    def _slug_pattern(slug: str) -> Pattern[str]:
        return re.compile(r"(?<![\w.-])" + re.escape(slug.strip("/")) + r"(?![\w-])", re.I)

    def check_line(self, line: str, license_file: bool = False, private: bool = True) -> List[str]:
        for slug in self.slugs:
            line = slug.sub(" ", line)
        found = [rid for rid, rx in self.secrets if rx.search(line)]
        found += [rid for rid, check in IDENTIFIER_RULES.items() if check(line)]
        if private and not license_file:
            found += [rid for rid, rx in self.tokens if rx.search(line)]
        return found

    def check_text(self, text: str, path: str = "", private: bool = True) -> List[Tuple[int, str]]:
        """(line number, rule id) for every hit in `text`. `path` decides the LICENSE exemption; `private` gates the
        environment tokens, so pre-baseline history gets the generic denylist only."""
        license_file = path in LICENSE_FILES
        hits: List[Tuple[int, str]] = []
        for number, line in enumerate(text.splitlines(), 1):
            hits.extend((number, rid) for rid in self.check_line(line, license_file, private))
        return hits


def env_tokens(environ: Optional[Dict[str, str]] = None) -> List[Tuple[int, str]]:
    """(line number, token) for every usable line of MOCKREVIEW_SWEEP_TOKENS."""
    raw = (os.environ if environ is None else environ).get(ENV_VAR, "")
    out = []
    for index, line in enumerate(raw.splitlines(), 1):
        token = line.strip()
        if token and not token.startswith("#"):
            out.append((index, token))
    return out


def git(repo: str, *args: str, data: Optional[bytes] = None) -> bytes:
    proc = subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True)
    if proc.returncode != 0:
        raise UsageError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


class UsageError(Exception):
    pass


def repo_slug(repo: str, explicit: Optional[str] = None) -> Optional[str]:
    """owner/repo from the flag, GITHUB_REPOSITORY or the origin remote; None when none is known."""
    if explicit:
        return explicit
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    proc = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True, text=True)
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", proc.stdout.strip())
    return m.group(1) if m else None


def baseline_sha(repo: str, explicit: Optional[str] = None) -> Optional[str]:
    """The resolved baseline commit, or None when neither --since nor a baseline file is present.

    --since overrides the file. A file without a sha line, a revision that does not resolve to a commit and a commit
    that no ref reaches are usage errors: an unusable baseline must never quietly sweep nothing."""
    if explicit is not None:
        rev = explicit
    else:
        path = os.path.join(repo, BASELINE_FILE)
        if not os.path.isfile(path):
            return None
        rev = ""
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#"):
                    rev = line
                    break
        if not rev:
            raise UsageError(f"{BASELINE_FILE} has no baseline commit sha")
    proc = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}"],
                          capture_output=True, text=True)
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise UsageError(f"baseline {rev!r} is not a commit in this repository")
    if sha not in git(repo, "rev-list", "--all").decode().split():
        raise UsageError(f"baseline {rev!r} resolves to a commit that is not reachable from any ref")
    return sha


def read_objects(repo: str, ids: List[str]) -> Iterator[Tuple[str, str, bytes]]:
    """(id, type, content) for each object id, through one `git cat-file --batch`."""
    if not ids:
        return
    out = git(repo, "cat-file", "--batch", data=("\n".join(ids) + "\n").encode())
    pos = 0
    while pos < len(out):
        end = out.index(b"\n", pos)
        oid, kind, size = out[pos:end].decode().split()
        start = end + 1
        yield oid, kind, out[start:start + int(size)]
        pos = start + int(size) + 1


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8000]


def sweep_tree(repo: str, rev: str, rules: Rules) -> List[str]:
    listing = git(repo, "ls-tree", "-r", "-z", rev).decode("utf-8", "replace")
    paths: Dict[str, List[str]] = {}
    for entry in filter(None, listing.split("\0")):
        meta, path = entry.split("\t", 1)
        mode, kind, oid = meta.split()
        if kind == "blob":
            paths.setdefault(oid, []).append(path)
    hits = []
    for oid, _kind, data in read_objects(repo, list(paths)):
        if is_binary(data):
            continue
        text = data.decode("utf-8", "replace")
        for path in paths[oid]:
            hits.extend(f"{path}:{n} {rid}" for n, rid in rules.check_text(text, path))
    return sorted(hits, key=_hit_order)


def require_full_history(repo: str) -> None:
    """A shallow clone cannot prove a history sweep, so refuse it before resolving the baseline."""
    if git(repo, "rev-parse", "--is-shallow-repository").decode().strip() != "false":
        raise UsageError("shallow repository: a history sweep needs the full history (fetch-depth: 0)")


def sweep_history(repo: str, rules: Rules, baseline: Optional[str]) -> List[str]:
    commits = git(repo, "rev-list", "--all").decode().split()
    new_commits = set()
    baseline_objects = set()
    if baseline:
        new_commits = set(git(repo, "rev-list", "--all", "--not", baseline).decode().split())
        listing = git(repo, "rev-list", "--objects", baseline).decode("utf-8", "replace")
        baseline_objects = {line.split(" ", 1)[0] for line in listing.splitlines() if line}
    paths: Dict[str, str] = {}
    for line in git(repo, "rev-list", "--all", "--objects").decode("utf-8", "replace").splitlines():
        oid, _, path = line.partition(" ")
        if path:
            paths.setdefault(oid, path)
    hits = []
    for oid, kind, data in read_objects(repo, commits):
        message = data.split(b"\n\n", 1)[1] if b"\n\n" in data else b""
        private = not baseline or oid in new_commits
        for n, rid in rules.check_text(message.decode("utf-8", "replace"), private=private):
            hits.append(f"commit {oid[:12]}:{n} {rid}")
    for oid, kind, data in read_objects(repo, list(paths)):
        if kind != "blob" or is_binary(data):
            continue
        path = paths[oid]
        private = not baseline or oid not in baseline_objects
        hits.extend(f"{path}:{n} {rid} @{oid[:12]}"
                    for n, rid in rules.check_text(data.decode("utf-8", "replace"), path, private=private))
    since = f"private tokens since {baseline[:7]}" if baseline else \
        f"private tokens since the first commit (no {BASELINE_FILE})"
    print(f"history: {since}; generic rules: all {len(commits)} commit(s), {len(paths)} path object(s) scanned",
          file=sys.stderr)
    return sorted(set(hits), key=_hit_order)


def _hit_order(hit: str) -> Tuple[str, int, str]:
    m = re.match(r"(.*):(\d+) (.*)$", hit)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (hit, 0, "")


def _known_keys(hit: str) -> Tuple[str, List[str]]:
    """The rule id and its location keys for one hit line, most specific first."""
    m = re.match(r"^commit ([0-9a-f]{12}):\d+ (\S+)$", hit)
    if m:
        return m.group(2), [f"commit {m.group(1)}"]
    m = re.match(r"^(.*):(\d+) (\S+?)(?: @[0-9a-f]+)?$", hit)
    if m:
        return m.group(3), [f"{m.group(1)}:{m.group(2)}", m.group(1)]
    return "", []


def known_reason(hit: str) -> Optional[str]:
    """The KNOWN_HITS reason for this hit, or None when it is not accepted."""
    rule, locations = _known_keys(hit)
    for location in locations:
        reason = KNOWN_HITS.get((rule, location))
        if reason:
            return reason
    return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Sweep for secrets and personal identifiers (tree or history).")
    ap.add_argument("--repo", default=".", help="repository to sweep (default: the current directory)")
    ap.add_argument("--rev", default="HEAD", help="revision for tree mode (default: HEAD)")
    ap.add_argument("--history", action="store_true", help="sweep every blob and commit message of rev-list --all")
    ap.add_argument("--since", help="history mode: check private tokens only in commits after this revision "
                                    "(default: scripts/sweep-baseline in the swept repository)")
    ap.add_argument("--repo-slug", help="owner/repo whose own address is not a leak (default: GITHUB_REPOSITORY)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    tokens = env_tokens()
    if not tokens:
        if os.environ.get(REQUIRE_VAR, "").strip().lower() in TRUTHY:
            print(f"error: {REQUIRE_VAR} is set but {ENV_VAR} is empty: the repository secret {ENV_VAR} is not set, "
                  "so no private tokens were checked", file=sys.stderr)
            return 2
        prefix = "::notice::" if os.environ.get("GITHUB_ACTIONS") == "true" else "notice: "
        print(f"{prefix}{ENV_VAR} is empty, so only the generic denylist ran")
    try:
        git(args.repo, "rev-parse", "--git-dir")
        if args.since is not None and not args.history:
            raise UsageError("--since is only valid with --history")
        if args.history:
            require_full_history(args.repo)
        rules = Rules(tokens, repo_slug(args.repo, args.repo_slug))
        hits = sweep_history(args.repo, rules, baseline_sha(args.repo, args.since)) if args.history \
            else sweep_tree(args.repo, args.rev, rules)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    known = 0
    for hit in hits:
        reason = known_reason(hit)
        if reason is None:
            print(hit)
        else:
            known += 1
            print(f"known: {hit} - {reason}", file=sys.stderr)
    mode = "history" if args.history else f"tree at {args.rev}"
    accepted = f", {known} known" if known else ""
    print(f"{mode}: {len(hits)} hit(s){accepted}, {len(tokens)} private token(s), "
          f"{len(SECRET_RULES) + len(IDENTIFIER_RULES)} generic rules")
    return 1 if known < len(hits) else 0


if __name__ == "__main__":
    sys.exit(main())
