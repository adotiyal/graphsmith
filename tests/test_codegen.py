"""
I1 verification suite — structured file changes (tools/codegen.py).

Proves the text-format failure classes are STRUCTURALLY IMPOSSIBLE in the tools
path, not merely discouraged:
- duplicate emission → clean overwrite, last wins, no marker text can corrupt a file
- stale edit anchor → clean failure carrying the CURRENT on-disk content
- oracle/kit protection + path escapes enforced at ONE deterministic choke point
- a clarify round discards its staging (no half-applied work)
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools import codegen


def _mk_project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_app.py").write_text("def test_add():\n    assert True\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x")
    return root


# ---------------------------------------------------------------------------
# Executor semantics (shared by the api loop; the CLI's own Edit tool matches)
# ---------------------------------------------------------------------------

def test_stage_excludes_junk_and_copies_source(tmp_path):
    root = _mk_project(tmp_path)
    staging = codegen._stage(str(root))
    assert (Path(staging) / "src" / "app.py").is_file()
    assert not (Path(staging) / ".git").exists()
    assert not (Path(staging) / "node_modules").exists()


def test_duplicate_write_is_clean_overwrite_last_wins(tmp_path):
    # The corruption class: a duplicate unclosed ===FILE:=== marker once glued
    # marker text INTO five source files. With real writes there is no marker
    # and no parsing — a re-emission is a clean whole-file overwrite.
    root = str(_mk_project(tmp_path))
    codegen.tool_write(root, "src/new.py", "VERSION_ONE = 1\n")
    ok, _ = codegen.tool_write(root, "src/new.py", "VERSION_TWO = 2\n")
    assert ok
    content = (Path(root) / "src" / "new.py").read_text()
    assert content == "VERSION_TWO = 2\n"
    assert "===" not in content


def test_stale_edit_anchor_fails_clean_with_current_content(tmp_path):
    # The stale-SEARCH plague: the model anchored edits on its MEMORY of a file.
    # Now a bad anchor fails cleanly and the error carries the REAL content so
    # the next attempt anchors on disk truth. The file is untouched.
    root = str(_mk_project(tmp_path))
    before = (Path(root) / "src" / "app.py").read_text()
    ok, msg = codegen.tool_edit(root, "src/app.py",
                                "def add(x, y):", "def add(a, b, c):")
    assert not ok
    assert "not found" in msg and "def add(a, b):" in msg   # current content shown
    assert (Path(root) / "src" / "app.py").read_text() == before


def test_ambiguous_edit_anchor_fails_clean(tmp_path):
    root = str(_mk_project(tmp_path))
    p = Path(root) / "src" / "app.py"
    p.write_text("x = 1\nx = 1\n")
    ok, msg = codegen.tool_edit(root, "src/app.py", "x = 1", "x = 2")
    assert not ok and "2 times" in msg
    assert p.read_text() == "x = 1\nx = 1\n"


def test_unique_edit_applies(tmp_path):
    root = str(_mk_project(tmp_path))
    ok, _ = codegen.tool_edit(root, "src/app.py", "return a + b", "return a + b + 0")
    assert ok
    assert "a + b + 0" in (Path(root) / "src" / "app.py").read_text()


def test_path_escape_blocked_in_every_op(tmp_path):
    root = str(_mk_project(tmp_path))
    outside = tmp_path / "outside.txt"
    for op in (lambda: codegen.tool_write(root, "../outside.txt", "x"),
               lambda: codegen.tool_write(root, "/etc/passwd", "x"),
               lambda: codegen.tool_edit(root, "../outside.txt", "a", "b"),
               lambda: codegen.tool_delete(root, "../../outside.txt"),
               lambda: codegen.tool_read(root, "../outside.txt")):
        ok, msg = op()
        assert not ok and "escapes" in msg
    assert not outside.exists()


# ---------------------------------------------------------------------------
# Sync-back — the single guarded choke point
# ---------------------------------------------------------------------------

def _no_protection(rel):
    return False


def _oracle_protection(rel):
    return any(p in ("tests", "e2e") for p in Path(rel).parts)


def test_sync_back_applies_new_changed_and_deleted(tmp_path):
    root = _mk_project(tmp_path)
    staging = Path(codegen._stage(str(root)))
    (staging / "src" / "app.py").write_text("def add(a, b):\n    return a + b  # v2\n")
    (staging / "src" / "extra.py").write_text("NEW = True\n")
    (staging / "src" / "stale.py").write_text("x")          # then delete from staging
    (root / "src" / "stale.py").write_text("x")             # exists in original
    (staging / "src" / "stale.py").unlink()
    written, deleted, violations = codegen.sync_back(str(staging), str(root), _no_protection)
    assert str(root / "src" / "app.py") in written
    assert str(root / "src" / "extra.py") in written
    assert deleted == [str(root / "src" / "stale.py")]
    assert not violations
    assert "# v2" in (root / "src" / "app.py").read_text()
    assert not (root / "src" / "stale.py").exists()


def test_sync_back_blocks_oracle_change_and_delete(tmp_path):
    root = _mk_project(tmp_path)
    staging = Path(codegen._stage(str(root)))
    (staging / "tests" / "test_app.py").write_text("def test_add():\n    assert 1 == 2\n")
    (staging / "backend" / "tests").mkdir(parents=True)
    (staging / "backend" / "tests" / "test_deep.py").write_text("hacked")  # any depth
    written, deleted, violations = codegen.sync_back(str(staging), str(root), _oracle_protection)
    assert (root / "tests" / "test_app.py").read_text() == "def test_add():\n    assert True\n"
    assert not (root / "backend").exists()
    assert len(violations) == 2 and all("protected" in v for v in violations)
    assert not written and not deleted

    # oracle deletion is also blocked
    staging2 = Path(codegen._stage(str(root)))
    (staging2 / "tests" / "test_app.py").unlink()
    _, deleted2, violations2 = codegen.sync_back(str(staging2), str(root), _oracle_protection)
    assert (root / "tests" / "test_app.py").exists()
    assert not deleted2 and any("delete" in v for v in violations2)


def test_sync_back_case_rename_survives(tmp_path):
    # macOS case-insensitive FS: rename-by-case (Icons.tsx → icons.tsx) made the
    # old write-then-delete order delete the freshly written file — a live run
    # lost the kit's icon module. Deletions now run FIRST.
    root = _mk_project(tmp_path)
    (root / "src" / "Icons.tsx").write_text("export const OLD = 1;\n")
    staging = Path(codegen._stage(str(root)))
    # the model renames by case in staging
    (staging / "src" / "Icons.tsx").unlink()
    (staging / "src" / "icons.tsx").write_text("export const NEW = 2;\n")
    codegen.sync_back(str(staging), str(root), _no_protection)
    survivors = [p for p in (root / "src").iterdir() if p.name.lower() == "icons.tsx"]
    assert survivors, "the icon module must survive a case-rename"
    assert "NEW" in survivors[0].read_text()


def test_sync_back_spares_files_created_after_staging(tmp_path):
    # A concurrent repair (CTO writing into root mid-round) is ABSENT from staging
    # but was never seen — the deletion mirror must spare it (a live repair was
    # silently erased before this fix).
    root = _mk_project(tmp_path)
    staging = codegen._stage(str(root))
    staged_at_start = set(codegen._walk_files(staging))
    (root / "src" / "repair.py").write_text("CTO = 'fixed it'\n")   # lands AFTER copy
    _, deleted, _ = codegen.sync_back(staging, str(root), _no_protection,
                                      staged_at_start=staged_at_start)
    assert (root / "src" / "repair.py").exists()
    assert not deleted


def test_sync_back_never_touches_ignored_dirs(tmp_path):
    # .git / node_modules are absent from staging — the deletion mirror must
    # NOT interpret that as "the model deleted them".
    root = _mk_project(tmp_path)
    staging = codegen._stage(str(root))
    _, deleted, _ = codegen.sync_back(staging, str(root), _no_protection)
    assert not deleted
    assert (root / ".git" / "HEAD").exists()
    assert (root / "node_modules" / "junk.js").exists()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def test_cli_codegen_command_shape(tmp_path, monkeypatch):
    root = _mk_project(tmp_path)
    staging = codegen._stage(str(root))
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, cwd, env, input=None):
        seen["cmd"], seen["cwd"], seen["prompt"] = cmd, cwd, input
        class R:
            returncode = 0
            stdout = json.dumps({"result": "done", "usage": {}})
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("tools.llm._find_claude_bin", lambda: "/fake/claude")
    out = codegen._cli_codegen("SYS", "make it", staging, "claude-sonnet-5")
    cmd = seen["cmd"]
    assert out == "done"
    assert seen["cwd"] == staging                            # session lives in staging
    allowed = cmd[cmd.index("--allowed-tools") + 1]
    assert "Write" in allowed and "Edit" in allowed and "Read" in allowed
    assert "Bash" not in allowed                             # no shell in codegen
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "--strict-mcp-config" in cmd
    sys_arg = cmd[cmd.index("--append-system-prompt") + 1]
    assert sys_arg.startswith("SYS") and "FILE-CHANGE MODE" in sys_arg


def test_api_codegen_tool_loop(tmp_path, monkeypatch):
    root = _mk_project(tmp_path)
    staging = codegen._stage(str(root))

    class Block:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    responses = [
        # round 1: the model writes a file and makes an edit
        Block(stop_reason="tool_use", content=[
            Block(type="tool_use", id="t1", name="write_file",
                  input={"path": "src/new.py", "content": "NEW = 1\n"}),
            Block(type="tool_use", id="t2", name="edit_file",
                  input={"path": "src/app.py", "old_string": "return a + b",
                         "new_string": "return a + b  # edited"}),
        ]),
        # round 2: done
        Block(stop_reason="end_turn", content=[Block(type="text", text="summary: done")]),
    ]

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                return responses.pop(0)

    monkeypatch.setattr("tools.llm.get_client", lambda: FakeClient)
    text = codegen._api_codegen("SYS", "make it", staging, "strong")
    assert text == "summary: done"
    assert (Path(staging) / "src" / "new.py").read_text() == "NEW = 1\n"
    assert "# edited" in (Path(staging) / "src" / "app.py").read_text()


def test_cli_codegen_salvages_partial_work_on_turn_cap(tmp_path, monkeypatch):
    # Live: phase-3's build hit the turn cap at 59K output tokens and the raise
    # DISCARDED 14 minutes of staged work. A cap now returns a marker; generate
    # syncs the partial work and the test loop continues from it.
    import subprocess
    root = _mk_project(tmp_path)

    def fake_run(cmd, capture_output, text, timeout, cwd, env, input=None):
        Path(cwd, "src", "partial.py").write_text("HALF_DONE = True\n")
        class R:
            returncode = 1
            stdout = json.dumps({"subtype": "error_max_turns", "is_error": True,
                                 "num_turns": 150})
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("tools.llm._find_claude_bin", lambda: "/fake/claude")
    monkeypatch.setenv("LLM_BACKEND", "claude-cli")
    result = codegen.generate("SYS", "build it", str(root), _no_protection)
    assert "turn cap" in result["text"]
    assert any(p.endswith("partial.py") for p in result["written"])   # work kept
    assert (root / "src" / "partial.py").read_text() == "HALF_DONE = True\n"


def test_generate_discards_staging_on_clarify(tmp_path, monkeypatch):
    # A NEEDS_INPUT round must not half-apply work product.
    root = _mk_project(tmp_path)

    def fake_cli(system, user_msg, staging, model, timeout=0):
        Path(staging, "src", "halfdone.py").write_text("partial")
        return "===NEEDS_INPUT===\n{\"ceo\": \"which db?\"}\n===END==="

    monkeypatch.setenv("LLM_BACKEND", "claude-cli")
    monkeypatch.setattr(codegen, "_cli_codegen", fake_cli)
    result = codegen.generate("SYS", "msg", str(root), _no_protection,
                              apply_when=lambda t: "NEEDS_INPUT" not in t)
    assert "NEEDS_INPUT" in result["text"]
    assert result["written"] == [] and result["deleted"] == []
    assert not (root / "src" / "halfdone.py").exists()


def test_generate_applies_and_reports(tmp_path, monkeypatch):
    root = _mk_project(tmp_path)

    def fake_cli(system, user_msg, staging, model, timeout=0):
        Path(staging, "src", "feature.py").write_text("DONE = True\n")
        Path(staging, "tests", "test_app.py").write_text("tampered")   # guard food
        return "implemented the feature"

    monkeypatch.setenv("LLM_BACKEND", "claude-cli")
    monkeypatch.setattr(codegen, "_cli_codegen", fake_cli)
    result = codegen.generate("SYS", "msg", str(root), _oracle_protection)
    assert (root / "src" / "feature.py").read_text() == "DONE = True\n"
    assert (root / "tests" / "test_app.py").read_text() != "tampered"
    assert any("protected" in v for v in result["violations"])


# ---------------------------------------------------------------------------
# Engineer wiring (tools mode)
# ---------------------------------------------------------------------------

def test_engineer_tools_mode_wiring(tmp_path, monkeypatch):
    from tests.conftest import base_state, seed
    from agents import engineer

    monkeypatch.setenv("AGENT_CODEGEN", "tools")
    ws_root = tmp_path / "ws"
    monkeypatch.setattr("tools.file_io.WORKSPACE_ROOT", ws_root)
    monkeypatch.setattr(engineer, "WORKSPACE_ROOT", ws_root, raising=False)
    spec = seed(ws_root, "p1", "design", "tech_spec.md", "build the thing")
    seed(ws_root, "p1", "tests", "test_x.py", "def test_x(): pass")
    monkeypatch.setattr(engineer, "run_linter", lambda d: (True, "ok"))
    monkeypatch.setattr(engineer, "run_project_tests",
                        lambda d, timeout=300, test_path="tests/": (True, "1 passed"))

    captured = {}

    def fake_generate(system, user_msg, root, is_protected, tier="strong", apply_when=None):
        captured.update(system=system, user_msg=user_msg, root=root,
                        is_protected=is_protected)
        f = Path(root) / "src" / "impl.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("OK = 1\n")
        return {"text": "implemented", "written": [str(f)], "deleted": [],
                "violations": ["protected path change discarded: tests/test_x.py"]}

    monkeypatch.setattr(engineer.codegen, "generate", fake_generate)
    state = base_state(design_path=spec)
    out = engineer._do_work(state, [], {}, allow_clarify=False)

    assert out["tests_passed"] is True
    assert out["code_files"] and out["code_files"][0].endswith("impl.py")
    assert any("GUARD" in w for w in out["security_warnings"])
    assert "file tools" in captured["user_msg"]              # tools prompt, no ===FILE===
    assert "===FILE:" not in captured["user_msg"]
    # the protection predicate guards oracles at any depth + greenfield meta-dirs
    p = captured["is_protected"]
    assert p("tests/test_x.py") and p("backend/tests/test_y.py") and p("e2e/a.spec.ts")
    assert p("design/mockup.html") and p("prd/prd.md")
    assert not p("src/impl.py") and not p("docker-compose.yml")


def test_engineer_code_files_accumulate_across_fix_rounds(tmp_path, monkeypatch):
    # Live finding: a tool-mode retry writes only the minimal diff (one round wrote
    # just docker-compose.yml) — REPLACING code_files blinded QA to the actual
    # implementation. The list must accumulate, dropping files that no longer exist.
    from tests.conftest import base_state, seed
    from agents import engineer

    monkeypatch.setenv("AGENT_CODEGEN", "tools")
    ws_root = tmp_path / "ws"
    monkeypatch.setattr("tools.file_io.WORKSPACE_ROOT", ws_root)
    spec = seed(ws_root, "p1", "design", "tech_spec.md", "fix it")
    impl = seed(ws_root, "p1", "src", "impl.py", "OK = 1\n")
    monkeypatch.setattr(engineer, "run_linter", lambda d: (True, "ok"))
    monkeypatch.setattr(engineer, "run_project_tests",
                        lambda d, timeout=300, test_path="tests/": (True, "1 passed"))

    def fake_generate(system, user_msg, root, is_protected, tier="strong", apply_when=None):
        f = Path(root) / "docker-compose.yml"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("services: {}\n")
        return {"text": "minimal fix", "written": [str(f)], "deleted": [],
                "violations": []}

    monkeypatch.setattr(engineer.codegen, "generate", fake_generate)
    state = base_state(design_path=spec, code_files=[impl], fix_attempts=1)
    out = engineer._do_work(state, [], {}, allow_clarify=False)
    assert impl in out["code_files"]                            # earlier round kept
    assert any(p.endswith("docker-compose.yml") for p in out["code_files"])


def test_engineer_tools_mode_clarify_path(tmp_path, monkeypatch):
    from tests.conftest import base_state, seed
    from agents import engineer

    monkeypatch.setenv("AGENT_CODEGEN", "tools")
    ws_root = tmp_path / "ws"
    monkeypatch.setattr("tools.file_io.WORKSPACE_ROOT", ws_root)
    spec = seed(ws_root, "p1", "design", "tech_spec.md", "build the thing")

    def fake_generate(system, user_msg, root, is_protected, tier="strong", apply_when=None):
        text = "===NEEDS_INPUT===\n{\"ceo\": \"which auth provider?\"}\n===END==="
        assert apply_when is not None and not apply_when(text)   # clarify discards staging
        return {"text": text, "written": [], "deleted": [], "violations": []}

    monkeypatch.setattr(engineer.codegen, "generate", fake_generate)
    out = engineer._do_work(base_state(design_path=spec), [], {}, allow_clarify=True)
    assert "_clarify" in out and "auth provider" in str(out["_clarify"])


# ---------------------------------------------------------------------------
# I17 — domain-restricted writers (design/test_author/qa via the tools path)
# ---------------------------------------------------------------------------

def test_domain_protected_prefix_and_segment():
    kit = codegen.domain_protected(allowed_prefixes=["frontend/src/components/kit/"])
    assert kit("frontend/src/components/kit/Card.tsx") is False     # own domain
    assert kit("frontend/src/components/TaskPage.tsx") is True      # protected
    assert kit("backend/app/main.py") is True

    tests = codegen.domain_protected(allowed_segments={"tests"})
    assert tests("tests/test_x.py") is False                        # flat layout
    assert tests("backend/tests/test_y.py") is False               # split layout
    assert tests("frontend/src/app.tsx") is True                   # protected
    assert tests("backend/app/models.py") is True


def test_generate_in_domain_blocks_out_of_domain_writes(tmp_path, monkeypatch):
    # A domain writer that strays outside its lane has those writes DISCARDED as
    # guard violations, never silently applied (the structural I17 guarantee).
    root = _mk_project(tmp_path)
    (root / "tests" / "test_app.py").write_text("def test_old():\n    assert True\n")

    def fake_cli(system, user_msg, staging, model, timeout=0):
        # writes one legit test AND tries to clobber app source
        Path(staging, "tests", "test_new.py").write_text("def test_new():\n    assert 1\n")
        Path(staging, "src", "app.py").write_text("HACKED = True\n")
        return "wrote tests"

    monkeypatch.setenv("LLM_BACKEND", "claude-cli")
    monkeypatch.setattr(codegen, "_cli_codegen", fake_cli)
    result = codegen.generate_in_domain("SYS", "write tests", str(root),
                                        allowed_segments={"tests"})
    assert any(p.endswith("test_new.py") for p in result["written"])   # domain write ok
    assert (root / "src" / "app.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert any("app.py" in v and "protected" in v for v in result["violations"])


def test_qa_and_design_emit_via_tools_path(tmp_path, monkeypatch):
    # I17: QA e2e authoring and design kit emission route through the domain-guarded
    # tools path (Read-before-write), not text parsing.
    from tests.conftest import base_state, seed
    from agents import qa, design
    monkeypatch.setenv("AGENT_CODEGEN", "tools")
    ws = tmp_path / "ws"
    monkeypatch.setattr("tools.file_io.WORKSPACE_ROOT", ws)

    seen = {}
    def fake_gen(system, user_msg, root, allowed_prefixes=(), allowed_segments=(),
                tier="strong", apply_when=None):
        seen.setdefault("calls", []).append({"segments": set(allowed_segments),
                                              "prefixes": list(allowed_prefixes)})
        # QA case: drop a real spec into e2e/
        if "e2e" in set(allowed_segments):
            d = Path(root) / "e2e"; d.mkdir(parents=True, exist_ok=True)
            (d / "test_flow.py").write_text("def test_x(page):\n    expect(1)\n")
        return {"text": "done", "written": [], "deleted": [], "violations": []}
    monkeypatch.setattr(design.codegen, "generate_in_domain", fake_gen)
    monkeypatch.setattr(qa.codegen, "generate_in_domain", fake_gen)

    files = qa._emit_e2e(base_state(project_id="proj"), "SYS", "author specs")
    assert files == ["e2e/test_flow.py"]
    assert any(c["segments"] == {"e2e"} for c in seen["calls"])     # e2e-only domain
