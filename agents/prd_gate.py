"""
PRD Approval Gate (Phase 1.3)
-----------------------------
Blocking CEO sign-off on the PRD before any design/engineering happens.
`interrupt_before=["prd_gate"]` pauses the graph; main.py shows the PRD and reads
the CEO's approve/reject decision, injecting it via update_state.

- approve → prd_approved=True → routing continues to design.
- reject  → review_notes = CEO feedback → routing loops back to pm to regenerate.

The asking side (pm) sets approval_pending="prd" before routing here, mirroring the
ceo_qa pattern. This node just records the decision and clears the interrupt state.
"""

from graph.state import ProjectState


def run(state: ProjectState) -> dict:
    # DEFAULT-DENY: a missing decision means NO human approved — treat as reject
    # (mirrors pr_gate; a gate must never approve on a driver's silent resume).
    decision = (state.get("approval_decision") or "reject").strip().lower()
    feedback = state.get("approval_feedback")

    if decision == "reject":
        from tools.learnings import emit_feedback
        emit_feedback("pm", "prd_gate_reject", feedback or "")
        return {
            "current_node": "prd_gate",
            "prd_approved": False,
            "approval_pending": None,
            "approval_decision": None,
            "approval_feedback": None,
            "review_notes": feedback or "CEO requested changes to the PRD.",
        }

    return {
        "current_node": "prd_gate",
        "prd_approved": True,
        "approval_pending": None,
        "approval_decision": None,
        "approval_feedback": None,
        "review_notes": None,
    }
