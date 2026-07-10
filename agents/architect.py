"""
Architect Agent
---------------
DESIGN DECISION: Architect proposes the technical plan, but the TECH STACK is a
CTO decision — it is finalized by the human CEO/CTO, not silently hardcoded.
- Output is a TECHNICAL SPEC. Engineer reads this file, not the PRD.
  Highest-risk node: if the spec is wrong, everything downstream is wrong.

PHASE 0 (0.3): runs on the "reason" tier (Opus) — highest-leverage reasoning.

STACK CONFIRMATION (resolves audit #2): on the first architecture pass the architect
proposes a default stack (FastAPI + Next.js + Postgres) and MUST get the CEO/CTO to
confirm or change it before committing the spec. The decision is recorded in
state["tech_stack"] / state["tech_stack_confirmed"] so it sticks across critic retries.

Q&A: Architect can ask CEO/CTO (scaling/infra/stack), PM (scope), or Design (UI data).
"""

from graph.state import ProjectState
from tools.file_io import load_prompt, load_skill, read_artifact, write_artifact
from tools.registry import validate_api_spec
from tools.learnings import augment_system
from tools import product
from tools.qa_utils import run_with_qa, work_call, format_qa_context, product_invariants_block

CONSULT = ["ceo", "pm", "design"]

DEFAULT_STACK = (
    "Dockerized full stack — FastAPI (backend, Python) + Next.js (frontend, TypeScript) "
    "+ Postgres (database), orchestrated with docker-compose. Pinned slim/alpine base "
    "images (python:3.12-slim, node:22-alpine, postgres:17-alpine), multi-stage builds. "
    "Tests per layer: pytest (backend), vitest (frontend), Playwright (e2e user-flow)."
)

# Affirmative replies that mean "use the proposed default".
_AFFIRMATIVE = {"", "confirm", "confirmed", "yes", "y", "ok", "okay", "proceed",
                "default", "go ahead", "approve", "approved", "sounds good", "lgtm"}


def run(state: ProjectState) -> dict:
    # The tech stack is a CTO decision: confirm it with the human before committing.
    if not state.get("tech_stack_confirmed"):
        # "external" = an arbitrary --repo we don't own (detect its stack, don't persist).
        # Our own project (managed) and greenfield reuse the CEO/CTO's persisted decision.
        external = bool(state.get("target_repo")) and not state.get("managed_project")
        if not external and product.load_stack():
            state = {**state, "tech_stack": product.load_stack(), "tech_stack_confirmed": True}
        else:
            detected = state.get("detected_stack")
            default = detected if (detected and detected != "unknown") else DEFAULT_STACK
            decision = _stack_decision_from_log(state.get("qa_log") or [], default)
            if decision is None:
                return _ask_stack(state)             # escalate stack to CEO/CTO
            if not external:
                product.save_stack(decision)         # persist for future features of this product
            state = {**state, "tech_stack": decision, "tech_stack_confirmed": True}

    # Regenerating after a critic gap: apply the notes, skip a fresh Q&A round.
    if state.get("review_notes"):
        return _do_work(
            state,
            list(state.get("qa_log") or []),
            dict(state.get("qa_rounds") or {}),
            allow_clarify=False,
        )
    return run_with_qa(state, "architect", _do_work, consultable_agents=CONSULT)


def _ask_stack(state: dict) -> dict:
    """Mandatory: ask the CEO/CTO to confirm or change the tech stack before commit."""
    qa_log = list(state.get("qa_log") or [])
    # In extend mode, propose the stack the Surveyor detected in the existing repo.
    detected = state.get("detected_stack")
    if detected and detected != "unknown":
        proposed = f"the existing codebase's stack ({detected})"
    else:
        proposed = f"the default {DEFAULT_STACK}"
    question = (
        f"TECH STACK DECISION (CTO call): proposed is {proposed}. "
        "Confirm this stack, or specify a different one, before I commit the technical "
        "spec. This choice drives all downstream code, tests, and deployment."
    )
    qa_log.append({"from": "architect", "to": "ceo", "question": question})
    return {
        "current_node": "architect",
        "qa_log": qa_log,
        "ceo_qa_pending": question,
        "ceo_qa_from": "architect",
    }


def _stack_decision_from_log(qa_log: list, default: str = DEFAULT_STACK):
    """Return the CEO/CTO's stack decision if they've answered, else None.

    An affirmative reply ("confirm"/"yes"/…) means "use the proposed default", which in
    extend mode is the stack detected in the existing repo.
    """
    for e in qa_log:
        if (e.get("from") == "architect" and e.get("to") == "ceo"
                and "tech stack" in e.get("question", "").lower() and e.get("answer")):
            ans = e["answer"].strip()
            return default if ans.lower() in _AFFIRMATIVE else ans
    return None


def _do_work(state: dict, qa_log: list, rounds: dict, allow_clarify: bool = True) -> dict:
    identity = load_prompt("architect")
    skill = load_skill("architect")
    system = augment_system(f"{identity}\n\n{skill}" if skill else identity, "architect")

    prd = read_artifact(state["prd_path"])
    # Read the design spec effectively untruncated (40000 ≈ 10K tokens): the default 24000 cap
    # head-sliced the 30KB P6 storefront spec, so the architect wrote a tech spec MISSING the
    # tail (the console-nav wiring), which then propagated to the engineer. It's a build contract.
    design = read_artifact(state["design_path"], 40000) if state.get("design_path") else "No design spec."
    qa_ctx = format_qa_context(qa_log, "architect")
    stack = state.get("tech_stack") or DEFAULT_STACK

    # Extend mode: include the Surveyor's integration brief so the spec describes changes
    # to the EXISTING codebase (which files to modify/create), not a greenfield build.
    repo_block = ""
    if state.get("repo_map_path"):
        repo_block = (
            "\n\nEXISTING CODEBASE (integration brief — extend it, don't rebuild it; "
            "the File Structure section must list real files to MODIFY or CREATE):\n"
            + read_artifact(state["repo_map_path"])
        )

    ledger = state.get("project_ledger")
    ledger_block = f"\n\nPROJECT HISTORY (features already built — stay consistent with prior decisions, reuse existing modules, do not duplicate):\n{ledger}" if ledger else ""

    feedback = state.get("review_notes")
    feedback_block = f"\n\nCRITIC FOUND GAPS IN THE PREVIOUS SPEC (fix every one):\n{feedback}" if feedback else ""

    inv_block = product_invariants_block(state)

    user_msg = f"""
PRD:
{prd}

Design Spec:
{design}
{repo_block}{ledger_block}{inv_block}

{qa_ctx}{feedback_block}

Produce a technical spec with ONLY these sections:

## Stack
Use EXACTLY this CEO/CTO-confirmed stack — do not change it:
{stack}
State each component (backend, frontend, database) explicitly.

## Data Models
For each model: name, fields, types, relationships.
Use simple table format.

## API Endpoints
Method | Path | Request body fields | Response body fields | Auth required?
List only what this feature needs. If you find yourself writing more than 10 endpoints,
the feature scope is likely too large — flag it rather than omitting endpoints silently.

## File Structure
List files to be created, following the stack's conventions.
Format: path/to/file.ext — one line description

## Migration Plan
Is this migration additive (new table/column — safe) or destructive (rename/drop — risky)?
Can it run zero-downtime, or does the app need to be down? Any backfill required?
If no schema changes: write "No schema changes."

## Test Strategy
What to unit test, what to integration test.
Name the specific functions/endpoints to cover.

## Implementation Notes
Any gotchas, ordering constraints, or security considerations.
Max 5 bullet points.
"""

    # §4.2: ground the tech spec in CURRENT library versions/APIs/CVEs (web_search is
    # opt-in via LLM_WEB_SEARCH; no-op otherwise) — the architect pins the stack, so this
    # is the highest-value place to verify versions instead of trusting training memory.
    questions, spec = work_call(system, user_msg, "reason", CONSULT, allow_clarify,
                                web_search=True)
    if questions:
        return {"_clarify": questions}

    valid, tool_msg = validate_api_spec(spec)
    if not valid:
        spec += f"\n\n---\n⚠️ API SPEC WARNING:\n{tool_msg}"

    path = write_artifact(state["project_id"], "design", "tech_spec.md", spec)

    return {
        "current_node": "architect",
        "design_path": path,
        "tech_stack": stack,
        "tech_stack_confirmed": True,
        "review_notes": None,        # consumed
        "review_action": None,       # reset routing signal before re-entering critic
        "qa_log": qa_log,
        "qa_rounds": rounds,
        "ceo_qa_from": None,
    }
