"""
Tests for tools/file_io.py write helpers.

Regression: write_artifact must create the file's full parent chain even when the
filename itself contains nested dirs (e.g. DevOps emits ".github/workflows/deploy.yml").
Before the fix, workspace_path only made the `subdir`, so the nested write crashed
with FileNotFoundError — surfaced by a live DevOps run.
"""

from tools import file_io
from tools.file_io import write_artifact


def test_write_artifact_creates_nested_filename_dirs(ws):
    rel = write_artifact("proj1", "deploy", ".github/workflows/deploy.yml", "name: deploy\n")
    written = ws / "proj1" / "deploy" / ".github" / "workflows" / "deploy.yml"
    assert written.exists()
    assert written.read_text() == "name: deploy\n"
    # returned path is relative to the workspace parent
    assert rel.endswith("deploy/.github/workflows/deploy.yml")


def test_write_artifact_flat_filename_still_works(ws):
    write_artifact("proj1", "prd", "prd.md", "# PRD\n")
    assert (ws / "proj1" / "prd" / "prd.md").read_text() == "# PRD\n"
