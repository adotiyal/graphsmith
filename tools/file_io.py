"""
DESIGN DECISION: All file I/O through these helpers.
- Agents never call open() directly.
- read_artifact() has a max_chars guard as a safety net against pathological
  files, NOT as an aggressive token-saver.

PHASE 0 CHANGE (0.4): caps raised so agents stop being starved of context.
The old 6000-char read cap meant the Engineer literally could not see a full
tech spec for a non-trivial feature — truncation was silently lowering quality.
Token frugality now comes from prompt caching and paths-not-content state,
not from blinding the agents. Truncation remains logged and is a last resort.
"""

import os
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).parent.parent / "workspace"
MAX_READ_CHARS = 24000  # ~6000 tokens - safety net only, not an optimization
# Skills carry the domain knowledge; don't starve them. I2 (live finding): the engineer
# skill crossed the old 8000 cap and its NEWEST mandates (SEO floor, theme wiring) were
# silently cut from every call — the cap is a safety net against pathological files,
# never a budget. tests/ assert every skills/*.md loads untruncated.
MAX_SKILL_CHARS = 16000  # ~4000 tokens


def workspace_path(project_id: str, subdir: str, filename: str) -> Path:
    p = WORKSPACE_ROOT / project_id / subdir
    p.mkdir(parents=True, exist_ok=True)
    return p / filename


def code_root(state: dict) -> Path:
    """
    Where generated CODE and TESTS live.
    - extend mode (target_repo set): the real repository.
    - greenfield: workspace/<project-id>/.
    Meta-artifacts (prd, design, qa report, deploy) always go to workspace/, never here.
    """
    repo = state.get("target_repo")
    if repo:
        return Path(repo)
    return WORKSPACE_ROOT / state["project_id"]


def write_artifact(project_id: str, subdir: str, filename: str, content: str) -> str:
    """Write content to disk. Returns the relative path string for state."""
    path = workspace_path(project_id, subdir, filename)
    # filename may itself contain nested dirs (e.g. ".github/workflows/deploy.yml"
    # from DevOps); workspace_path only made the subdir, so ensure the file's
    # own parent chain exists before writing.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path.relative_to(WORKSPACE_ROOT.parent))


def read_artifact(path_str: str, max_chars: int = MAX_READ_CHARS) -> str:
    """
    Read artifact from disk with a character cap.
    DESIGN DECISION: truncation is explicit and logged, not silent.
    """
    path = Path(path_str)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8")
    if len(content) > max_chars:
        print(f"[file_io] WARNING: {path.name} truncated {len(content)} → {max_chars} chars")
        return content[:max_chars] + "\n\n[... truncated ...]"
    return content


def load_prompt(agent_name: str) -> str:
    """Load system prompt from prompts/<agent_name>.txt"""
    prompt_path = Path(__file__).parent.parent / "prompts" / f"{agent_name}.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


def load_skill(agent_name: str) -> str:
    """
    Load domain skill document from skills/<agent_name>.md.
    DESIGN DECISION: skill is loaded separately from system prompt.
    - System prompt = identity + output contract (short, rarely changes)
    - Skill = domain knowledge + patterns + rules (longer, evolves over time)
    - They are concatenated at call time in the agent, not here.
    - If no skill file exists, returns empty string (graceful degradation).
    """
    skill_path = Path(__file__).parent.parent / "skills" / f"{agent_name}.md"
    if not skill_path.exists():
        return ""
    content = skill_path.read_text(encoding="utf-8").strip()
    # Skills carry the domain knowledge that makes output good — cap generously.
    # BLOCKER WATCH: if a skill exceeds this, put the most important rules first.
    if len(content) > MAX_SKILL_CHARS:
        print(f"[file_io] WARNING: skill/{agent_name}.md truncated to {MAX_SKILL_CHARS} chars")
        return content[:MAX_SKILL_CHARS]
    return content


def strip_md_fences(content: str) -> str:
    """Strip wrapping ```lang fences from LLM-emitted file content. Live failures:
    a QA spec revision AND design's kit emission both wrapped files in fences —
    written verbatim, each was a SyntaxError that burned integration rounds."""
    lines = content.strip().splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
