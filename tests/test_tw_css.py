"""The committed Tailwind stylesheet is built from its sources, never edited by hand.

The compiled file is what the board server ships, so a release needs no Node. When Node and the local CLI are
present this test rebuilds from `mockreview/board/src/board.src.css` and fails if the bytes differ. Without Node it
skips with a named reason. CI's dev-only `ui-css` job installs the UI toolchain with `npm ci` and runs
`npm run check:css`, so a stale committed file fails the build; the Python matrix and the plugin runtime stay
Node-free.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "mockreview" / "board" / "src" / "board.src.css"
OUT = ROOT / "mockreview" / "board" / "tw.css"
CLI = ROOT / "node_modules" / "@tailwindcss" / "cli" / "dist" / "index.mjs"


def _node():
    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="Node is not installed: the UI build is a release step")
@pytest.mark.skipif(not CLI.is_file(), reason="Tailwind CLI is not installed: run npm install first")
def test_committed_tw_css_is_current(tmp_path):
    out = tmp_path / "tw.css"
    proc = subprocess.run(
        [_node(), str(CLI), "-i", str(SRC), "-o", str(out)],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert proc.returncode == 0, f"tailwind build failed:\n{proc.stdout}\n{proc.stderr}"
    built = out.read_text(encoding="utf-8")
    committed = OUT.read_text(encoding="utf-8")
    assert built.replace("\r\n", "\n") == committed.replace("\r\n", "\n"), (
        "mockreview/board/tw.css is stale: run `npm run build:css` and commit the result"
    )


def test_the_board_and_settings_link_the_committed_stylesheet():
    html = (ROOT / "mockreview" / "board" / "index.html").read_text(encoding="utf-8")
    assert 'href="tw.css"' in html
    settings = (ROOT / "mockreview" / "pages.py").read_text(encoding="utf-8")
    assert 'href="/_/tw.css"' in settings
