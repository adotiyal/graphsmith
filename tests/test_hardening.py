"""
Phase 2.3 — execution hardening: static security scan + Docker resource limits.
"""

from conftest import base_state, seed
from tools.registry import scan_security, _build_test_cmd, DOCKER_LIMITS


def test_scan_security_flags_dangerous_code(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("import os\nresult = eval(user_input)\nos.system('rm -rf /')\n"
                 "API_KEY = 'sk_live_abcd1234efgh'\n")
    ok, issues = scan_security([str(f)])
    assert not ok
    blob = " ".join(issues).lower()
    assert "eval" in blob and "os.system" in blob and "secret" in blob


def test_scan_security_clean_code(tmp_path):
    f = tmp_path / "good.py"
    f.write_text("def add(a, b):\n    return a + b\n")
    ok, issues = scan_security([str(f)])
    assert ok and issues == []


def test_scan_security_ignores_non_code_files(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("we should eval( the options ) and pick a password = 'discussiononly'")
    ok, _ = scan_security([str(f)])
    assert ok                                   # .txt is not scanned


def test_scan_security_shell_true(tmp_path):
    f = tmp_path / "run.py"
    f.write_text("import subprocess\nsubprocess.run(cmd, shell=True)\n")
    ok, issues = scan_security([str(f)])
    assert not ok and any("shell=True" in i for i in issues)


def test_docker_cmd_has_resource_limits():
    cmd = _build_test_cmd("/proj/dir", "tests/")
    for limit in DOCKER_LIMITS:
        assert limit in cmd
    assert "/proj/dir:/app" in cmd
    assert any("pytest tests/" in part for part in cmd)


def test_engineer_surfaces_security_warnings(llm, ws, no_docker):
    from agents import engineer
    tech = seed(ws, "proj", "design", "tech_spec.md", "## API\nPOST /run")
    seed(ws, "proj", "tests", "test_run.py", "def test_run():\n    assert True")
    llm.default = "===FILE: src/main.py===\nimport os\nos.system('echo hi')\n===END==="
    out = engineer.run(base_state(design_path=tech))
    assert out["security_warnings"]
    assert any("os.system" in w for w in out["security_warnings"])
