"""
CEO Agent
---------
DESIGN DECISION: This node is where interrupt_before fires.
LangGraph pauses BEFORE this node runs. You type your requirement
into main.py, it gets injected into state["feature_request"], then
this node simply formats it into a clean brief for PM.

Why not just pass raw input to PM? Because raw CEO language ("make auth work")
is ambiguous. CEO agent's job is to add: scope, constraints, priority.
That's a light Opus (decision-tier) call but saves PM from hallucinating scope.
"""

from graph.state import ProjectState
from tools.llm import call_llm
from tools.file_io import load_prompt, load_skill, write_artifact


def run(state: ProjectState) -> dict:
    identity = load_prompt("ceo")
    skill = load_skill("ceo")     # establishes the human as CEO *and* CTO
    system = f"{identity}\n\n{skill}" if skill else identity

    user_msg = f"""
Feature request from the CEO/CTO:
{state["feature_request"]}

Project ID: {state["project_id"]}

Produce a concise brief (max 300 words) covering:
1. What we are building (one sentence)
2. Who it is for
3. What is explicitly OUT of scope for this iteration
4. Success criteria (2-3 bullet points)
5. Any technical intent or constraints the CEO/CTO stated (or "None")
"""

    brief = call_llm(system, user_msg, tier="fast")

    path = write_artifact(state["project_id"], "prd", "ceo_brief.md", brief)

    return {
        "current_node": "ceo",
        "prd_path": path,   # PM will read this path, not re-read feature_request
    }
