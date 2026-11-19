"""scripts/version_only.py, the cheap CI path for release-please version-only pull requests."""

import os
import shutil
import subprocess
import sys

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
import version_only  # noqa: E402

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")

ALLOWED = [
    ".release-please-manifest.json",
    "CHANGELOG.md",
    "mockreview/__init__.py",
    "plugin.yaml",
    "pyproject.toml",
]
BASE = {
    ".release-please-manifest.json": '{".": "1.0.0"}\n',
    "CHANGELOG.md": "## [1.0.0]\n",
    "mockreview/__init__.py": '__version__ = "1.0.0"\n',
    "mockreview/board/board.js": "// board\n",
    "plugin.yaml": 'version: "1.0.0"\n',
    "pyproject.toml": 'version = "1.0.0"\n',
    "README.md": "# demo\n",
}


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="Test Author", GIT_AUTHOR_EMAIL="author@example.test",
               GIT_COMMITTER_NAME="Test Author", GIT_COMMITTER_EMAIL="author@example.test",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run([GIT, "-C", str(repo), "-c", "commit.gpgsign=false", *args], env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout


def commit(repo, files, message):
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("version-only") / "repo"
    path.mkdir()
    git(path, "init", "-q")
    return path, commit(path, BASE, "base")


def at_base(repo, base):
    """A clean branch at the base commit, so every case starts from the same tree."""
    git(repo, "checkout", "-q", "-B", "case", base)


def classify(monkeypatch, capsys, repo, *args):
    monkeypatch.chdir(repo)
    code = version_only.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err


@pytest.mark.parametrize("name", ALLOWED)
@needs_git
def test_any_allowlisted_path_alone_is_version_only(repo, monkeypatch, name):
    path, base = repo
    at_base(path, base)
    commit(path, {name: "released\n"}, "release")
    monkeypatch.chdir(path)
    paths = version_only.changed_paths(base, "HEAD")
    assert paths == [name]
    assert version_only.classify(paths) == "version-only"


@needs_git
def test_all_five_release_paths_are_version_only(repo, monkeypatch, capsys):
    path, base = repo
    at_base(path, base)
    commit(path, {name: "released\n" for name in ALLOWED}, "release")
    code, out, err = classify(monkeypatch, capsys, path, "--base", base)
    assert (code, out) == (0, "version-only"), err


@needs_git
def test_a_change_outside_the_release_files_needs_the_full_matrix(repo, monkeypatch, capsys):
    path, base = repo
    at_base(path, base)
    commit(path, {"mockreview/board/board.js": "// edited\n"}, "board")
    code, out, err = classify(monkeypatch, capsys, path, "--base", base)
    assert (code, out) == (0, "full"), err


@needs_git
def test_a_mixed_diff_needs_the_full_matrix(repo, monkeypatch, capsys):
    path, base = repo
    at_base(path, base)
    commit(path, {"plugin.yaml": 'version: "1.0.1"\n', "tests/test_new_feature.py": "def test_new():\n    pass\n"},
           "release plus a fix")
    code, out, err = classify(monkeypatch, capsys, path, "--base", base)
    assert (code, out) == (0, "full"), err


@needs_git
def test_an_unresolvable_base_is_a_usage_error(repo, monkeypatch, capsys):
    path, _ = repo
    code, out, err = classify(monkeypatch, capsys, path, "--base", "0" * 40)
    assert code == 2 and not out
    assert "does not resolve to a commit" in err


@needs_git
def test_an_identical_head_and_base_keep_the_full_matrix(repo, monkeypatch, capsys):
    """An empty diff proves nothing about the code: it must not earn the cheap path."""
    path, base = repo
    at_base(path, base)
    assert version_only.classify([]) == "full"
    code, out, err = classify(monkeypatch, capsys, path, "--base", base, "--head", base)
    assert (code, out) == (0, "full"), err


@needs_git
def test_a_directory_outside_a_git_repository_is_a_usage_error(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    code, out, err = classify(monkeypatch, capsys, plain, "--base", "HEAD")
    assert code == 2 and not out
    assert "not a git repository" in err
