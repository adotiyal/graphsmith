"""
Code-quality layer — auto-format + advisory lint/type/complexity + frontend tooling.

These AUGMENT the proven blocking E,F linter (run_linter) without becoming a new hard
gate. The pure parsers are tested deterministically; the subprocess-backed functions are
tested for graceful degradation (they must never raise and must return the right shape
whether or not ruff/mypy are installed), and the deterministic frontend check is tested
end to end. Engineer surfacing mirrors the security-warnings test in test_hardening.py.
"""

import json
import shutil

from conftest import base_state, seed
from tools import registry
from tools.registry import (
    format_code, code_quality_report, check_frontend_quality_tooling,
    _parse_ruff_statistics, _mypy_error_count,
    _parse_coverage, quality_gate_level, check_quality_gate, _build_coverage_cmd,
    DOCKER_LIMITS,
)


# ── pure parsers (deterministic, no external tool) ───────────────────────────

def test_parse_ruff_statistics():
    out = _parse_ruff_statistics(
        "  12\tF401\t[*] `os` imported but unused\n"
        "   3\tE501\tLine too long\n"
        "   2\tC901\t`handler` is too complex\n"
        "garbage line that should be ignored\n"
    )
    assert (12, "F401", "[*] `os` imported but unused") in out
    assert (3, "E501", "Line too long") in out
    assert (2, "C901", "`handler` is too complex") in out
    assert len(out) == 3                                  # the garbage line is skipped


def test_mypy_error_count_from_summary():
    assert _mypy_error_count("src/main.py:4: error: Bad\nFound 7 errors in 2 files") == 7


def test_mypy_error_count_falls_back_to_line_count():
    out = "a.py:1: error: x\nb.py:2: error: y\nb.py:2: note: see here\n"
    assert _mypy_error_count(out) == 2                    # notes are not errors


# ── format_code: graceful + (when ruff present) real ─────────────────────────

def test_format_code_returns_tuple_and_never_raises(tmp_path):
    (tmp_path / "m.py").write_text("import os,sys\nx=1\n")
    ran, msg = format_code(str(tmp_path))                 # must not raise
    assert isinstance(ran, bool) and isinstance(msg, str)


def test_format_code_cleans_when_ruff_available(tmp_path):
    if not shutil.which("ruff"):
        import pytest
        pytest.skip("ruff not installed in this environment")
    f = tmp_path / "m.py"
    f.write_text("import sys, os\n\n\ndef f():\n    return os.getpid()\n")  # sys unused
    ran, _ = format_code(str(tmp_path))
    assert ran
    assert "import sys" not in f.read_text()              # unused import auto-removed


# ── code_quality_report: graceful + scoped ───────────────────────────────────

def test_code_quality_report_returns_list_and_never_raises(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    out = code_quality_report(str(tmp_path), [str(f)])
    assert isinstance(out, list)                          # clean → [] (or [] if no tooling)


def test_code_quality_report_handles_no_python_files(tmp_path):
    f = tmp_path / "page.tsx"
    f.write_text("export default function Page() { return null }\n")
    assert isinstance(code_quality_report(str(tmp_path), [str(f)]), list)


# ── frontend tooling check (fully deterministic, no Node needed) ──────────────

def test_frontend_check_noop_when_backend_only(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n")
    assert check_frontend_quality_tooling(str(tmp_path)) == []


def test_frontend_check_flags_missing_tooling(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "app", "scripts": {"dev": "next dev"}, "dependencies": {"next": "14"},
    }))
    out = " ".join(check_frontend_quality_tooling(str(tmp_path)))
    assert "ESLint" in out and "Prettier" in out and "typecheck" in out


def test_frontend_check_passes_with_full_tooling(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "app",
        "scripts": {"lint": "next lint", "format": "prettier -w .", "typecheck": "tsc --noEmit"},
        "devDependencies": {"eslint": "9", "prettier": "3"},
    }))
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}')
    assert check_frontend_quality_tooling(str(tmp_path)) == []


def test_frontend_check_flags_non_strict_tsconfig(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "app",
        "scripts": {"lint": "next lint", "format": "prettier -w .", "typecheck": "tsc --noEmit"},
        "devDependencies": {"eslint": "9", "prettier": "3"},
    }))
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {"strict": false}}')
    out = " ".join(check_frontend_quality_tooling(str(tmp_path)))
    assert "strict" in out


def test_frontend_check_finds_nested_frontend_dir(tmp_path):
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "package.json").write_text(json.dumps({"name": "fe", "scripts": {}}))
    out = " ".join(check_frontend_quality_tooling(str(tmp_path)))
    assert "ESLint" in out                                 # nested package.json is discovered


# ── engineer surfaces the advisory report (mirrors the security-warnings test) ─

def test_engineer_surfaces_code_quality(llm, ws, no_docker):
    from agents import engineer
    tech = seed(ws, "proj", "design", "tech_spec.md", "## API\nPOST /run")
    seed(ws, "proj", "tests", "test_run.py", "def test_run():\n    assert True")
    llm.default = ("===FILE: src/main.py===\n"
                   "import os\n\n\ndef run() -> int:\n    return os.getpid()\n"
                   "===END===")
    out = engineer.run(base_state(design_path=tech))
    assert "code_quality" in out
    assert isinstance(out["code_quality"], list)


# ── Coverage (§2.2, report-only) + complexity soft-gate (§2.1, opt-in) ───────

def test_parse_coverage_from_term_report():
    report = ("Name        Stmts   Miss  Cover\n"
              "main.py        10      2    80%\n"
              "TOTAL         100     20    80%\n")
    assert _parse_coverage(report) == 80
    assert _parse_coverage("TOTAL   50   0   100%") == 100
    assert _parse_coverage("no coverage total present") is None


def test_quality_gate_level_env(monkeypatch):
    monkeypatch.delenv("QUALITY_GATE", raising=False)
    assert quality_gate_level() == ""                       # default OFF
    monkeypatch.setenv("QUALITY_GATE", "report")
    assert quality_gate_level() == "report"
    monkeypatch.setenv("QUALITY_GATE", "BLOCK")
    assert quality_gate_level() == "block"                  # case-insensitive
    monkeypatch.setenv("QUALITY_GATE", "nonsense")
    assert quality_gate_level() == ""                       # unknown → off


def test_build_coverage_cmd_has_cov_flags_limits_and_workdir():
    cmd = _build_coverage_cmd("/proj", "tests/", "backend")
    joined = " ".join(cmd)
    assert "--cov" in joined and "pytest-cov" in joined
    for lim in DOCKER_LIMITS:
        assert lim in cmd
    assert "/app/backend" in cmd                            # runs in the python layer's dir


def test_quality_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("QUALITY_GATE", raising=False)
    assert check_quality_gate("/proj", []) == (True, "")    # never blocks unless opted in


def test_quality_gate_blocks_on_over_budget_complexity(monkeypatch):
    monkeypatch.setenv("QUALITY_GATE", "block")
    monkeypatch.setattr(registry, "_complexity_over_budget", lambda d, f=None: 3)
    ok, msg = check_quality_gate("/proj", [])
    assert ok is False and "complexity" in msg


def test_quality_gate_passes_when_under_budget(monkeypatch):
    monkeypatch.setenv("QUALITY_GATE", "block")
    monkeypatch.setattr(registry, "_complexity_over_budget", lambda d, f=None: 0)
    assert check_quality_gate("/proj", []) == (True, "")


def test_engineer_surfaces_coverage_in_report_mode(llm, ws, no_docker, monkeypatch):
    from agents import engineer
    tech = seed(ws, "proj", "design", "tech_spec.md", "## API\nPOST /run")
    seed(ws, "proj", "tests", "test_run.py", "def test_run():\n    assert True")
    monkeypatch.setattr(engineer, "quality_gate_level", lambda: "report")
    monkeypatch.setattr(engineer, "measure_coverage", lambda d: 85)
    llm.default = "===FILE: src/main.py===\nx = 1\n===END==="
    out = engineer.run(base_state(design_path=tech))
    assert out["tests_passed"] is True                      # report-only NEVER blocks
    assert any("coverage: 85%" in c for c in out["code_quality"])


def test_engineer_blocked_by_quality_gate_routes_back(llm, ws, no_docker, monkeypatch):
    from agents import engineer
    tech = seed(ws, "proj", "design", "tech_spec.md", "## API\nPOST /run")
    seed(ws, "proj", "tests", "test_run.py", "def test_run():\n    assert True")
    monkeypatch.setattr(engineer, "quality_gate_level", lambda: "block")
    monkeypatch.setattr(engineer, "measure_coverage", lambda d: 42)
    monkeypatch.setattr(engineer, "check_quality_gate",
                        lambda d, f=None: (False, "QUALITY GATE: 2 function(s) exceed complexity 10"))
    llm.default = "===FILE: src/main.py===\nx = 1\n===END==="
    out = engineer.run(base_state(design_path=tech))
    assert out["tests_passed"] is False                     # gate routes back to the engineer
    assert "QUALITY GATE" in out["error_log"]
    assert any("coverage: 42%" in c for c in out["code_quality"])  # still surfaced
