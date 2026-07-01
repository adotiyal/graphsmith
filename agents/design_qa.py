"""
Design QA Agent (vision-based design verification)
--------------------------------------------------
Runs AFTER integration (the app provably runs) and BEFORE the PR gate. Compares a
screenshot of the LIVE app against the rendered design mockup + the design spec,
using a vision-capable model, and demands strict alignment: title/branding, layout
structure, components, microcopy, and visible states must MATCH the design.

Why this exists: a live run shipped a fully working app that looked nothing like
design/mockup.html — and a Tailwind purge bug shipped it completely UNSTYLED — and
every other check (pytest, vitest, smoke, e2e) passed. Pixels are a bug class only
visual verification can catch.

Mechanics:
- Integration captures tests/app_screenshot.png while the compose stack is up.
- This node renders design/mockup.html to a PNG (playwright container, file://).
- One vision LLM call (strong tier) sees both images + the design spec and returns
  ===VERDICT: ALIGNED=== or ===VERDICT: MISALIGNED=== with itemized findings.
- MISALIGNED → loops to the Engineer with the findings (review_notes), bounded by
  MAX_DESIGN_QA_ATTEMPTS; at the cap it proceeds to the gate with the red report
  (pipeline always completes — the CEO sees the findings and decides).
- Skips gracefully (pass-through with a note) when there is no mockup, no captured
  screenshot, or the mockup render fails — never blocks the pipeline on tooling.
"""

from pathlib import Path

from graph.state import ProjectState
from tools.file_io import load_prompt, read_artifact, write_artifact, WORKSPACE_ROOT
from tools.llm import call_structured
from tools.registry import render_mockup_screenshot

# SINGLE-SHOT by design (cost control): with design-owned components (alignment by
# construction) + the free deterministic microcopy gate in integration, the vision
# check is a final confirmation, not a correction loop. One MISALIGNED verdict gives
# the engineer one fix round; after that the CEO sees the red report at the gate.
MAX_DESIGN_QA_ATTEMPTS = 1
MAX_FINDINGS_CHARS = 3500

# §4.1: the verdict is a VALIDATED enum (call_structured), not a regex over the vision
# reply. A parse failure used to default to MISALIGNED → a wasted engineer round; now
# call_structured retries once first, then defaults to MISALIGNED (preserving the safe
# "never silently pass a divergent UI" behavior — the CEO still sees the report at the gate).
_VERDICT_SCHEMA = {
    "verdict": {"type": "enum", "values": ["ALIGNED", "MISALIGNED"], "required": True},
    "findings": {"type": "string", "required": False},
}


def run(state: ProjectState) -> dict:
    skip_reason = _skip_reason(state)
    if skip_reason:
        _write_report(state, f"# Design QA — skipped\n\n{skip_reason}\n")
        return _out(state, passed=True, note=skip_reason)

    mockup_png = str(WORKSPACE_ROOT / state["project_id"] / "tests" / "mockup_screenshot.png")
    ok, msg = render_mockup_screenshot(state["design_mockup_path"], mockup_png)
    if not ok:
        note = f"mockup render failed — design check skipped: {msg}"
        _write_report(state, f"# Design QA — skipped\n\n{note}\n")
        return _out(state, passed=True, note=note)

    verdict, findings = _compare(state, state["app_screenshot_path"], mockup_png)
    attempts = state.get("design_qa_attempts", 0) + 1

    if not verdict:
        from tools.learnings import emit_feedback
        emit_feedback("design", "design_qa_misaligned", findings or "")
        emit_feedback("engineer", "design_qa_misaligned", findings or "")
    report = (f"# Design QA — {'ALIGNED ✓' if verdict else 'MISALIGNED ✗'} "
              f"(attempt {attempts})\n\n{findings}\n")
    _write_report(state, report)

    out = _out(state, passed=verdict, attempts=attempts)
    if not verdict:
        out["review_notes"] = ("DESIGN QA FINDINGS — the implemented UI must MATCH the "
                               "design (mockup + design spec). Fix ONLY presentation: "
                               "markup, styling, microcopy. Do not change behavior or "
                               "break any test.\n" + findings)[:MAX_FINDINGS_CHARS]
    return out


def _compare(state: dict, app_png: str, mockup_png: str) -> tuple[bool, str]:
    """One vision call: app screenshot vs mockup screenshot vs design spec."""
    system = load_prompt("design_qa")
    # Full spec, not a truncation — a 6000-char cap once hid half the design contract
    # from the judge, which then flagged correct implementations of unseen rules.
    design_spec = read_artifact(state["design_spec_path"], 14000) \
        if state.get("design_spec_path") else "(no design spec on file)"

    user_msg = f"""
Compare the LIVE APP screenshot against the DESIGN MOCKUP screenshot and the design
spec below. The mockup may be a design BOARD showing several screens/states with
annotations — compare the app against the screen matching the app's current state
(e.g. empty/first-run vs populated).

DESIGN SPEC (the contract — layout, components, microcopy, states):
{design_spec}

Judge STRICTLY on what a user sees:
1. Title/branding and all visible MICROCOPY (exact wording matters)
2. Layout structure (containers, alignment, hierarchy, spacing intent)
3. Components present and styled as designed (buttons, inputs, counters, lists)
4. State correctness (empty state copy, counter visibility rules)
Ignore: font rendering differences, minor pixel offsets, scrollbar artifacts, and
the mockup board's own annotation chrome (labels, callouts, frame borders).
A control shown in a DISABLED state consistent with the design's stated rules (e.g.
an Add button disabled while the input is empty) is CORRECT — judge its enabled
styling only if the screenshot shows it enabled. Judge only against what is VISIBLE
in the mockup/spec — do not invent stricter sub-structure than the design shows.
Elements the design specifies as HOVER-REVEALED (e.g. a delete icon with opacity-0
until row hover) are necessarily INVISIBLE in a static screenshot — their absence
is CORRECT and must NOT be flagged. The screenshot cannot hover, focus, or type.
The app screenshot is captured at DESKTOP width (1280px) in LIGHT mode: the mockup may
show mobile/desktop frames and light/dark variants — compare ONLY against the matching
(desktop, light) frames; mobile-only rules (e.g. always-visible touch targets) and
dark-mode styling cannot be observed here and must not be flagged. Subtle opacity-based dimming (e.g. opacity-60 rows)
may be imperceptible in a screenshot — when the design's OTHER completed-state
signals (strikethrough, muted text) are clearly present, treat the treatment as
implemented rather than flagging unverifiable opacity. The mockup's EXAMPLE DATA
(task names, dates, count values like "3 remaining · 2 completed") is illustrative
only — judge the FORMAT and components, never the literal values or row counts.

Set `verdict` to ALIGNED only if a reviewer would say the app IS the design. Any
wrong/missing microcopy, missing component, or unstyled/divergent layout = MISALIGNED.
Put every divergence in `findings`, one per line as "- [element]: expected <design> /
got <app>" (findings may be empty when ALIGNED).
"""
    data = call_structured(
        system, user_msg, _VERDICT_SCHEMA, tier="reason",
        default={"verdict": "MISALIGNED",
                 "findings": "(design-QA verdict could not be parsed — treated as MISALIGNED)"},
        images=[("LIVE APP (what users currently see):", app_png),
                ("DESIGN MOCKUP (the contract):", mockup_png)])
    verdict = data.get("verdict") == "ALIGNED"
    findings = (data.get("findings") or "").strip()
    return verdict, findings


def _skip_reason(state: dict) -> str:
    if not state.get("design_mockup_path"):
        return "no design mockup for this change (quick lane / backend-only) — nothing to verify"
    if not Path(state["design_mockup_path"]).exists():
        return "design mockup file missing on disk"
    if not state.get("app_screenshot_path") or not Path(state["app_screenshot_path"]).exists():
        return "no live-app screenshot captured by integration (stack/screenshot unavailable)"
    return ""


def _write_report(state: dict, text: str):
    write_artifact(state["project_id"], "tests", "design_qa.md", text)


def _out(state: dict, passed: bool, attempts: int = None, note: str = None) -> dict:
    return {
        "current_node": "design_qa",
        "design_qa_passed": passed,
        "design_qa_attempts": attempts if attempts is not None
                              else state.get("design_qa_attempts", 0),
    }
