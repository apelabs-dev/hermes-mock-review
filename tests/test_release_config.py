"""Release Please wiring: one version everywhere, v-prefixed tags, an action pinned by commit.

`release-please.yml` runs on every push to main and keeps a release pull request open. Merging that pull request tags
vX.Y.Z. The version lives in three places and this file asserts they agree, so a bump only ever has to touch the files
the action itself edits: `.release-please-manifest.json`, `plugin.yaml` and `mockreview/__init__.py`.
"""

import json
import re

import pytest
import yaml

from conftest import ROOT

CONFIG = ROOT / "release-please-config.json"
MANIFEST = ROOT / ".release-please-manifest.json"
WORKFLOW = ROOT / ".github" / "workflows" / "release-please.yml"
PLUGIN = ROOT / "plugin.yaml"
INIT = ROOT / "mockreview" / "__init__.py"
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


def test_one_version_lives_in_the_manifest_the_plugin_and_the_package():
    versions = {
        ".release-please-manifest.json": read_json(MANIFEST)["."],
        "plugin.yaml": plugin_manifest()["version"],
        "mockreview/__init__.py": init_version(),
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
    ]


@pytest.mark.parametrize("path, declaration", [(PLUGIN, "version:"), (INIT, "__version__ =")])
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
