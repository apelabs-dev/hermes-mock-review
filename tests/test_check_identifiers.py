"""scripts/check_identifiers.py, the CI identifier and secret sweep, against temporary git repositories.

Planted secrets are assembled at run time so this file itself passes the sweep."""

import os
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
import check_identifiers  # noqa: E402

GITHUB_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5
PRIVATE_KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
TOKEN = "Zebra-Quill-7731"


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="Test Author", GIT_AUTHOR_EMAIL="author@example.test",
               GIT_COMMITTER_NAME="Test Author", GIT_COMMITTER_EMAIL="author@example.test",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args], env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout


def make_repo(path, files, message="initial"):
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        git(path, "init", "-q")
    for name, content in files.items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            f.unlink()
        elif isinstance(content, bytes):
            f.write_bytes(content)
        else:
            f.write_text(content, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "--allow-empty", "-m", message)
    return path


@pytest.fixture
def sweep(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv(check_identifiers.REQUIRE_VAR, raising=False)

    def run(repo, *args, tokens=None):
        if tokens is None:
            monkeypatch.delenv(check_identifiers.ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(check_identifiers.ENV_VAR, "\n".join(tokens))
        code = check_identifiers.main(["--repo", str(repo), *args])
        out = capsys.readouterr()
        return code, out.out, out.err
    return run


def test_planted_generic_secret_fails_without_printing_it(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"README.md": "intro\n", "app/config.py": f"KEY = '{GITHUB_TOKEN}'\n"})
    code, out, _ = sweep(repo)
    assert code == 1
    assert "app/config.py:1 github-token" in out
    assert GITHUB_TOKEN not in out


def test_env_token_in_another_case_fails(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"docs/notes.md": "line one\nmade on " + TOKEN.upper() + "'s laptop\n"})
    code, out, _ = sweep(repo, tokens=["# comment line", TOKEN.lower()])
    assert code == 1
    assert "docs/notes.md:2 token-2" in out
    assert TOKEN.lower() not in out.lower()


def test_token_that_exists_nowhere_passes(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"README.md": "Nothing personal here.\n", "a.py": "x = 1\n"})
    code, out, _ = sweep(repo, tokens=["impossible-" + "q9z7" * 4])
    assert code == 0
    assert "0 hit(s), 1 private token(s)" in out
    code, out, _ = sweep(repo, "--history", tokens=["impossible-" + "q9z7" * 4])
    assert code == 0


def test_license_is_exempt_from_tokens_only(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"LICENSE": f"Copyright (c) 2026 {TOKEN}\n", "NOTICE.md": f"by {TOKEN}\n"})
    code, out, _ = sweep(repo, tokens=[TOKEN])
    assert code == 1 and "NOTICE.md:1 token-1" in out and "LICENSE" not in out
    make_repo(repo, {"NOTICE.md": None, "LICENSE": f"Copyright (c) 2026 {TOKEN}\n{PRIVATE_KEY}\n"})
    code, out, _ = sweep(repo, tokens=[TOKEN])
    assert code == 1
    assert out.splitlines()[-2] == "LICENSE:2 private-key"


def test_repo_url_and_slug_are_not_leaks(tmp_path, sweep):
    readme = ("See https://github.com/acme-owner/widget for the source.\n"
              "    hermes plugins install acme-owner/widget\n")
    repo = make_repo(tmp_path / "r", {"README.md": readme})
    code, out, _ = sweep(repo, "--repo-slug", "acme-owner/widget", tokens=["acme-owner"])
    assert code == 0, out
    code, out, _ = sweep(repo, tokens=["acme-owner"])
    assert code == 1 and "README.md:1 token-1" in out and "README.md:2 token-1" in out
    make_repo(repo, {"README.md": readme + "Written by acme-owner.\nhttps://github.com/acme-owner/other\n"})
    code, out, _ = sweep(repo, "--repo-slug", "acme-owner/widget", tokens=["acme-owner"])
    assert code == 1 and "README.md:3 token-1" in out and "README.md:4 token-1" in out
    assert "README.md:1 " not in out and "README.md:2 " not in out


def test_github_repository_env_sets_the_slug(tmp_path, sweep, monkeypatch):
    repo = make_repo(tmp_path / "r", {"README.md": "https://github.com/acme-owner/widget\n"})
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme-owner/widget")
    monkeypatch.setenv(check_identifiers.ENV_VAR, "acme-owner")
    assert check_identifiers.main(["--repo", str(repo)]) == 0


def test_history_finds_removed_tokens_and_messages_but_not_identities(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"notes.txt": f"owner {TOKEN}\n"})
    make_repo(repo, {"notes.txt": "clean now\n"}, message=f"remove the note\n\nIt named {TOKEN.lower()}.")
    code, out, _ = sweep(repo, tokens=[TOKEN])
    assert code == 0                                            # the tip is clean
    code, out, err = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 1
    assert any(line.startswith("notes.txt:1 token-1 @") for line in out.splitlines())
    assert any(line.startswith("commit ") and line.endswith(":3 token-1") for line in out.splitlines())
    assert "2 commit(s)" in err
    code, out, _ = sweep(repo, "--history", tokens=["Test Author", "author@example.test"])
    assert code == 0                                            # author and committer fields are not swept


def test_history_refuses_a_shallow_clone(tmp_path, sweep):
    origin = make_repo(tmp_path / "origin", {"a.txt": "one\n"})
    make_repo(origin, {"a.txt": "two\n"})
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", origin.as_uri(), str(shallow)], check=True,
                   capture_output=True)
    code, _, err = sweep(shallow, "--history")
    assert code == 2 and "shallow" in err
    code, _, _ = sweep(shallow)                                  # tree mode is fine on a shallow clone
    assert code == 0


def test_history_refuses_a_shallow_clone_before_resolving_an_older_baseline(tmp_path, sweep):
    origin = make_repo(tmp_path / "origin", {"a.txt": "one\n"})
    older = head_sha(origin)
    make_repo(origin, {"a.txt": "two\n"})
    write_baseline(origin, older)
    make_repo(origin, {"a.txt": "three\n"})
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", origin.as_uri(), str(shallow)], check=True,
                   capture_output=True)
    code, _, err = sweep(shallow, "--history", tokens=[TOKEN])
    assert code == 2 and "shallow" in err
    assert "is not a commit" not in err


def test_generic_identifier_shapes_and_placeholders(tmp_path, sweep):
    bad = {
        "home.md": "see /" + "Users/jdoe/projects/app\n",
        "linux.md": "cd /ho" + "me/jdoe/src\n",
        "win.md": "C:\\" + "Users\\jdoe\\app\n",
        "host.md": "https://laptop.tail1a2b3c." + "ts.net/r/1\n",
        "mail.md": "contact jdoe" + "@corp-mail.io\n",
        "id.md": "thread 12903512345" + "67891234\n",
    }
    repo = make_repo(tmp_path / "bad", bad)
    code, out, _ = sweep(repo)
    assert code == 1
    for name, rule in [("home.md", "home-path"), ("linux.md", "home-path"), ("win.md", "home-path"),
                       ("host.md", "tailnet-host"), ("mail.md", "email"), ("id.md", "snowflake-id")]:
        assert f"{name}:1 {rule}" in out
    good = {
        "ok.md": "\n".join([
            "/home/runner/work and /" + "Users/<you>/ and C:\\" + "Users\\you\\",
            "https://box.example.ts.net:8790",
            "reviewer@example.test, ops@example.com, 123+user@users.noreply.github.com, noreply@vendor.test",
            "git@github.com:acme/widget.git and logo@2x.png",
            "fixtures 1490000000000000001 and 11111111111111111111 and 12345678901234567890",
        ]) + "\n",
        "image.png": b"\x89PNG\r\n\x1a\n\x00\x00" + GITHUB_TOKEN.encode(),
    }
    repo = make_repo(tmp_path / "good", good)
    code, out, _ = sweep(repo)
    assert code == 0, out


def test_empty_token_list_says_only_the_generic_denylist_ran(tmp_path, sweep, monkeypatch):
    repo = make_repo(tmp_path / "r", {"a.txt": "fine\n"})
    code, out, _ = sweep(repo, tokens=["", "  "])
    assert code == 0 and out.startswith("notice: ") and "only the generic denylist ran" in out
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code, out, _ = sweep(repo)
    assert out.startswith("::notice::")


def test_usage_errors_exit_2(tmp_path, sweep, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))   # keep git discovery below the pytest tmp dir
    assert sweep(tmp_path, tokens=[])[0] == 2                    # not a repository
    repo = make_repo(tmp_path / "r", {"a.txt": "x\n"})
    assert sweep(repo, "--rev", "no-such-rev")[0] == 2
    assert sweep(repo, "--bogus-flag")[0] == 2


def test_require_tokens_fails_closed_on_an_empty_list(tmp_path, sweep, monkeypatch):
    repo = make_repo(tmp_path / "r", {"a.txt": "fine\n"})
    monkeypatch.setenv(check_identifiers.REQUIRE_VAR, "YES")
    code, out, err = sweep(repo, tokens=["", "  "])
    assert code == 2 and check_identifiers.ENV_VAR in err
    assert "only the generic denylist ran" not in out + err


def test_empty_token_list_without_the_require_variable_only_notices(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"a.txt": "fine\n"})
    code, out, err = sweep(repo)
    assert code == 0 and "only the generic denylist ran" in out
    assert err == ""


def test_a_nonempty_token_list_with_the_require_variable_still_fails_on_a_secret(tmp_path, sweep, monkeypatch):
    repo = make_repo(tmp_path / "r", {"a.txt": f"KEY = '{GITHUB_TOKEN}'\n"})
    monkeypatch.setenv(check_identifiers.REQUIRE_VAR, "true")
    code, out, _ = sweep(repo, tokens=[TOKEN])
    assert code == 1 and "a.txt:1 github-token" in out


def test_former_own_address_is_not_a_leak_but_a_bare_token_still_is(tmp_path, sweep):
    former = check_identifiers.FORMER_SLUGS[0]
    repo = make_repo(tmp_path / "r", {"README.md": f"See https://github.com/{former} for the source.\n"
                                                  f"The old name was {TOKEN}.\n"})
    code, out, _ = sweep(repo, tokens=[TOKEN, former.split("/")[0]])
    assert code == 1 and "README.md:2 token-1" in out
    assert "README.md:1 " not in out and "token-2" not in out


def test_a_known_hit_is_accepted_and_reported_but_the_same_token_elsewhere_is_not(tmp_path, sweep, monkeypatch):
    repo = make_repo(tmp_path / "r", {"README.md": "clean\n"}, message=f"initial\n\nnow {TOKEN}")
    sha = git(repo, "rev-parse", "HEAD").strip()[:12]
    monkeypatch.setitem(check_identifiers.KNOWN_HITS, ("token-1", f"commit {sha}"), "test reason")
    code, out, err = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 0
    assert f"known: commit {sha}:3 token-1 - test reason" in err
    assert "1 hit(s), 1 known" in out
    make_repo(repo, {"notes.md": f"{TOKEN}\n"})
    code, out, _ = sweep(repo, tokens=[TOKEN])
    assert code == 1 and "notes.md:1 token-1" in out


VENDOR_PATH = "mockreview/board/vendor/" + "turndown.min.js"
VENDOR_EMAIL = "luc" + "thevenard" + "@" + "gmail" + ".com"
OTHER_EMAIL = "other" + ".person" + "@" + "vendor-mail" + ".io"


def vendor_lines(address=VENDOR_EMAIL, line=331):
    return "\n" * (line - 1) + address + "\n"


def test_the_real_tree_reports_only_the_vendor_attribution_as_known(sweep):
    code, out, err = sweep(ROOT)
    assert code == 0
    assert "1 hit(s), 1 known" in out
    assert f"known: {VENDOR_PATH}:331 email" in err


def test_the_same_vendor_address_on_another_line_still_fails(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {VENDOR_PATH: vendor_lines() + VENDOR_EMAIL + "\n"})
    code, out, err = sweep(repo)
    assert code == 1
    assert f"{VENDOR_PATH}:332 email" in out
    assert f"{VENDOR_PATH}:331 email" not in out
    assert f"known: {VENDOR_PATH}:331 email" in err


def test_the_same_vendor_address_in_another_file_still_fails(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {VENDOR_PATH: vendor_lines(), "notes.md": VENDOR_EMAIL + "\n"})
    code, out, err = sweep(repo)
    assert code == 1
    assert "notes.md:1 email" in out
    assert f"{VENDOR_PATH}:331 email" not in out
    assert f"known: {VENDOR_PATH}:331 email" in err


def test_a_different_address_in_the_vendor_file_still_fails(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {VENDOR_PATH: vendor_lines() + OTHER_EMAIL + "\n"})
    code, out, err = sweep(repo)
    assert code == 1
    assert f"{VENDOR_PATH}:332 email" in out
    assert f"{VENDOR_PATH}:331 email" not in out
    assert f"known: {VENDOR_PATH}:331 email" in err


def head_sha(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def write_baseline(repo, sha):
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "sweep-baseline").write_text(sha + "\n# recorded baseline\n", encoding="utf-8")
    return sha


def test_history_private_tokens_skip_commits_before_the_baseline(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"notes.txt": f"owner {TOKEN}\n"})
    make_repo(repo, {"notes.txt": "clean now\n"})
    baseline = write_baseline(repo, head_sha(repo))
    make_repo(repo, {"later.txt": "innocuous\n"})
    code, out, err = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 0, out
    assert "0 hit(s), 1 private token(s)" in out
    assert f"private tokens since {baseline[:7]}" in err
    assert "generic rules: all 3 commit(s)" in err


def test_history_does_not_flag_a_token_in_a_commit_message_before_the_baseline(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"older.txt": "older\n"}, message=f"older {TOKEN}")
    repo = make_repo(repo, {"base.txt": "base\n"}, message=f"initial {TOKEN}")
    baseline = write_baseline(repo, head_sha(repo))
    make_repo(repo, {"later.txt": "clean\n"})
    code, out, err = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 0, out
    assert "token-1" not in out
    assert f"private tokens since {baseline[:7]}" in err


def test_history_names_a_post_baseline_token_in_a_blob_and_in_a_commit_message(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"base.txt": "base\n"})
    baseline = write_baseline(repo, head_sha(repo))
    make_repo(repo, {"notes.txt": f"owner {TOKEN}\n"}, message=f"add a note\n\nmentioning {TOKEN}")
    sha = head_sha(repo)
    code, out, err = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 1
    assert any(line.startswith("notes.txt:1 token-1 @") for line in out.splitlines())
    assert f"commit {sha[:12]}:3 token-1" in out
    assert f"private tokens since {baseline[:7]}" in err


def test_history_scans_a_blob_rewritten_after_the_baseline_but_not_an_unchanged_one(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"keep.txt": f"owner {TOKEN}\n"})
    write_baseline(repo, head_sha(repo))
    make_repo(repo, {"other.txt": "clean\n"})
    code, out, _ = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 0, out
    make_repo(repo, {"keep.txt": f"changed {TOKEN}\n"})
    code, out, _ = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 1
    assert any(line.startswith("keep.txt:1 token-1 @") for line in out.splitlines())


def test_history_generic_rules_still_sweep_commits_before_the_baseline(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"home.md": "see /" + "Users/jdoe/projects/app\n"})
    write_baseline(repo, head_sha(repo))
    make_repo(repo, {"later.txt": "clean\n"})
    code, out, _ = sweep(repo, "--history", tokens=["impossible-" + "q9z7" * 4])
    assert code == 1
    assert any(line.startswith("home.md:1 home-path @") for line in out.splitlines())


def test_since_overrides_the_baseline_file(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"base.txt": "clean\n"})
    older = head_sha(repo)
    make_repo(repo, {"notes.txt": f"owner {TOKEN}\n"})
    newer = head_sha(repo)
    write_baseline(repo, older)
    code, out, _ = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 1 and "notes.txt:1 token-1" in out
    code, _, err = sweep(repo, "--history", "--since", newer, tokens=[TOKEN])
    assert code == 0
    assert f"private tokens since {newer[:7]}" in err
    write_baseline(repo, newer)
    code, out, _ = sweep(repo, "--history", "--since", older, tokens=[TOKEN])
    assert code == 1 and "notes.txt:1 token-1" in out


def test_a_bogus_or_unreachable_baseline_is_a_usage_error(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"a.txt": "clean\n"})
    write_baseline(repo, "0123456789" + "abcdef" * 5)
    code, _, err = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 2 and "baseline" in err
    code, _, _ = sweep(repo, "--history", "--since", "no-such-rev", tokens=[TOKEN])
    assert code == 2
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    orphan = git(repo, "commit-tree", tree, "-m", "x").strip()
    code, _, err = sweep(repo, "--history", "--since", orphan, tokens=[TOKEN])
    assert code == 2 and "not reachable" in err
    (repo / "scripts" / "sweep-baseline").write_text("# only a comment\n", encoding="utf-8")
    code, _, err = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 2 and "no baseline commit sha" in err


def test_since_is_rejected_in_tree_mode(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"a.txt": "clean\n"})
    code, _, err = sweep(repo, "--since", "HEAD")
    assert code == 2 and "--history" in err


def test_without_a_baseline_file_private_tokens_sweep_all_history(tmp_path, sweep):
    repo = make_repo(tmp_path / "r", {"notes.txt": f"owner {TOKEN}\n"})
    make_repo(repo, {"notes.txt": "clean now\n"})
    code, out, err = sweep(repo, "--history", tokens=[TOKEN])
    assert code == 1
    assert any(line.startswith("notes.txt:1 token-1 @") for line in out.splitlines())
    assert "no scripts/sweep-baseline" in err


def test_ci_sweep_job_sweeps_the_full_history_with_the_private_tokens():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["sweep"]
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0
    steps = [step for step in job["steps"] if "check_identifiers.py" in step.get("run", "")]
    assert len(steps) == 2
    # github.event.pull_request.head.repo.fork is absent on a push, so `!= true` is true there and false only for a
    # pull request from a fork: push runs must fail on an empty token list, fork runs may not.
    for step in steps:
        assert step["env"]["MOCKREVIEW_SWEEP_TOKENS"] == "${{ secrets.MOCKREVIEW_SWEEP_TOKENS }}"
        assert step["env"]["MOCKREVIEW_REQUIRE_TOKENS"] == "${{ github.event.pull_request.head.repo.fork != true }}"
    assert steps[0]["run"] == "python3 scripts/check_identifiers.py"
    assert steps[1]["run"] == "python3 scripts/check_identifiers.py --history"
