"""Self-improvement now reaches the CTO-in-the-loop cases. Before, only a gate REJECT
emitted feedback, so a QA NO-GO the CTO ADJUDICATED or HAND-FIXED (e.g. the phase-3 badge
tier never recomputed) taught the retro nothing. Now: a NO-GO sign-off emits a feedback
event regardless of the gate outcome, and the operator can log an out-of-band hand-fix.
run_retro still GENERALISES the app-specific finding into a product-agnostic rule."""
import argparse

import tools.learnings
import tools.trace
from agents import qa

NOGO = ("## Features Verified\n- AC-1 ok\n- AC-2 ok\n"
        "## Code Review\n- A derived field is not recomputed on the event that changes it.\n"
        "## Go / No-Go\n**NO-GO** — recompute the derived value on every mutating event.")
GO = "## Code Review\n- clean\n## Go / No-Go\n**GO** — ships."


def test_nogo_emits_feedback_to_engineer_and_qa(monkeypatch):
    calls = []
    monkeypatch.setattr(tools.learnings, "emit_feedback", lambda a, k, t: calls.append((a, k, t)))
    assert qa._emit_nogo_feedback(NOGO) is True
    assert {a for a, _, _ in calls} == {"engineer", "qa"}     # bug class + test blind spot
    assert all("recompute" in t for _, _, t in calls)         # the finding, not the AC checklist


def test_go_emits_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(tools.learnings, "emit_feedback", lambda a, k, t: calls.append((a, k, t)))
    assert qa._emit_nogo_feedback(GO) is False
    assert calls == []


def test_blocking_finding_is_the_verdict_not_the_ac_checklist():
    f = qa._blocking_finding(NOGO)
    assert "NO-GO" in f and "recompute" in f
    assert "AC-1 ok" not in f                                  # skips the verbose AC checklist


def test_operator_feedback_command_emits_a_cto_handfix_event(monkeypatch):
    import live_run
    calls = []
    monkeypatch.setattr(tools.trace, "start", lambda t: None)
    monkeypatch.setattr(tools.learnings, "emit_feedback", lambda a, k, t: calls.append((a, k, t)))
    live_run.cmd_feedback(argparse.Namespace(
        thread="t1", agent="engineer", text="recompute derived fields on mutating events"))
    assert calls == [("engineer", "cto_handfix", "recompute derived fields on mutating events")]
