"""
evals/overseer.py — the runtime overseer (cheap, deterministic guardrails)
--------------------------------------------------------------------------
With the human mostly out of the loop, something must check that the agents actually did
their jobs. This runs deterministic invariants + loop/budget checks on the run's final
state (and the trace totals). It is the *out-of-band* counterpart to the in-band critics:
critics judge an artifact during the run; the overseer audits the whole run after it.

Findings have a severity. A `high` failing finding means the run should NOT be trusted —
surface it to the human even if the pipeline "completed". Everything here is deterministic
(no LLM), so it's free to run on every single run.

Next steps (documented, not built here): an LLM overseer that reviews the trajectory, and
per-node real-time halting. This is the cheap, high-signal trio first.
"""

from pathlib import Path

# Mirror the pipeline caps (kept local so the overseer has no graph import dependency).
MAX_FIX_ATTEMPTS = 3
MAX_REVIEW_ATTEMPTS = 2
TOKEN_BUDGET = 200_000
CALL_BUDGET = 60


def _finding(check, ok, severity, detail=""):
    return {"check": check, "ok": ok, "severity": severity, "detail": detail}


def _is_test_path(p: str) -> bool:
    p = (p or "").replace("\\", "/")
    return "/tests/" in p or p.startswith("tests/") or Path(p).name.startswith("test_")


def check_invariants(state: dict) -> list:
    """Properties that must hold for a trustworthy run."""
    out = []
    code = state.get("code_files") or []
    change_type = state.get("change_type") or "feature"
    full_lane = change_type == "feature"

    # The engineer must never author/modify tests (the TDD oracle stays independent).
    offenders = [c for c in code if _is_test_path(c)]
    out.append(_finding("engineer_protected_tests", not offenders, "high",
                        f"engineer wrote test files: {offenders}" if offenders else "tests untouched"))

    # A full-lane feature that produced code must have gone through a PRD and a confirmed stack.
    if full_lane and code:
        out.append(_finding("feature_has_prd", bool(state.get("prd_path")), "medium",
                            "feature shipped code with no PRD" if not state.get("prd_path") else ""))
        out.append(_finding("stack_confirmed", bool(state.get("tech_stack_confirmed")), "medium",
                            "code produced without a CEO/CTO-confirmed stack" if not state.get("tech_stack_confirmed") else ""))

    # "Silent" means NO HUMAN APPROVED — hitting the retry cap is never a licence to
    # ship. A PR may only exist if pr_approved (an explicit CEO "approve" at the gate;
    # the gates are default-deny). Red tests + a PR is allowed ONLY with that approval
    # (the CEO knowingly accepted an imperfect diff). A live run auto-shipped red code
    # through a driver bug before this was tightened.
    pr_attempted = bool(state.get("pr_url"))
    if pr_attempted:
        ok = bool(state.get("pr_approved"))
        out.append(_finding("no_silent_red_ship", ok, "high",
                            "ship ran without explicit CEO approval at the PR gate" if not ok else ""))
    return out


def check_loops(state: dict) -> list:
    """Detect non-convergence — caps were hit rather than the work actually resolving."""
    out = []
    if state.get("fix_attempts", 0) >= MAX_FIX_ATTEMPTS and not state.get("tests_passed"):
        out.append(_finding("engineer_qa_converged", False, "high",
                            f"engineer⟷QA hit the {MAX_FIX_ATTEMPTS}-retry cap without passing"))
    for stage, n in (state.get("review_attempts") or {}).items():
        if n >= MAX_REVIEW_ATTEMPTS:
            out.append(_finding(f"critic_{stage}_escalated", True, "info",
                                f"{stage} critic hit its cap → escalated to CEO/CTO (review the spec)"))
    return out


def check_budget(totals: dict, token_budget=TOKEN_BUDGET, call_budget=CALL_BUDGET) -> list:
    out = []
    if not totals:
        return out
    tok = totals.get("in_tokens", 0) + totals.get("out_tokens", 0)
    out.append(_finding("token_budget", tok <= token_budget, "medium",
                        f"{tok} tokens (budget {token_budget})"))
    calls = totals.get("llm_calls", 0)
    out.append(_finding("call_budget", calls <= call_budget, "low",
                        f"{calls} LLM calls (budget {call_budget})"))
    return out


def check_autonomy(autonomy: dict) -> list:
    """Surface the autonomy metric (§3.3 / I10) — human interventions per run, the number
    a software company manages. INFO severity: it never fails a run (autonomy is a metric
    to drive down over time, not a correctness gate). Empty when not supplied."""
    if not autonomy:
        return []
    detail = (f"autonomy_rate {autonomy.get('autonomy_rate')} — "
              f"{autonomy.get('interventions', 0)} human intervention(s): "
              f"{autonomy.get('clarifications', 0)} clarification + "
              f"{autonomy.get('rejections', 0)} gate-reject + "
              f"{autonomy.get('manual_edits', 0)} manual-edit; "
              f"{autonomy.get('approvals', 0)} gate approval(s), "
              f"{autonomy.get('agent_steps', 0)} agent steps")
    return [_finding("autonomy_rate", True, "info", detail)]


def oversee(state: dict, totals: dict = None, autonomy: dict = None) -> dict:
    """Run all checks. `ok` is False if any HIGH-severity finding failed. `autonomy`
    (from run_stats.compute_autonomy) is surfaced as an info finding when provided."""
    findings = (check_invariants(state) + check_loops(state)
                + check_budget(totals or {}) + check_autonomy(autonomy))
    high = [f for f in findings if not f["ok"] and f["severity"] == "high"]
    return {"ok": not high, "findings": findings, "high_severity_failures": high}


def format_report(report: dict) -> str:
    lines = ["── Overseer ──"]
    for f in report["findings"]:
        mark = "✓" if f["ok"] else "✗"
        sev = "" if f["ok"] else f" [{f['severity'].upper()}]"
        detail = f" — {f['detail']}" if f["detail"] else ""
        lines.append(f"  {mark} {f['check']}{sev}{detail}")
    verdict = "TRUSTWORTHY" if report["ok"] else "NEEDS HUMAN REVIEW"
    lines.append(f"  → {verdict}")
    return "\n".join(lines)
