#!/usr/bin/env python3
"""Classify a pull request's diff for CI: a release-only change needs no test matrix.

    python3 scripts/version_only.py --base <sha> [--head <rev>]

Release Please pulls only re-serialise the version, so their pull requests touch just the files the action rewrites.
Prints `version-only` when every path in `git diff --name-only <base> <rev>` is one of those files, and `full` otherwise.
Both classifications exit 0; a usage error (no base, an unresolvable revision, not a git repository) exits 2.

Read-only and offline: it validates the revisions, runs one local diff, and never fetches. A shallow checkout works
when both commits are present, so the workflow fetches the pull request's base sha before calling it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import List, Optional, Sequence

ALLOWED = frozenset({
    "plugin.yaml",
    "pyproject.toml",
    "mockreview/__init__.py",
    ".release-please-manifest.json",
    "CHANGELOG.md",
})
VERSION_ONLY = "version-only"
FULL = "full"


def git(*args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def resolve(rev: str) -> Optional[str]:
    """The commit sha `rev` names, or None when it does not resolve here."""
    result = git("rev-parse", "--verify", "--quiet", rev + "^{commit}")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def changed_paths(base: str, head: str) -> List[str]:
    result = git("diff", "--name-only", base, head)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return [line for line in result.stdout.splitlines() if line]


def classify(paths: Sequence[str]) -> str:
    # An empty diff proves nothing about the code, so it never earns the cheap path: only a diff that really is
    # release files and nothing else may skip the tiers, which is the safe direction for a required check.
    if not paths:
        return FULL
    return VERSION_ONLY if all(path in ALLOWED for path in paths) else FULL


def fail(message: str) -> int:
    print("version_only: " + message, file=sys.stderr)
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Print version-only when a diff touches only release files.")
    parser.add_argument("--base", required=True, help="the base commit of the pull request")
    parser.add_argument("--head", default="HEAD", help="the commit to compare with (default: HEAD)")
    args = parser.parse_args(argv)

    try:
        inside = git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return fail("not a git repository")
        base = resolve(args.base)
        if base is None:
            return fail("base revision {!r} does not resolve to a commit".format(args.base))
        head = resolve(args.head)
        if head is None:
            return fail("head revision {!r} does not resolve to a commit".format(args.head))
        paths = changed_paths(base, head)
    except OSError as exc:
        return fail("git could not be run: {}".format(exc))
    except RuntimeError as exc:
        return fail(str(exc))

    print(classify(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
