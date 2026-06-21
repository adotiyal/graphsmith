"""
Project continuity — the single persistent project + feature ledger (tools/project_ctx.py).
"""

from tools import project_ctx


def test_has_code_false_until_real_files(ws):
    assert project_ctx.has_code() is False
    d = project_ctx.project_dir()
    (d / ".agent").mkdir(parents=True)
    (d / ".agent" / "ledger.md").write_text("x")     # .agent doesn't count as code
    assert project_ctx.has_code() is False
    (d / "app.py").write_text("x = 1")
    assert project_ctx.has_code() is True


def test_ledger_accumulates_across_features(ws):
    assert project_ctx.load_ledger() == ""
    project_ctx.append_ledger("add user login", {
        "change_type": "feature", "tech_stack": "FastAPI + Next.js",
        "code_files": ["/p/auth.py", "/p/models.py"], "tests_passed": True})
    led = project_ctx.load_ledger()
    assert "add user login" in led and "auth.py" in led and "feature" in led

    project_ctx.append_ledger("fix login redirect", {
        "change_type": "bugfix", "code_files": [], "tests_passed": True})
    led2 = project_ctx.load_ledger()
    assert "add user login" in led2 and "fix login redirect" in led2   # history accumulates
    assert led2.count("## ") == 2
