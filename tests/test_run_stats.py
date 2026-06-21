"""Per-step run statistics (e2e investigation record): node timing wrapper,
event attribution, aggregation (incl. human wait at pauses), and rendering."""

import json

from evals import run_stats
from tools import trace, report_html


def test_traced_wrapper_times_and_attributes(tmp_path):
    tr = trace.start("t1", trace_dir=tmp_path)
    seen = {}

    def node_fn(state):
        seen["node"] = trace.current_node()
        trace.emit("llm_call", tier="fast", node=trace.current_node(),
                   in_tokens=5, out_tokens=7, latency_ms=10)
        return {"ok": True}

    wrapped = trace.traced("design", node_fn)
    out = wrapped({})
    trace.reset()

    assert out == {"ok": True}
    assert seen["node"] == "design"
    kinds = [e["kind"] for e in tr.events]
    assert "node_exec" in kinds
    exec_ev = next(e for e in tr.events if e["kind"] == "node_exec")
    assert exec_ev["node"] == "design" and exec_ev["wall_ms"] >= 0
    assert trace.current_node() is None        # cleared after execution


def _write_trace(tmp_path):
    events = [
        {"ts": 100.0, "kind": "llm_call", "tier": "fast", "node": "ceo",
         "in_tokens": 10, "out_tokens": 20, "latency_ms": 500},
        {"ts": 101.0, "kind": "node_exec", "node": "ceo", "wall_ms": 1000},
        {"ts": 103.0, "kind": "node_exec", "node": "ceo_qa", "wall_ms": 100},
        # 60s gap after the pause node = the human deciding
        {"ts": 164.0, "kind": "llm_call", "tier": "strong", "node": "design",
         "in_tokens": 100, "out_tokens": 900, "latency_ms": 30000},
        {"ts": 195.0, "kind": "codegen", "node": "engineer", "written": 3,
         "latency_ms": 26000},
        {"ts": 196.0, "kind": "node_exec", "node": "design", "wall_ms": 31000},
        {"ts": 230.0, "kind": "node_exec", "node": "engineer", "wall_ms": 30000},
    ]
    p = tmp_path / "run.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events))
    return str(p)


def test_aggregate_per_node_totals_and_human_wait(tmp_path):
    stats = run_stats.aggregate(_write_trace(tmp_path))
    assert stats["nodes"]["ceo"]["llm_calls"] == 1
    assert stats["nodes"]["ceo"]["in_tokens"] == 10
    assert stats["nodes"]["design"]["out_tokens"] == 900
    assert stats["nodes"]["engineer"]["codegen_runs"] == 1
    assert stats["nodes"]["engineer"]["files_written"] == 3
    t = stats["totals"]
    assert t["llm_calls"] == 2 and t["out_tokens"] == 920
    assert t["nodes_executed"] == 4
    assert t["human_wait_ms"] >= 55000          # the post-pause gap is the human
    assert stats["by_tier"]["strong"]["calls"] == 1


def test_markdown_and_render(tmp_path, ws):
    trace_path = _write_trace(tmp_path)
    md = run_stats.to_markdown(run_stats.aggregate(trace_path), "run-x")
    assert "| design |" in md and "Per tier" in md and "human wait" in md
    out = run_stats.render(trace_path, "proj")
    assert out["md"].endswith("stats.md") and out["html"].endswith("stats.html")
    assert "design" in (ws / "proj" / "stats.md").read_text()
    assert (ws / "proj" / "review" / "stats.html").exists()


def test_render_run_flight_recorder(tmp_path, ws):
    trace_path = _write_trace(tmp_path)
    state = {"project_id": "proj", "feature_request": "demo feature",
             "tests_passed": True, "pr_approved": False}
    out = report_html.render_run(state, trace_path, overseer={"ok": True})
    assert out and out.endswith("run.html")
    html = (ws / "proj" / "review" / "run.html").read_text()
    # the visual layer the text-dump stats.html lacks
    assert "Run summary" in html and "Path taken" in html
    assert "Model spend by tier" in html
    assert "TRUSTWORTHY" in html                       # overseer badge rendered
    assert "design" in html and "engineer" in html      # path chips by node
    assert 'class="bar"' in html and 'class="chip"' in html


def test_render_run_graceful_without_trace(ws):
    # no trace yet → must still produce a page, never raise
    out = report_html.render_run({"project_id": "p2", "feature_request": "x"}, "")
    assert out and (ws / "p2" / "review" / "run.html").exists()
