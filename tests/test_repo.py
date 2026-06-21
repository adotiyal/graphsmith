"""
Phase 2.1 — tools/repo.py (read-only codebase access + guarded writer).
"""

import pytest
from tools import repo


def _make_repo(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"app"}')
    (tmp_path / "next.config.js").write_text("module.exports = {}")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp_path / "app" / "auth.py").write_text("def login():\n    return 'token'\n")
    nm = tmp_path / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = 1")
    return tmp_path


def test_list_files_skips_ignored_dirs(tmp_path):
    _make_repo(tmp_path)
    files = repo.list_files(tmp_path)
    assert "app/main.py" in files
    assert not any("node_modules" in f for f in files)   # ignored


def test_detect_stack(tmp_path):
    _make_repo(tmp_path)
    stack = repo.detect_stack(tmp_path)
    assert "Next.js" in stack and "Python" in stack


def test_build_repo_map_is_compact_and_informative(tmp_path):
    _make_repo(tmp_path)
    m = repo.build_repo_map(tmp_path, max_chars=2000)
    assert "Detected stack:" in m and "app/main.py" in m
    assert len(m) <= 2000


def test_grep_finds_matches(tmp_path):
    _make_repo(tmp_path)
    hits = repo.grep(tmp_path, "FastAPI")
    assert any(rel == "app/main.py" for rel, _, _ in hits)


def test_read_repo_file_truncates(tmp_path):
    (tmp_path / "big.py").write_text("x" * 5000)
    out = repo.read_repo_file(tmp_path, "big.py", max_chars=100)
    assert out.endswith("[... truncated ...]") and len(out) < 200


def test_write_into_repo_writes_and_creates_dirs(tmp_path):
    p = repo.write_into_repo(tmp_path, "app/routers/new.py", "print('x')")
    assert (tmp_path / "app" / "routers" / "new.py").read_text() == "print('x')"
    assert p.endswith("new.py")


def test_write_into_repo_blocks_path_traversal(tmp_path):
    (tmp_path / "inside").mkdir()
    with pytest.raises(ValueError):
        repo.write_into_repo(tmp_path / "inside", "../../etc/evil", "nope")


# ── #3 minimal-diff edit applier ─────────────────────────────────────────────

def test_apply_edit_success_is_minimal(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n\ndef g():\n    return 9\n")
    ok, _ = repo.apply_edit(tmp_path, "a.py", "    return 1", "    return 2")
    assert ok
    text = f.read_text()
    assert "return 2" in text and "def g():\n    return 9" in text   # only the target line changed


def test_apply_edit_not_found(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    ok, msg = repo.apply_edit(tmp_path, "a.py", "y = 2", "y = 3")
    assert not ok and "not found" in msg


def test_apply_edit_ambiguous_match_refused(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n")
    ok, msg = repo.apply_edit(tmp_path, "a.py", "x = 1", "x = 2")
    assert not ok and "not unique" in msg
    assert (tmp_path / "a.py").read_text() == "x = 1\nx = 1\n"       # file untouched on failure


def test_apply_edit_missing_file(tmp_path):
    ok, msg = repo.apply_edit(tmp_path, "nope.py", "a", "b")
    assert not ok and "not found for edit" in msg
