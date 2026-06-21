"""
Web search for the thinking/spec agents (§4.2) — OPT-IN via LLM_WEB_SEARCH.

Like adaptive thinking (§5.1), this is OFF by default so enabling it is a deliberate,
separately-verified step. The tests verify: the opt-in gate; the CLI command wiring
(the part that needs a live run to truly exercise, so at minimum the command is built
right); that call_llm ignores a search request when opted out and FALLS BACK to a plain
call if the search wiring fails (so it can never break a run); and that work_call threads
the flag so the architect/surveyor actually request it.
"""

import subprocess

from conftest import base_state, seed
from tools import llm
from tools.qa_utils import work_call


# ── opt-in gating ─────────────────────────────────────────────────────────────

def test_web_search_off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_WEB_SEARCH", raising=False)
    assert llm._web_search_active() is False


def test_web_search_on_when_opted_in(monkeypatch):
    for v in ("1", "true", "on", "YES"):
        monkeypatch.setenv("LLM_WEB_SEARCH", v)
        assert llm._web_search_active() is True
    monkeypatch.setenv("LLM_WEB_SEARCH", "nonsense")
    assert llm._web_search_active() is False


# ── CLI command construction (the wiring a live run exercises end-to-end) ──────

def test_cli_call_web_search_branch(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, cwd, env, input=None):
        seen["cmd"] = cmd
        class R:
            returncode = 0
            stdout = '{"result": "grounded", "usage": {"input_tokens": 1, "output_tokens": 2}}'
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(llm, "_find_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(llm, "_CLI_WORKDIR", str(tmp_path))
    text, _, _ = llm._cli_call("SYS", "pin current versions", "claude-opus-4-8", web_search=True)
    cmd = seen["cmd"]
    assert text == "grounded"
    assert cmd[cmd.index("--allowed-tools") + 1] == "WebSearch"   # search tool allowed
    assert cmd[cmd.index("--max-turns") + 1] == "8"               # turns to search THEN answer
    sys_arg = cmd[cmd.index("--append-system-prompt") + 1]
    assert "WebSearch tool" in sys_arg and "Tools are DISABLED" not in sys_arg


# ── call_llm gating + safety-net fallback ──────────────────────────────────────

def test_call_llm_ignores_search_when_opted_out(monkeypatch):
    monkeypatch.delenv("LLM_WEB_SEARCH", raising=False)
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    seen = {}

    def fake_cli(s, u, m, timeout=1800, images=None, web_search=False):
        seen["web_search"] = web_search
        return ("out", 1, 2)

    monkeypatch.setattr(llm, "_cli_call", fake_cli)
    assert llm.call_llm("s", "u", tier="reason", web_search=True) == "out"
    assert seen["web_search"] is False           # opted out → no search even when requested


def test_call_llm_falls_back_when_search_fails(monkeypatch):
    monkeypatch.setenv("LLM_WEB_SEARCH", "1")
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    attempts = []

    def fake_cli(s, u, m, timeout=1800, images=None, web_search=False):
        attempts.append(web_search)
        if web_search:
            raise RuntimeError("WebSearch wiring unavailable")
        return ("memory-grounded", 1, 2)

    monkeypatch.setattr(llm, "_cli_call", fake_cli)
    # enabling search can NEVER break a run — it falls back to a plain (memory) call
    assert llm.call_llm("s", "u", tier="reason", web_search=True) == "memory-grounded"
    assert attempts == [True, False]             # tried search, then fell back


def test_call_llm_skips_search_for_vision(monkeypatch):
    monkeypatch.setenv("LLM_WEB_SEARCH", "1")
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    seen = {}

    def fake_cli(s, u, m, timeout=1800, images=None, web_search=False):
        seen["web_search"] = web_search
        return ("ok", 1, 2)

    monkeypatch.setattr(llm, "_cli_call", fake_cli)
    llm.call_llm("s", "u", tier="strong", images=[("a", "/x.png")], web_search=True)
    assert seen["web_search"] is False           # don't mix search with the vision Read flow


# ── work_call threads it; the spec agents request it ──────────────────────────

def test_work_call_threads_web_search(llm):
    llm.default = "SPEC"
    work_call("sys", "design the spec", "reason", [], allow_clarify=False, web_search=True)
    assert llm.calls[-1]["web_search"] is True
    # default stays off for the other producing agents
    work_call("sys", "do other work", "fast", [], allow_clarify=False)
    assert llm.calls[-1]["web_search"] is False


def test_architect_requests_web_search(llm, ws):
    from agents import architect
    prd = seed(ws, "proj", "prd", "prd.md", "## Acceptance Criteria\n1. user can log in")
    design = seed(ws, "proj", "design", "design_spec.md", "## Screens\nLogin")
    llm.default = "## Stack\nFastAPI\n## API Endpoints\nPOST /login → 200"
    architect.run(base_state(prd_path=prd, design_path=design,
                             tech_stack="FastAPI+Next.js+Postgres", tech_stack_confirmed=True))
    assert any(c["web_search"] for c in llm.calls)        # the tech spec is grounded


def test_surveyor_requests_web_search(llm, ws, tmp_path):
    from agents import surveyor
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("import fastapi\n")       # non-empty → surveyor actually runs
    prd = seed(ws, "proj", "prd", "prd.md", "## Acceptance Criteria\n1. add a route")
    llm.default = "## Stack & Conventions\nFastAPI in app.py\n## Where The Feature Plugs In\napp.py"
    surveyor.run(base_state(prd_path=prd, target_repo=str(repo), managed_project=False))
    assert any(c["web_search"] for c in llm.calls)
