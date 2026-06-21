"""
PM Agent
--------
DESIGN DECISION: PM reads only the CEO brief file, not feature_request.
- PRD format is intentionally minimal: user stories + acceptance criteria only.

Q&A: PM can ask CEO for scope/priority. Clarification is folded into the work call.

Phase 1.3: on a successful PRD, PM sets approval_pending="prd" so the graph routes
to the PRD approval gate. If the CEO rejected a prior PRD, review_notes carries the
feedback and PM regenerates directly (no fresh Q&A round).
"""

from graph.state import ProjectState
from tools.file_io import load_prompt, read_artifact, write_artifact
from tools.qa_utils import run_with_qa, work_call, format_qa_context

CONSULT = ["ceo"]


def run(state: ProjectState) -> dict:
    # Regenerating after a CEO rejection: skip fresh Q&A, apply the feedback.
    if state.get("review_notes"):
        return _do_work(
            state,
            list(state.get("qa_log") or []),
            dict(state.get("qa_rounds") or {}),
            allow_clarify=False,
        )
    return run_with_qa(state, "pm", _do_work, consultable_agents=CONSULT)


def _do_work(state: dict, qa_log: list, rounds: dict, allow_clarify: bool = True) -> dict:
    from tools.learnings import augment_system
    system = augment_system(load_prompt("pm"), "pm")
    brief = read_artifact(state["prd_path"])
    qa_ctx = format_qa_context(qa_log, "pm")
    profile = state.get("product_profile")
    profile_block = f"\n\nPRODUCT PROFILE (standing context — users, use cases, goals):\n{profile}" if profile else ""
    ledger = state.get("project_ledger")
    ledger_block = f"\n\nPROJECT HISTORY (features already built — scope this feature consistently and don't re-propose what exists):\n{ledger}" if ledger else ""

    feedback = state.get("review_notes")
    feedback_block = f"\n\nCEO FEEDBACK ON THE PREVIOUS PRD (address this):\n{feedback}" if feedback else ""

    user_msg = f"""
CEO Brief:
{brief}{profile_block}{ledger_block}

{qa_ctx}{feedback_block}

Write a PRD with ONLY these sections (keep it tight):

## Feature
One paragraph summary.

## Success Metric
One measurable signal that confirms this feature worked (e.g. "% of new users who
complete first action within 24h increases", "p50 latency on /api/items stays <200ms",
"task completion rate for X flow"). If genuinely unknown, escalate — don't write "TBD."

## User Stories
As a [user], I want [action] so that [outcome].
List max 5 stories.

## Acceptance Criteria
The CONTRACT every downstream agent (design, test author, QA, engineer) references and
that an automated coverage gate enforces — so each line must be ONE concrete, binary,
INDEPENDENTLY TESTABLE behavior with a STABLE ID and a SURFACE tag. EXACTLY:
  - AC-1 (ui): <one thing the USER can observe/do, phrased as a checkable outcome>
  - AC-2 (backend): <one server/data behavior: storage, calculation, validation, API result>
Rules (a noisy contract creates false test-coverage work downstream):
- Each AC is a SINGLE verifiable behavior — never a goal ("provide visibility"), a tech
  choice ("use PostgreSQL/Express"), or a meta-line ("all criteria are met").
- `(ui)` iff verifying it requires looking at a screen / clicking; else `(backend)`.
- Number AC-1, AC-2, … with no gaps. Every AC must be testable as written.
- If you find yourself needing more than 9 ACs, the feature is too large — cut scope
  or escalate to CEO rather than writing a sprawling contract.

## Out of Scope
What we are NOT building. Copied and confirmed from CEO brief.

## Open Questions
Any ambiguities that need CEO input before engineering starts.
If none, write "None."
"""

    questions, raw = work_call(system, user_msg, "fast", CONSULT, allow_clarify)
    if questions:
        return {"_clarify": questions}

    path = write_artifact(state["project_id"], "prd", "prd.md", raw)

    return {
        "current_node": "pm",
        "prd_path": path,
        "approval_pending": "prd",   # route to PRD approval gate
        "review_notes": None,        # consumed
        "qa_log": qa_log,
        "qa_rounds": rounds,
        "ceo_qa_from": None,
    }
