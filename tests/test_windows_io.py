"""Portable text I/O and subprocess boundaries, including a real legacy-encoding stream."""

import ast
import io
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from conftest import ROOT, python_child_env
from mockreview import console, owner, setup
from test_setup import SHIM_SRC


def text_calls(source):
    """Yield actual text file calls, including multiline calls and Path/file-like open methods."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name == "tarfile.open":  # archive API, not a text stream
            continue
        method = name.rsplit(".", 1)[-1]
        if method not in {"open", "fdopen", "read_text", "write_text"}:
            continue
        if method in {"open", "fdopen"}:
            # Builtin/io.open/os.fdopen take the path/fd first; Path.open takes mode first.
            index = 1 if name in {"open", "io.open", "os.fdopen"} else 0
            mode = next((k.value for k in node.keywords if k.arg == "mode"),
                        node.args[index] if len(node.args) > index else ast.Constant("r"))
            assert isinstance(mode, ast.Constant), f"audit dynamic mode at line {node.lineno}"
            if "b" in mode.value:
                continue
        yield node, name


def test_text_files_use_explicit_utf8():
    sources = [(p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8"))
               for folder in ("mockreview", "tests") for p in sorted((ROOT / folder).rglob("*.py"))]
    sources.append(("tests/test_setup.py:SHIM_SRC", SHIM_SRC))
    missing = []
    for path, source in sources:
        for node, name in text_calls(source):
            encoding = next((k.value for k in node.keywords if k.arg == "encoding"), None)
            if not isinstance(encoding, ast.Constant) or encoding.value != "utf-8":
                missing.append(f"{path}:{node.lineno}: {name}")
    assert not missing, "\n".join(missing)


def test_text_subprocesses_use_explicit_decoding():
    for folder in ("mockreview", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                kwargs = {k.arg: k.value for k in node.keywords}
                if not any(isinstance(kwargs.get(k), ast.Constant) and kwargs[k].value is True
                           for k in ("text", "universal_newlines")):
                    continue
                for key, value in (("encoding", "utf-8"), ("errors", "replace")):
                    assert isinstance(kwargs.get(key), ast.Constant) and kwargs[key].value == value, (
                        f"{path.relative_to(ROOT)}:{node.lineno}: {key}")


def test_strict_cp1252_console_and_unicode_files(tmp_path):
    # Assertions execute inside an actual TextIOWrapper-backed child, outside pytest's capture streams.
    source = r'''
import io, json, sys
from pathlib import Path
from types import SimpleNamespace
from mockreview import cli, setup, store
assert isinstance(sys.stdout, io.TextIOWrapper)
assert sys.stdout.encoding.lower() == "cp1252" and sys.stdout.errors == "strict"
sys.stderr.reconfigure(errors="strict")
text = "Review \u2713 \u4e16\u754c \U0001f600"
path = Path(sys.argv[1]) / "feedback.json"
store.atomic_write(path, json.dumps({"text": text}, ensure_ascii=False))
assert json.loads(path.read_text(encoding="utf-8"))["text"] == text
assert text.encode("utf-8") in path.read_bytes()
def command(args):
    print(text)
    print(text, file=sys.stderr)
    return 0
assert cli.run(SimpleNamespace(**{cli.FN: command})) == 0
assert sys.stdout.errors == sys.stderr.errors == "strict"
# The setup command prints Unicode route descriptions even with no external calls in dry-run.
setup.run_hermes = lambda args, **kw: SimpleNamespace(
    stdout="Hermes v0.22.0" if args == ["--version"] else "", stderr="", returncode=1)
setup.port_free = lambda port: (True, "")
assert cli.main(["setup", "--dry-run", "--hosting", "local", "--origin-platform", "telegram"]) == 0
assert sys.stdout.errors == sys.stderr.errors == "strict"
'''
    env = python_child_env(tmp_path)
    env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="cp1252:strict", PYTHONUTF8="0",
               HERMES_HOME=str(tmp_path / "hermes"), MOCKREVIEW_CONFIG=str(tmp_path / "absent.yaml"))
    proc = subprocess.run([sys.executable, "-c", source, str(tmp_path)], env=env, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode("cp1252", "replace")
    assert b"Review ? ?? ?" in proc.stdout and b"Review ? ?? ?" in proc.stderr


def test_console_capture_and_embedding_are_preserved(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    with console.safe_output():
        print("世界 ✓")
        assert sys.stdout is stream
    assert stream.getvalue() == "世界 ✓\n"


def test_console_restores_errors_after_failure(monkeypatch):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    with pytest.raises(RuntimeError), console.safe_output():
        print("✓")
        raise RuntimeError("command failed")
    assert stream.errors == "strict"


def test_hermes_executable_and_invalid_subprocess_bytes(monkeypatch):
    # Exercise the real argv-list runner; Windows uses Hermes's installed console-script .exe.
    monkeypatch.setenv("MOCKREVIEW_HERMES_BIN", sys.executable)
    proc = setup.run_hermes(["-c", "import sys; sys.stdout.buffer.write(b'\\xff'); sys.stderr.buffer.write(b'\\xfe')"])
    assert proc.returncode == 0 and proc.stdout == proc.stderr == "\ufffd"


def test_archive_keys_use_posix_spelling_for_windows_paths(monkeypatch):
    root = PureWindowsPath("C:/reviews/example")
    child = root / "rounds" / "1"

    monkeypatch.setattr(PureWindowsPath, "is_symlink", lambda self: False, raising=False)
    monkeypatch.setattr(PureWindowsPath, "is_file", lambda self: False, raising=False)
    # _tree_digest only needs rglob from its root; relative_to accepts the simulated Windows root.
    class WindowsTree(PureWindowsPath):
        def rglob(self, pattern):
            return [child]

    keys = owner._tree_digest(WindowsTree(root))
    assert keys == {"rounds/1": "dir"} and all("\\" not in key for key in keys)


def test_child_environment_keeps_windows_runtime_and_isolates_apps(tmp_path, monkeypatch):
    for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT"):
        monkeypatch.setenv(key, "runtime-value")
    for key in ("HERMES_HOME", "MOCKREVIEW_HOME", "PYTHONPATH", "PYTHONHOME", "HERENOW_API_KEY"):
        monkeypatch.setenv(key, "discard-me")
    env = python_child_env(tmp_path)
    runtime = {key.upper(): value for key, value in env.items()}
    for key in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT"):
        assert runtime[key.upper()] == "runtime-value"
    assert not any(value == "discard-me" for value in env.values())
    assert all(Path(env[key]).is_relative_to(tmp_path)
               for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"))
