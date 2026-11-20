#!/usr/bin/env python3
"""The test-speed check loop: one command per kind of verification, standard library only.

    python scripts/check.py fast                  # every non-browser test, parallel, fail-fast (the fix loop)
    python scripts/check.py changed --base main   # the tests a branch's changed files can affect, plus smoke
    python scripts/check.py changed --files mockreview/board/board.css --dry-run
    python scripts/check.py full                  # every test: non-browser in parallel, then browser modules
    python scripts/check.py full --part browser   # one bounded half (each is quick enough for one tool call)
    python scripts/check.py full --all            # see every failure instead of stopping the run early

Tiers
    fast     The non-browser tests, under pytest-xdist. Target: under two minutes.
    changed  A branch's changed files mapped to tests through the table below, always plus SMOKE. Unmapped
             paths fall back to the non-browser tier, because a narrower guess could miss the affected test.
             `full` is the acceptance run; `changed` is the per-round gate.
    full     The non-browser tier in parallel, then the browser modules in a pool. Each browser module runs
             in its own pytest process (and with up to three xdist workers inside it), so the module-scoped
             Chrome fixture is reused and the machine-wide slot files hold the cap. The browser phase starts
             only when the non-browser phase passed, unless --all asks to see every failure.

Browser inventory
    The browser modules are not the hand-kept list alone: one `pytest --collect-only -m browser` call finds every
    module that carries the mark, and the run is the union of that inventory and BROWSER_JOBS. A declared module
    that exists without the mark is an error; a marked module that is not declared runs anyway (one worker,
    with a warning). A failed collection aborts the run instead of guessing a plan.

Chrome budget
    MOCKREVIEW_CHROMES (default 3, never more than 4) is this run's budget; tests/browserkit.py also enforces
    the machine-wide ceiling of 4 with slot files shared by every pytest run. Every Chrome a browser job starts
    waits for a slot, so two concurrent runs respect each other. A job asking for more workers than the budget
    runs at the budget; work that still cannot be scheduled is reported as a failure, never dropped.

Environment knobs
    CHECK_ROOT           run against another tree (default: this checkout; the inventory and plans are relative to it)
    CHECK_WORKERS        non-browser xdist workers (default: min(8, CPU count - 2))
    CHECK_CHROMES        this run's Chrome budget (default: MOCKREVIEW_CHROMES or 3)
    CHECK_BROWSER_BASE   browser job log/XML base (default: a temp dir)
    CHECK_LOG_DIR        write logs/XML into this directory (created if missing), keep it even when green, and
                         print its absolute path after the summary (CI uploads it); default is a temp dir kept
                         only on red or CHECK_KEEP_LOGS
    CHECK_KEEP_LOGS=1    keep the logs of a green run (a red one is always kept)

Exit status is 1 when any selected test fails, a pytest run produced no report, or a part ran nothing. The
last line is the summary: counts, wall time, workers and Chrome budget, and the log directory when it is kept.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CHECK_ROOT") or SCRIPT_ROOT).resolve()
TESTS = ROOT / "tests"

# Browser test modules, heaviest first (the measured serial seconds are what the order encodes). units = xdist
# workers inside that pytest process; each worker owns at most one Chrome, and the pool never has more units
# running than the Chrome budget. The list is not the inventory: discover_browser_files() finds every module
# that actually carries pytest.mark.browser, and browser_jobs() unions the two.
BROWSER_JOBS: List[Tuple[str, int]] = [
    ("tests/test_ui_matrix.py", 3),
    ("tests/test_header_integration.py", 3),
    ("tests/test_export_browser.py", 3),
    ("tests/test_board_canvas.py", 3),
    # one worker each; 215 s / 215 s, 148 s / 203 s and 68 s / 113 s serial on the macOS / Windows runner (browser
    # tier). Undeclared they ran after every listed module, so the tier ended on them instead of starting by weight.
    ("tests/test_header_hdr.py", 1),
    ("tests/test_page_mode.py", 1),
    ("tests/test_avatar_colors.py", 1),
    # one worker each: 112 s / 61 s serial on the macOS runner, 131-168 s / 79-119 s on Windows; declared so they
    # start by weight instead of joining the tail of the run
    ("tests/test_cux_anchors.py", 1),
    ("tests/test_board_threads.py", 3),
    ("tests/test_share_design.py", 3),
    ("tests/test_anchors.py", 3),
    ("tests/test_pin_stack.py", 1),
    ("tests/test_board_share.py", 2),
    ("tests/test_page_marks.py", 2),
    # one worker: its module-scoped fixtures (scenario and navigation) can each hold a Chrome at once
    ("tests/test_artifact_browser.py", 1),
    ("tests/test_media_range.py", 1),
    ("tests/test_artifact_data.py", 1),
    ("tests/test_live_round_tool.py", 2),
    ("tests/test_threads_scope.py", 2),
    ("tests/test_board_frames_threads.py", 1),
    ("tests/test_browserkit.py", 1),
]

# Board-side test modules that never start a browser themselves; they join every browser module in BOARD_TESTS.
BOARD_ONLY_TESTS = [
    "tests/test_board_artifact.py",
    "tests/test_board_links.py",
    "tests/test_board_plan.py",
    "tests/test_pin_fix.py",
    "tests/test_share_dialog.py",
    "tests/test_design_system.py",
    "tests/test_tw_css.py",
]


def board_tests(browser: Sequence[str]) -> List[str]:
    """Every test a board asset can affect: the browser modules plus the board-side helpers."""
    return [name for name in browser if name != "tests/test_browserkit.py"] + list(BOARD_ONLY_TESTS)


# Always run by `changed`: the core store, submit path, settings and release consistency, about five seconds.
SMOKE = [
    "tests/test_store.py",
    "tests/test_submit.py",
    "tests/test_settings.py",
    "tests/test_release_compliance.py",
]

PLUGIN_TESTS = [
    "tests/test_plugin.py",
    "tests/test_release_config.py",
    "tests/test_release_compliance.py",
    "tests/test_build_flow.py",
]

# Path globs whose tests are not an import away. First match wins. None means "every board test", which is
# resolved from the discovered browser inventory at run time.
MAP: List[Tuple[str, Optional[List[str]]]] = [
    ("mockreview/board/src/*", None),
    ("mockreview/board/tw.css", None),
    ("mockreview/board/*", None),
    ("plugin.yaml", PLUGIN_TESTS),
    ("pyproject.toml", PLUGIN_TESTS),
    ("requirements-dev.txt", PLUGIN_TESTS),
    ("scripts/check_identifiers.py", ["tests/test_check_identifiers.py"]),
    ("scripts/sse_probe.py", ["tests/test_sse_probe.py"]),
    ("scripts/gen_plugin_manifest.py", ["tests/test_plugin.py", "tests/test_release_compliance.py"]),
    ("scripts/gen_docs_reference.py", ["tests/test_release_compliance.py"]),
    ("scripts/check_docs.py", ["tests/test_release_compliance.py"]),
    ("scripts/tempguard.py", ["tests/test_tempguard.py"]),
    ("README.md", []),
    ("docs/*", []),
    ("CHANGELOG.md", []),
    (".github/*", []),
]
UNMAPPED_NOTE = "unmapped -> fast tier (every non-browser test)"


def _env_int(name: str, default: int, lo: int = 1, hi: Optional[int] = None) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if hi is not None:
        value = min(hi, value)
    return max(lo, value)


_CPU = os.cpu_count() or 4
_SMALL_MAC = sys.platform == "darwin" and _CPU <= 6
if _SMALL_MAC:
    # The macos-26-arm64 runner has 3 performance cores and reports 5 CPUs to Python, so the generic defaults
    # (3 pytest workers, 3 Chromes) put six CPU-heavy processes on it: every browser module's wall time blew up
    # (test_frame_tokens 54.6 s against 8.6/10.1 s on 4-core Ubuntu; test_page_mode 100.1 s against 81.5/96.5 s)
    # and one Chrome did not answer Page.enable for 20 s. Use Ubuntu's 2 non-browser workers and one Chrome fewer
    # on that small runner only; a full-size Mac and every other OS keep the generic budget. An explicit
    # CHECK_WORKERS / CHECK_CHROMES / MOCKREVIEW_CHROMES still wins.
    _DEFAULT_WORKERS, _DEFAULT_CHROMES = 2, 2
else:
    _DEFAULT_WORKERS, _DEFAULT_CHROMES = min(8, max(2, _CPU - 2)), 3
WORKERS = _env_int("CHECK_WORKERS", _DEFAULT_WORKERS)
CHROMES = _env_int("CHECK_CHROMES", _env_int("MOCKREVIEW_CHROMES", _DEFAULT_CHROMES, 1, 4), 1, 4)


class InventoryError(RuntimeError):
    """The browser-marked modules could not be established, so no plan can be verified."""


_BROWSER_INVENTORY: Dict[str, List[str]] = {}


def _tail(text: str, limit: int = 1200) -> str:
    return text.strip()[-limit:]


def discover_browser_files() -> List[str]:
    """Every test module that actually carries pytest.mark.browser, from one collect-only call per run.

    A bare list cannot be the inventory: a module that gains the mark but not a BROWSER_JOBS row would run in
    neither the `-m "not browser"` pass nor the listed browser pass. One collection (about 0.4 s for this suite)
    is cached for the process; a collection that fails or prints no parsable module list is an error, never an
    empty plan.
    """
    key = str(ROOT)
    if key in _BROWSER_INVENTORY:
        return list(_BROWSER_INVENTORY[key])
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", "browser"]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode not in (0, 5):                 # 5 = pytest collected no tests at all
        raise InventoryError("browser inventory discovery failed (pytest exited %d): %s"
                             % (proc.returncode, _tail(proc.stdout + proc.stderr)))
    modules: List[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        path = line.split("::", 1)[0].strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path.endswith(".py") and path not in modules:
            modules.append(path)
    if proc.returncode == 0 and not modules:
        raise InventoryError("browser inventory discovery produced no parsable module list: %s"
                             % _tail(proc.stdout))
    _BROWSER_INVENTORY[key] = modules
    return list(modules)


def browser_jobs() -> List[Tuple[str, int]]:
    """BROWSER_JOBS validated against the discovered inventory, plus any undeclared marked module.

    A declared module that exists but carries no marked test is the list rotting: hard error. A marked module
    that is not declared still runs, with one worker and a warning, so it can never escape both passes.
    """
    discovered = discover_browser_files()
    marked = set(discovered)
    jobs: List[Tuple[str, int]] = []
    for path, units in BROWSER_JOBS:
        if path in marked:
            jobs.append((path, units))
    for path, _ in BROWSER_JOBS:
        if path not in marked and ((ROOT / path).is_file() or ROOT == SCRIPT_ROOT):
            raise InventoryError(
                "%s is listed in BROWSER_JOBS but carries no pytest.mark.browser test (%d marked module(s) "
                "found); fix the mark or the list" % (path, len(discovered)))
    declared = {path for path, _ in BROWSER_JOBS}
    for path in discovered:
        if path not in declared:
            print("[browser] warning: %s carries pytest.mark.browser but is not in BROWSER_JOBS; "
                  "running it with 1 worker" % path, flush=True)
            jobs.append((path, 1))
    return jobs


def unmarked_browser_files(browser: Sequence[str]) -> List[str]:
    """The browser modules in `browser` that also hold at least one test without the mark.

    A browser job runs `-m browser` only, so `changed` must keep a mixed module in the non-browser pass as well
    or its unmarked tests would disappear. One collect-only call answers it; a module without unmarked tests is
    left out so the non-browser phase does not fail with "no tests ran" (pytest exit 5).
    """
    if not browser:
        return []
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
           "-m", "not browser", *browser]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode not in (0, 5):                 # 5 = a module has no unmarked test at all
        raise InventoryError("unmarked-test discovery failed (pytest exited %d): %s"
                             % (proc.returncode, _tail(proc.stdout + proc.stderr)))
    wanted = set(browser)
    found: List[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        path = line.split("::", 1)[0].strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path in wanted and path not in found:
            found.append(path)
    return found


@dataclass
class Result:
    name: str
    cmd: List[str]
    log: Path
    xml: Path
    wall: float = 0.0
    rc: int = 0
    tests: int = 0
    failed: int = 0
    skipped: int = 0
    note: str = ""

    @property
    def passed(self) -> int:
        return max(0, self.tests - self.failed - self.skipped)

    @property
    def ok(self) -> bool:
        return self.rc == 0 and self.failed == 0 and not self.note


class Runner:
    def __init__(self, logs: Path, all_failures: bool):
        self.logs, self.all_failures = logs, all_failures
        self.logs.mkdir(parents=True, exist_ok=True)
        self.results: List[Result] = []
        self.started = time.monotonic()
        self._serial = 0
        self._lock = threading.Lock()

    def _paths(self, name: str) -> Tuple[Path, Path]:
        with self._lock:
            self._serial += 1
            serial = self._serial
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)
        return (self.logs / ("%02d-%s.log" % (serial, safe)),
                self.logs / ("%02d-%s.xml" % (serial, safe)))

    def run(self, name: str, files: Sequence[str], marker: Optional[str] = None,
            workers: int = 1) -> Result:
        log, xml = self._paths(name)
        temp = self.logs / ("tmp-" + log.stem)
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfEP",
               "--junitxml=%s" % xml, "--basetemp=%s" % temp]
        if marker:
            cmd += ["-m", marker]
        if not self.all_failures:
            cmd.append("--maxfail=1")
        if workers > 1:
            cmd += ["-n", str(workers)]
        cmd += list(files)
        print("[%s] $ %s" % (name, " ".join(cmd[1:])), flush=True)
        res = Result(name=name, cmd=cmd, log=log, xml=xml)
        env = dict(os.environ)
        env.update(MOCKREVIEW_CHROMES=str(CHROMES),
                   MOCKREVIEW_CHROME_RUN="check-%d-%d" % (os.getpid(), time.time_ns()),
                   MOCKREVIEW_CHROME_WAIT="900",
                   PYTHONUNBUFFERED="1")
        t0 = time.monotonic()
        with log.open("w", encoding="utf-8", errors="replace") as out:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT)
            try:
                rc = proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise
        res.wall, res.rc = time.monotonic() - t0, rc
        res.tests, res.failed, res.skipped = _counts(xml)
        if not xml.exists():
            res.note = "pytest produced no report"
        elif res.tests == 0:
            res.note = "pytest ran no tests"
        self.results.append(res)
        print("[%s] %d tests, %d failed, %d skipped in %.1fs%s"
              % (name, res.tests, res.failed, res.skipped, res.wall, (" (" + res.note + ")") if res.note else ""),
              flush=True)
        _replacements(res)
        if not res.ok:
            _failure_tail(res)
        return res


def _counts(xml: Path) -> Tuple[int, int, int]:
    try:
        root = ET.parse(xml).getroot()
    except (OSError, ET.ParseError):
        return 0, 0, 0
    cases = list(root.iter("testcase"))
    failed = sum(1 for c in cases if c.find("failure") is not None or c.find("error") is not None)
    skipped = sum(1 for c in cases if c.find("skipped") is not None)
    return len(cases), failed, skipped


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                          check=True).stdout


def changed_files(base: str) -> List[str]:
    """Every path this branch changed against `base`, working tree and untracked files included."""
    merge_base = _git("merge-base", "HEAD", base).strip()
    files = set(_git("diff", "--name-only", "--no-renames", merge_base).split("\n"))
    files |= set(_git("ls-files", "--others", "--exclude-standard").split("\n"))
    return sorted(f for f in files if f)


_IMPORT_MOD = re.compile(r"^\s*from\s+mockreview\s+import\s+([A-Za-z0-9_,\s]+)", re.M)
_IMPORT_SUB = re.compile(r"^\s*(?:from|import)\s+mockreview\.([A-Za-z0-9_]+)", re.M)
_TEST_IMPORT = re.compile(
    r"^\s*from\s+(test_[A-Za-z0-9_]+|authkit|conftest|browserkit)\s+import"
    r"|^\s*import\s+(test_[A-Za-z0-9_]+|authkit|conftest|browserkit)\b", re.M)


def _imports() -> Tuple[Dict[str, set], Dict[str, set]]:
    """({test file: mockreview modules it imports}, {helper module: test files importing it})."""
    modules: Dict[str, set] = {}
    helpers: Dict[str, set] = {}
    for f in sorted(TESTS.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        rel = "tests/" + f.name
        mods: set = set()
        for m in _IMPORT_MOD.finditer(src):
            for name in m.group(1).replace("(", " ").split(","):
                name = name.strip().split(" as ")[0].strip()
                if name:
                    mods.add(name)
        mods.update(_IMPORT_SUB.findall(src))
        modules[rel] = mods
        for found in _TEST_IMPORT.finditer(src):
            helper = found.group(1) or found.group(2)
            helpers.setdefault(helper, set()).add(rel)
    return modules, helpers


def map_changed(files: Iterable[str], browser: Sequence[str]) -> Tuple[Dict[str, List[str]], List[str], bool]:
    """({selected test file: why}, unmapped paths, fallback?). The map is MAP above plus imports of mockreview
    modules and of tests/ helper modules; `browser` is the discovered browser inventory."""
    imports, helpers = _imports()
    board = board_tests(browser)
    selected: Dict[str, List[str]] = {}
    unmapped: List[str] = []
    fallback = False

    def add(path: str, why: str) -> None:
        if (ROOT / path).is_file():
            selected.setdefault(path, []).append(why)

    for path in files:
        target = Path(path)
        if path.startswith("tests/") and target.suffix == ".py" and target.stem.startswith("test_"):
            add(path, "itself")
            for importer in sorted(helpers.get(target.stem, ())):
                if importer != path:
                    add(importer, "imports " + target.stem)
            continue
        if path == "tests/browserkit.py":
            for testfile in browser:
                add(testfile, "the browser harness")
            for importer in sorted(helpers.get("browserkit", ())):
                add(importer, "imports browserkit")
            continue
        if path.startswith("tests/") and target.suffix == ".py":
            hits = sorted(helpers.get(target.stem, ()))
            if target.name == "conftest.py" or not hits:
                unmapped.append(path)
                fallback = True
            else:
                for importer in hits:
                    add(importer, "imports " + target.stem)
            continue
        if path == "mockreview/__init__.py":
            unmapped.append(path)
            fallback = True
            continue
        if path.startswith("mockreview/") and target.suffix == ".py":
            module = target.stem
            before = len(selected)
            for testfile, mods in imports.items():
                if module in mods:
                    add(testfile, "imports mockreview." + module)
            if len(selected) == before:      # a module only reached through another module: no narrower guess
                unmapped.append(path)
                fallback = True
            continue
        for pattern, tests in MAP:
            if fnmatch.fnmatch(path, pattern):
                for testfile in (board if tests is None else tests):
                    add(testfile, "rule " + pattern)
                break
        else:
            if path.startswith("mockreview/") or re.match(r"^[^/]+\.[a-z]+$", path):
                unmapped.append(path)
                fallback = True
            elif path.startswith(("docs/", ".dispatch/", ".github/", "tests/", "examples/", "skills/")):
                continue
            else:
                unmapped.append(path)
                fallback = True
    return selected, unmapped, fallback


def _ordered(paths: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(paths))


def run_nonbrowser(runner: Runner, files: Optional[Sequence[str]] = None, name: str = "fast") -> Result:
    return runner.run(name, files or [], marker="not browser", workers=WORKERS)


def run_browser_pool(runner: Runner, jobs: Sequence[Tuple[str, int]]) -> List[Result]:
    """Start jobs, heaviest first, while their worker counts fit under the Chrome budget; stop on the first
    failure unless --all. In-flight jobs are never killed: a browser job ends on its own (bounded), and killing
    pytest would leave its Chromes behind."""
    pending = list(jobs)
    active: Dict[str, Tuple[int, threading.Thread, List[Result]]] = {}
    used = 0
    failures = 0
    while pending or active:
        started = False
        for job in list(pending):
            path, requested = job
            # A job never asks for more workers than this run's budget: at CHECK_CHROMES=1 or 2 a
            # three-worker job still runs, one or two at a time, instead of never fitting.
            units = max(1, min(requested, CHROMES))
            if used + units <= CHROMES:
                pending.remove(job)
                name = Path(path).stem
                holder: List[Result] = []

                def target(p: str = path, u: int = units, box: List[Result] = holder) -> None:
                    box.append(runner.run(Path(p).stem, [p], marker="browser", workers=u))

                t = threading.Thread(target=target)
                t.start()
                active[name] = (units, t, holder)
                used += units
                started = True
                break
        if started:
            continue
        if not active:
            break
        while True:
            done = [name for name, (_, t, _) in active.items() if not t.is_alive()]
            if done:
                break
            time.sleep(0.1)
        for name in done:
            units, _, holder = active.pop(name)
            used -= units
            failures += sum(1 for r in holder if not r.ok)
        if failures and not runner.all_failures:
            if pending:
                print("[browser] stopped scheduling after a failure", flush=True)
            pending.clear()
    if pending:                                 # nothing that fits was left, yet work remains: never green
        names = ", ".join(Path(path).stem for path, _ in pending)
        note = "%d browser module(s) never ran: %s" % (len(pending), names)
        print("[browser] %s" % note, flush=True)
        runner.results.append(Result(name="browser", cmd=[], log=runner.logs / "browser-unscheduled.log",
                                     xml=runner.logs / "browser-unscheduled.xml", note=note))
    return runner.results


def _summary(kind: str, results: Sequence[Result], wall: float, detail: str) -> str:
    tests = sum(r.tests for r in results)
    failed = sum(r.failed for r in results) + sum(1 for r in results if r.note)
    skipped = sum(r.skipped for r in results)
    return "%s: %d tests, %d passed, %d skipped, %d failed in %s; %s" % (
        kind, tests, tests - sum(r.failed for r in results) - skipped, skipped, failed,
        _duration(wall), detail)


def _duration(seconds: float) -> str:
    if seconds < 90:
        return "%.1fs" % seconds
    minutes, rest = divmod(int(round(seconds)), 60)
    return "%dm%02ds" % (minutes, rest)


REPLACING = "browserkit: replacing"


def _replacements(res: Result) -> None:
    """Print the browser replacements a job's log recorded, whatever the job's outcome. pytest hides a passing test's
    stderr unless -rP lists it, and a green run that had to replace a browser is the one nobody opens the log of."""
    try:
        lines = res.log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        if REPLACING in line:
            print("[%s] %s" % (res.name, line[line.index(REPLACING):].rstrip()), flush=True)


def _failure_tail(res: Result) -> None:
    """Print one group's failure tail at the moment it fails, so a job killed by the job cap still shows the test."""
    try:
        lines = res.log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    print("\n----- %s failed (%s) -----\n%s" % (res.name, res.log, "\n".join(lines[-60:])),
          file=sys.stderr, flush=True)


def _failed_logs(runner: Runner) -> None:
    """The end-of-run roll-up of what failed and where its log is. The 60-line tail was printed by Runner.run."""
    failed = [res for res in runner.results if not res.ok]
    if not failed:
        return
    print("\nfailed: %s" % ", ".join(res.name for res in failed), file=sys.stderr)
    for res in failed:
        note = (" (" + res.note + ")") if res.note else ""
        print("  %s%s: %s" % (res.name, note, res.log), file=sys.stderr)
    sys.stderr.flush()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the mock-review tests by tier.")
    parser.add_argument("tier", choices=["fast", "changed", "full"])
    parser.add_argument("--base", default="main", help="changed: git ref to compare against (default main)")
    parser.add_argument("--files", nargs="*", help="changed: treat these paths as changed")
    parser.add_argument("--dry-run", action="store_true", help="changed: print the plan and run nothing")
    parser.add_argument("--part", choices=["nonbrowser", "browser"], help="full: run one bounded half")
    parser.add_argument("--all", action="store_true", help="see every failure instead of stopping early")
    parser.add_argument("--keep-logs", action="store_true", help="keep this run's logs even when green")
    args = parser.parse_args(argv)

    logs_env = os.environ.get("CHECK_LOG_DIR", "").strip()
    if logs_env:
        logs = Path(logs_env).expanduser().resolve()
        keep_logs = True              # an explicit log directory is an output, not scratch: it survives a green run
    else:
        logs = Path(tempfile.mkdtemp(prefix="mockreview-check-"))
        keep_logs = bool(args.keep_logs or os.environ.get("CHECK_KEEP_LOGS"))
    runner = Runner(logs, args.all)
    t0 = time.monotonic()
    results: List[Result] = []
    detail = "%d workers, %d Chrome budget%s" % (
        WORKERS, CHROMES, (", part " + args.part) if getattr(args, "part", None) else "")
    try:
        if args.tier == "fast":
            results.append(run_nonbrowser(runner))
            summary = _summary("FAST", results, time.monotonic() - t0, detail)
        elif args.tier == "full":
            half = args.part
            jobs = browser_jobs() if half in (None, "browser") else []
            nonbrowser_result: Optional[Result] = None
            if half in (None, "nonbrowser"):
                nonbrowser_result = run_nonbrowser(runner)
                results.append(nonbrowser_result)
            if half in (None, "browser"):
                if half == "browser" or args.all or nonbrowser_result is None or nonbrowser_result.ok:
                    run_browser_pool(runner, jobs)
                else:
                    print("browser phase skipped: the non-browser phase failed (--all runs both)", flush=True)
            results = runner.results
            summary = _summary("FULL", results, time.monotonic() - t0, detail)
        else:
            files = args.files if args.files is not None else changed_files(args.base)
            jobs = browser_jobs()
            browser_files = [path for path, _ in jobs]
            selected, unmapped, fallback = map_changed(files, browser_files)
            for path in SMOKE:
                if (ROOT / path).is_file():
                    selected.setdefault(path, []).append("always-on smoke")
            browser = [(p, units) for p, units in jobs if p in selected]
            mixed = set(unmarked_browser_files([p for p, _ in browser])) if browser and not fallback else set()
            nonbrowser: Optional[List[str]] = None if fallback else [
                p for p in selected if p not in set(browser_files) or p in mixed]
            print("changed against %s: %d file(s)" % (args.base, len(files)), flush=True)
            for path in files:
                print("  %s" % path, flush=True)
            for path, why in sorted(selected.items()):
                print("  -> %s (%s)" % (path, ", ".join(dict.fromkeys(why))), flush=True)
            if fallback:
                print("  unmapped: %s; running %s" % (", ".join(unmapped), UNMAPPED_NOTE), flush=True)
            print("plan: %d browser file(s), %s non-browser file(s), %d Chrome budget"
                  % (len(browser), "all" if nonbrowser is None else len(nonbrowser), CHROMES),
                  flush=True)
            if args.dry_run:
                summary = "CHANGED: dry run (nothing ran)"
                results = []
                if not keep_logs:
                    shutil.rmtree(logs, ignore_errors=True)
                print(summary, flush=True)
                return 0
            nonbrowser_result = None
            if nonbrowser is None or nonbrowser:
                nonbrowser_result = runner.run("changed-nonbrowser", nonbrowser or [], marker="not browser",
                                               workers=WORKERS)
                results.append(nonbrowser_result)
            if browser:
                if args.all or nonbrowser_result is None or nonbrowser_result.ok:
                    run_browser_pool(runner, browser)
                else:
                    print("browser phase skipped: the non-browser phase failed (--all runs both)", flush=True)
            results = runner.results
            if not results:
                results.append(Result(name="changed", cmd=[], log=logs / "none", xml=logs / "none",
                                      note="no test files selected"))
            summary = _summary("CHANGED", results, time.monotonic() - t0, detail)
    except InventoryError as e:
        print("check: %s" % e, file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    green = all(r.ok for r in results)
    print(summary, flush=True)
    if keep_logs:
        print("logs kept: %s" % logs, flush=True)
    if not green:
        _failed_logs(runner)
        if not keep_logs:                       # a red run is never deleted, but say where it is either way
            print("logs kept: %s" % logs, file=sys.stderr)
        return 1
    if not keep_logs:
        shutil.rmtree(logs, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
