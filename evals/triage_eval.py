"""
evals/triage_eval.py — accuracy eval for the Triage classifier
--------------------------------------------------------------
Triage is a classifier, so it gets a classic ML eval: a labeled dataset → accuracy +
a confusion matrix. This is the cheapest high-signal eval and proves the harness works.

- `evaluate(classify, dataset)` is pure/deterministic — testable with a fake classifier.
- `run_live()` runs the REAL triage agent (needs ANTHROPIC_API_KEY) over the dataset:
      ANTHROPIC_API_KEY=... python -m evals.triage_eval

To turn a production misclassification into a regression test, append the case to
datasets/triage.jsonl.
"""

import json
from collections import defaultdict
from pathlib import Path

DATASET = Path(__file__).parent / "datasets" / "triage.jsonl"


def load_dataset(path=DATASET) -> list:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(classify, dataset: list) -> dict:
    """classify: request_str -> label. Returns accuracy, per-label and confusion."""
    correct = 0
    results = []
    confusion = defaultdict(int)
    per_label = defaultdict(lambda: {"n": 0, "correct": 0})
    for case in dataset:
        pred = classify(case["request"])
        exp = case["expected"]
        ok = pred == exp
        correct += ok
        per_label[exp]["n"] += 1
        per_label[exp]["correct"] += ok
        confusion[(exp, pred)] += 1
        results.append({"request": case["request"], "expected": exp, "pred": pred, "ok": ok})
    n = len(dataset)
    return {
        "accuracy": correct / n if n else 0.0,
        "n": n, "correct": correct,
        "per_label": {k: v for k, v in per_label.items()},
        "confusion": dict(confusion),
        "results": results,
    }


def run_live() -> dict:
    from agents import triage
    dataset = load_dataset()
    classify = lambda req: triage.run({"feature_request": req, "prd_path": None})["change_type"]
    report = evaluate(classify, dataset)
    print(f"Triage accuracy: {report['accuracy']:.0%}  ({report['correct']}/{report['n']})")
    for label, s in sorted(report["per_label"].items()):
        print(f"  {label:9s} {s['correct']}/{s['n']}")
    misses = [(e, p, c) for (e, p), c in report["confusion"].items() if e != p]
    for exp, pred, c in sorted(misses):
        print(f"  MISS  expected={exp} got={pred}  ×{c}")
    return report


if __name__ == "__main__":
    run_live()
