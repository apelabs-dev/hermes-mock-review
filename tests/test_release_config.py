"""Release Please wiring: one version everywhere, v-prefixed tags, an action pinned by commit.

`release-please.yml` runs on every push to main and keeps a release pull request open. Merging that pull request tags
vX.Y.Z. The version lives in four places and this file asserts they agree, so a bump only ever has to touch the files
the action itself edits: `.release-please-manifest.json`, `plugin.yaml`, `mockreview/__init__.py` and `pyproject.toml`.
"""

import json
import re

import pytest
import yaml

from conftest import ROOT

CONFIG = ROOT / "release-please-config.json"
MANIFEST = ROOT / ".release-please-manifest.json"
WORKFLOW = ROOT / ".github" / "workflows" / "release-please.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
PLUGIN = ROOT / "plugin.yaml"
INIT = ROOT / "mockreview" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
ANNOTATION = "x-release-please-version"
SEMVER = re.compile(r"\d+\.\d+\.\d+")
FULL_SHA = re.compile(r"[0-9a-f]{40}")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def plugin_manifest():
    return yaml.safe_load(PLUGIN.read_text(encoding="utf-8"))


def annotated_lines(path):
    """The lines carrying the Release Please marker, as (stripped line, version found on it)."""
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if ANNOTATION not in line:
            continue
        match = SEMVER.search(line)
        assert match, "{}: the {} line carries no version".format(path.name, ANNOTATION)
        found.append((line.strip(), match.group(0)))
    return found


def init_version():
    match = re.search(r'^__version__ = "([^"]+)"', INIT.read_text(encoding="utf-8"), re.M)
    assert match, "mockreview/__init__.py declares no __version__"
    return match.group(1)


def crypto_requirement():
    """The PyJWT specifier inside the crypto extra, read from the text so no TOML dependency is needed."""
    text = PYPROJECT.read_text(encoding="utf-8")
    block = re.search(r"^crypto\s*=\s*\[(.*?)^\]", text, re.S | re.M)
    assert block, "pyproject.toml declares no crypto extra"
    match = re.search(r'"PyJWT\[crypto\]([^"]*)"', block.group(1))
    assert match, "the crypto extra does not require PyJWT[crypto]"
    return match.group(1)


def admits(spec, version):
    """A tiny numeric-version comparator for comma-separated comparison clauses, without `packaging`."""
    got = tuple(int(part) for part in version.split("."))
    for clause in spec.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)\s*", clause)
        assert match, "unparsable version clause: {}".format(clause)
        op, want = match.group(1), tuple(int(part) for part in match.group(2).split("."))
        if not {"==": got == want, "!=": got != want, ">=": got >= want,
                "<=": got <= want, ">": got > want, "<": got < want}[op]:
            return False
    return True


def test_one_version_lives_in_the_manifest_the_plugin_and_the_package():
    versions = {
        ".release-please-manifest.json": read_json(MANIFEST)["."],
        "plugin.yaml": plugin_manifest()["version"],
        "mockreview/__init__.py": init_version(),
        "pyproject.toml": annotated_lines(PYPROJECT)[0][1],
    }
    assert SEMVER.fullmatch(versions["plugin.yaml"]), "the manifest version is bare: tags add the v"
    assert len(set(versions.values())) == 1, "the version drifted: {}".format(versions)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [{}]".format(versions["plugin.yaml"]) in changelog, "the released version has a changelog entry"


def test_config_releases_the_root_package_with_v_prefixed_tags():
    config = read_json(CONFIG)
    assert config["release-type"] == "simple"
    assert config["include-v-in-tag"] is True
    assert list(config["packages"]) == ["."]
    package = config["packages"]["."]
    assert package["release-type"] == "simple"
    assert package["package-name"] == plugin_manifest()["name"]
    assert package["changelog-path"] == "CHANGELOG.md"
    # `generic` is not cosmetic. A bare path whose name ends in .yaml/.yml makes Release Please run its YAML
    # updater first, which re-serialises the file and drops every comment, marker included.
    assert package["extra-files"] == [
        {"type": "generic", "path": "plugin.yaml"},
        {"type": "generic", "path": "mockreview/__init__.py"},
        {"type": "generic", "path": "pyproject.toml"},
    ]


@pytest.mark.parametrize("path, declaration", [(PLUGIN, "version:"), (INIT, "__version__ ="),
                                                   (PYPROJECT, "version =")])
def test_every_extra_file_exists_and_marks_its_version_line(path, declaration):
    assert path.is_file(), "{} is named in extra-files but missing".format(path.name)
    lines = annotated_lines(path)
    assert len(lines) == 1, "{} needs exactly one {} line".format(path.name, ANNOTATION)
    line, version = lines[0]
    assert line.startswith(declaration), "the marker must sit on the version declaration, not near it"
    assert version == plugin_manifest()["version"]


def test_workflow_pins_the_action_to_a_commit_and_runs_on_main():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    trigger = workflow.get("on") or workflow.get(True)  # PyYAML reads an unquoted `on` as a boolean
    assert trigger["push"]["branches"] == ["main"]
    permissions = {k: v for k, v in workflow["permissions"].items() if k != "id-token"}
    assert permissions == {"contents": "write", "pull-requests": "write"}
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*(\S+)@(\S+)", text)
    assert len(uses) == 1
    action, ref = uses[0]
    assert action == "googleapis/release-please-action"
    assert FULL_SHA.fullmatch(ref), "actions are pinned to a full commit sha, not a tag: {}".format(ref)
    assert re.search(r"@{}\s+#\s+v\d+\.\d+\.\d+".format(re.escape(ref)), text), "say which release the sha is"
    steps = workflow["jobs"]["release-please"]["steps"]
    inputs = steps[0]["with"]
    assert (ROOT / inputs["config-file"]).is_file()
    assert (ROOT / inputs["manifest-file"]).is_file()


def test_ci_tests_job_keeps_every_required_check_and_scopes_only_the_run_steps():
    """Release-only pull requests skip the heavy steps, never the job: a skipped job reports no required check."""
    job = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["tests"]
    assert job["name"] == "tests (${{ matrix.os }}, Python ${{ matrix.python }})"
    assert job["timeout-minutes"] == 40
    assert "if" not in job, "a job-level condition would leave every required tests check unreported"
    assert job["strategy"]["matrix"]["include"] == [
        {"os": "ubuntu-latest", "python": "3.11"},
        {"os": "ubuntu-latest", "python": "3.14"},
        {"os": "macos-latest", "python": "3.14"},
        {"os": "windows-latest", "python": "3.14"},
    ]
    steps = {step["name"]: step for step in job["steps"] if "name" in step}
    scope = [step for step in job["steps"] if step.get("id") == "scope"]
    assert len(scope) == 1, "the tests job needs exactly one scope step"
    gate = scope[0]["if"]
    assert "github.event_name == 'pull_request'" in gate
    assert "startsWith(github.head_ref, 'release-please--')" in gate, \
        "a hand-made pull request that only edits plugin.yaml must still run every test"
    for name in ("Run the non-browser tier", "Run the browser tier"):
        assert steps[name]["if"] == "${{ !cancelled() && steps.scope.outputs.tests != 'skip' }}"
    for name in ("Upload the non-browser logs", "Upload the browser logs"):
        assert steps[name]["if"] == "always()"


def test_crypto_extra_admits_every_supported_hermes_pyjwt_pin():
    """The crypto extra must range over both PyJWT pins Hermes ships, not re-pin one of them.

    CI installs requirements-dev.txt, not the extra, so an exact pin here only shows up when Hermes's own pin cannot
    share the plugin environment and the gateway disables the plugin at boot. Hermes 0.22.x pins 2.13.0, 0.23.0+ 2.15.1.
    """
    spec = crypto_requirement()
    assert not any(clause.strip().startswith("==") for clause in spec.split(",")), \
        "an exact pin cannot sit next to Hermes core's own PyJWT pin: {}".format(spec)
    for version in ("2.13.0", "2.15.1"):
        assert admits(spec, version), "the crypto extra excludes Hermes's PyJWT {}: {}".format(version, spec)
