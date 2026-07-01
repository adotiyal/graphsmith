"""
tools/qa_utils.py
-----------------
Bidirectional Q&A for agents.

PHASE 0 CHANGE (0.2): the separate "do you have questions?" probe call is GONE.
Previously every agent made an extra Haiku call just to ask whether it had
questions — usually returning "no". Now clarification is folded into the agent's
single work call:

  - The agent's work prompt carries a CLARIFICATION PROTOCOL (appended by
    work_call). The LLM either produces its artifact OR, if genuinely blocked,
    emits a ===NEEDS_INPUT=== JSON block instead of guessing.
  - Common case (no questions): ONE call total, down from two.
  - Blocked case: peer questions are resolved synchronously via consult() and
    the work call is retried with the answers in context; CEO questions trigger
    the graph interrupt via the shared ceo_qa node.

Caps:
  - MAX_AGENT_INTERACTIONS (10): total peer consult() calls per agent. Overflow
    is escalated to CEO, never silently dropped.
  - MAX_QA_ROUNDS (10): times an agent may pause for CEO input. Once exhausted,
    the agent is forced to produce output with what it has.

CONTRACT for an agent's task_fn:
    task_fn(state, qa_log, rounds, allow_clarify=True) -> dict
  It must call work_call(...) to make its main LLM call. If work_call returns
  questions, task_fn returns {"_clarify": questions} WITHOUT writing artifacts.
  Otherwise it parses/writes as normal and returns its state-update dict.
"""

import json
import os
import re
from pathlib import Path

from tools.llm import call_llm
from tools.file_io import load_prompt, load_skill, read_artifact, MAX_READ_CHARS

MAX_QA_ROUNDS = 10            # max CEO pauses per agent
MAX_AGENT_INTERACTIONS = 10   # max peer consult() calls per agent


def run_with_qa(state: dict, agent_name: str, task_fn, consultable_agents: list = None) -> dict:
    """
    Orchestrate an agent's work with folded clarification.

    Calls task_fn (which makes the actual work LLM call via work_call). If the
    agent emitted clarification questions, resolve peers synchronously and retry,
    or pause for CEO. Bounded by MAX_AGENT_INTERACTIONS and MAX_QA_ROUNDS.
    """
    consultable_agents = consultable_agents or []
    qa_log = list(state.get("qa_log") or [])
    rounds = dict(state.get("qa_rounds") or {})
    counts = dict(state.get("agent_qa_counts") or {})
    interactions = counts.get(agent_name, 0)
    rounds_used = rounds.get(agent_name, 0)

    while True:
        result = task_fn(state, qa_log, rounds, allow_clarify=True)
        questions = result.get("_clarify") if isinstance(result, dict) else None

        if not questions:
            result.setdefault("agent_qa_counts", counts)
            return result

        # Split into peer questions (resolved here) and CEO questions (need interrupt)
        ceo_q = questions.get("ceo")
        peer_qs = {k: v for k, v in questions.items() if k != "ceo" and k in consultable_agents}
        escalated = []
        progressed = False

        for target, question in peer_qs.items():
            if interactions < MAX_AGENT_INTERACTIONS:
                ctx = _get_artifact_for_agent(state, target)
                answer = consult(target, question, ctx)
                qa_log.append({
                    "from": agent_name, "to": target,
                    "question": question, "answer": answer,
                })
                interactions += 1
                progressed = True
            else:
                escalated.append(f"[Escalated from {target} — agent interaction limit reached] {question}")
        counts[agent_name] = interactions

        ceo_parts = [q for q in [ceo_q] + escalated if q]
        combined_ceo = "\n\n".join(ceo_parts) if ceo_parts else None

        # Pause for CEO if there are CEO questions and we still have rounds left
        if combined_ceo and rounds_used < MAX_QA_ROUNDS:
            rounds_used += 1
            rounds[agent_name] = rounds_used
            qa_log.append({"from": agent_name, "to": "ceo", "question": combined_ceo})
            return {
                "qa_log": qa_log,
                "qa_rounds": rounds,
                "agent_qa_counts": counts,
                "ceo_qa_pending": combined_ceo,
                "ceo_qa_from": agent_name,
            }

        if progressed:
            continue  # re-run the work call now that peer answers are in qa_log

        # Cannot pause (round cap hit) and no peer progress possible:
        # force the agent to produce output with what it has.
        final = task_fn(state, qa_log, rounds, allow_clarify=False)
        final.pop("_clarify", None)
        final["qa_log"] = qa_log
        final["qa_rounds"] = rounds
        final.setdefault("agent_qa_counts", counts)
        return final


def work_call(system: str, user_msg: str, tier: str, consultable_agents: list,
              allow_clarify: bool, web_search: bool = False):
    """
    The single work LLM call for an agent, with the clarification protocol folded in.

    Returns (questions_or_None, raw_text):
      - If allow_clarify and the LLM emitted a NEEDS_INPUT block → (questions, raw)
      - Otherwise → (None, raw) where raw is the produced artifact text.

    web_search (§4.2, opt-in): the THINKING/spec agents pass True to ground their spec in
    current library versions/APIs/CVEs; no-op unless LLM_WEB_SEARCH is set (see tools.llm).
    """
    if allow_clarify:
        user_msg = f"{user_msg}\n\n{_clarify_instruction(consultable_agents)}"
    raw = call_llm(system, user_msg, tier=tier, web_search=web_search)
    questions = _parse_needs_input(raw) if allow_clarify else None
    return questions, raw


def consult(agent_name: str, questions: str, context: str) -> str:
    """
    Ask a peer agent questions without triggering its full artifact-producing run.
    Lightweight, synchronous, `fast` tier (Opus decision/analysis).
    """
    system = load_prompt(agent_name)
    skill = load_skill(agent_name)
    if skill:
        system = f"{system}\n\n{skill}"

    user_msg = f"""A peer agent is consulting you before they do their work.

Context they are working with:
{context or "(no artifact produced yet)"}

Their questions:
{questions}

Answer concisely and specifically. Do not produce full artifacts — just answer the questions."""
    return call_llm(system, user_msg, tier="fast")


def product_invariants_block(state: dict) -> str:
    """Standing, code-verifiable product context for the generation agents that otherwise
    have NONE (architect/test_author/engineer/qa carry zero product_profile/ledger refs).
    Returns "" when state has no invariants (greenfield first run / undetected repo) so the
    agent's prompt is unchanged. The OVERRIDES label gives a learned-lesson-vs-invariant
    conflict a machine-visible loser at prompt-assembly time."""
    inv = (state.get("product_invariants") or "").strip()
    if not inv:
        return ""
    return ("\n\nCANONICAL PRODUCT INVARIANTS — statically derived from the existing codebase. "
            "These OVERRIDE any learned lesson, prior assumption, or per-run guess; NEVER "
            "violate or silently change them (e.g. if a column is computed-not-stored, do not "
            "add it as a stored column):\n" + inv + "\n")


def format_qa_context(qa_log: list, agent_name: str) -> str:
    """Return a formatted Q&A block for entries involving this agent."""
    relevant = [e for e in qa_log if e.get("from") == agent_name or e.get("to") == agent_name]
    if not relevant:
        return ""
    lines = ["Relevant Q&A from earlier in this pipeline:"]
    for e in relevant:
        answer = e.get("answer", "(awaiting CEO...)")
        lines.append(f"  [{e['from']} → {e['to']}] Q: {e['question']}")
        lines.append(f"  A: {answer}")
    return "\n".join(lines)


# --- Internal helpers ---

def _clarify_instruction(consultable_agents: list) -> str:
    peers = [a for a in (consultable_agents or []) if a != "ceo"]
    targets = ", ".join(["ceo"] + peers) if peers else "ceo"
    return (
        "CLARIFICATION PROTOCOL:\n"
        "If something is genuinely ambiguous and would block you from producing "
        "correct output, do NOT guess. Respond with ONLY this block and nothing else:\n"
        "===NEEDS_INPUT===\n"
        '{"ceo": "<questions only the CEO/CTO can answer — business OR technical, or null>", '
        '"<peer_agent>": "<a specific question for that peer agent>"}\n'
        "===END===\n"
        f"Valid recipients: {targets} (the human plays CEO and CTO — escalate either "
        "business or technical decisions to them). Omit any key you do not need.\n"
        "Only ask if you are truly blocked — otherwise produce your full output now."
    )


def _parse_needs_input(raw: str) -> dict:
    """Return a dict of {recipient: question} if a NEEDS_INPUT block is present, else None."""
    m = re.search(r"===NEEDS_INPUT===\s*(.*?)\s*===END===", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cleaned = {
        k: v for k, v in data.items()
        if v and isinstance(v, str) and v.strip().lower() not in ("", "null", "none")
    }
    return cleaned or None


def _get_artifact_for_agent(state: dict, agent_name: str) -> str:
    """Return the most recent artifact for a peer agent, for use as consult context."""
    path_map = {
        "pm":        "prd_path",
        "design":    "design_path",
        "architect": "design_path",  # architect overwrites design_path with tech_spec
        "engineer":  "code_path",
    }
    path_key = path_map.get(agent_name)
    if not path_key:
        return ""
    path = state.get(path_key)
    if not path:
        return ""
    # In managed/extend mode the engineer's code_path is the repo DIRECTORY, not a
    # single file — read_artifact would choke on it (IsADirectoryError). Fall back to
    # the explicitly written code_files (capped) so the consult still gets real context.
    if os.path.isdir(path):
        chunks = []
        for f in state.get("code_files") or []:
            try:
                chunks.append(f"# === {f} ===\n{Path(f).read_text(encoding='utf-8')}")
            except OSError:
                continue
        return ("\n\n".join(chunks))[:MAX_READ_CHARS]
    return read_artifact(path)
