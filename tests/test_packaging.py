"""Tests for the supply-chain shape of this repo: pinned actions, pinned tools.

Neither file is Python, so both are read as text rather than parsed: `tomllib`
is 3.11 and this package supports 3.10, and a YAML parser is not worth a
dependency for four lines. What is being guarded is narrow and mechanical.
"""

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "test.yml"
PYPROJECT_PATH = REPOSITORY_ROOT / "pyproject.toml"

USES_LINE_PATTERN = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.MULTILINE)
PINNED_ACTION_PATTERN = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
TEST_EXTRA_PATTERN = re.compile(r"test = \[(.*?)\]", re.DOTALL)


def test_every_action_is_pinned_to_a_commit_with_its_tag_named():
    """A tag is a pointer its owner can move; this workflow runs with the repo checked out."""
    used = USES_LINE_PATTERN.findall(WORKFLOW_PATH.read_text())
    assert used, "no actions found, so this test is guarding nothing"
    for reference, trailing_comment in used:
        assert PINNED_ACTION_PATTERN.match(reference), f"{reference} is not pinned to a commit"
        assert trailing_comment.strip().startswith("#"), f"{reference} does not name its release"


def test_the_workflow_asks_for_no_more_than_read_access():
    workflow = WORKFLOW_PATH.read_text()
    assert "permissions:" in workflow, "the default token permissions are broader than needed"
    assert "contents: read" in workflow


def test_the_test_extra_pins_exact_versions():
    """CI installs whatever this says, so a new linter rule must arrive in a commit."""
    extra = TEST_EXTRA_PATTERN.search(PYPROJECT_PATH.read_text())
    assert extra is not None, "the test extra moved; this guard needs updating"
    requirements = [line.strip().strip('",') for line in extra.group(1).splitlines()]
    requirements = [line for line in requirements if line and not line.startswith("#")]
    assert requirements, "the test extra is empty"
    for requirement in requirements:
        assert "==" in requirement, f"{requirement} is not pinned to an exact version"
