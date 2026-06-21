"""
PR Approval Gate (Phase 1.3)
----------------------------
Blocking CEO sign-off before the PR is opened — the high-stakes ship step.
`interrupt_before=["pr_gate"]` pauses the graph; main.py shows the QA report and
reads the CEO's approve/reject decision.

- approve → pr_approved=True → routing continues to ship (which opens the PR).
- reject  → review_notes = CEO feedback → routing loops back to engineer to revise.

QA sets approval_pending="pr" on a passing run before routing here.
"""

from graph.state import ProjectState


def run(state: ProjectState) -> dict:
    # DEFAULT-DENY: a missing decision means NO human approved — treat as reject.
    # (The old default-approve let a driver bug auto-resume past the interrupt and
    # ship red code with zero sign-off in a live run.)
    decision = (state.get("approval_decision") or "reject").strip().lower()
    feedback = state.get("approval_feedback")

    if decision == "reject":
        from tools.learnings import emit_feedback
        emit_feedback("engineer", "pr_gate_reject", feedback or "")
        return {
            "current_node": "pr_gate",
            "pr_approved": False,
            "approval_pending": None,
            "approval_decision": None,
            "approval_feedback": None,
            # Feed CEO feedback to the engineer as a failure to address, and force
            # a re-test cycle by marking tests not yet passed for this revision.
            "review_notes": feedback or "CEO requested changes before merge.",
            "tests_passed": False,
            "error_log": f"CEO PR review requested changes:\n{feedback or '(no detail)'}",
        }

    return {
        "current_node": "pr_gate",
        "pr_approved": True,
        "approval_pending": None,
        "approval_decision": None,
        "approval_feedback": None,
        "review_notes": None,
    }
