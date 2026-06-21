"""
tools/learnings.py  (Phase 2.2 — cross-run learning)
----------------------------------------------------
Institutional memory that PERSISTS ACROSS pipeline runs. When a run hits a failure,
the lesson is distilled and recorded here; future runs load it into the relevant agent's
system prompt so the company stops repeating the same mistakes.

DESIGN DECISION: separate from skills/.
- skills/<agent>.md = curated, human-authored domain knowledge (stable).
- learnings/<agent>.md = machine-accumulated lessons from real failures (grows over time).
Keeping them separate means auto-generated lessons never corrupt the curated skill, and
either can be inspected or reset independently.

DESIGN DECISION: bounded + deduped.
- Exact-substring dedupe so the same lesson isn't recorded twice across retries/runs.
- Capped at MAX_LEARNINGS_CHARS (oldest trimmed first) so prompts stay lean.
"""

from pathlib import Path

LEARNINGS_ROOT = Path(__file__).parent.parent / "learnings"
MAX_LEARNINGS_CHARS = 4000
MIN_LESSON_CHARS = 8


def load_learnings(agent: str) -> str:
    """Return accumulated lessons for an agent (most recent if over the cap), or ""."""
    p = LEARNINGS_ROOT / f"{agent}.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    return text[-MAX_LEARNINGS_CHARS:] if len(text) > MAX_LEARNINGS_CHARS else text


def record_learning(agent: str, lesson: str) -> bool:
    """
    Append a generalizable lesson for an agent. Returns True if newly recorded.
    No-op for empty/too-short lessons or exact duplicates.
    """
    lesson = " ".join((lesson or "").strip().split())
    if len(lesson) < MIN_LESSON_CHARS:
        return False
    p = LEARNINGS_ROOT / f"{agent}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    if lesson.lower() in existing.lower():
        return False  # already known

    updated = existing + f"- {lesson}\n"
    if len(updated) > MAX_LEARNINGS_CHARS:
        lines = updated.splitlines(keepends=True)
        while len("".join(lines)) > MAX_LEARNINGS_CHARS and len(lines) > 1:
            lines.pop(0)               # drop oldest lessons first
        updated = "".join(lines)
    p.write_text(updated, encoding="utf-8")
    return True


def augment_system(system: str, agent: str) -> str:
    """Append the agent's learnings to its system prompt, if any."""
    learnings = load_learnings(agent)
    if not learnings:
        return system
    return f"{system}\n\n## Learnings from past runs (do not repeat these mistakes)\n{learnings}"


# ── Universal self-improvement (CEO mandate 2026-06-13) ─────────────────────
# Feedback from ANY source (gates, critics, integration, guards, vision QA, CTO
# answers) is emitted into the run trace as `feedback` events at the choke points
# where it occurs; at run end ONE retro call distils per-agent GENERALIZABLE
# lessons and records them here, so every agent improves on the next iteration.

RETRO_AGENTS = ["pm", "design", "architect", "test_author", "engineer", "qa", "devops"]
MAX_FEEDBACK_EVENTS = 30
MAX_EVENT_CHARS = 900


def gather_feedback(trace_path: str) -> list:
    """All `feedback` events from a run trace: [{agent, kind, text}]."""
    import json
    events = []
    p = Path(trace_path)
    if not p.exists():
        return events
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "feedback" and e.get("agent"):
            events.append({"agent": e["agent"], "kind": e.get("fb_kind", ""),
                           "text": (e.get("text") or "")[:MAX_EVENT_CHARS]})
    return events[-MAX_FEEDBACK_EVENTS:]


def run_retro(trace_path: str, state: dict) -> dict:
    """Per-run retrospective: distil at most 2 generalizable lessons per agent from
    the run's feedback events + the CEO's recorded directives, and record them.
    ONE fast-tier call; never raises; returns {agent: [lessons]}."""
    try:
        from tools.llm import call_llm
        events = gather_feedback(trace_path)
        directives = [f"[CEO answer to {e.get('from')}] Q: {(e.get('question') or '')[:200]} "
                      f"A: {(e.get('answer') or '')[:300]}"
                      for e in (state.get("qa_log") or [])
                      if e.get("to") == "ceo" and e.get("answer")]
        if not events and not directives:
            return {}
        lines = [f"[{e['agent']} | {e['kind']}] {e['text']}" for e in events] + directives
        user_msg = (
            "You are running the engineering retrospective for an AI agent team "
            "(agents: " + ", ".join(RETRO_AGENTS) + "). Below are the run's failure/"
            "feedback events and the human CEO's directives.\n\n"
            + "\n".join(lines) +
            "\n\nFor each agent that has something to learn, output AT MOST 2 lines:\n"
            "agent_name: <one GENERALIZABLE lesson — a rule that prevents this CLASS of "
            "mistake next run; never feature-specific names/values>\n"
            "Only lines in that exact format. Agents with nothing to learn: omit. "
            "Lessons must be actionable imperatives under 200 chars.")
        raw = call_llm("You distil concise, generalizable engineering lessons.",
                       user_msg, tier="fast")
        out = {}
        for line in raw.splitlines():
            m = line.strip().split(":", 1)
            if len(m) == 2 and m[0].strip().lower() in RETRO_AGENTS:
                agent = m[0].strip().lower()
                if len(out.get(agent, [])) < 2 and record_learning(agent, m[1]):
                    out.setdefault(agent, []).append(m[1].strip())
        return out
    except Exception:
        return {}   # the retro must never break a finished run


def emit_feedback(agent: str, fb_kind: str, text: str):
    """Record a feedback moment into the run trace for the end-of-run retro."""
    from tools import trace
    try:
        trace.emit("feedback", agent=agent, fb_kind=fb_kind, text=(text or "")[:MAX_EVENT_CHARS])
    except Exception:
        pass
