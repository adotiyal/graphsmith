"""
tools/trace.py — lightweight run tracing (observability foundation)
-------------------------------------------------------------------
You can't oversee what you can't see. This records a structured trace of every run:
node transitions and every LLM call (tier, tokens, latency). Written to
traces/<run_id>.jsonl so a run is fully replayable/auditable after the fact, and the
overseer can compute budget totals from it.

DESIGN DECISION: a single global tracer + module-level emit().
- `call_llm` and `main.py` call `trace.emit(...)` with no plumbing.
- When no tracer is active (e.g. unit tests with a mocked LLM), emit() is a no-op.
- Failures to write a trace never break a run (best-effort).
"""

import json
import time
from pathlib import Path

TRACE_DIR = Path(__file__).parent.parent / "traces"
_current = None  # the active Tracer, or None


class Tracer:
    def __init__(self, run_id: str, trace_dir=None):
        self.run_id = run_id
        self.dir = Path(trace_dir) if trace_dir else TRACE_DIR
        self.events = []

    @property
    def path(self) -> Path:
        return self.dir / f"{self.run_id}.jsonl"

    def emit(self, kind: str, **data) -> dict:
        ev = {"ts": round(time.time(), 3), "kind": kind, **data}
        self.events.append(ev)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError:
            pass  # tracing must never break a run
        return ev

    def totals(self) -> dict:
        calls = [e for e in self.events if e["kind"] == "llm_call"]
        return {
            "llm_calls": len(calls),
            "in_tokens": sum(e.get("in_tokens", 0) for e in calls),
            "out_tokens": sum(e.get("out_tokens", 0) for e in calls),
            "latency_ms": sum(e.get("latency_ms", 0) for e in calls),
            "nodes": len([e for e in self.events if e["kind"] == "node"]),
        }


_current_node = None   # which graph node is executing (attributes llm/codegen events)


def current_node():
    return _current_node


def traced(name: str, fn):
    """Wrap a graph node fn: every execution emits `node_exec` (wall-clock ms) and
    everything the node triggers (llm_call, codegen) is attributed to it via
    current_node(). The per-step statistics the e2e investigation needs."""
    def _wrapped(state):
        global _current_node
        prev = _current_node
        _current_node = name
        t0 = time.perf_counter()
        try:
            return fn(state)
        finally:
            emit("node_exec", node=name,
                 wall_ms=round((time.perf_counter() - t0) * 1000))
            _current_node = prev
    return _wrapped


def start(run_id: str, trace_dir=None) -> Tracer:
    global _current
    _current = Tracer(run_id, trace_dir)
    return _current


def get():
    return _current


def emit(kind: str, **data):
    return _current.emit(kind, **data) if _current is not None else None


def reset():
    global _current
    _current = None
