"""scripts/check_identifiers.py, the CI identifier and secret sweep, against temporary git repositories.

Planted secrets are assembled at run time so this file itself passes the sweep."""

import os
import subprocess
import sys

import pytest

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


def test_usage_errors_exit_2(tmp_path, sweep):
    assert sweep(tmp_path, tokens=[])[0] == 2                    # not a repository
    repo = make_repo(tmp_path / "r", {"a.txt": "x\n"})
    assert sweep(repo, "--rev", "no-such-rev")[0] == 2
    assert sweep(repo, "--bogus-flag")[0] == 2
