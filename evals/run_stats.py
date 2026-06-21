"""
Per-step run statistics (CEO mandate 2026-06-12, for the e2e investigation):
aggregate the trace (traces/<id>.jsonl) into per-node numbers — wall time, visits,
LLM calls, tokens in/out, LLM latency, codegen activity — plus run totals, a
chronological timeline, and the HUMAN WAIT time at pauses (the gap between a
pause-y node finishing and the next node starting across process invocations).

Deterministic; renders both markdown (canonical, agent/git-friendly) and an HTML
dashboard (review/stats.html). CLI: python -m evals.run_stats <trace.jsonl>
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

# A gap AFTER one of these nodes is (mostly) a human deciding, not compute.
PAUSE_NODES = {"ceo_qa", "prd_gate", "pr_gate"}

# Feedback kinds that mean a human REJECTED a deliverable at a gate (vs a clean approve).
GATE_REJECT_KINDS = {"prd_gate_reject", "pr_gate_reject"}
# A CTO out-of-band hand-fix the pipeline can't otherwise observe, logged via
# `live_run.py feedback` (emit_feedback(agent, "cto_handfix", ...)) — a real manual edit.
HANDFIX_KIND = "cto_handfix"


def load_events(trace_path: str) -> list:
    events = []
    p = Path(trace_path)
    if not p.exists():
        return events
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def aggregate(trace_path: str) -> dict:
    events = load_events(trace_path)
    nodes = defaultdict(lambda: {"visits": 0, "wall_ms": 0, "llm_calls": 0,
                                 "in_tokens": 0, "out_tokens": 0, "llm_ms": 0,
                                 "codegen_runs": 0, "files_written": 0})
    timeline, human_wait_ms = [], 0
    prev_end_ts, prev_node = None, None

    for e in events:
        kind = e.get("kind")
        node = e.get("node") or "(unattributed)"
        if kind == "node_exec":
            n = nodes[node]
            n["visits"] += 1
            n["wall_ms"] += e.get("wall_ms", 0)
            start_ts = e["ts"] - e.get("wall_ms", 0) / 1000
            if prev_end_ts is not None:
                gap_ms = max(0, round((start_ts - prev_end_ts) * 1000))
                if prev_node in PAUSE_NODES and gap_ms > 2000:
                    human_wait_ms += gap_ms
            timeline.append({"node": node, "ts": e["ts"],
                             "wall_ms": e.get("wall_ms", 0)})
            prev_end_ts, prev_node = e["ts"], node
        elif kind == "llm_call":
            n = nodes[node]
            n["llm_calls"] += 1
            n["in_tokens"] += e.get("in_tokens", 0)
            n["out_tokens"] += e.get("out_tokens", 0)
            n["llm_ms"] += e.get("latency_ms", 0)
        elif kind == "codegen":
            n = nodes[node]
            n["codegen_runs"] += 1
            n["files_written"] += e.get("written", 0)
            n["llm_ms"] += e.get("latency_ms", 0)

    llm_events = [e for e in events if e.get("kind") == "llm_call"]
    totals = {
        "nodes_executed": sum(n["visits"] for n in nodes.values()),
        "wall_ms": sum(n["wall_ms"] for n in nodes.values()),
        "llm_calls": len(llm_events),
        "in_tokens": sum(e.get("in_tokens", 0) for e in llm_events),
        "out_tokens": sum(e.get("out_tokens", 0) for e in llm_events),
        "llm_ms": sum(e.get("latency_ms", 0) for e in llm_events),
        "human_wait_ms": human_wait_ms,
        "elapsed_ms": round((events[-1]["ts"] - events[0]["ts"]) * 1000) if events else 0,
    }
    by_tier = defaultdict(lambda: {"calls": 0, "in_tokens": 0, "out_tokens": 0, "llm_ms": 0})
    for e in llm_events:
        t = by_tier[e.get("tier", "?")]
        t["calls"] += 1
        t["in_tokens"] += e.get("in_tokens", 0)
        t["out_tokens"] += e.get("out_tokens", 0)
        t["llm_ms"] += e.get("latency_ms", 0)

    return {"nodes": dict(nodes), "totals": totals,
            "by_tier": dict(by_tier), "timeline": timeline}


def compute_autonomy(events: list, state: dict, manual_edits: int = 0) -> dict:
    """Autonomy metric (§3.3 / I10) — how much the humans had to intervene this run, the
    number a software company actually manages. Deterministic, from the trace events +
    final state; never raises.

    An intervention is the human having to ACT beyond a clean gate sign-off:
      clarifications — CEO answers to an agent's question (`qa_log` to=ceo, answered)
      rejections     — gate rejects with content (`feedback` events, GATE_REJECT_KINDS)
      manual_edits   — CTO hand-fixes the pipeline couldn't observe: the `cto_handfix`
                       feedback events logged via `live_run.py feedback` (trace-derived)
                       PLUS any git-diff count the caller passes (a hook for un-logged
                       hand-edits the trace genuinely can't see)

      interventions = clarifications + rejections + manual_edits
      autonomy_rate = approvals / (approvals + interventions)
                      → 1.0 when the human only rubber-stamped the mandatory gates;
                        every reject / clarification / hand-fix drags it down.
    `agent_steps` (autonomous node executions) and `pauses` (human-touch node execs) are
    reported as context. Drive `interventions` to zero to push the rate to 1.0."""
    state = state or {}
    node_execs = [e for e in (events or []) if e.get("kind") == "node_exec"]
    agent_steps = sum(1 for e in node_execs if e.get("node") not in PAUSE_NODES)
    pauses = sum(1 for e in node_execs if e.get("node") in PAUSE_NODES)

    qa_log = state.get("qa_log") or []
    clarifications = sum(1 for e in qa_log if e.get("to") == "ceo" and e.get("answer"))
    rejections = sum(1 for e in (events or [])
                     if e.get("kind") == "feedback" and e.get("fb_kind") in GATE_REJECT_KINDS)
    approvals = sum(1 for k in ("prd_approved", "pr_approved") if state.get(k))
    handfixes = sum(1 for e in (events or [])
                    if e.get("kind") == "feedback" and e.get("fb_kind") == HANDFIX_KIND)
    manual_edits = max(0, int(manual_edits or 0)) + handfixes

    interventions = clarifications + rejections + manual_edits
    denom = approvals + interventions
    autonomy_rate = round(approvals / denom, 3) if denom else 1.0
    return {
        "autonomy_rate": autonomy_rate,
        "interventions": interventions,
        "clarifications": clarifications,
        "rejections": rejections,
        "manual_edits": manual_edits,
        "approvals": approvals,
        "agent_steps": agent_steps,
        "pauses": pauses,
    }


def _fmt_ms(ms: int) -> str:
    if ms >= 60000:
        return f"{ms / 60000:.1f}m"
    return f"{ms / 1000:.1f}s"


def to_markdown(stats: dict, run_id: str = "") -> str:
    t = stats["totals"]
    lines = [f"# Run statistics — {run_id}", "",
             f"- elapsed: {_fmt_ms(t['elapsed_ms'])} (compute {_fmt_ms(t['wall_ms'])}, "
             f"human wait ~{_fmt_ms(t['human_wait_ms'])})",
             f"- node executions: {t['nodes_executed']}",
             f"- LLM calls: {t['llm_calls']} — {t['in_tokens']} in / "
             f"{t['out_tokens']} out tokens, {_fmt_ms(t['llm_ms'])} model time", "",
             "## Per node", "",
             "| node | visits | wall | llm calls | tok in | tok out | llm time | codegen | files |",
             "|---|---|---|---|---|---|---|---|---|"]
    ranked = sorted(stats["nodes"].items(), key=lambda kv: -kv[1]["wall_ms"])
    for name, n in ranked:
        lines.append(f"| {name} | {n['visits']} | {_fmt_ms(n['wall_ms'])} | "
                     f"{n['llm_calls']} | {n['in_tokens']} | {n['out_tokens']} | "
                     f"{_fmt_ms(n['llm_ms'])} | {n['codegen_runs']} | {n['files_written']} |")
    lines += ["", "## Per tier", "", "| tier | calls | tok in | tok out | llm time |",
              "|---|---|---|---|---|"]
    for tier, d in sorted(stats["by_tier"].items()):
        lines.append(f"| {tier} | {d['calls']} | {d['in_tokens']} | "
                     f"{d['out_tokens']} | {_fmt_ms(d['llm_ms'])} |")
    lines += ["", "## Timeline", ""]
    for step in stats["timeline"]:
        lines.append(f"- {step['node']}: {_fmt_ms(step['wall_ms'])}")
    return "\n".join(lines) + "\n"


def render(trace_path: str, run_id: str, workspace_root=None) -> dict:
    """Aggregate + write stats.md (run dir) and review/stats.html. Never raises."""
    out = {}
    try:
        from tools import report_html
        from tools.file_io import WORKSPACE_ROOT
        root = Path(workspace_root) if workspace_root else WORKSPACE_ROOT
        stats = aggregate(trace_path)
        md = to_markdown(stats, run_id)
        md_path = root / run_id / "stats.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
        out["md"] = str(md_path)
        html_path = root / run_id / "review" / "stats.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(
            report_html._page(f"Run statistics — {run_id}",
                              f"<div class=\"card\">{report_html._md(md)}</div>"),
            encoding="utf-8")
        out["html"] = str(html_path)
        out["stats"] = stats
    except Exception:
        pass
    return out


if __name__ == "__main__":
    path = sys.argv[1]
    rid = Path(path).stem
    print(to_markdown(aggregate(path), rid))
