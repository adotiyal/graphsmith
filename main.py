"""
main.py — your interface as CEO.

Usage:
    python main.py                  # start a new feature
    python main.py --resume <id>    # resume an interrupted run

DESIGN DECISION: thread_id = project_id.
LangGraph uses thread_id to namespace checkpoints. Using project_id
means each feature is an independent resumable thread. You can have
multiple features in flight; they don't share state.

DESIGN DECISION: you type your requirement once, at the start.
The interrupt fires BEFORE the CEO node runs. You inject feature_request
into the state update, then invoke(None) to resume. LangGraph resumes
from the checkpoint with your input in state.
"""

import argparse
import os
import uuid
import re
from graph.graph import build_graph
from graph.state import ProjectState
from tools import product, project_ctx, trace, registry
from evals import overseer


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def run_new_feature(graph, target_repo: str = None, design_source: str = None):
    print("\n=== CEO INPUT ===")

    # Project continuity: unless you point at an external --repo, every run targets the
    # SAME persistent project (workspace/project). The first run seeds it; later runs
    # auto-extend it, so the agents accumulate the real codebase + the feature ledger.
    managed = target_repo is None
    ledger = ""
    if managed:
        project_ctx.ensure_repo()
        target_repo = str(project_ctx.project_dir())
        ledger = project_ctx.load_ledger()
        if project_ctx.has_code():
            print(f"[project] extending the existing project at {target_repo}")
            print(f"[project] {ledger.count(chr(10) + '## ')} feature(s) already built")
        else:
            print(f"[project] starting a NEW project at {target_repo}")
    else:
        print(f"[external repo] target: {target_repo}")

    print("Describe the feature you want to build:")
    feature_request = input("> ").strip()

    # One-time product profile: standing context Design + PM reason from (CEO/CTO sets once).
    if not product.has_profile():
        print("\n=== PRODUCT PROFILE (one-time setup) ===")
        print("Tell me about the product so Design and PM have real context — a few lines on:")
        print("  • product category & what it is   • target users / customer base")
        print("  • key use cases                   • brand & tone (e.g. playful vs enterprise)")
        print("  • business goals / what success looks like")
        print("End with an empty line:")
        lines = []
        while True:
            ln = input()
            if ln.strip() == "":
                break
            lines.append(ln)
        if any(l.strip() for l in lines):
            product.save_profile("\n".join(lines))
            print("[+] Product profile saved to product/profile.md (reused across features).")

    project_id = slugify(feature_request) + "-" + uuid.uuid4().hex[:6]
    thread_id = project_id

    # Initial state - all optional fields start as None/False/0
    initial_state: ProjectState = {
        "project_id": project_id,
        "feature_request": feature_request,
        "prd_path": None,
        "design_path": None,
        "code_path": None,
        "deploy_path": None,
        "tests_passed": False,
        "deployed": False,
        "pr_url": None,
        "deploy_url": None,
        "fix_attempts": 0,
        "error_log": None,
        "current_node": "start",
        "change_type": None,
        "qa_log": [],
        "qa_rounds": {},
        "agent_qa_counts": {},
        "ceo_qa_pending": None,
        "ceo_qa_from": None,
        "ceo_qa_answer": None,
        "test_path": None,
        "review_attempts": {},
        "review_notes": None,
        "review_action": None,
        "prd_approved": False,
        "pr_approved": False,
        "approval_pending": None,
        "approval_decision": None,
        "approval_feedback": None,
        "tech_stack": None,
        "tech_stack_confirmed": False,
        "target_repo": target_repo,
        "repo_map_path": None,
        "detected_stack": None,
        "design_source": design_source,
        "managed_project": managed,
        "project_ledger": ledger or None,
        "test_files": [],
        "security_warnings": [],
        "code_files": [],
        "product_profile": product.load_profile() or None,
        "product_invariants": registry.extract_product_invariants(target_repo) or None,
        "design_mockup_path": None,
        "design_spec_path": None,
        "integration_passed": False,
        "app_screenshot_path": None,
        "design_qa_passed": False,
        "design_qa_attempts": 0,
        "design_component_files": [],
        "components_manifest_path": None,
        "integration_attempts": 0,
        "e2e_files": [],
    }

    config = {"configurable": {"thread_id": thread_id}}
    trace.start(project_id)   # observability: record this run to traces/<id>.jsonl

    print(f"\n[+] Project ID: {project_id}")
    print("[+] Starting pipeline...\n")

    # First invoke hits the interrupt_before=["ceo"] and pauses.
    # We pass state here; the graph saves it and waits.
    graph.invoke(initial_state, config)

    # Resume from interrupt - state already has feature_request
    # DESIGN DECISION: we don't re-ask for input here. The interrupt
    # fires before CEO runs, and CEO reads from state["feature_request"].
    # If you want to modify the request before CEO runs, this is where to do it.
    _run_to_completion(graph, config, project_id)

    # Record this feature in the project ledger so the next run has the history.
    if managed:
        final = graph.get_state(config).values
        project_ctx.append_ledger(feature_request, final)
        print("[project] recorded in ledger; next run will build on this.")


def resume_feature(graph, project_id: str):
    """Resume a previously interrupted run."""
    config = {"configurable": {"thread_id": project_id}}
    trace.start(project_id)
    print(f"\n[+] Resuming project: {project_id}")
    _run_to_completion(graph, config, project_id)


def _run_to_completion(graph, config: dict, project_id: str):
    """
    Stream graph events, handling both normal node completions and CEO Q&A interrupts.

    DESIGN DECISION: loop until no pending nodes remain.
    Each iteration streams until interrupted or done. If interrupted for CEO Q&A,
    we print the questions, read the answer, inject it via update_state, and resume.
    If interrupted for the initial CEO node (first run only), we just resume.
    """
    while True:
        for event in graph.stream(None, config, stream_mode="updates"):
            for node_name, updates in event.items():
                if node_name == "__interrupt__":
                    continue
                current = updates.get("current_node", node_name)
                trace.emit("node", node=node_name, change_type=updates.get("change_type"))
                print(f"[{current}] ✓ done")

                if updates.get("prd_path"):
                    print(f"  → PRD: {updates['prd_path']}")
                if updates.get("design_path"):
                    print(f"  → Spec: {updates['design_path']}")
                if updates.get("design_mockup_path"):
                    print(f"  → Mockup: {updates['design_mockup_path']}  (open in a browser)")
                if updates.get("code_path"):
                    print(f"  → Code: {updates['code_path']}")
                if "tests_passed" in updates:
                    status = "✓ PASSED" if updates["tests_passed"] else "✗ FAILED"
                    print(f"  → Tests: {status}")
                if updates.get("pr_url"):
                    print(f"  → PR: {updates['pr_url']}")
                if updates.get("error_log") and not updates.get("tests_passed"):
                    print(f"  → Error (first 200 chars): {updates['error_log'][:200]}")
                if updates.get("ceo_qa_pending"):
                    from_agent = updates.get("ceo_qa_from", "an agent")
                    print(f"  → {from_agent.upper()} has questions for CEO")
                if updates.get("approval_pending"):
                    print(f"  → Awaiting CEO approval: {updates['approval_pending'].upper()}")
                if updates.get("test_path"):
                    print(f"  → Tests: {updates['test_path']}")

        # Check whether the graph is paused at an interrupt or truly done
        snapshot = graph.get_state(config)
        if not snapshot.next:
            break  # pipeline complete

        values = snapshot.values
        if values.get("ceo_qa_pending"):
            _handle_ceo_qa(graph, config, values)
        elif values.get("approval_pending"):
            _handle_approval(graph, config, values, project_id)
        # else: initial ceo interrupt or similar — just resume on the next loop

    print(f"\n[+] Pipeline complete. Workspace: workspace/{project_id}/")

    # Overseer: audit the finished run (deterministic invariants + loop/budget checks).
    final = graph.get_state(config).values
    totals = trace.get().totals() if trace.get() else None
    # Autonomy metric (§3.3 / I10): human interventions per run, surfaced in the overseer
    # report + flight recorder. Computed once from the trace, passed to both.
    _auton = None
    if trace.get():
        from evals import run_stats as _rs
        _auton = _rs.compute_autonomy(_rs.load_events(str(trace.get().path)), final)
    report = overseer.oversee(final, totals, autonomy=_auton)
    from tools import learnings as _learnings
    if trace.get():
        _retro = _learnings.run_retro(str(trace.get().path), final)
        if _retro:
            print("── Retro lessons recorded ──")
            for _agent, _lessons in _retro.items():
                for _l in _lessons:
                    print(f"  {_agent}: {_l}")
        from tools import report_html as _rh
        _audit = _rh.render_audit(final, str(trace.get().path), retro=_retro, overseer=report)
        if _audit:
            print(f"Audit folder: file://{_audit}")
        _flight = _rh.render_run(final, str(trace.get().path), overseer=report, autonomy=_auton)
        if _flight:
            print(f"Flight recorder: file://{_flight}")
    print("\n" + overseer.format_report(report))
    if totals:
        print(f"  (trace: {totals['llm_calls']} LLM calls, "
              f"{totals['in_tokens'] + totals['out_tokens']} tokens → traces/{project_id}.jsonl)")
    if not report["ok"]:
        print("  ⚠️  This run has HIGH-severity findings — review before trusting it.")


def _handle_ceo_qa(graph, config: dict, values: dict):
    """A node paused with questions for the CEO. Collect the answer and resume."""
    from_agent = values.get("ceo_qa_from", "an agent")
    print(f"\n{'='*50}")
    print(f"QUESTION FROM {from_agent.upper()}  (you answer as CEO/CTO)")
    print(f"{'='*50}")
    print(values.get("ceo_qa_pending"))
    print()
    answer = input("CEO/CTO> ").strip()
    graph.update_state(config, {"ceo_qa_answer": answer})


def _handle_approval(graph, config: dict, values: dict, project_id: str):
    """A blocking approval gate (PRD or PR). Show the artifact, collect approve/reject."""
    stage = values.get("approval_pending")
    print(f"\n{'='*50}")
    print(f"CEO/CTO APPROVAL REQUIRED: {stage.upper()}")
    print(f"{'='*50}")
    from tools import report_html
    review = report_html.render_gate(dict(values), stage)
    if review:
        print(f"📄 Review dashboard: file://{review}")

    if stage == "prd":
        print(_read_file(values.get("prd_path")))
    elif stage == "pr":
        report_path = f"workspace/{project_id}/tests/qa_report.md"
        print(_read_file(report_path))
        security = values.get("security_warnings") or []
        if security:
            print("\n⚠️  SECURITY SCAN FINDINGS:")
            for s in security:
                print(f"   - {s}")
        if values.get("code_path"):
            print(f"\n(Code: {values['code_path']})")

    print()
    decision = input("Approve? [y]es / [n]o: ").strip().lower()
    if decision in ("n", "no", "reject"):
        feedback = input("What needs to change? ").strip()
        graph.update_state(config, {"approval_decision": "reject", "approval_feedback": feedback})
    else:
        graph.update_state(config, {"approval_decision": "approve", "approval_feedback": None})


def _read_file(path: str) -> str:
    if not path:
        return "(nothing to show)"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return f"(could not read {path})"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", type=str, help="Project ID to resume")
    parser.add_argument("--repo", type=str, default=None,
                        help="Path to an existing repo to EXTEND (omit for greenfield)")
    parser.add_argument("--design-source", type=str, default=None,
                        help="Local dir OR git URL of an existing design (HTML mockups) to "
                             "REUSE — Design matches it and skips the 3-directions pick")
    args = parser.parse_args()

    graph = build_graph()

    if args.resume:
        resume_feature(graph, args.resume)
    else:
        repo = os.path.abspath(args.repo) if args.repo else None
        if repo and not os.path.isdir(repo):
            parser.error(f"--repo path is not a directory: {repo}")
        # A local design source is abspath'd; a git URL is passed through untouched.
        design_source = args.design_source
        if design_source and os.path.isdir(design_source):
            design_source = os.path.abspath(design_source)
        run_new_feature(graph, target_repo=repo, design_source=design_source)
