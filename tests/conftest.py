"""
Shared test fixtures for the AgentPlatform test suites.

These tests run WITHOUT an Anthropic API key or Docker — every LLM call and the
Docker test runner are mocked. They verify the architecture and each agent's
input/output/escalation behavior deterministically.

Run with:  pytest tests/ -q   (inside the project venv)
"""

import importlib
import pytest

# Modules that hold a direct reference to call_llm (imported by name).
# tools.llm is included so call_structured's internal call_llm is the mock too (the
# structured control-plane signals: triage/critic/design_qa route through call_structured).
_LLM_MODULES = ["tools.llm", "tools.qa_utils", "agents.ceo", "agents.triage",
                "agents.engineer", "agents.qa", "agents.critic", "agents.design",
                "agents.design_qa"]
# Modules that imported WORKSPACE_ROOT by name (must be redirected for isolation).
_WSROOT_MODULES = ["tools.file_io", "agents.engineer", "agents.test_author", "agents.ship",
                   "agents.integration", "agents.design_qa", "agents.design",
                   "tools.report_html"]


class MockLLM:
    """
    Callable stand-in for tools.llm.call_llm.

    - .default       : returned when nothing else matches
    - .queue         : list of responses popped in order (overrides default)
    - .router(s,u,t) : optional fn returning a response or None to fall through
    - .calls         : recorded [{system, user, tier}] for assertions
    """
    def __init__(self):
        self.calls = []
        self.queue = []
        self.default = "MOCK OUTPUT"
        self.router = None

    def __call__(self, system_prompt, user_message, tier="fast", images=None,
                 web_search=False):
        self.calls.append({"system": system_prompt, "user": user_message,
                           "tier": tier, "images": images, "web_search": web_search})
        if self.router is not None:
            r = self.router(system_prompt, user_message, tier)
            if r is not None:
                return r
        if self.queue:
            return self.queue.pop(0)
        return self.default

    def user_texts(self):
        return "\n".join(c["user"] for c in self.calls)

    def system_texts(self):
        return "\n".join(c["system"] for c in self.calls)


@pytest.fixture
def llm(monkeypatch):
    m = MockLLM()
    for name in _LLM_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "call_llm", m, raising=False)
    return m


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    """Redirect WORKSPACE_ROOT to an isolated temp dir for every test."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in _WSROOT_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "WORKSPACE_ROOT", root, raising=False)
    return root


@pytest.fixture(autouse=True)
def learnings_root(tmp_path, monkeypatch):
    """Isolate the cross-run learnings store so tests never touch the real one."""
    root = tmp_path / "learnings"
    monkeypatch.setattr(importlib.import_module("tools.learnings"), "LEARNINGS_ROOT", root, raising=False)
    return root


@pytest.fixture(autouse=True)
def product_root(tmp_path, monkeypatch):
    """Isolate the product profile so tests never read/write the real product/profile.md."""
    mod = importlib.import_module("tools.product")
    root = tmp_path / "product"
    monkeypatch.setattr(mod, "PROFILE_ROOT", root, raising=False)
    monkeypatch.setattr(mod, "PROFILE_PATH", root / "profile.md", raising=False)
    return root


@pytest.fixture
def no_docker(monkeypatch):
    """Stub the linter and Docker test runner so the engineer can run offline."""
    eng = importlib.import_module("agents.engineer")
    monkeypatch.setattr(eng, "run_linter", lambda d: (True, "lint ok"))
    monkeypatch.setattr(eng, "run_project_tests",
                        lambda d, timeout=300, test_path="tests/": (True, "3 passed"))
    # Code-quality layer auto-fixes/reports by shelling out to ruff/mypy — stub it so the
    # agent tests stay hermetic and fast (real behavior covered in test_code_quality.py).
    monkeypatch.setattr(eng, "format_code", lambda d: (True, "formatted"))
    monkeypatch.setattr(eng, "code_quality_report", lambda d, files=None: [])
    monkeypatch.setattr(eng, "check_frontend_quality_tooling", lambda d: [])
    monkeypatch.setattr(eng, "check_dependencies", lambda d, files=None: [])
    return eng


@pytest.fixture(autouse=True)
def text_codegen(monkeypatch):
    """Pin the legacy ===FILE=== text path for the existing agent tests (they mock
    call_llm, which the tools-codegen path doesn't use). Codegen tests opt back in
    with monkeypatch.setenv("AGENT_CODEGEN", "tools") + their own backend mocks."""
    monkeypatch.setenv("AGENT_CODEGEN", "text")


@pytest.fixture(autouse=True)
def no_compose(monkeypatch):
    """Stub the integration compose runner for every test (no real docker compose).
    Tests that exercise the integration node's own behavior re-patch this locally."""
    integ = importlib.import_module("agents.integration")
    monkeypatch.setattr(integ, "run_compose_integration",
                        lambda d, **kw: (True, "integration ok (stubbed)"))
    return integ


def seed(ws_root, project_id, subdir, filename, content) -> str:
    """Write an input artifact and return its ABSOLUTE path (for state[...])."""
    d = ws_root / project_id / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_text(content, encoding="utf-8")
    return str(p)


def base_state(project_id="proj", **overrides) -> dict:
    """A full ProjectState with sensible defaults; override per test."""
    s = {
        "project_id": project_id,
        "feature_request": "Build a user login feature",
        "prd_path": None, "design_path": None, "code_path": None, "deploy_path": None,
        "tests_passed": False, "deployed": False, "pr_url": None, "deploy_url": None,
        "fix_attempts": 0, "error_log": None, "current_node": "start", "change_type": None,
        "qa_log": [], "qa_rounds": {}, "agent_qa_counts": {},
        "ceo_qa_pending": None, "ceo_qa_from": None, "ceo_qa_answer": None,
        "test_path": None, "review_attempts": {}, "review_notes": None, "review_action": None,
        "prd_approved": False, "pr_approved": False,
        "approval_pending": None, "approval_decision": None, "approval_feedback": None,
        "tech_stack": None, "tech_stack_confirmed": False,
        "target_repo": None, "repo_map_path": None, "detected_stack": None, "test_files": [],
        "managed_project": False, "project_ledger": None,
        "security_warnings": [], "code_quality": [], "code_files": [], "product_profile": None,
        "design_mockup_path": None,
    }
    s.update(overrides)
    return s


NEEDS_CEO = '===NEEDS_INPUT===\n{"ceo": "Which auth providers must we support?"}\n===END==='
