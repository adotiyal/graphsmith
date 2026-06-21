"""
Eval harness + overseer + tracer — all deterministic (no API key needed).
"""

import json
from evals import overseer, triage_eval
from tools import trace as tracemod
from conftest import base_state


# ── Tracer ────────────────────────────────────────────────────────────────────

def test_tracer_writes_and_totals(tmp_path):
    t = tracemod.Tracer("run1", tmp_path)
    t.emit("node", node="ceo")
    t.emit("llm_call", tier="fast", in_tokens=100, out_tokens=50, latency_ms=200)
    t.emit("llm_call", tier="strong", in_tokens=300, out_tokens=80, latency_ms=400)
    assert t.totals() == {"llm_calls": 2, "in_tokens": 400, "out_tokens": 130,
                          "latency_ms": 600, "nodes": 1}
    lines = (tmp_path / "run1.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["kind"] == "node"


def test_trace_emit_is_noop_without_active_tracer():
    tracemod.reset()
    assert tracemod.emit("llm_call", in_tokens=1) is None


# ── Overseer invariants ───────────────────────────────────────────────────────

def test_overseer_flags_engineer_writing_tests():
    rep = overseer.oversee(base_state(code_files=["/p/src/main.py", "/p/tests/test_x.py"]))
    f = next(x for x in rep["findings"] if x["check"] == "engineer_protected_tests")
    assert f["ok"] is False and f["severity"] == "high"
    assert rep["ok"] is False                                  # high-severity → not trustworthy


def test_overseer_clean_full_lane_is_trustworthy():
    rep = overseer.oversee(
        base_state(change_type="feature", code_files=["/p/src/main.py"],
                   prd_path="x", tech_stack_confirmed=True),
        totals={"in_tokens": 5000, "out_tokens": 2000, "llm_calls": 10})
    assert rep["ok"] is True


def test_overseer_feature_missing_prd():
    rep = overseer.oversee(base_state(change_type="feature", code_files=["/p/src/a.py"],
                                      prd_path=None, tech_stack_confirmed=True))
    assert any(x["check"] == "feature_has_prd" and not x["ok"] for x in rep["findings"])


def test_overseer_catches_silent_red_ship():
    rep = overseer.oversee(base_state(code_files=["/p/src/a.py"], pr_url="http://pr/1",
                                      tests_passed=False, fix_attempts=1,
                                      prd_path="x", tech_stack_confirmed=True))
    f = next(x for x in rep["findings"] if x["check"] == "no_silent_red_ship")
    assert f["ok"] is False and rep["ok"] is False


# ── Overseer loops + budget ───────────────────────────────────────────────────

def test_overseer_loop_not_converged():
    rep = overseer.oversee(base_state(tests_passed=False, fix_attempts=overseer.MAX_FIX_ATTEMPTS))
    assert any(x["check"] == "engineer_qa_converged" and not x["ok"] for x in rep["findings"])
    assert rep["ok"] is False


def test_overseer_budget_exceeded():
    rep = overseer.oversee(base_state(), totals={"in_tokens": 500_000, "out_tokens": 0, "llm_calls": 80})
    assert any(x["check"] == "token_budget" and not x["ok"] for x in rep["findings"])
    assert any(x["check"] == "call_budget" and not x["ok"] for x in rep["findings"])


# ── Triage eval ───────────────────────────────────────────────────────────────

def test_triage_eval_scores_and_confusion():
    ds = [{"request": "add login", "expected": "feature"},
          {"request": "fix crash", "expected": "bugfix"},
          {"request": "rename module", "expected": "refactor"},
          {"request": "bump deps", "expected": "chore"}]

    def fake(req):
        for kw, lbl in [("fix", "bugfix"), ("rename", "refactor"), ("bump", "chore")]:
            if kw in req:
                return lbl
        return "feature"

    rep = triage_eval.evaluate(fake, ds)
    assert rep["accuracy"] == 1.0 and rep["n"] == 4
    assert rep["per_label"]["bugfix"]["correct"] == 1


def test_triage_eval_reports_misclassifications():
    ds = [{"request": "add login", "expected": "feature"},
          {"request": "fix the crash", "expected": "bugfix"}]
    rep = triage_eval.evaluate(lambda r: "feature", ds)   # always says feature
    assert rep["accuracy"] == 0.5
    assert rep["confusion"][("bugfix", "feature")] == 1   # the miss is recorded


def test_triage_dataset_is_valid():
    ds = triage_eval.load_dataset()
    assert len(ds) >= 12
    valid = {"feature", "bugfix", "refactor", "chore"}
    assert all(c["expected"] in valid and c["request"].strip() for c in ds)
