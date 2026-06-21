"""
graph.py is the ONLY place edges are defined. Agents are nodes; this file is the
wiring. No agent knows what comes before or after it.

CURRENT PIPELINE (Phase 1 + 2.1 + triage):

  ceo → triage ─(feature)→ pm → [prd_gate] → surveyor → design → critic_design → architect → critic_architect → test_author
       └─(bugfix/refactor/chore → QUICK lane)────────────────────────────────────────────────────────────────► engineer
                          (no-op in greenfield;       ⇅ retry / escalate
                           maps the repo in extend mode)
        ↑        │                      ↑   │ (retry)                  │
        └────────┘ (reject)             └───┘                          ▼
                                    (escalate→ceo_qa→test_author)   engineer ⇄ qa
                                                                        │
                                                  ┌─────────────────────┤
                                                  ▼ (pass)              ▼ (fail, <max) → engineer
                                              [pr_gate] ──(approve)──► ship → devops → END
                                                  │ (reject)
                                                  └──► engineer

  - [prd_gate] / [pr_gate]: blocking CEO approval interrupts (Phase 1.3).
  - critic_architect: bounded review loop; escalates to CEO if still failing (1.2).
  - test_author: writes the authoritative tests BEFORE the engineer (1.1, TDD).
  - integration (4.2/4.3): after QA passes, brings the app's own docker-compose stack
    UP, smoke-checks it, and runs QA's Playwright e2e specs against it. Failure loops
    to the engineer (bounded by MAX_INTEGRATION_ATTEMPTS, then proceeds to the gate
    with the red report visible). Deterministic node — no LLM, never escalates.
  - Any agent may also pause at the shared ceo_qa node for clarification questions.

ADDING A CRITIC FOR ANOTHER STAGE:
  1. Add the stage to STAGE_CONFIG in agents/critic.py
  2. add_node("critic_<stage>", lambda s: critic.run(s, stage="<stage>"))
  3. point the producing agent's edge at critic_<stage>, and add a routing fn
  4. add "<stage>_critic" → <next node> in ceo_qa_return_routing
"""

import sqlite3

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from graph.state import ProjectState
from agents import (
    ceo, triage, pm, prd_gate, surveyor, design, architect, critic, test_author,
    engineer, qa, integration, design_qa, pr_gate, ship, devops, ceo_qa,
)
from agents.integration import MAX_INTEGRATION_ATTEMPTS
from tools import trace
from agents.design_qa import MAX_DESIGN_QA_ATTEMPTS

MAX_FIX_ATTEMPTS = 3


# --- Routing helpers ---

def _needs_ceo_qa(agent_name: str, next_node: str):
    """Route an agent to ceo_qa if it raised CEO questions, else to next_node."""
    def route(state: ProjectState) -> str:
        if state.get("ceo_qa_pending") and state.get("ceo_qa_from") == agent_name:
            return "ceo_qa"
        return next_node
    route.__name__ = f"{agent_name}_routing"
    return route


def triage_routing(state: ProjectState) -> str:
    # Net-new features get the full pipeline; bugfix/refactor/chore take the quick lane.
    return "pm" if (state.get("change_type") or "feature") == "feature" else "engineer"


def pm_routing(state: ProjectState) -> str:
    if state.get("ceo_qa_pending") and state.get("ceo_qa_from") == "pm":
        return "ceo_qa"
    return "prd_gate"   # PRD produced → CEO approval gate


def prd_gate_routing(state: ProjectState) -> str:
    # After approval, survey the codebase (no-op in greenfield) before design.
    return "surveyor" if state.get("prd_approved") else "pm"


def critic_design_routing(state: ProjectState) -> str:
    action = state.get("review_action")
    if action == "retry":
        return "design"
    if action == "escalate":
        return "ceo_qa"
    return "architect"


def critic_architect_routing(state: ProjectState) -> str:
    action = state.get("review_action")
    if action == "retry":
        return "architect"
    if action == "escalate":
        return "ceo_qa"
    return "test_author"


def qa_routing(state: ProjectState) -> str:
    if state.get("ceo_qa_pending") and state.get("ceo_qa_from") == "qa":
        return "ceo_qa"
    if state["tests_passed"]:
        return "integration"                      # passed → prove the app actually RUNS
    if state["fix_attempts"] >= MAX_FIX_ATTEMPTS:
        # Give up on tests: feature lane → DevOps dry-run; quick lane → let the CEO/CTO
        # decide on the imperfect diff at the PR gate.
        return "devops" if (state.get("change_type") or "feature") == "feature" else "pr_gate"
    return "engineer"


def integration_routing(state: ProjectState) -> str:
    # The composed stack ran + smoke + e2e green → design QA (does it LOOK right?).
    # I4(e): an e2e-stage failure on a healthy app routes ONE bounded revision round
    # to QA (the spec author) — most live e2e reds were spec mechanics, not app bugs.
    # Other failures loop the engineer (bounded); at the cap, proceed to the gate
    # anyway — the CEO/CTO sees the red integration report and decides.
    if state.get("integration_passed"):
        return "design_qa"
    if state.get("e2e_revision_pending"):
        return "qa"
    if state.get("integration_attempts", 0) >= MAX_INTEGRATION_ATTEMPTS:
        return "pr_gate"
    return "engineer"


def design_qa_routing(state: ProjectState) -> str:
    # The app matches the design → human gate. Misaligned → loop the engineer with the
    # vision findings (bounded); at the cap, the gate shows the red design report.
    if state.get("design_qa_passed"):
        return "pr_gate"
    if state.get("design_qa_attempts", 0) >= MAX_DESIGN_QA_ATTEMPTS:
        return "pr_gate"
    return "engineer"


def pr_gate_routing(state: ProjectState) -> str:
    return "ship" if state.get("pr_approved") else "engineer"


def ship_routing(state: ProjectState) -> str:
    # DevOps (regenerate IaC) only matters for net-new features; quick lane ends after ship.
    return "devops" if (state.get("change_type") or "feature") == "feature" else "end"


def devops_routing(state: ProjectState) -> str:
    # DevOps can escalate to the CEO too (no agent is ever blocked); otherwise END.
    if state.get("ceo_qa_pending") and state.get("ceo_qa_from") == "devops":
        return "ceo_qa"
    return "end"


def ceo_qa_return_routing(state: ProjectState) -> str:
    """After the CEO answers, return to whoever asked."""
    mapping = {
        "pm": "pm", "design": "design", "architect": "architect",
        "surveyor": "surveyor",
        "test_author": "test_author", "engineer": "engineer", "qa": "qa",
        "devops": "devops",
        "design_critic": "architect",        # critic escalation proceeds forward
        "architect_critic": "test_author",   # critic escalation proceeds forward
    }
    return mapping.get(state.get("ceo_qa_from"), "pm")


def build_graph(db_path: str = "checkpoints.db"):
    # NOTE: SqliteSaver.from_conn_string() returns a context manager in current
    # langgraph, not a saver — using it directly breaks compile(). We instead own a
    # long-lived connection so the checkpointer outlives build_graph(). check_same_thread
    # is False because langgraph may touch the connection from worker threads.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    builder = StateGraph(ProjectState)

    # --- Nodes ---
    # Every node is wrapped in trace.traced(): per-node wall time (node_exec events)
    # + attribution of llm_call/codegen events to the executing node (run statistics).
    _n = trace.traced
    builder.add_node("ceo",              _n("ceo", ceo.run))
    builder.add_node("triage",           _n("triage", triage.run))
    builder.add_node("pm",               _n("pm", pm.run))
    builder.add_node("prd_gate",         _n("prd_gate", prd_gate.run))
    builder.add_node("surveyor",         _n("surveyor", surveyor.run))
    builder.add_node("design",           _n("design", design.run))
    builder.add_node("critic_design",    _n("critic_design", lambda s: critic.run(s, stage="design")))
    builder.add_node("architect",        _n("architect", architect.run))
    builder.add_node("critic_architect", _n("critic_architect", lambda s: critic.run(s, stage="architect")))
    builder.add_node("test_author",      _n("test_author", test_author.run))
    builder.add_node("engineer",         _n("engineer", engineer.run))
    builder.add_node("qa",               _n("qa", qa.run))
    builder.add_node("integration",      _n("integration", integration.run))
    builder.add_node("design_qa",        _n("design_qa", design_qa.run))
    builder.add_node("pr_gate",          _n("pr_gate", pr_gate.run))
    builder.add_node("ship",             _n("ship", ship.run))
    builder.add_node("devops",           _n("devops", devops.run))
    builder.add_node("ceo_qa",           _n("ceo_qa", ceo_qa.run))

    # --- Edges ---
    builder.set_entry_point("ceo")
    builder.add_edge("ceo", "triage")
    builder.add_conditional_edges("triage", triage_routing,
                                  {"pm": "pm", "engineer": "engineer"})

    builder.add_conditional_edges("pm", pm_routing,
                                  {"ceo_qa": "ceo_qa", "prd_gate": "prd_gate"})
    builder.add_conditional_edges("prd_gate", prd_gate_routing,
                                  {"surveyor": "surveyor", "pm": "pm"})
    builder.add_conditional_edges("surveyor", _needs_ceo_qa("surveyor", "design"),
                                  {"ceo_qa": "ceo_qa", "design": "design"})
    builder.add_conditional_edges("design", _needs_ceo_qa("design", "critic_design"),
                                  {"ceo_qa": "ceo_qa", "critic_design": "critic_design"})
    builder.add_conditional_edges("critic_design", critic_design_routing,
                                  {"design": "design", "ceo_qa": "ceo_qa", "architect": "architect"})
    builder.add_conditional_edges("architect", _needs_ceo_qa("architect", "critic_architect"),
                                  {"ceo_qa": "ceo_qa", "critic_architect": "critic_architect"})
    builder.add_conditional_edges("critic_architect", critic_architect_routing,
                                  {"architect": "architect", "ceo_qa": "ceo_qa", "test_author": "test_author"})
    builder.add_conditional_edges("test_author", _needs_ceo_qa("test_author", "engineer"),
                                  {"ceo_qa": "ceo_qa", "engineer": "engineer"})
    builder.add_conditional_edges("engineer", _needs_ceo_qa("engineer", "qa"),
                                  {"ceo_qa": "ceo_qa", "qa": "qa"})
    builder.add_conditional_edges("qa", qa_routing,
                                  {"ceo_qa": "ceo_qa", "engineer": "engineer",
                                   "integration": "integration", "devops": "devops",
                                   "pr_gate": "pr_gate"})
    builder.add_conditional_edges("integration", integration_routing,
                                  {"design_qa": "design_qa", "pr_gate": "pr_gate",
                                   "engineer": "engineer",
                                   # I4(e): e2e-stage failure on a healthy app → ONE
                                   # bounded spec-revision round at QA
                                   "qa": "qa"})
    builder.add_conditional_edges("design_qa", design_qa_routing,
                                  {"pr_gate": "pr_gate", "engineer": "engineer"})
    builder.add_conditional_edges("pr_gate", pr_gate_routing,
                                  {"ship": "ship", "engineer": "engineer"})
    builder.add_conditional_edges("ship", ship_routing, {"devops": "devops", "end": END})
    builder.add_conditional_edges("devops", devops_routing, {"ceo_qa": "ceo_qa", "end": END})

    builder.add_conditional_edges(
        "ceo_qa",
        ceo_qa_return_routing,
        {"pm": "pm", "surveyor": "surveyor", "design": "design", "architect": "architect",
         "test_author": "test_author", "engineer": "engineer", "qa": "qa",
         "devops": "devops"},
    )

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["ceo", "ceo_qa", "prd_gate", "pr_gate"],
    )
