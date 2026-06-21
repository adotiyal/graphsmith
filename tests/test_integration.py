"""
SET 1 (end-to-end) — full pipeline integration.

Drives the REAL compiled LangGraph from CEO input to END with the LLM and Docker
mocked, exercising the gates, the critic, the checkpointer, and the interrupt/resume
loop exactly as main.py does. Proves the architecture runs end-to-end and that a
CEO-reject loop actually loops.
"""

import importlib
import pytest
from conftest import base_state


def _router(system, user, tier):
    """Return a plausible artifact for whichever agent is calling, by its prompt."""
    if "Classify this change request" in user:  return '{"change_type":"feature"}'
    if "concise brief" in user:                 return "BRIEF: build login. Scope: email+password."
    if "Write a PRD" in user:                   return "## Acceptance Criteria\n1. User can log in"
    if "UI/UX" in user:                         return "## Design Context\nFor end users.\n## Screens & Components\nLogin — Button, Input"
    if "technical spec with ONLY" in user:      return "## Stack\nFastAPI\n## API Endpoints\nPOST /login → 200"
    if "INTEGRATION BRIEF" in user:             return "## Stack & Conventions\nFastAPI in app/main.py\n## Where The Feature Plugs In\napp/auth.py"
    if "Judge whether" in user:                 return '{"verdict":"pass","gaps":null}'
    if "authoritative pytest suite" in user:    return "===FILE: tests/test_login.py===\ndef test_login():\n    assert True\n===END==="
    if "Generate the code" in user:             return "===FILE: src/main.py===\nfrom fastapi import FastAPI\napp=FastAPI()\n===END===\n===FILE: requirements.txt===\nfastapi\n===END==="
    if "QA sign-off report" in user:            return "QA sign-off: GO. All criteria verified."
    if "deployment configuration files" in user:return "===FILE: Dockerfile===\nFROM python:3.11\n===END==="
    return "MOCK"


def _drive(graph, config, max_interrupts=30):
    """Headless version of main.py's resume loop. Returns the count of interrupts handled."""
    handled = 0
    while True:
        list(graph.stream(None, config))           # run to next interrupt or END
        snap = graph.get_state(config)
        if not snap.next:
            return handled
        v = snap.values
        if v.get("ceo_qa_pending"):
            graph.update_state(config, {"ceo_qa_answer": "proceed"})
        elif v.get("approval_pending"):
            graph.update_state(config, {"approval_decision": "approve", "approval_feedback": None})
        handled += 1
        if handled > max_interrupts:
            raise AssertionError("pipeline did not terminate")


@pytest.fixture
def wired(llm, no_docker, tmp_path, monkeypatch):
    llm.router = _router
    # Stay out of real git/gh in the ship node.
    monkeypatch.setattr(importlib.import_module("agents.ship"), "_open_pr",
                        lambda project_dir, pid: "https://github.com/acme/repo/pull/1")
    monkeypatch.chdir(tmp_path)                     # so inter-agent relative paths resolve
    return llm


def test_full_pipeline_reaches_end_and_produces_artifacts(wired, tmp_path):
    from graph.graph import build_graph
    g = build_graph(":memory:")
    config = {"configurable": {"thread_id": "happy"}}

    g.invoke(base_state("happy", feature_request="let users log in"), config)
    _drive(g, config)

    final = g.get_state(config).values
    assert final["prd_approved"] is True
    assert final["pr_approved"] is True
    assert final["tech_stack_confirmed"] is True       # CTO finalized the stack
    assert final["tests_passed"] is True
    assert final["pr_url"].startswith("https://github.com")
    assert final["deploy_path"]
    ws = tmp_path / "workspace" / "happy"
    for rel in ["prd/prd.md", "design/tech_spec.md", "tests/test_login.py",
                "src/main.py", "tests/qa_report.md"]:
        assert (ws / rel).exists(), f"missing {rel}"


def test_extend_mode_surveys_repo_and_uses_detected_stack(wired, tmp_path):
    """In extend mode the Surveyor runs, maps the repo, and the architect proposes the
    detected stack — the full graph still reaches END."""
    from graph.graph import build_graph
    # A tiny existing FastAPI repo to extend.
    repo_dir = tmp_path / "existing_app"
    (repo_dir / "app").mkdir(parents=True)
    (repo_dir / "requirements.txt").write_text("fastapi\n")
    (repo_dir / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")

    g = build_graph(":memory:")
    config = {"configurable": {"thread_id": "extend"}}
    g.invoke(base_state("extend", feature_request="add logout", target_repo=str(repo_dir)), config)
    _drive(g, config)

    final = g.get_state(config).values
    assert final["repo_map_path"]                       # surveyor produced a repo map
    assert "Python" in (final["detected_stack"] or "")  # detected the existing stack
    assert final["tech_stack_confirmed"] is True
    assert final["pr_approved"] is True                 # reached the end of the pipeline
    # Slice 2: code + tests were written back INTO the existing repo, not workspace/.
    assert (repo_dir / "src" / "main.py").exists()      # engineer wrote into the repo
    assert (repo_dir / "tests" / "test_login.py").exists()  # test author wrote into the repo
    assert final["test_files"]                          # recorded for protection


def test_quick_lane_bugfix_skips_full_pipeline(wired, ws):
    """A bugfix is triaged to the quick lane: straight to Engineer → QA → PR gate → Ship → END,
    skipping PM/PRD-gate/Design/Architect/Test-author and DevOps."""
    from graph.graph import build_graph

    def quick_router(system, user, tier):
        if "Classify this change request" in user:  return '{"change_type":"bugfix"}'
        if "Generate the code" in user:             return "===FILE: src/fix.py===\nVALUE = 2\n===END==="
        if "QA sign-off report" in user:            return "QA: GO — fix looks correct."
        return "MOCK"
    wired.router = quick_router

    g = build_graph(":memory:")
    config = {"configurable": {"thread_id": "quick"}}
    g.invoke(base_state("quick", feature_request="fix off-by-one in pagination"), config)
    _drive(g, config)

    final = g.get_state(config).values
    assert final["change_type"] == "bugfix"
    assert final["pr_approved"] is True                          # reached the PR gate + ship
    assert final.get("deploy_path") is None                     # DevOps skipped in quick lane
    # Full-lane artifacts were never produced:
    assert not (ws / "quick" / "prd" / "prd.md").exists()
    assert not (ws / "quick" / "design" / "tech_spec.md").exists()
    assert (ws / "quick" / "src" / "fix.py").exists()           # the engineer's change


def test_prd_reject_loops_back_to_pm(wired):
    """First PRD is rejected with feedback; second is approved. Pipeline still finishes."""
    from graph.graph import build_graph
    g = build_graph(":memory:")
    config = {"configurable": {"thread_id": "reject"}}
    g.invoke(base_state("reject", feature_request="login"), config)

    state = {"rejected_once": False, "pm_runs": 0}

    while True:
        for event in g.stream(None, config):
            for node, _ in event.items():
                if node == "pm":
                    state["pm_runs"] += 1
        snap = g.get_state(config)
        if not snap.next:
            break
        v = snap.values
        if v.get("approval_pending") == "prd" and not state["rejected_once"]:
            state["rejected_once"] = True
            g.update_state(config, {"approval_decision": "reject", "approval_feedback": "add SSO"})
        elif v.get("ceo_qa_pending"):
            g.update_state(config, {"ceo_qa_answer": "ok"})
        else:
            g.update_state(config, {"approval_decision": "approve", "approval_feedback": None})

    assert state["pm_runs"] >= 2                    # PM ran again after the rejection
    assert g.get_state(config).values["pr_approved"] is True
