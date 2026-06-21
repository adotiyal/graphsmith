"""
tools/repo.py
-------------
Read-only access to an EXISTING codebase, for "extend mode" (Phase 2.1).

When the pipeline targets a real repository instead of building greenfield, agents
need to understand what already exists: the file tree, the stack, and the areas the
new feature touches. These helpers provide that — safely and within a token budget.

DESIGN DECISION: read-only + one guarded writer.
- Everything here is read-only except write_into_repo(), which refuses to write
  outside the repo root (no path traversal).
- No git operations here — surveying is just filesystem reads. Diffs/commits are the
  caller's concern.
- Compact by construction: build_repo_map() returns a bounded textual map, not the
  whole repo, so it fits an agent prompt.
"""

import re
from pathlib import Path

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".pytest_cache", ".mypy_cache", ".ruff_cache", "target", ".idea",
    ".vscode", "coverage", ".turbo", ".cache",
}

# (marker file, human label) — first matches win, in priority order.
STACK_HINTS = [
    ("next.config.js", "Next.js"), ("next.config.mjs", "Next.js"), ("next.config.ts", "Next.js"),
    ("package.json", "Node/JS"),
    ("pyproject.toml", "Python"), ("requirements.txt", "Python"), ("setup.py", "Python"),
    ("go.mod", "Go"), ("Cargo.toml", "Rust"), ("pom.xml", "Java/Maven"),
    ("build.gradle", "Java/Gradle"), ("Gemfile", "Ruby"), ("composer.json", "PHP"),
    ("Dockerfile", "Docker"), ("docker-compose.yml", "Docker Compose"),
    ("alembic.ini", "Alembic migrations"),
]


def is_repo(path: str) -> bool:
    return bool(path) and Path(path).is_dir()


def _ignored(rel: Path) -> bool:
    return any(part in IGNORE_DIRS for part in rel.parts)


def list_files(root, max_files: int = 400) -> list:
    """Relative file paths under root, skipping ignored dirs. Bounded by max_files."""
    root = Path(root)
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _ignored(rel):
            continue
        out.append(str(rel))
        if len(out) >= max_files:
            break
    return out


def read_repo_file(root, relpath: str, max_chars: int = 8000) -> str:
    p = Path(root) / relpath
    if not p.is_file():
        return ""
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + "\n[... truncated ...]"
    return text


def grep(root, pattern: str, max_hits: int = 50) -> list:
    """Return [(relpath, lineno, line)] for lines matching pattern (case-insensitive)."""
    root = Path(root)
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return []
    hits = []
    for rel in list_files(root, max_files=3000):
        try:
            lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append((rel, i, line.strip()[:200]))
                if len(hits) >= max_hits:
                    return hits
    return hits


def detect_stack(root) -> str:
    """Best-effort stack detection from marker files at the repo root (and one level in)."""
    root = Path(root)
    found = []
    search_roots = [root] + [d for d in root.iterdir() if d.is_dir() and d.name not in IGNORE_DIRS] \
        if root.is_dir() else [root]
    for marker, label in STACK_HINTS:
        for sr in search_roots:
            if (sr / marker).exists() and label not in found:
                found.append(label)
                break
    return ", ".join(found) if found else "unknown"


def build_repo_map(root, max_files: int = 200, max_chars: int = 6000) -> str:
    """A compact textual map of the repo for an agent prompt: stack + file tree."""
    root = Path(root)
    files = list_files(root, max_files)
    stack = detect_stack(root)
    header = [f"Repository: {root.name}", f"Detected stack: {stack}",
              f"Files (showing up to {max_files}):"]
    body = "\n".join(f"  {f}" for f in files)
    text = "\n".join(header) + "\n" + body
    return text[:max_chars]


def write_into_repo(root, relpath: str, content: str) -> str:
    """Write a file into the repo, refusing any path that escapes the repo root."""
    dest = _safe_dest(root, relpath)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return str(dest)


def delete_from_repo(root, relpath: str) -> tuple[bool, str]:
    """Delete a single FILE from the repo (never a directory), with the same
    escape-the-root guard as write_into_repo. Returns (ok, message).

    Exists so an engineer retry can REMOVE a conflicting leftover (e.g. a stale
    pages/index.tsx next to app/page.tsx) instead of accreting files forever."""
    try:
        dest = _safe_dest(root, relpath)
    except ValueError as e:
        return False, str(e)
    if not dest.is_file():
        return False, f"{relpath}: not a file (nothing deleted)"
    dest.unlink()
    return True, f"deleted {relpath}"


def apply_edit(root, relpath: str, search: str, replace: str) -> tuple[bool, str]:
    """
    Apply a minimal search/replace edit to an EXISTING file (Phase 2.1 #3 — minimal diffs).

    Refuses unless the SEARCH text matches EXACTLY ONCE — this is the safety property:
    no whole-file rewrites, and an ambiguous or stale snippet fails loudly (so the
    engineer retries with the real content) rather than silently corrupting the file.
    Returns (ok, message).
    """
    dest = _safe_dest(root, relpath)
    if not dest.is_file():
        return False, f"{relpath}: file not found for edit"
    text = dest.read_text(encoding="utf-8", errors="replace")
    count = text.count(search)
    if count == 0:
        return False, f"{relpath}: SEARCH block not found (it must match the current file exactly)"
    if count > 1:
        return False, f"{relpath}: SEARCH block is not unique (matched {count}×) — include more context"
    dest.write_text(text.replace(search, replace, 1), encoding="utf-8")
    return True, f"{relpath}: edit applied"


def _safe_dest(root, relpath: str) -> Path:
    root = Path(root).resolve()
    dest = (root / relpath).resolve()
    if dest != root and root not in dest.parents:
        raise ValueError(f"refusing to touch path outside repo root: {relpath}")
    return dest
