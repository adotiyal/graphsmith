"""
Universal agent self-improvement (CEO mandate 2026-06-13): feedback from ANY
source — gates, critics, integration, guards, vision QA, CEO directives — is
emitted into the run trace; a per-run retro distils per-agent GENERALIZABLE
lessons; every producing agent loads its lessons next run.
"""

import importlib
import json

import pytest

from tests.conftest import base_state, seed
from tools import learnings, trace


@pytest.fixture
def llm_learn(llm, monkeypatch):
    """The llm fixture + patch call_llm inside tools.learnings (retro caller)."""
    monkeypatch.setattr(learnings, "call_llm", llm, raising=False)
    # run_retro imports call_llm from tools.llm at call time — patch there too
    monkeypatch.setattr("tools.llm.call_llm", llm, raising=False)
    return llm


# ── feedback emission at the choke points ─────────────────────────────────────

def test_gates_emit_feedback_on_reject(tmp_path):
    tr = trace.start("t-fb", trace_dir=tmp_path)
    from agents import pr_gate, prd_gate
    pr_gate.run(base_state(approval_decision="reject",
                           approval_feedback="ship blocked: counts wrong"))
    prd_gate.run(base_state(approval_decision="reject",
                            approval_feedback="PRD missed the unhappy path"))
    trace.reset()
    fb = [e for e in tr.events if e["kind"] == "feedback"]
    assert {(e["agent"], e["fb_kind"]) for e in fb} == {
        ("engineer", "pr_gate_reject"), ("pm", "prd_gate_reject")}
    assert "counts wrong" in fb[0]["text"]


def test_integration_failure_emits_feedback(monkeypatch, tmp_path, ws):
    tr = trace.start("t-int", trace_dir=tmp_path)
    from agents import integration as integ
    monkeypatch.setattr(integ, "run_compose_integration",
                        lambda d, **kw: (False, "=== e2e (playwright) — FAILED ===\n3 failed"))
    integ.run(base_state(e2e_files=["e2e/x.py"]))
    trace.reset()
    fb = [e for e in tr.events if e["kind"] == "feedback"]
    assert fb and fb[0]["agent"] == "qa" and fb[0]["fb_kind"] == "integration_e2e"


def test_design_qa_misaligned_emits_feedback_for_design_and_engineer(llm, ws, tmp_path, monkeypatch):
    tr = trace.start("t-dqa", trace_dir=tmp_path)
    from agents import design_qa
    monkeypatch.setattr(design_qa, "render_mockup_screenshot", lambda *a: (True, "ok"),
                        raising=False)
    llm.default = "findings...\n===VERDICT: MISALIGNED==="
    spec = seed(ws, "proj", "design", "design_spec.md", "the spec")
    mock = seed(ws, "proj", "design", "mockup.html", "<html/>")
    shot = seed(ws, "proj", "tests", "app_screenshot.png", "png")
    design_qa.run(base_state(design_spec_path=spec, design_mockup_path=mock,
                             app_screenshot_path=shot))
    trace.reset()
    agents_hit = {e["agent"] for e in tr.events if e["kind"] == "feedback"}
    assert {"design", "engineer"} <= agents_hit


# ── the retro ─────────────────────────────────────────────────────────────────

def _trace_with_feedback(tmp_path):
    tr = trace.start("t-retro", trace_dir=tmp_path)
    learnings.emit_feedback("engineer", "integration_compose",
                            "frontend build failed: fenced kit file")
    learnings.emit_feedback("qa", "e2e_lint_drop", "invented testid stats-total")
    trace.reset()
    return str(tr.path)


def test_gather_feedback_reads_trace(tmp_path):
    path = _trace_with_feedback(tmp_path)
    events = learnings.gather_feedback(path)
    assert {e["agent"] for e in events} == {"engineer", "qa"}


def test_run_retro_records_per_agent_lessons(llm_learn, tmp_path):
    path = _trace_with_feedback(tmp_path)
    llm_learn.default = (
        "engineer: Validate generated files parse before shipping them to a build.\n"
        "qa: Only use selectors that exist in the design kit manifest.\n"
        "narrator: ignore me — not an agent\n")
    state = base_state(qa_log=[{"from": "devops", "to": "ceo",
                                "question": "Deploy target?",
                                "answer": "Local docker only, never cloud."}])
    out = learnings.run_retro(path, state)
    assert "engineer" in out and "qa" in out
    assert "narrator" not in out
    assert "parse" in learnings.load_learnings("engineer").lower()
    assert "manifest" in learnings.load_learnings("qa").lower()
    # the retro prompt carried BOTH trace feedback and the CEO directive
    prompt = llm_learn.calls[-1]["user"]
    assert "fenced kit file" in prompt and "Local docker only" in prompt


def test_run_retro_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr("tools.llm.call_llm", boom)
    assert learnings.run_retro("/nonexistent/trace.jsonl",
                               base_state(qa_log=[{"to": "ceo", "answer": "x",
                                                   "question": "y", "from": "pm"}])) == {}


def test_retro_caps_two_lessons_per_agent(llm_learn, tmp_path):
    path = _trace_with_feedback(tmp_path)
    llm_learn.default = ("qa: lesson one about selectors.\n"
                        "qa: lesson two about isolation.\n"
                        "qa: lesson three must be dropped.\n")
    out = learnings.run_retro(path, base_state())
    assert len(out["qa"]) == 2


# ── every producing agent loads its lessons ───────────────────────────────────

def test_all_producing_agents_load_learnings(llm, ws):
    from agents import pm
    for agent in ("pm", "design", "architect", "test_author", "engineer", "qa", "devops"):
        learnings.record_learning(agent, f"unique-lesson-for-{agent} alpha bravo")
    prd = seed(ws, "proj", "prd", "ceo_brief.md", "story")
    llm.default = "## PRD\nok"
    pm.run(base_state(prd_path=prd))
    assert f"unique-lesson-for-pm" in llm.calls[0]["system"]
    assert "Learnings from past runs" in llm.calls[0]["system"]
