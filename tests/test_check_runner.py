"""Regressions for scripts/check.py: the browser inventory, the Chrome-budget scheduler, the browser retry and the
phase gate.

These run no browser and no repository test: the scheduler is driven with a fake runner, and the inventory and
phase-gate regressions point the real runner (`CHECK_ROOT`) at a miniature suite in a temp directory.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check.py"


def _load_check():
    spec = importlib.util.spec_from_file_location("check_runner_under_test", CHECK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check()


def _load_check_as(monkeypatch, platform: str, cpus: int):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "cpu_count", lambda: cpus)
    spec = importlib.util.spec_from_file_location("check_runner_platform_under_test", CHECK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_macos_defaults_one_worker_and_one_chrome_fewer(monkeypatch):
    """The 3-core macOS runner reports 5 CPUs; 3 workers + 3 Chromes starved every browser module there. A full-size
    Mac (this mini reports 10) keeps the generic budget."""
    for name in ("CHECK_WORKERS", "CHECK_CHROMES", "MOCKREVIEW_CHROMES"):
        monkeypatch.delenv(name, raising=False)
    mac = _load_check_as(monkeypatch, "darwin", 5)
    assert (mac.WORKERS, mac.CHROMES) == (2, 2)
    full_mac = _load_check_as(monkeypatch, "darwin", 10)
    assert (full_mac.WORKERS, full_mac.CHROMES) == (8, 3)
    four = _load_check_as(monkeypatch, "linux", 4)
    assert (four.WORKERS, four.CHROMES) == (2, 3)
    five = _load_check_as(monkeypatch, "linux", 5)
    assert (five.WORKERS, five.CHROMES) == (3, 3)
    monkeypatch.setenv("CHECK_WORKERS", "4")
    monkeypatch.setenv("CHECK_CHROMES", "1")
    explicit = _load_check_as(monkeypatch, "darwin", 5)
    assert (explicit.WORKERS, explicit.CHROMES) == (4, 1)


class FakeRunner:
    """A Runner shape for the scheduler: records every job and returns a green Result with a fixed case count.

    `fail` maps a module to how many of its runs fail; a failing run writes a JUnit report with the two FLAKY_CASES
    failed, so the retry can rebuild their node IDs. Every call's file arguments and workers land in `calls`."""

    def __init__(self, logs: Path, fail: dict | None = None):
        self.logs = logs
        self.logs.mkdir(parents=True, exist_ok=True)
        self.all_failures = False
        self.results = []
        self.runs = []
        self.calls = []
        self.fail = dict(fail or {})
        self.cases = {path: 10 + index for index, (path, _) in enumerate(check.BROWSER_JOBS)}

    def record(self, result):
        self.results.append(result)

    def run(self, name, files, marker=None, workers=1, record=True):
        path = files[0].split("::", 1)[0]
        self.runs.append((path, workers))
        self.calls.append((list(files), workers))
        log, xml = self.logs / (name + ".log"), self.logs / (name + ".xml")
        tests = self.cases.get(path, 1) if files == [path] else len(files)
        result = check.Result(name=name, cmd=[], log=log, xml=xml, tests=tests)
        if self.fail.get(path, 0) > 0:
            self.fail[path] -= 1
            write_junit(xml, path, {case: True for case in FLAKY_CASES})
            result.rc, result.failed = 1, len(FLAKY_CASES)
        if record:
            self.record(result)
        return result


# (classname suffix after the module, name): a module-level test and a class test with a parametrize ID that holds
# a dot and "::", which must stay in the name.
FLAKY_CASES = [("", "test_one"), (".TestTwo", "test_two[a.b::c]")]


def write_junit(xml: Path, path: str, cases: dict) -> None:
    """A JUnit report shaped like pytest's: {(classname suffix, name): failed?} for the module at `path`."""
    module = path[:-3].replace("/", ".")
    rows = ['<testcase classname="%s%s" name="%s">%s</testcase>'
            % (module, suffix, name, "<failure message=\"x\"/>" if failed else "")
            for (suffix, name), failed in cases.items()]
    xml.write_text('<testsuites><testsuite name="pytest">%s</testsuite></testsuites>' % "".join(rows),
                   encoding="utf-8")


def test_failed_ids_rebuild_the_node_ids_pytest_wrote(tmp_path):
    """Module, class and nested-class tests map back to node IDs; a passing case is left out and a teardown error
    that repeats a test's row is one ID. A failure with no test behind it (a collection error) gives [], so the
    caller retries the whole module."""
    xml = tmp_path / "report.xml"
    write_junit(xml, "tests/test_x.py", {("", "test_a"): True, (".TestB", "test_b[p.q::r]"): True,
                                          (".TestB.TestC", "test_c"): True, ("", "test_green"): False})
    root = xml.read_text(encoding="utf-8")
    xml.write_text(root.replace("</testsuite>", '<testcase classname="tests.test_x" name="test_a">'
                                "<error message=\"teardown\"/></testcase></testsuite>"), encoding="utf-8")
    assert check.failed_ids(xml, "tests/test_x.py") == [
        "tests/test_x.py::test_a", "tests/test_x.py::TestB::test_b[p.q::r]", "tests/test_x.py::TestB::TestC::test_c"]
    xml.write_text('<testsuites><testsuite><testcase classname="" name="tests.test_x"><error/></testcase>'
                   "</testsuite></testsuites>", encoding="utf-8")
    assert check.failed_ids(xml, "tests/test_x.py") == []
    assert check.failed_ids(tmp_path / "missing.xml", "tests/test_x.py") == []


FLAKY_JOBS = [("tests/test_anchors.py", 3), ("tests/test_pin_stack.py", 1)]
FLAKY_IDS = ["tests/test_anchors.py::test_one", "tests/test_anchors.py::TestTwo::test_two[a.b::c]"]


def test_a_module_that_passes_on_the_retry_is_green_and_lists_its_flaky_tests(tmp_path, monkeypatch):
    """With every failure seen (--all, as CI runs) only the failed tests run again, on no more workers than tests;
    the module is then one green result with its full case count, and the summary counts the flaky tests."""
    monkeypatch.setattr(check, "CHROMES", 3)
    monkeypatch.setattr(check, "RETRIES", 1)
    runner = FakeRunner(tmp_path / "logs", fail={"tests/test_anchors.py": 1})
    runner.all_failures = True
    results = check.run_browser_pool(runner, FLAKY_JOBS)
    assert (FLAKY_IDS, 2) in runner.calls, runner.calls
    assert len(results) == 2 and all(result.ok for result in results), results
    anchors = next(result for result in results if result.name == "test_anchors")
    assert anchors.flaky == FLAKY_IDS
    assert anchors.tests == runner.cases["tests/test_anchors.py"]
    assert anchors.retry_log is not None and anchors.retry_log.name == "test_anchors-retry.log"
    assert "0 failed, 2 flaky in" in check._summary("FULL", results, 1.0, "3 workers")


def test_a_fail_fast_run_retries_the_whole_module(tmp_path, monkeypatch):
    """A fail-fast run stopped at the first failure, so the tests after it never ran: the retry is the whole module."""
    monkeypatch.setattr(check, "CHROMES", 3)
    monkeypatch.setattr(check, "RETRIES", 1)
    runner = FakeRunner(tmp_path / "logs", fail={"tests/test_anchors.py": 1})
    results = check.run_browser_pool(runner, FLAKY_JOBS)
    assert [call for call in runner.calls if call[0] == ["tests/test_anchors.py"]] == [
        (["tests/test_anchors.py"], 3), (["tests/test_anchors.py"], 3)]
    assert all(result.ok for result in results), results
    assert next(result for result in results if result.name == "test_anchors").flaky == FLAKY_IDS


def test_a_module_that_fails_its_retry_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "CHROMES", 3)
    monkeypatch.setattr(check, "RETRIES", 1)
    runner = FakeRunner(tmp_path / "logs", fail={"tests/test_anchors.py": 2})
    runner.all_failures = True
    results = check.run_browser_pool(runner, FLAKY_JOBS)
    anchors = [result for result in results if result.name == "test_anchors"]
    assert len(anchors) == 1 and not anchors[0].ok and anchors[0].flaky == []
    assert anchors[0].retry_log is not None
    assert "2 failed in" in check._summary("FULL", results, 1.0, "3 workers")


def test_check_retries_0_fails_a_module_on_its_first_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "CHROMES", 3)
    monkeypatch.setattr(check, "RETRIES", 0)
    runner = FakeRunner(tmp_path / "logs", fail={"tests/test_anchors.py": 1})
    runner.all_failures = True
    results = check.run_browser_pool(runner, FLAKY_JOBS)
    assert [files for files, _ in runner.calls].count(["tests/test_anchors.py"]) == 1
    assert not any(len(files) == 2 for files, _ in runner.calls), runner.calls
    assert not all(result.ok for result in results)


@pytest.mark.parametrize("budget", [1, 2, 3, 4])
def test_every_budget_schedules_the_whole_browser_inventory(tmp_path, monkeypatch, budget):
    """A job asking for three workers must still run at a budget of one or two; nothing may be dropped."""
    monkeypatch.setattr(check, "CHROMES", budget)
    jobs = list(check.BROWSER_JOBS)
    runner = FakeRunner(tmp_path / ("logs-%d" % budget))
    results = check.run_browser_pool(runner, jobs)
    assert sorted(path for path, _ in runner.runs) == sorted(path for path, _ in jobs)
    assert sum(result.tests for result in results) == sum(runner.cases[path] for path, _ in jobs)
    requested = dict(jobs)
    assert dict(runner.runs) == {path: min(units, budget) for path, units in jobs}
    assert all(result.ok for result in results)


def test_an_unscheduled_job_makes_the_run_non_green(tmp_path, monkeypatch):
    """A job that never fits is a red run with its name in the summary, never a silently shorter green run."""
    monkeypatch.setattr(check, "CHROMES", 0)
    runner = FakeRunner(tmp_path / "logs")
    results = check.run_browser_pool(runner, [("tests/test_unreachable.py", 1)])
    assert runner.runs == []
    notes = [result.note for result in results if result.note]
    assert any("test_unreachable" in note for note in notes)
    assert not all(result.ok for result in results)
    assert "1 failed" in check._summary("FULL", results, 1.0, "0 workers")


MINI_PYTEST_INI = """\
[pytest]
testpaths = tests
addopts = --strict-markers
markers =
    browser: starts a headless browser
    slow: slow
"""

MINI_PLAIN = """\
def test_plain_passes():
    assert True
"""

MINI_PLAIN_FAILS = """\
def test_plain_fails():
    assert False, "the unmarked phase failed on purpose"
"""

MINI_BROWSER = """\
import pytest


def test_unmarked_passes():
    assert True


@pytest.mark.browser
def test_marked_passes():
    assert True


@pytest.mark.browser
def test_marked_failure():
    assert False, "the marked failure must run"
"""

MINI_MIXED = """\
import pytest


def test_mixed_unmarked():
    assert True


@pytest.mark.browser
def test_mixed_marked():
    assert True
"""


def make_mini_suite(root: Path, plain_fails: bool = False) -> Path:
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pytest.ini").write_text(MINI_PYTEST_INI, encoding="utf-8")
    (tests / "test_plain.py").write_text(MINI_PLAIN_FAILS if plain_fails else MINI_PLAIN, encoding="utf-8")
    (tests / "test_new_browser.py").write_text(MINI_BROWSER, encoding="utf-8")
    (tests / "test_mixed.py").write_text(MINI_MIXED, encoding="utf-8")
    return root


def make_mixed_suite(root: Path) -> Path:
    """A green miniature suite whose only browser module is mixed: 1 unmarked and 1 marked test."""
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pytest.ini").write_text(MINI_PYTEST_INI, encoding="utf-8")
    (tests / "test_mixed.py").write_text(MINI_MIXED, encoding="utf-8")
    return root


def collect_count(mini: Path, *files: str) -> int:
    """What the miniature suite collects, so a summary can be compared with it instead of a hardcoded count."""
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                           *files], cwd=str(mini), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode in (0, 5), proc.stdout + proc.stderr
    found = re.search(r"(\d+) tests? collected", proc.stdout)
    assert found, proc.stdout + proc.stderr
    return int(found.group(1))


def run_check(mini: Path, *args: str, merge_stderr: bool = False,
              env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e["CHECK_ROOT"] = str(mini)
    # When this suite itself runs on Actions, a miniature run must not annotate or summarize the real job.
    for name in ("CHECK_KEEP_LOGS", "CHECK_LOG_DIR", "CHECK_RETRIES", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY"):
        e.pop(name, None)
    e.update(env or {})
    return subprocess.run([sys.executable, str(CHECK), *args], cwd=str(ROOT), env=e,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def test_full_runs_a_marked_module_that_is_not_in_browser_jobs(tmp_path):
    """A new module carrying pytest.mark.browser may not fall between the two passes: `full` must run it."""
    mini = make_mini_suite(tmp_path / "mini")
    total = collect_count(mini)
    proc = run_check(mini, "full")
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "[browser] warning: tests/test_new_browser.py carries pytest.mark.browser" in output
    assert "[test_new_browser] 2 tests, 1 failed" in output
    assert "[test_new_browser] failed again on the retry" in output, output
    assert ("FULL: %d tests" % total) in output, output
    assert "test_marked_failure" in output


def test_changed_runs_a_marked_module_that_is_not_in_browser_jobs(tmp_path):
    """`changed` for the same new module must run its marked tests too, not only its unmarked ones."""
    mini = make_mini_suite(tmp_path / "mini")
    total = collect_count(mini, "tests/test_new_browser.py")
    proc = run_check(mini, "changed", "--files", "tests/test_new_browser.py")
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "[test_new_browser] 2 tests, 1 failed" in output
    assert ("CHANGED: %d tests" % total) in output, output
    assert "test_marked_failure" in output


def test_a_mixed_module_runs_each_test_exactly_once_in_full_and_changed(tmp_path):
    """A module with marked and unmarked tests is neither doubled nor dropped: summary count == collect count."""
    mini = make_mixed_suite(tmp_path / "mini")
    total = collect_count(mini)
    assert total == 2
    full = run_check(mini, "full")
    output = full.stdout + full.stderr
    assert full.returncode == 0, output
    assert ("FULL: %d tests" % total) in output, output
    assert "2 passed" in output and "0 failed" in output, output
    changed = run_check(mini, "changed", "--files", "tests/test_mixed.py")
    output = changed.stdout + changed.stderr
    assert changed.returncode == 0, output
    assert ("CHANGED: %d tests" % collect_count(mini, "tests/test_mixed.py")) in output, output
    assert "2 passed" in output and "0 failed" in output, output


def test_a_failed_browser_inventory_aborts_instead_of_running_a_guess(tmp_path):
    """Discovery that cannot be verified is a hard stop, not an empty plan that silently runs fewer tests."""
    mini = make_mini_suite(tmp_path / "mini")
    (mini / "tests" / "test_broken.py").write_text("import module_that_does_not_exist\n", encoding="utf-8")
    proc = run_check(mini, "changed", "--files", "tests/test_new_browser.py")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "browser inventory discovery failed" in output


def test_a_failed_nonbrowser_phase_skips_the_browser_phase_by_default(tmp_path):
    mini = make_mini_suite(tmp_path / "mini", plain_fails=True)
    proc = run_check(mini, "full")
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "browser phase skipped: the non-browser phase failed (--all runs both)" in output
    assert "[test_new_browser]" not in output


def test_all_runs_the_browser_phase_after_a_failed_nonbrowser_phase(tmp_path):
    mini = make_mini_suite(tmp_path / "mini", plain_fails=True)
    total = collect_count(mini)
    proc = run_check(mini, "full", "--all")
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "browser phase skipped" not in output
    assert "[test_new_browser] 2 tests, 1 failed" in output
    assert ("FULL: %d tests" % total) in output, output
    assert "test_marked_failure" in output


def test_a_failed_group_prints_its_tail_before_the_summary(tmp_path):
    """The failure tail prints when the group finishes, so a run killed by the job cap still names the test. The
    end-of-run roll-up lists the log path and does not repeat the tail."""
    mini = make_mini_suite(tmp_path / "mini", plain_fails=True)
    proc = run_check(mini, "full", merge_stderr=True)
    output = proc.stdout
    assert proc.returncode != 0, output
    header = "----- fast failed ("
    assert output.count(header) == 1, output
    summary_at = output.index("FULL: ")
    assert output.index(header) < summary_at, output
    assert "the unmarked phase failed on purpose" in output[:summary_at], output
    after = output[summary_at:]
    assert "failed: fast" in after, after
    assert "  fast: " in after, after
    assert "logs kept:" in after, after
    assert header not in after, after


def test_check_log_dir_keeps_a_green_runs_logs_and_names_the_directory(tmp_path):
    """CI uploads one directory per tier: an explicit CHECK_LOG_DIR is created, kept on a green run (the default
    temp dir is deleted then), and its absolute path prints next to the summary."""
    mini = make_mixed_suite(tmp_path / "mini")
    logs = tmp_path / "artifacts" / "nonbrowser"
    proc = run_check(mini, "full", env={"CHECK_LOG_DIR": str(logs)})
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert ("logs kept: %s" % logs.resolve()) in output, output
    assert logs.is_dir(), output
    names = sorted(p.name for p in logs.iterdir())
    assert any(name.endswith(".log") for name in names), names
    assert any(name.endswith(".xml") for name in names), names


def test_a_green_run_without_check_log_dir_removes_its_scratch_logs(tmp_path):
    """The default stays a throwaway temp dir: a green run must not leave a log directory behind."""
    mini = make_mixed_suite(tmp_path / "mini")
    proc = run_check(mini, "full", merge_stderr=True)
    output = proc.stdout
    assert proc.returncode == 0, output
    assert "logs kept:" not in output, output


MINI_REPLACING = """\
import sys
import pytest


def test_plain_replaced_a_browser():
    print("browserkit: replacing a browser whose page bring-up failed (1/2): the plain job", file=sys.stderr)


@pytest.mark.browser
def test_marked_replaced_a_browser():
    print("browserkit: replacing a browser whose launch failed (1/2): the browser job", file=sys.stderr)
"""


def test_a_green_run_still_prints_the_browser_replacements_with_the_job_name(tmp_path):
    """pytest hides a passing test's stderr, so a green job that replaced a browser left no trace in the run's output.
    Every job's log is searched for the warning and the lines are printed under the job's name, green or not."""
    mini = make_mixed_suite(tmp_path / "mini")
    (mini / "tests" / "test_replaced.py").write_text(MINI_REPLACING, encoding="utf-8")
    proc = run_check(mini, "full", merge_stderr=True)
    output = proc.stdout
    assert proc.returncode == 0, output
    lines = [line for line in output.splitlines() if "browserkit: replacing" in line]
    assert len(lines) == 2, output
    plain = next(line for line in lines if "the plain job" in line)
    marked = next(line for line in lines if "the browser job" in line)
    assert plain.startswith("[fast] browserkit: replacing a browser whose page bring-up failed"), lines
    assert marked.startswith("[test_replaced] browserkit: replacing a browser whose launch failed"), lines


MINI_FLAKY = """\
from pathlib import Path

import pytest


@pytest.mark.browser
def test_steady():
    assert True


@pytest.mark.browser
def test_flaky_once():
    flag = Path(__file__).with_name("flaky.flag")
    if not flag.exists():
        flag.write_text("failed once", encoding="utf-8")
        assert False, "the first attempt fails on purpose"
"""


@pytest.mark.parametrize("all_failures", [True, False], ids=["all", "fail-fast"])
def test_a_browser_test_that_passes_on_the_retry_is_reported_flaky_and_the_run_stays_green(tmp_path, all_failures):
    """The real runner end to end: the module's failed test (or, fail-fast, the whole module) runs once more, the
    run exits 0, and the flaky test is named in the output, in the summary count and, on Actions, as a warning
    annotation and in the job summary."""
    mini = make_mixed_suite(tmp_path / "mini")
    (mini / "tests" / "test_flaky.py").write_text(MINI_FLAKY, encoding="utf-8")
    total = collect_count(mini)
    job_summary = tmp_path / "step-summary.md"
    args = ["full", "--all"] if all_failures else ["full"]
    proc = run_check(mini, *args, merge_stderr=True,
                     env={"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(job_summary)})
    output = proc.stdout
    test_id = "tests/test_flaky.py::test_flaky_once"
    assert proc.returncode == 0, output
    retrying = "[test_flaky] retrying 1 failed test(s) once" if all_failures else "[test_flaky] retrying the module once"
    assert retrying in output, output
    assert ("[test_flaky] flaky: 1 test(s) failed, then passed on the retry: %s" % test_id) in output, output
    assert ("FULL: %d tests, %d passed, 0 skipped, 0 failed, 1 flaky in" % (total, total)) in output, output
    assert ("::warning title=Flaky browser test::%s failed, then passed on the retry" % test_id) in output, output
    assert ("- `%s`" % test_id) in job_summary.read_text(encoding="utf-8")
