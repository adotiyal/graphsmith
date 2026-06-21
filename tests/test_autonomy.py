"""
Autonomy metric (§3.3 / I10) — human interventions per run, the number a software company
manages. Deterministic, computed from the trace events + final state.

`compute_autonomy` is unit-tested across the cases that matter (a clean rubber-stamp run
scores 1.0; rejects/clarifications/manual-edits drag it down; pause nodes don't count as
agent steps; empty input is safe); the overseer surfaces it as a non-failing info finding
(backward compatible when absent); the flight recorder renders the autonomy card.
"""

from evals import run_stats, overseer
from tools import report_html


_TS = [100.0]


def _exec(node):
    _TS[0] += 1
    return {"kind": "node_exec", "node": node, "wall_ms": 10, "ts": _TS[0]}


def _reject(kind):
    _TS[0] += 1
    return {"kind": "feedback", "fb_kind": kind, "agent": "x", "text": "fix it", "ts": _TS[0]}


# ── compute_autonomy ─────────────────────────────────────────────────────────

def test_clean_run_is_fully_autonomous():
    # Only the mandatory gate approvals; no clarifications, no rejects.
    events = [_exec("pm"), _exec("engineer"), _exec("qa"),
              _exec("prd_gate"), _exec("pr_gate")]
    state = {"prd_approved": True, "pr_approved": True, "qa_log": []}
    a = run_stats.compute_autonomy(events, state)
    assert a["autonomy_rate"] == 1.0
    assert a["interventions"] == 0
    assert a["approvals"] == 2
    assert a["agent_steps"] == 3          # pm/engineer/qa — gates are pauses, not steps
    assert a["pauses"] == 2


def test_rejects_and_clarifications_lower_the_rate():
    events = [_exec("pm"), _exec("engineer"),
              _reject("pr_gate_reject"), _reject("pr_gate_reject")]
    state = {
        "pr_approved": True,                      # one clean approval in the end
        "qa_log": [
            {"from": "architect", "to": "ceo", "question": "q", "answer": "a"},  # clarified
            {"from": "design", "to": "ceo", "question": "q2"},                   # unanswered → not counted
            {"from": "pm", "to": "design", "question": "peer"},                  # not a CEO touch
        ],
    }
    a = run_stats.compute_autonomy(events, state)
    assert a["clarifications"] == 1
    assert a["rejections"] == 2
    assert a["interventions"] == 3
    assert a["approvals"] == 1
    assert a["autonomy_rate"] == round(1 / 4, 3)   # approvals / (approvals + interventions)


def test_manual_edits_count_as_interventions():
    a = run_stats.compute_autonomy([_exec("engineer")],
                                   {"pr_approved": True}, manual_edits=2)
    assert a["manual_edits"] == 2
    assert a["interventions"] == 2
    assert a["autonomy_rate"] == round(1 / 3, 3)


def test_cto_handfix_feedback_counts_as_a_manual_edit():
    # A hand-fix logged via `live_run.py feedback` is trace-derived; the git-diff param adds on top.
    events = [_exec("engineer"), _reject("cto_handfix")]   # _reject just builds a feedback event
    a = run_stats.compute_autonomy(events, {"pr_approved": True}, manual_edits=1)
    assert a["manual_edits"] == 2                          # 1 logged hand-fix + 1 passed-in
    assert a["interventions"] == 2


def test_empty_inputs_are_safe():
    a = run_stats.compute_autonomy([], {})
    assert a["autonomy_rate"] == 1.0           # nothing required a human → vacuously autonomous
    assert a["interventions"] == 0 and a["approvals"] == 0 and a["agent_steps"] == 0


def test_negative_manual_edits_clamped():
    a = run_stats.compute_autonomy([], {}, manual_edits=-5)
    assert a["manual_edits"] == 0


# ── overseer surfacing ───────────────────────────────────────────────────────

def test_overseer_adds_info_finding_when_autonomy_given():
    auton = run_stats.compute_autonomy([_exec("engineer"), _reject("pr_gate_reject")],
                                       {"pr_approved": True})
    report = overseer.oversee({"code_files": []}, {}, autonomy=auton)
    f = next((f for f in report["findings"] if f["check"] == "autonomy_rate"), None)
    assert f is not None and f["severity"] == "info" and f["ok"] is True
    assert "intervention" in f["detail"]
    # info findings never flip the trustworthy verdict
    assert report["ok"] is True


def test_overseer_omits_autonomy_finding_when_absent():
    report = overseer.oversee({"code_files": []}, {})       # backward compatible
    assert not any(f["check"] == "autonomy_rate" for f in report["findings"])


# ── flight recorder card ─────────────────────────────────────────────────────

def test_render_run_shows_autonomy_card(tmp_path, ws):
    import json
    events = [_exec("engineer"), _exec("qa"), _reject("pr_gate_reject")]
    trace_path = tmp_path / "run.jsonl"
    trace_path.write_text("\n".join(json.dumps(e) for e in events))
    state = {"project_id": "proj", "feature_request": "demo", "pr_approved": True,
             "qa_log": [{"from": "pm", "to": "ceo", "question": "q", "answer": "a"}]}
    out = report_html.render_run(state, str(trace_path), overseer={"ok": True})
    assert out and out.endswith("run.html")
    html = (ws / "proj" / "review" / "run.html").read_text()
    assert "Autonomy" in html and "autonomy rate" in html
    assert "human interventions" in html
