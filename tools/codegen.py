"""
I1 — structured file changes for code-writing agents (engineer first).

WHY: the ===FILE/EDIT/DELETE=== text format caused EVERY mechanical failure class
observed live — duplicate-block file corruption (5 files in one run), the
stale-SEARCH plague (≥6 burned attempts), silent no-op rounds, destructive
re-emit rounds. 30–40% of engineer attempts died on format mechanics.

HOW: the model changes files THROUGH TOOLS against a STAGING COPY of the project;
we then sync the staging diff back through ONE guarded choke point (oracle/kit
protection, path escapes, deletion mirroring). No text parsing of code remains —
the failure classes are structurally impossible, not just discouraged.

Backends (same selection as tools/llm.py):
- claude-cli (default): the headless CLI session gets Read/Write/Edit/Glob/Grep
  in the staging dir. Claude Code's own Edit tool enforces exact-unique anchors
  and reports mismatches back to the model IN-SESSION, so a stale anchor is
  self-healed inside the call instead of burning a whole engineer round.
- api: a real Messages tool-use loop (read/write/edit/delete_file) with the same
  executor semantics, implemented here.

The model's final TEXT (summary, or a ===NEEDS_INPUT=== block) is returned for
the normal clarification protocol; `apply_when(text)` gates whether the staged
changes are synced back at all (a clarify run discards its staging).
"""

import os
import shutil
import tempfile
import time
from pathlib import Path

from tools import trace

# Junk/state dirs: never staged in, never synced back, never deletion-mirrored.
# .agent is the feature ledger (pipeline metadata), not the model's to touch.
IGNORE_NAMES = {".git", "node_modules", "__pycache__", ".next", ".pytest_cache",
                ".venv", ".agent", ".DS_Store"}

MAX_CLI_TURNS = 150         # reads + one write/edit per file + self-heal slack
# (80 was hit live by phase-3's upload+adventures+badges build at 59K output tokens)
MAX_API_ITERATIONS = 60
CLI_TIMEOUT = 3600   # phase-3-scale builds legitimately exceed 30 min

_CODEGEN_GUARD = (
    "\n\nFILE-CHANGE MODE: you are in a working copy of the project — make the "
    "changes DIRECTLY with your file tools (Read existing files before you Edit "
    "or overwrite them; prefer minimal Edit anchors over whole-file rewrites; "
    "create new files with Write). Work only with relative paths inside the "
    "working directory. When you are done, reply with a SHORT plain-text summary "
    "of what you changed — do not paste file contents into the reply.")


def _stage(root: str) -> str:
    staging = tempfile.mkdtemp(prefix="agentplatform-codegen-")
    # copytree into the existing temp dir's subpath keeps cleanup trivial
    dst = os.path.join(staging, "work")
    shutil.copytree(root, dst, ignore=shutil.ignore_patterns(*IGNORE_NAMES))
    return dst


def _safe_rel(root: Path, rel: str):
    """Resolve rel inside root; None if it escapes (absolute, .., symlink trick)."""
    if not rel or rel.strip().startswith(("/", "\\")):
        return None
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p


# ---------------------------------------------------------------------------
# Tool executor (api backend; also THE unit-tested semantics of this module).
# All ops act on the staging root. Errors return (False, message-for-the-model);
# the message carries CURRENT file content where that helps the model re-anchor
# from reality instead of from memory of its own past output.
# ---------------------------------------------------------------------------

def tool_read(root: str, path: str, max_chars: int = 24000):
    p = _safe_rel(Path(root), path)
    if p is None:
        return False, f"{path}: path escapes the project root"
    if not p.is_file():
        return False, f"{path}: file does not exist"
    return True, p.read_text(encoding="utf-8", errors="replace")[:max_chars]


def tool_write(root: str, path: str, content: str):
    p = _safe_rel(Path(root), path)
    if p is None:
        return False, f"{path}: path escapes the project root"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")   # re-emission = clean overwrite, last wins
    return True, f"wrote {path}"


def tool_edit(root: str, path: str, old: str, new: str):
    p = _safe_rel(Path(root), path)
    if p is None:
        return False, f"{path}: path escapes the project root"
    if not p.is_file():
        return False, f"{path}: file does not exist — use write_file to create it"
    current = p.read_text(encoding="utf-8", errors="replace")
    n = current.count(old) if old else 0
    if n == 0:
        # The anti-stale-SEARCH property: a bad anchor returns the REAL content
        # so the very next tool call can anchor on what is actually on disk.
        return False, (f"{path}: old_string not found. CURRENT content:\n"
                       f"{current[:4000]}")
    if n > 1:
        return False, f"{path}: old_string matches {n} times — add surrounding lines to make it unique"
    p.write_text(current.replace(old, new, 1), encoding="utf-8")
    return True, f"edited {path}"


def tool_delete(root: str, path: str):
    p = _safe_rel(Path(root), path)
    if p is None:
        return False, f"{path}: path escapes the project root"
    if not p.is_file():
        return False, f"{path}: not a file"
    p.unlink()
    return True, f"deleted {path}"


# ---------------------------------------------------------------------------
# Sync-back: the single guarded choke point between the model's staging work
# and the real project. Guards are deterministic and live HERE, not in prompts.
# ---------------------------------------------------------------------------

def _walk_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_NAMES]
        for f in filenames:
            if f in IGNORE_NAMES:
                continue
            yield str(Path(dirpath, f).relative_to(root))


def sync_back(staging: str, root: str, is_protected, staged_at_start: set = None) -> tuple:
    """Apply the staging diff to root. Returns (written, deleted, violations) —
    written/deleted are ABSOLUTE paths under root; violations are messages for
    attempts the guard discarded (protected paths, escapes).

    staged_at_start: the files that existed when staging was COPIED. The deletion
    mirror only deletes files the model could have SEEN — a root file created
    AFTER the copy (e.g. a concurrent CTO repair) must never be interpreted as
    a model deletion (a live repair was silently erased this way)."""
    written, deleted, violations = [], [], []
    root_p, staging_p = Path(root), Path(staging)

    staged = set(_walk_files(staging))
    original = set(_walk_files(root))
    deletable = original - staged
    if staged_at_start is not None:
        deletable &= staged_at_start

    # DELETIONS FIRST: on a case-INSENSITIVE filesystem (macOS), a rename-by-case
    # (Icons.tsx → icons.tsx) means the write lands in the SAME file as the old
    # name — running deletions after writes then removed the freshly written file
    # entirely (a live run lost the kit's icon module this way).
    for rel in sorted(deletable):
        if is_protected(rel):
            violations.append(f"protected path delete discarded: {rel}")
            continue
        dst = _safe_rel(root_p, rel)
        if dst is not None and dst.is_file():
            dst.unlink()
            deleted.append(str(dst))

    for rel in sorted(staged):
        src = staging_p / rel
        dst = _safe_rel(root_p, rel)
        if dst is None:
            violations.append(f"path escape discarded: {rel}")
            continue
        new_bytes = src.read_bytes()
        if dst.exists() and dst.read_bytes() == new_bytes:
            continue   # unchanged
        if is_protected(rel):
            violations.append(f"protected path change discarded: {rel}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(new_bytes)
        written.append(str(dst))

    return written, deleted, violations


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def _cli_codegen(system: str, user_msg: str, staging: str, model: str,
                 timeout: int = CLI_TIMEOUT) -> str:
    """One headless Claude Code session WITH file tools, cwd = staging."""
    import json as json_mod
    import subprocess
    from tools.llm import _find_claude_bin
    cmd = [_find_claude_bin(), "--model", model,
           "--output-format", "json", "--strict-mcp-config",
           "--append-system-prompt", system + _CODEGEN_GUARD,
           "--allowed-tools", "Read,Write,Edit,Glob,Grep",
           "--permission-mode", "acceptEdits",
           "--max-turns", str(MAX_CLI_TURNS),
           "-p"]
    env = dict(os.environ)
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "32000")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=staging, env=env, input=user_msg)
    except subprocess.TimeoutExpired:
        # SALVAGE on timeout too — the child is killed but staging holds every file
        # it completed; sync-back applies them and the loop continues next round.
        return ("(codegen timed out mid-build — the work completed so far has been "
                "kept; continue implementing the remaining parts)")
    try:
        out = json_mod.loads(r.stdout) if r.stdout else {}
    except ValueError:
        out = {}
    # SALVAGE on turn-cap: the staging dir holds REAL completed file work — raising
    # here threw away 14 minutes of build once. Return a marker; sync-back applies
    # what exists and the test loop continues from the partial state next round.
    if out.get("subtype") == "error_max_turns":
        return ("(codegen hit the turn cap mid-build — the work completed so far has "
                "been kept; continue implementing the remaining parts)")
    if r.returncode != 0:
        raise RuntimeError(f"codegen cli call failed: {(r.stderr or r.stdout)[:500]}")
    if out.get("is_error"):
        raise RuntimeError(f"codegen cli call errored: {str(out)[:500]}")
    return out.get("result", "")


_API_TOOLS = [
    {"name": "read_file", "description": "Read a file from the project.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Create or fully overwrite one file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}},
    {"name": "edit_file",
     "description": "Replace old_string (must match the current file exactly and "
                    "uniquely) with new_string. On mismatch you get the current "
                    "content back — re-anchor from it.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old_string": {"type": "string"},
         "new_string": {"type": "string"}},
         "required": ["path", "old_string", "new_string"]}},
    {"name": "delete_file", "description": "Delete a stale/conflicting file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
]

_API_EXECUTORS = {
    "read_file":   lambda root, i: tool_read(root, i.get("path", "")),
    "write_file":  lambda root, i: tool_write(root, i.get("path", ""), i.get("content", "")),
    "edit_file":   lambda root, i: tool_edit(root, i.get("path", ""),
                                             i.get("old_string", ""), i.get("new_string", "")),
    "delete_file": lambda root, i: tool_delete(root, i.get("path", "")),
}


def _api_codegen(system: str, user_msg: str, staging: str, tier: str) -> str:
    """Messages-API tool-use loop with the executor above."""
    from tools.llm import MAX_TOKENS, MODELS, get_client
    client = get_client()
    messages = [{"role": "user", "content": user_msg + _CODEGEN_GUARD}]
    final_text = ""
    for _ in range(MAX_API_ITERATIONS):
        resp = client.messages.create(
            model=MODELS[tier], max_tokens=MAX_TOKENS[tier],
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            tools=_API_TOOLS, messages=messages)
        texts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        if texts:
            final_text = "\n".join(texts)
        if resp.stop_reason != "tool_use":
            break
        results = []
        for block in resp.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            ok, msg = _API_EXECUTORS.get(
                block.name, lambda r, i: (False, f"unknown tool {block.name}")
            )(staging, block.input or {})
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": msg, "is_error": not ok})
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": results})
    return final_text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate(system: str, user_msg: str, root: str, is_protected,
             tier: str = "strong", apply_when=None) -> dict:
    """Run one tool-based code-change session against a staging copy of `root`,
    then sync the diff back through the guard. Returns
    {text, written, deleted, violations}. When `apply_when(text)` is falsy
    (e.g. the model asked for clarification instead of working), the staged
    changes are DISCARDED — a clarify round must not half-apply work."""
    staging = _stage(root)
    staged_at_start = set(_walk_files(staging))
    t0 = time.perf_counter()
    backend = os.environ.get("LLM_BACKEND", "claude-cli").strip().lower()
    try:
        from tools.llm import MODELS
        if backend == "claude-cli":
            text = _cli_codegen(system, user_msg, staging, MODELS[tier])
        else:
            text = _api_codegen(system, user_msg, staging, tier)
        if apply_when is not None and not apply_when(text):
            written, deleted, violations = [], [], []
        else:
            written, deleted, violations = sync_back(staging, root, is_protected,
                                                      staged_at_start=staged_at_start)
    finally:
        shutil.rmtree(os.path.dirname(staging), ignore_errors=True)
    try:
        trace.emit("codegen", backend=backend, tier=tier, node=trace.current_node(),
                   latency_ms=round((time.perf_counter() - t0) * 1000),
                   written=len(written), deleted=len(deleted),
                   violations=len(violations))
    except Exception:
        pass
    return {"text": text, "written": written, "deleted": deleted,
            "violations": violations}


def domain_protected(allowed_prefixes=(), allowed_segments=()):
    """An INVERTED guard for I17: a domain-restricted writer (design→kit, test_author
    →tests, qa→e2e) may ONLY touch its own domain; everything else is protected.
    A path is allowed if it starts with an allowed PREFIX (e.g. frontend/src/
    components/kit/) or contains an allowed path SEGMENT (e.g. "tests"/"e2e" — which
    matches both flat tests/ and split backend/tests/). Routing these agents through
    the tools path makes them READ existing files before changing them — structurally
    preventing blind full-file overwrites that dropped exports / self-deleted modules."""
    prefixes = tuple(p.rstrip("/") + "/" for p in allowed_prefixes)
    segs = set(allowed_segments)

    def is_protected(rel: str) -> bool:
        rel = rel.replace("\\", "/")
        if any(rel.startswith(p) for p in prefixes):
            return False
        if segs and any(part in segs for part in rel.split("/")):
            return False
        return True

    return is_protected


def generate_in_domain(system: str, user_msg: str, root: str, allowed_prefixes=(),
                       allowed_segments=(), tier: str = "strong", apply_when=None) -> dict:
    """generate(), but the agent may only write within its own domain (allowed
    prefixes/segments). Returns the same dict; writes outside the domain are reported
    as guard violations, never silently applied."""
    return generate(system, user_msg, root,
                    domain_protected(allowed_prefixes, allowed_segments),
                    tier=tier, apply_when=apply_when)
