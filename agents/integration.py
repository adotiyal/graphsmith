"""
Integration Agent (Phase 4.2 / 4.3 — run-and-verify)
----------------------------------------------------
Sits AFTER QA (tests green) and BEFORE the PR gate. Proves the app actually RUNS,
not just compiles: brings the project's own docker-compose stack up, waits for
healthy, smoke-checks the conventional endpoints (api :8000/health, frontend :3000),
and runs QA's Playwright e2e specs (e2e/*.spec.ts) against the live stack.

DESIGN DECISION: this node is fully deterministic — no LLM call. It is a tool
executor like the Docker test runner; the *authoring* intelligence lives in QA
(which writes the e2e specs) and the Engineer (which must ship a working compose).

Failure → loops back to the Engineer with the compose/smoke/e2e log in error_log
(same channel the test loop uses), bounded by MAX_INTEGRATION_ATTEMPTS. When the
cap is hit the pipeline proceeds to the PR gate anyway — the CEO/CTO sees the red
integration report and decides (pipeline always completes, nothing silently ships).

External --repo runs (not the managed project) skip gracefully when there is no
compose file — we don't impose our stack conventions on someone else's repo.
"""

import re
from pathlib import Path

from graph.state import ProjectState
from tools.file_io import write_artifact, code_root, read_artifact, WORKSPACE_ROOT
from tools.registry import run_compose_integration

# 3 (was 2): I4's bounded QA spec-revision round consumes one integration attempt —
# the engineer keeps its two real shots at app-level failures.
MAX_INTEGRATION_ATTEMPTS = 3
MAX_ERROR_CHARS = 4000


def run(state: ProjectState) -> dict:
    project_dir = str(code_root(state))
    # Managed project + greenfield must ship their own compose file; an arbitrary
    # external repo is not held to our conventions.
    external = bool(state.get("target_repo")) and not state.get("managed_project")

    # Where design_qa will find the live-app screenshot (captured while the stack is
    # up). Only requested when there is a design mockup to compare against.
    shot_path = None
    if state.get("design_mockup_path"):
        shot_path = str(WORKSPACE_ROOT / state["project_id"] / "tests" / "app_screenshot.png")

    passed, report = run_compose_integration(
        project_dir,
        require_compose=not external,
        e2e=True,
        screenshot_to=shot_path,
        required_microcopy=_required_microcopy(state),
        # consumer app: the served HTML must carry the SEO/AEO floor whenever the
        # feature has a UI surface (free deterministic check, before vision/e2e)
        check_seo=bool(state.get("design_mockup_path")),
    )

    # Feature-Contract COVERAGE (deterministic, free): every acceptance criterion must
    # have a test, every UI AC an e2e — proven from the AC `# covers:` tags. Prepended
    # to the WRITTEN report so the human/overseer always sees the AC→test map; the app
    # report stays separate for accurate stage detection.
    cov_ok, cov_msg = _ac_coverage(state)
    app_report, app_passed = report, passed
    report = (f"=== AC coverage (deterministic) — {'OK' if cov_ok else 'FAILED'} ===\n"
              f"{cov_msg}\n\n" + app_report)

    attempts = state.get("integration_attempts", 0) + 1
    write_artifact(state["project_id"], "tests", "integration_report.md", report)

    # A coverage gap on a UI criterion is a QA spec gap, not an app bug.
    passed = app_passed and cov_ok
    out = {
        "current_node": "integration",
        "integration_passed": passed,
        "integration_attempts": attempts,
        "app_screenshot_path": shot_path if (shot_path and Path(shot_path).exists()) else None,
    }
    if not passed:
        # head (which stage failed) + TAIL (the actual test failures live at the END —
        # a head-only slice fed the QA revision nothing but compose build noise and it
        # revised blind; the never-head-slice-a-test-log rule, 4th occurrence)
        out["error_log"] = ("INTEGRATION FAILURE (the app did not run correctly when "
                            "brought up with docker compose):\n"
                            + report[:1200] + "\n[...]\n" + report[-(MAX_ERROR_CHARS - 1200):])
        # Stage from the APP report; if the app is all-green but coverage failed, the
        # gap is a QA/test problem → tag "coverage" so it routes to QA, not the engineer.
        stage = _failed_stage(app_report)
        if app_passed and not cov_ok:
            stage = "coverage"
        out["integration_failed_stage"] = stage
        # NOTE/backlog: a build failure caused by a DESIGN-KIT file (components/kit/*.tsx)
        # can't be fixed by the engineer (the kit is protected) — it should route to
        # design, not loop the engineer. Needs an integration→design edge + a design
        # kit-fix path. For now the CTO adjudicates such failures at the gate.
        from tools.learnings import emit_feedback
        emit_feedback("engineer" if stage not in ("e2e", "coverage") else "qa",
                      f"integration_{stage}", (cov_msg if stage == "coverage" else report)[-900:])
        # I4(e): the app is healthy but the e2e RUN failed (or a UI AC lacks an e2e) →
        # the specs are the prime suspect. Route ONE bounded revision round to QA.
        if (stage in ("e2e", "coverage")
                and state.get("e2e_files") and not state.get("e2e_revised")):
            out["e2e_revision_pending"] = True
    return out


def _ac_coverage(state: ProjectState) -> tuple:
    """Deterministic Feature-Contract coverage from the PRD's AC ids + the `# covers:`
    tags in the tests/e2e on disk. Never raises (returns (True, note) on any error)."""
    try:
        from tools import contract
        prd = read_artifact(state["prd_path"]) if state.get("prd_path") else ""
        acs = contract.parse_acs(prd)
        if not acs:
            return True, "no acceptance criteria parsed — coverage check skipped"
        root = code_root(state)
        unit, e2e = [], []
        for sub in ("tests", "backend/tests", "frontend/tests"):
            d = root / sub
            if d.is_dir():
                unit += [p.read_text(encoding="utf-8", errors="replace")
                         for p in d.rglob("test_*.py")]
        for rel in (state.get("e2e_files") or []):
            p = root / rel
            if p.exists():
                e2e.append(p.read_text(encoding="utf-8", errors="replace"))
        return contract.coverage_report(contract.coverage(acs, unit, e2e))
    except Exception as e:
        return True, f"coverage check skipped ({type(e).__name__})"


def _failed_stage(report: str) -> str:
    """First failed stage header in the report: 'compose', 'health', 'smoke',
    'microcopy', 'seo', 'theme', 'e2e', or '' when unparseable."""
    m = re.search(r"=== ([\w][\w ()/\-]*?) — FAILED ===", report)
    if not m:
        return ""
    name = m.group(1).lower()
    for stage in ("compose", "health", "smoke", "microcopy", "seo", "theme", "e2e"):
        if stage in name:
            return stage
    return name.split()[0]


def _required_microcopy(state: ProjectState) -> list:
    """Parse the design manifest's REQUIRED MICROCOPY section — the FREE conformance
    gate that runs before any vision tokens are spent. Lines look like: - "text"."""
    path = state.get("components_manifest_path")
    if not path:
        return []
    text = read_artifact(path)
    m = re.search(r"REQUIRED MICROCOPY.*?\n(.*?)(?:\n#|\Z)", text, re.DOTALL | re.I)
    if not m:
        return []
    strings = re.findall(r'-\s*"(.+?)"', m.group(1))
    # placeholder templates ("{N} remaining") can never match verbatim — skip them;
    # the vision design-QA judges those visually instead
    return [s for s in strings if "{" not in s]
