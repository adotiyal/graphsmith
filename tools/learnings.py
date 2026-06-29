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
    """Append the agent's learnings to its system prompt: the COMMITTED shared tier
    (generic, ships with the harness — see below) FIRST, then this deployment's LOCAL
    accumulated lessons. Either/both/neither may be empty."""
    shared = load_shared_learnings(agent)
    local = load_learnings(agent)
    blocks = []
    if shared:
        blocks.append("## Shared learnings (curated — apply to ANY product/stack)\n" + shared)
    if local:
        blocks.append("## Learnings from past runs (do not repeat these mistakes)\n" + local)
    if not blocks:
        return system
    return system + "\n\n" + "\n\n".join(blocks)


# ── Committed "shared" tier (propagating GENERIC learnings to the harness) ───
# The LEARNINGS_ROOT/<agent>.md store above is machine-accumulated, gitignored, and LOCAL
# to one installation — its lessons never leave the clone that learned them, and may be
# stack/product-specific. The shared tier (learnings/shared/<agent>.md) is the opposite:
# COMMITTED (un-ignored in .gitignore) and therefore shipped with the harness to EVERY
# clone and project. A lesson reaches it only by human-gated PROMOTION (promote_learning /
# the `promote` CLI), and MUST be product- AND stack-agnostic (keep stack specifics as a
# "(Default stack: …)" example). Kept separate from hand-authored skills/ so promoted
# machine lessons never corrupt the curated skill, and from the local store so raw
# candidates aren't shipped blindly. augment_system loads both tiers.
MAX_SHARED_LEARNINGS_CHARS = 4000


def _shared_root() -> Path:
    """The committed shared-learnings dir, derived from LEARNINGS_ROOT at call time so the
    test fixture's LEARNINGS_ROOT patch isolates the shared tier too."""
    return LEARNINGS_ROOT / "shared"


def load_shared_learnings(agent: str) -> str:
    """Committed, cross-project generic lessons for an agent (most recent if over cap), or ''."""
    p = _shared_root() / f"{agent}.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    return text[-MAX_SHARED_LEARNINGS_CHARS:] if len(text) > MAX_SHARED_LEARNINGS_CHARS else text


def promote_learning(agent: str, lesson: str) -> bool:
    """Graduate a VETTED, generic lesson into the COMMITTED shared tier (ships with the
    harness to every clone). Same normalize/dedupe/cap contract as record_learning, but
    writes learnings/shared/<agent>.md and only for a real producing agent. The CALLER owns
    genericity — a promoted lesson must be product- AND stack-agnostic. Returns True if
    newly recorded; False on empty/too-short/unknown-agent/duplicate."""
    lesson = " ".join((lesson or "").strip().split())
    if len(lesson) < MIN_LESSON_CHARS or agent not in RETRO_AGENTS:
        return False
    p = _shared_root() / f"{agent}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    if lesson.lower() in existing.lower():
        return False
    updated = existing + f"- {lesson}\n"
    if len(updated) > MAX_SHARED_LEARNINGS_CHARS:
        lines = updated.splitlines(keepends=True)
        while len("".join(lines)) > MAX_SHARED_LEARNINGS_CHARS and len(lines) > 1:
            lines.pop(0)
        updated = "".join(lines)
    p.write_text(updated, encoding="utf-8")
    return True


def _bullets(text: str) -> list:
    """The '- ' bullet lines of a learnings file as plain texts (order preserved)."""
    return [ln[2:].strip() for ln in (text or "").splitlines() if ln.startswith("- ")]


def _read_local_raw(agent: str) -> str:
    """The full (un-capped) local learnings file for an agent, or ''."""
    p = LEARNINGS_ROOT / f"{agent}.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def local_learning(agent: str, index: int) -> "str | None":
    """The Nth local (candidate) lesson text, or None if out of range. Index matches the
    `list` CLI output, so a human can promote by the number they see."""
    bl = _bullets(_read_local_raw(agent))
    return bl[index] if 0 <= index < len(bl) else None


def remove_local_learning(agent: str, index: int) -> "str | None":
    """Pop the Nth local (candidate) lesson — used to GRADUATE it once promoted, so it isn't
    injected from both tiers. Returns the removed text, or None if out of range."""
    p = LEARNINGS_ROOT / f"{agent}.md"
    if not p.exists():
        return None
    lines = p.read_text(encoding="utf-8").splitlines()
    bullets = [i for i, ln in enumerate(lines) if ln.startswith("- ")]
    if not (0 <= index < len(bullets)):
        return None
    removed = lines[bullets[index]][2:].strip()
    del lines[bullets[index]]
    p.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    return removed


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


# ── Promote CLI — human-gated graduation local → committed shared tier ───────
# `python -m tools.learnings list` reviews candidates; `... promote` lifts a vetted,
# GENERIC lesson into learnings/shared/<agent>.md (which ships with the harness).

def _cli_list(agent_filter: "str | None") -> None:
    agents = [agent_filter] if agent_filter else RETRO_AGENTS
    shown = False
    for agent in agents:
        local = _bullets(_read_local_raw(agent))
        shared = _bullets(load_shared_learnings(agent))
        if not local and not shared:
            continue
        shown = True
        print(f"\n=== {agent} ===")
        if shared:
            print("  [shared / committed — ships with the harness]")
            for s in shared:
                print(f"      • {s}")
        if local:
            print(f"  [local / candidates — promote with: promote --agent {agent} --index N --as '<generic rewrite>']")
            for i, s in enumerate(local):
                print(f"    [{i}] {s}")
    if not shown:
        print("(no learnings recorded yet)")


def main(argv: "list | None" = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m tools.learnings",
        description="Inspect cross-run learnings and PROMOTE generic ones into the committed "
                    "shared tier (learnings/shared/<agent>.md) that ships with the harness.")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list", help="show local (candidate) + shared (committed) learnings")
    pl.add_argument("--agent", default=None, help="limit to one agent")
    pp = sub.add_parser("promote", help="graduate a generic lesson into the committed shared tier")
    pp.add_argument("--agent", required=True)
    src = pp.add_mutually_exclusive_group(required=True)
    src.add_argument("--index", type=int, help="promote the Nth local candidate (see `list`)")
    src.add_argument("--text", help="promote this exact (already-generic) lesson text")
    pp.add_argument("--as", dest="as_text", default=None,
                    help="with --index: the generic rewrite to ship (recommended — raw "
                         "candidates are often stack-specific)")
    args = p.parse_args(argv)

    if args.cmd == "list":
        _cli_list(args.agent)
        return 0

    if args.agent not in RETRO_AGENTS:
        print(f"unknown agent '{args.agent}'. Agents: {', '.join(RETRO_AGENTS)}")
        return 2
    if args.text is not None:
        lesson = args.text
    else:
        src_text = local_learning(args.agent, args.index)
        if src_text is None:
            print(f"no local candidate at index {args.index} for {args.agent} (see `list`).")
            return 2
        lesson = args.as_text or src_text
    if not promote_learning(args.agent, lesson):
        print("not promoted (empty, too short, or already present in the shared tier).")
        return 1
    if args.text is None:                       # graduate: drop the candidate from local
        remove_local_learning(args.agent, args.index)
    print(f"promoted to learnings/shared/{args.agent}.md:\n  - {lesson}")
    print("REMINDER: the shared tier ships to every clone — ensure it is product- AND "
          "stack-agnostic (keep stack specifics as a '(Default stack: …)' example).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
