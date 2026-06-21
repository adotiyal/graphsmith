"""
Triage Agent (change-type router)
----------------------------------
Classifies the incoming request so the pipeline takes the right-sized path:

- feature  → FULL lane: PM → [PRD gate] → Surveyor → Design → critics → Architect →
             Test Author → Engineer ⇄ QA → [PR gate] → Ship → DevOps
- bugfix / refactor / chore → QUICK lane: straight to Engineer ⇄ QA → [PR gate] → Ship
  (skips PRD/design/architecture/TDD scaffolding and DevOps). Most 1→10 work is here.

DESIGN DECISION: safe default. On any ambiguity the classifier returns "feature" (the
fuller, safer process). Runs on the `fast` tier — one cheap classification call.
"""

from graph.state import ProjectState
from tools.llm import call_structured
from tools.file_io import load_prompt, read_artifact

# §4.1: the change-type is a VALIDATED enum decision, not a substring scan over the model's
# prose (which could pick up a stray "bug"/"feature" word in the reasoning). On any failure
# call_structured retries once then returns the SAFE DEFAULT — feature = the fuller, safer
# full lane — so a misparse can never silently route to the lighter quick lane.
_SCHEMA = {"change_type": {"type": "enum",
                           "values": ["bugfix", "refactor", "chore", "feature"],
                           "required": True}}


def run(state: ProjectState) -> dict:
    brief = read_artifact(state["prd_path"]) if state.get("prd_path") else state.get("feature_request", "")
    system = load_prompt("triage")
    data = call_structured(system, f"Classify this change request:\n\n{brief}",
                           _SCHEMA, tier="fast", default={"change_type": "feature"})
    return {"current_node": "triage", "change_type": data["change_type"]}
