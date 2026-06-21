"""
Surveyor Agent (Phase 2.1 — codebase awareness)
-----------------------------------------------
Runs after PRD approval, before Design. In EXTEND mode (state["target_repo"] set) it
reads the existing repository, detects its stack, and writes an integration brief +
repo map that downstream agents use to extend the codebase rather than build greenfield.

In GREENFIELD mode (no target_repo) it is a no-op pass-through — zero LLM cost.

Reads:  target_repo (filesystem), prd_path (what the feature is)
Writes: design/repo_map.md  → sets repo_map_path + detected_stack

DESIGN DECISION: the survey is read-only. It produces understanding, not changes.
It runs on the "reason" tier — understanding an unfamiliar codebase is high-leverage.
"""

import re

from graph.state import ProjectState
from tools.file_io import load_prompt, load_skill, read_artifact, write_artifact
from tools import repo
from tools.qa_utils import run_with_qa, work_call, format_qa_context

CONSULT = ["ceo", "pm"]
RELEVANT_KEYWORDS = 6   # how many PRD-derived terms to grep for


def run(state: ProjectState) -> dict:
    target = state.get("target_repo")
    if not target or not repo.is_repo(target):
        # Greenfield: nothing to survey.
        return {"current_node": "surveyor"}
    # A managed project's FIRST run has a target_repo dir that is still EMPTY — that is
    # greenfield too. Surveying it burned an Opus call, escalated a pointless "repo is
    # empty?!" CTO question, and set detected_stack='unknown' — a truthy value that made
    # Design skip its stack ask AND the component kit (live-run bug). No files → no-op,
    # and detected_stack stays unset so design-time stack confirmation fires correctly.
    if not repo.list_files(target, max_files=5):
        return {"current_node": "surveyor"}
    return run_with_qa(state, "surveyor", _do_work, consultable_agents=CONSULT)


def _do_work(state: dict, qa_log: list, rounds: dict, allow_clarify: bool = True) -> dict:
    target = state["target_repo"]
    identity = load_prompt("surveyor")
    skill = load_skill("surveyor")
    system = f"{identity}\n\n{skill}" if skill else identity

    prd = read_artifact(state["prd_path"])
    repo_map = repo.build_repo_map(target)
    detected = repo.detect_stack(target)
    excerpts = _relevant_excerpts(target, prd)
    qa_ctx = format_qa_context(qa_log, "surveyor")

    user_msg = f"""
PRD (the feature to add):
{prd}

REPOSITORY MAP:
{repo_map}

RELEVANT EXISTING FILE EXCERPTS:
{excerpts}

{qa_ctx}

Write the INTEGRATION BRIEF with these sections:
## Stack & Conventions
## Where The Feature Plugs In   (real files to MODIFY / CREATE)
## Reuse                        (existing modules/helpers to use)
## Risks & Blast Radius
## Open Questions               (or "None")
"""

    # §4.2: web_search (opt-in via LLM_WEB_SEARCH) lets the surveyor flag outdated/deprecated
    # deps or CVEs in the detected stack while mapping the repo; no-op when off.
    questions, brief = work_call(system, user_msg, "reason", CONSULT, allow_clarify,
                                 web_search=True)
    if questions:
        return {"_clarify": questions}

    brief = f"<!-- detected stack: {detected} -->\n\n{brief}"
    path = write_artifact(state["project_id"], "design", "repo_map.md", brief)

    return {
        "current_node": "surveyor",
        "repo_map_path": path,
        # never store the sentinel 'unknown' — downstream checks treat any truthy
        # value as a settled stack (design skips its CTO ask, kit gating breaks)
        "detected_stack": None if detected == "unknown" else detected,
        "qa_log": qa_log,
        "qa_rounds": rounds,
        "ceo_qa_from": None,
    }


def _relevant_excerpts(target: str, prd: str, max_files: int = 6, max_chars: int = 1500) -> str:
    """Grep the repo for terms drawn from the PRD and return short excerpts of the hits."""
    terms = _keywords(prd)
    seen, chunks = set(), []
    for term in terms:
        for relpath, lineno, _line in repo.grep(target, re.escape(term), max_hits=10):
            if relpath in seen:
                continue
            seen.add(relpath)
            chunks.append(f"# === {relpath} ===\n{repo.read_repo_file(target, relpath, max_chars)}")
            if len(seen) >= max_files:
                break
        if len(seen) >= max_files:
            break
    return "\n\n".join(chunks) if chunks else "(no obviously related files found by keyword search)"


def _keywords(prd: str) -> list:
    """Pull a few distinctive words from the PRD to grep for."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", prd.lower())
    stop = {"user", "users", "with", "that", "this", "from", "must", "should", "when",
            "page", "data", "feature", "criteria", "acceptance", "stories", "story",
            "scope", "able", "want", "have", "into", "they", "what", "will", "each"}
    out = []
    for w in words:
        if w in stop or w in out:
            continue
        out.append(w)
        if len(out) >= RELEVANT_KEYWORDS:
            break
    return out
