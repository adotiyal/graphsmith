"""
Critic Agent (Phase 1.2)
------------------------
Turns the waterfall into a bounded quality loop. After a spec is produced, the
Critic judges it against the requirements it must satisfy.

Behavior (chosen): retry, then escalate to CEO.
- verdict "pass"     → proceed downstream.
- verdict "fail" and attempts < MAX_REVIEW_ATTEMPTS → send back to regenerate
  with the gaps in review_notes (bounded loop).
- verdict "fail" and attempts exhausted → escalate the unresolved gap to the CEO
  via the shared ceo_qa interrupt, then proceed forward (the CEO is informed and
  the pipeline does not stall).

DESIGN DECISION: generic and parameterized by `stage`. Currently wired for the
Architect's technical spec — the highest-risk artifact, which gates all code.
Adding a critic for design or PRD is a few lines here + one wiring block in graph.py.

DESIGN DECISION: the Critic runs on the `reason` tier (Opus). Catching a spec
error here is far cheaper than discovering it in generated code.
"""

from graph.state import ProjectState
from tools.llm import call_structured
from tools.file_io import load_prompt, load_skill, read_artifact

MAX_REVIEW_ATTEMPTS = 2

# §4.1: the verdict is a VALIDATED structured decision, not a regex over prose. A malformed
# reply now triggers a corrective retry; only after that does it fall back to the safe
# default (pass = fail-open, never block the pipeline on a parse error). Before, the FIRST
# malformed JSON silently fell open to "pass" — a bad spec could sail through unreviewed.
_VERDICT_SCHEMA = {
    "verdict": {"type": "enum", "values": ["pass", "fail"], "required": True},
    "gaps": {"type": "string", "required": False},
}

# stage → how to review it. upstream_keys are state keys read as the requirements.
STAGE_CONFIG = {
    "design": {
        "artifact_key": "design_path",   # design_spec.md (architect hasn't overwritten yet)
        "upstream_keys": ["prd_path"],
        "artifact_label": "Design Spec",
        "upstream_label": "PRD",
        "review_focus": (
            "Review the design spec across these six dimensions. Be specific — cite the exact "
            "screen or flow with the gap, not a generic observation.\n\n"

            "1. PURPOSE CLARITY: Does the Design Context section name a real user, their "
            "job-to-be-done, and the success metric? Is the feature's intent obvious from the "
            "flows, or does it require guessing?\n\n"

            "2. USABILITY & FLOWS: Can the user accomplish their goal with the specified steps? "
            "Are there unnecessary steps? Is the FIRST-RUN/empty experience explicitly designed "
            "(never an unexplained empty screen)? Are ALL unhappy paths present: error, empty, "
            "loading, permission-denied, offline/partial? For each error state, is a recovery "
            "action specified (not just a red message)?\n\n"

            "3. VISUAL HIERARCHY & STATES: Is there exactly one primary action per screen? "
            "Are all 4 states (loading / success / error / empty) designed where relevant? "
            "For loading states, is the skeleton structure described (not just 'show spinner')? "
            "Are components drawn from the stated library only — flag any undeclared component.\n\n"

            "4. CONSISTENCY: Does the spec follow the persisted design system (tokens, spacing, "
            "patterns, voice)? Do similar interactions work the same way across screens? "
            "Are any new tokens introduced that contradict the established system?\n\n"

            "5. ACCESSIBILITY (WCAG 2.1 AA): Are color contrast ratios specified (≥4.5:1 normal "
            "text, ≥3:1 large/UI)? Are touch targets ≥44×44px? Are errors tied to their fields "
            "(aria-describedby), not just shown at the top? Is color the ONLY status signal "
            "anywhere (if so, flag it)? Is keyboard navigation addressed for interactive elements?\n\n"

            "6. COPY QUALITY: Do all CTAs start with a verb and describe the outcome "
            "('Save changes', not 'Submit')? Do error messages follow What + Why + How-to-fix "
            "('Payment declined. Your card was declined by your bank. Try a different card.')? "
            "Do empty states follow What-this-is + Why-it's-empty + How-to-start? "
            "Do confirmation dialogs describe consequences and label buttons with the action "
            "('Delete 3 files' / 'Keep files', never 'OK' / 'Cancel')? "
            "Are all data fields mapped to API models, with no placeholder copy?\n\n"

            "Also verify: dual-surface (375px mobile AND 1280px desktop) differences are called "
            "out per screen; dual-theme (light + dark token pairs) is specified.\n\n"

            "NOTE: the spec INTENTIONALLY proposes three alternative directions under "
            "'## Design Directions' — only the one named in '## Chosen Direction' (picked by "
            "the human CEO/CTO) is binding. Review under the chosen direction only; never flag "
            "the two unchosen directions as gaps."
        ),
    },
    "architect": {
        "artifact_key": "design_path",   # tech spec (architect overwrote design_path)
        "upstream_keys": ["prd_path"],   # must satisfy the PRD
        "artifact_label": "Technical Spec",
        "upstream_label": "PRD",
        "review_focus": (
            "Does the spec cover every user story / acceptance criterion with data models and "
            "endpoints? Are API contracts complete (methods, request/response, status codes), data "
            "models sufficient, the test strategy concrete, and security/ordering addressed? Flag "
            "any requirement with no corresponding technical element."
        ),
    },
}


def run(state: ProjectState, stage: str = "architect") -> dict:
    cfg = STAGE_CONFIG[stage]
    attempts = dict(state.get("review_attempts") or {})
    used = attempts.get(stage, 0)

    artifact = read_artifact(state[cfg["artifact_key"]])
    upstream = "\n\n".join(read_artifact(state[k]) for k in cfg["upstream_keys"] if state.get(k))

    identity = load_prompt("critic")
    skill = load_skill("critic")
    system = f"{identity}\n\n{skill}" if skill else identity

    user_msg = f"""
{cfg['upstream_label']} (the requirements that must be satisfied):
{upstream}

{cfg['artifact_label']} (the artifact under review):
{artifact}

Review focus:
{cfg.get('review_focus', '')}

Judge whether the {cfg['artifact_label']} fully and correctly satisfies the
{cfg['upstream_label']}. `gaps` = numbered specific gaps (null when the verdict is pass).
"""

    data = call_structured(system, user_msg, _VERDICT_SCHEMA, tier="reason",
                           default={"verdict": "pass", "gaps": None})
    verdict, gaps = _verdict_and_gaps(data)
    node = f"critic_{stage}"

    if verdict == "pass":
        return {"current_node": node, "review_action": "pass", "review_attempts": attempts}

    if used < MAX_REVIEW_ATTEMPTS:
        attempts[stage] = used + 1
        from tools.learnings import emit_feedback
        emit_feedback(stage, f"critic_{stage}_retry", gaps or "")
        return {
            "current_node": node,
            "review_action": "retry",
            "review_notes": gaps or "The spec does not fully satisfy the requirements.",
            "review_attempts": attempts,
        }

    # Exhausted retries → escalate to CEO, then proceed forward.
    qa_log = list(state.get("qa_log") or [])
    question = (
        f"The {stage} spec still has unresolved gaps after {used} revision(s):\n"
        f"{gaps}\n\nHow should we proceed? (Your guidance is recorded; the pipeline will continue.)"
    )
    qa_log.append({"from": f"{stage}_critic", "to": "ceo", "question": question})
    return {
        "current_node": node,
        "review_action": "escalate",
        "review_attempts": attempts,
        "qa_log": qa_log,
        "ceo_qa_pending": question,
        "ceo_qa_from": f"{stage}_critic",
    }


def _verdict_and_gaps(data: dict):
    """Map the VALIDATED structured decision (from call_structured) to (verdict, gaps),
    normalizing a literal "null"/"none"/empty gaps string to None. Verdict is already a
    canonical "pass"/"fail" enum; default to "pass" (fail-open) if somehow absent."""
    verdict = (data.get("verdict") or "pass").strip().lower()
    gaps = data.get("gaps")
    if gaps and str(gaps).strip().lower() in ("null", "none", ""):
        gaps = None
    return ("fail" if verdict == "fail" else "pass"), gaps
