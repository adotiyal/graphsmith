# evals/ — overseeing the agent orchestration

Two jobs, kept separate:

1. **Offline evals** — *is the output good?* Dataset-driven, graded, run before shipping a
   prompt/model change (regression gate).
2. **Online overseer** — *are the agents behaving correctly on this run?* Deterministic
   runtime guardrails that audit a finished run and flag anything untrustworthy.

This is the first, cheap, high-signal slice: **tracing → triage eval → overseer**.

## Components

| File | What it is |
|---|---|
| `tools/trace.py` | Per-run trace (node transitions + every LLM call w/ tokens & latency) → `traces/<id>.jsonl`. `call_llm` and `main.py` write to it. |
| `evals/overseer.py` | Deterministic checks on a run's final state: **invariants** (engineer didn't touch tests, feature has a PRD, stack confirmed, no silent red ship), **loop** detection (caps hit without converging), **budget** (tokens/calls). `main.py` runs it at the end of every run. |
| `evals/triage_eval.py` | Accuracy + confusion matrix for the Triage classifier over a labeled dataset. |
| `evals/datasets/triage.jsonl` | Labeled triage cases (request → expected change_type). |

## Run

```bash
# Overseer + tracing run automatically on every `python main.py` (printed at the end).

# Triage accuracy (real LLM — needs the key):
ANTHROPIC_API_KEY=... python -m evals.triage_eval

# The harness + overseer logic are unit-tested (no key needed):
pytest tests/test_evals.py -q
```

## Growing this (the roadmap)

- **Turn prod failures into cases.** Every misclassification → a line in `triage.jsonl`;
  every bad artifact → a case in a per-agent dataset. (This is what `learnings/` does for
  the engineer; formalize it for evals.)
- **Per-agent quality evals** (extend `tests/test_live_eval.py`): rubric LLM-judge graders,
  reusing the in-band checks (`critic`, `scan_security`, `validate_*`) as graders. Run each
  case K times for mean±std (LLMs are stochastic).
- **End-to-end golden tasks** with a **held-out** acceptance suite (never the tests the
  agent wrote) → task success rate + trajectory assertions.
- **LLM overseer**: an independent model that reviews the trace + artifacts for off-scope
  or low-quality steps; per-node real-time halting; confidence-gated selective human review.
- **CI regression gate**: fail a prompt/model change if eval scores drop.
