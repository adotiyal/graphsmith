"""
tools/project_ctx.py — the single persistent PROJECT (continuity across runs)
----------------------------------------------------------------------------
Your typical loop is: build a feature, verify it, then keep adding features to the SAME
product. For that to work, successive runs need the *project* — the accumulated code AND
the history of what was built and why — not a fresh throwaway each time.

This module makes one persistent project a first-class thing:
- `project_dir()`  → workspace/project/  (a real git repo; the product itself)
- First run seeds it; every later run auto-extends it (main.py points target_repo here),
  so the Surveyor maps the real code and the Engineer writes diffs back into it.
- A **feature ledger** (project/.agent/ledger.md) records each feature + key decisions and
  is fed (summarized) to the planning agents so they have the "why", not just the code.

Single default project (one product at a time); reset by deleting workspace/project/.
"""

import subprocess
from datetime import date
from pathlib import Path

from tools import file_io

LEDGER_HEADER = "# Project Ledger\n\nFeatures built for this product, in order. Newest at the bottom.\n"
MAX_LEDGER_CHARS = 4000


def project_dir() -> Path:
    # Computed from file_io.WORKSPACE_ROOT so tests can redirect it.
    return file_io.WORKSPACE_ROOT / "project"


def _ledger_path() -> Path:
    return project_dir() / ".agent" / "ledger.md"


def has_code() -> bool:
    """True once the project contains real product files (ignoring .git / .agent)."""
    root = project_dir()
    if not root.exists():
        return False
    for f in root.rglob("*"):
        if f.is_file() and ".git" not in f.parts and ".agent" not in f.parts:
            return True
    return False


def ensure_repo() -> Path:
    """Create the project dir and git-init it (best-effort — git is optional)."""
    root = project_dir()
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        try:
            subprocess.run(["git", "init", "-q"], cwd=str(root), capture_output=True, timeout=10)
        except Exception:
            pass
    return root


def load_ledger() -> str:
    p = _ledger_path()
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    return text[-MAX_LEDGER_CHARS:] if len(text) > MAX_LEDGER_CHARS else text


def append_ledger(feature_request: str, state: dict) -> str:
    """Record a finished feature in the ledger. Deterministic — no LLM call."""
    p = _ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    files = state.get("code_files") or []
    names = ", ".join(Path(f).name for f in files[:12]) or "—"
    shipped = "shipped" if state.get("tests_passed") else "tests did not pass"
    entry = (
        f"\n## {date.today().isoformat()} — {feature_request.strip()}\n"
        f"- type: {state.get('change_type') or 'feature'}\n"
        f"- stack: {state.get('tech_stack') or '—'}\n"
        f"- files: {names}\n"
        f"- status: {shipped}\n"
    )
    prev = p.read_text(encoding="utf-8") if p.exists() else LEDGER_HEADER
    p.write_text(prev + entry, encoding="utf-8")
    # Browsable twin for the human (deterministic; md stays canonical for the agents)
    from tools import report_html
    report_html.render_ledger(str(project_dir()))
    return str(p)
