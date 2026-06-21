"""
Phase 2.2 — cross-run learning.

The store accumulates generalizable lessons across runs; QA records them on failure;
agents load them into their system prompt so the company stops repeating mistakes.
"""

from conftest import base_state, seed
from tools import learnings
from tools.learnings import load_learnings, record_learning


def test_record_and_load(learnings_root):
    assert record_learning("engineer", "always pin dependency versions") is True
    assert "always pin dependency versions" in load_learnings("engineer")


def test_dedupe_skips_known_lesson(learnings_root):
    record_learning("engineer", "use yaml SafeLoader")
    assert record_learning("engineer", "use yaml SafeLoader") is False
    assert load_learnings("engineer").lower().count("safeloader") == 1


def test_too_short_lessons_rejected(learnings_root):
    assert record_learning("engineer", "no") is False
    assert load_learnings("engineer") == ""


def test_cap_trims_oldest(learnings_root, monkeypatch):
    monkeypatch.setattr(learnings, "MAX_LEARNINGS_CHARS", 60)
    record_learning("engineer", "lesson aaaa the first one here")
    record_learning("engineer", "lesson bbbb the second one here")
    record_learning("engineer", "lesson cccc the third one here")
    text = load_learnings("engineer")
    assert "cccc" in text                 # newest kept
    assert len(text) <= 60                # bounded


def test_qa_failure_records_engineer_lesson(llm, ws, learnings_root):
    from agents import qa
    llm.default = ("ROOT CAUSE: missing test dependency\nFIX: add httpx to requirements\n"
                   "LESSON: always add httpx to requirements when tests use FastAPI TestClient")
    qa.run(base_state(tests_passed=False, error_log="ModuleNotFoundError: No module named 'httpx'"))
    assert "always add httpx" in load_learnings("engineer")


def test_engineer_loads_learnings_into_system_prompt(llm, ws, no_docker, learnings_root):
    from agents import engineer
    record_learning("engineer", "never hardcode secrets in source")
    tech = seed(ws, "proj", "design", "tech_spec.md", "## API\nPOST /login")
    seed(ws, "proj", "tests", "test_login.py", "def test_login():\n    assert True")
    llm.default = "===FILE: src/main.py===\nx = 1\n===END==="
    engineer.run(base_state(design_path=tech))
    assert "never hardcode secrets in source" in llm.system_texts()
