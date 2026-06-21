"""
live_run.py — non-interactive driver for the pipeline, for operator-driven runs.

Unlike main.py (which blocks on input()), this runs the graph to the next point
that genuinely needs a human decision, prints a machine-readable PAUSE marker, and
exits — persisting state in the sqlite checkpointer (keyed by thread_id). Resume by
calling again with the answer/approval. This lets an operator drive the human-in-the-
loop pipeline one segment per process invocation.

Commands:
  start  --feature "..." [--repo PATH]      seed + run to first pause/end
  answer --thread ID --text "..."           inject a ceo_qa answer, continue
  approve --thread ID [--feedback "..."]     approve a gate, continue
  reject  --thread ID --feedback "..."       reject a gate, continue
  status  --thread ID                        dump current state summary

On each invocation it prints lines beginning with "PAUSE " or "DONE " so the
operator can parse what's needed next.
"""

import argparse
import json
import re
import sys
import uuid

# Line-buffer stdout even when piped (operator tails the output file for progress;
# fully-buffered output made a 36-minute segment look dead until process exit).
sys.stdout.reconfigure(line_buffering=True)

from graph.graph import build_graph
from graph.state import ProjectState
from tools import product, project_ctx, trace, registry
from evals import overseer


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def _emit(kind: str, payload: dict):
    """Machine-readable marker line for the operator to parse."""
    print(f"{kind} " + json.dumps(payload, default=str))


def _render_stats(project_id: str) -> dict:
    """Per-step statistics (wall time, tokens, calls per node + human wait) — the
    e2e investigation record. Rendered from the trace at every pause and at DONE."""
    from evals import run_stats
    tr = trace.get()
    if tr is None:
        return {}
    out = run_stats.render(str(tr.path), project_id)
    return {k: v for k, v in out.items() if k in ("md", "html")}


def _drive(graph, config, project_id):
    """Stream until the graph pauses for a human decision or completes.

    Returns when either: a ceo_qa/approval interrupt needs input (emits PAUSE),
    or the pipeline is done (emits DONE). Initial-ceo / gate interrupts that need
    no input are auto-resumed inside the loop.
    """
    while True:
        for event in graph.stream(None, config, stream_mode="updates"):
            for node_name, updates in event.items():
                if node_name == "__interrupt__":
                    continue
                current = updates.get("current_node", node_name)
                trace.emit("node", node=node_name, change_type=updates.get("change_type"))
                line = f"[{current}] done"
                for key, label in (
                    ("prd_path", "PRD"), ("design_path", "Spec"),
                    ("design_mockup_path", "Mockup"), ("code_path", "Code"),
                    ("test_path", "Tests"), ("pr_url", "PR"),
                ):
                    if updates.get(key):
                        line += f" | {label}: {updates[key]}"
                if "tests_passed" in updates:
                    line += f" | Tests: {'PASS' if updates['tests_passed'] else 'FAIL'}"
                if updates.get("error_log") and not updates.get("tests_passed"):
                    line += f" | err: {updates['error_log'][:160]!r}"
                print(line)

        snapshot = graph.get_state(config)
        if not snapshot.next:
            break  # complete

        values = snapshot.values
        _render_stats(project_id)   # per-step statistics, refreshed at every pause
        if values.get("ceo_qa_pending"):
            _emit("PAUSE", {
                "thread": project_id, "type": "ceo_qa",
                "from": values.get("ceo_qa_from"),
                "question": values.get("ceo_qa_pending"),
            })
            return
        if values.get("approval_pending"):
            stage = values.get("approval_pending")
            payload = {"thread": project_id, "type": "approval", "stage": stage}
            # Human-facing HTML dashboard for the gate (deterministic, never blocks)
            from tools import report_html
            review = report_html.render_gate(dict(values), stage)
            if review:
                payload["review_html"] = review
            if stage == "prd":
                payload["artifact_path"] = values.get("prd_path")
            elif stage == "pr":
                payload["qa_report_path"] = f"workspace/{project_id}/tests/qa_report.md"
                payload["code_path"] = values.get("code_path")
                payload["security_warnings"] = values.get("security_warnings") or []
            _emit("PAUSE", payload)
            return
        # else: initial ceo interrupt or gate with no pending — resume next loop

    # Complete: record the feature in the managed project's ledger (parity with main.py)
    # so the next run has the history, then run the overseer audit.
    final = graph.get_state(config).values
    if final.get("managed_project") and final.get("feature_request"):
        project_ctx.append_ledger(final["feature_request"], final)
    totals = trace.get().totals() if trace.get() else None
    # Autonomy metric (§3.3 / I10): human interventions per run → overseer + flight recorder.
    _auton = None
    if trace.get():
        from evals import run_stats as _rs
        _auton = _rs.compute_autonomy(_rs.load_events(str(trace.get().path)), final)
    report = overseer.oversee(final, totals, autonomy=_auton)
    stats_paths = _render_stats(project_id)
    # Self-improvement retro: distil per-agent lessons from this run's feedback
    # events (gates, critics, integration, guards, vision QA) + CEO directives.
    from tools import learnings as _learnings
    retro = _learnings.run_retro(str(trace.get().path), final) if trace.get() else {}
    if retro:
        print("── Retro lessons recorded ──")
        for agent, lessons in retro.items():
            for l in lessons:
                print(f"  {agent}: {l}")
    # Human audit folder: every actor, discussion, decision, and why — browsable HTML.
    from tools import report_html
    audit = report_html.render_audit(final, str(trace.get().path) if trace.get() else "",
                                     retro=retro, overseer=report)
    if audit:
        print(f"📋 Audit folder: file://{audit}")
    flight = report_html.render_run(final, str(trace.get().path) if trace.get() else "",
                                    overseer=report, autonomy=_auton)
    if flight:
        print(f"🛫 Flight recorder: file://{flight}")
    _emit("DONE", {
        "thread": project_id,
        "ok": report["ok"],
        "tests_passed": final.get("tests_passed"),
        "pr_url": final.get("pr_url"),
        "code_path": final.get("code_path"),
        "overseer_findings": report.get("findings", []),
        "totals": totals,
        "stats": stats_paths,
        "audit": audit,
        "flight": flight,
    })
    print("\n" + overseer.format_report(report))


def cmd_start(graph, args):
    managed = args.repo is None
    ledger = ""
    if managed:
        project_ctx.ensure_repo()
        target_repo = str(project_ctx.project_dir())
        ledger = project_ctx.load_ledger()
    else:
        target_repo = args.repo

    project_id = slugify(args.feature) + "-" + uuid.uuid4().hex[:6]
    initial_state: ProjectState = {
        "project_id": project_id,
        "feature_request": args.feature,
        "prd_path": None, "design_path": None, "code_path": None, "deploy_path": None,
        "tests_passed": False, "deployed": False, "pr_url": None, "deploy_url": None,
        "fix_attempts": 0, "error_log": None, "current_node": "start",
        "change_type": None, "qa_log": [], "qa_rounds": {}, "agent_qa_counts": {},
        "ceo_qa_pending": None, "ceo_qa_from": None, "ceo_qa_answer": None,
        "test_path": None, "review_attempts": {}, "review_notes": None,
        "review_action": None, "prd_approved": False, "pr_approved": False,
        "approval_pending": None, "approval_decision": None, "approval_feedback": None,
        "tech_stack": None, "tech_stack_confirmed": False, "target_repo": target_repo,
        "repo_map_path": None, "detected_stack": None, "managed_project": managed,
        "project_ledger": ledger or None, "test_files": [], "security_warnings": [],
        "code_files": [], "product_profile": product.load_profile() or None,
        "product_invariants": registry.extract_product_invariants(target_repo) or None,
        "design_mockup_path": None,
        "design_spec_path": None,
        "integration_passed": False, "integration_attempts": 0, "e2e_files": [],
        "app_screenshot_path": None, "design_qa_passed": False, "design_qa_attempts": 0,
        "design_component_files": [], "components_manifest_path": None,
    }
    config = {"configurable": {"thread_id": project_id}}
    trace.start(project_id)
    _emit("START", {"thread": project_id, "feature": args.feature, "managed": managed})
    graph.invoke(initial_state, config)  # pauses at interrupt_before=["ceo"]
    _drive(graph, config, project_id)


def cmd_answer(graph, args):
    config = {"configurable": {"thread_id": args.thread}}
    trace.start(args.thread)
    graph.update_state(config, {"ceo_qa_answer": args.text})
    _drive(graph, config, args.thread)


def cmd_gate(graph, args, decision):
    config = {"configurable": {"thread_id": args.thread}}
    trace.start(args.thread)
    graph.update_state(config, {
        "approval_decision": decision,
        "approval_feedback": args.feedback,
    })
    _drive(graph, config, args.thread)


def cmd_resume(graph, args):
    """Continue a thread from its last checkpoint (e.g. after a fixed crash)."""
    config = {"configurable": {"thread_id": args.thread}}
    trace.start(args.thread)
    _drive(graph, config, args.thread)


def cmd_status(graph, args):
    config = {"configurable": {"thread_id": args.thread}}
    snap = graph.get_state(config)
    v = snap.values
    _emit("STATUS", {
        "thread": args.thread,
        "next": snap.next,
        "current_node": v.get("current_node"),
        "change_type": v.get("change_type"),
        "ceo_qa_pending": v.get("ceo_qa_pending"),
        "approval_pending": v.get("approval_pending"),
        "tests_passed": v.get("tests_passed"),
        "prd_path": v.get("prd_path"),
        "design_path": v.get("design_path"),
        "design_mockup_path": v.get("design_mockup_path"),
        "code_path": v.get("code_path"),
        "code_files": v.get("code_files"),
        "test_files": v.get("test_files"),
    })


def cmd_feedback(args):
    """Log an out-of-band operator/CTO feedback moment so the end-of-run retro distils a
    GENERALIZABLE lesson from it — e.g. after a CTO hand-fix the pipeline can't observe, or
    a QA NO-GO the CTO adjudicated rather than rejected. `--text` should name the CLASS of
    mistake (a product-agnostic rule), NOT the app-specific fix; run_retro generalises
    further. Appends a feedback event to the thread's trace, which run_retro reads at DONE."""
    trace.start(args.thread)
    from tools.learnings import emit_feedback
    emit_feedback(args.agent, "cto_handfix", args.text)
    _emit("FEEDBACK", {"thread": args.thread, "agent": args.agent, "text": args.text})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start"); s.add_argument("--feature", required=True); s.add_argument("--repo", default=None)
    a = sub.add_parser("answer"); a.add_argument("--thread", required=True); a.add_argument("--text", required=True)
    ap = sub.add_parser("approve"); ap.add_argument("--thread", required=True); ap.add_argument("--feedback", default=None)
    rj = sub.add_parser("reject"); rj.add_argument("--thread", required=True); rj.add_argument("--feedback", required=True)
    rs = sub.add_parser("resume"); rs.add_argument("--thread", required=True)
    st = sub.add_parser("status"); st.add_argument("--thread", required=True)
    fb = sub.add_parser("feedback"); fb.add_argument("--thread", required=True); fb.add_argument("--agent", required=True); fb.add_argument("--text", required=True)

    args = p.parse_args()
    graph = build_graph()
    if args.cmd == "start":
        cmd_start(graph, args)
    elif args.cmd == "answer":
        cmd_answer(graph, args)
    elif args.cmd == "approve":
        cmd_gate(graph, args, "approve")
    elif args.cmd == "reject":
        cmd_gate(graph, args, "reject")
    elif args.cmd == "resume":
        cmd_resume(graph, args)
    elif args.cmd == "status":
        cmd_status(graph, args)
    elif args.cmd == "feedback":
        cmd_feedback(args)
