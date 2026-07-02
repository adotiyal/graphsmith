"""
Engineer Agent
--------------
DESIGN DECISION: Engineer reads the tech spec AND the authoritative test suite, and
must make those tests pass. It must NOT modify any test file (enforced via test_files).

Greenfield: writes code into workspace/<id>/, runs `pytest tests/` in Docker.
Extend mode (Phase 2.1 Slice 2): writes complete file contents INTO the target repo at
real paths (guarded), reads the existing files it changes + the repo map, runs the
repo's OWN test suite in Docker, and never touches the test author's files. The repo's
linter is left to the repo's own CI (we don't fail on pre-existing lint).

DESIGN DECISION: one LLM call, output cap 8192, with a bounded continuation if the
output is truncated mid-file.

Q&A: first run only. Retry runs go straight to fixing (allow_clarify=False).
"""

import os
import re
from pathlib import Path

from graph.state import ProjectState
from tools.llm import call_llm
from tools.file_io import load_prompt, load_skill, read_artifact, write_artifact, code_root
from tools.registry import (check_kit_wiring, run_linter, run_project_tests, scan_security,
                            format_code, code_quality_report, check_frontend_quality_tooling,
                            check_dependencies, quality_gate_level, measure_coverage,
                            check_quality_gate, COVERAGE_FLOOR)
from tools import codegen, repo as repo_tools
from tools.learnings import augment_system
from tools.qa_utils import (run_with_qa, work_call, format_qa_context,
                            product_invariants_block,
                            _clarify_instruction, _parse_needs_input)

# Generous: the fix loop's signal travels through error_log. At 500 chars QA/engineer
# saw only the head of the pytest output (deprecation warnings) while the actual
# assertion failure sat at the tail — a live run burned all 3 attempts fixing warnings,
# then QA hallucinated a "truncated test" from a mid-line cut. Keep the TAIL, generously.
MAX_ERROR_CHARS = 6000
MAX_CONTINUATIONS = 2
CONSULT = ["ceo", "architect", "design"]


def run(state: ProjectState) -> dict:
    # Q&A only on the very first attempt — retries go straight to fixing.
    if state.get("fix_attempts", 0) == 0:
        return run_with_qa(state, "engineer", _do_work, consultable_agents=CONSULT)

    return _do_work(
        state,
        list(state.get("qa_log") or []),
        dict(state.get("qa_rounds") or {}),
        allow_clarify=False,
    )


def _do_work(state: dict, qa_log: list, rounds: dict, allow_clarify: bool = True) -> dict:
    identity = load_prompt("engineer")
    skill = load_skill("engineer")
    system = augment_system(f"{identity}\n\n{skill}" if skill else identity, "engineer")

    extend = bool(state.get("target_repo"))
    change_type = state.get("change_type") or "feature"
    quick = change_type != "feature"

    # Full lane works from the tech spec; quick lane (bugfix/refactor/chore) has no spec —
    # work directly from the request (the CEO brief).
    spec_path = state.get("design_path") or state.get("prd_path")
    tech_spec = read_artifact(spec_path) if spec_path else "(no spec — work from the request)"
    spec_label = "Technical Spec" if not quick else f"Change request — {change_type.upper()}"
    test_code = _read_tests(state)
    qa_ctx = format_qa_context(qa_log, "engineer")

    extra = ""
    extra += product_invariants_block(state)
    if quick:
        guidance = {
            "bugfix":   "Find the root cause and make the SMALLEST fix. All existing tests must still pass.",
            "refactor": "Restructure WITHOUT changing behavior. Every existing test must still pass; do not edit tests.",
            "chore":    "Scope strictly to this chore (deps/config/build/docs). Keep all existing tests passing.",
        }.get(change_type, "Make the smallest change that satisfies the request.")
        extra += f"\n\nThis is a {change_type.upper()}. {guidance} Touch as few files as possible."

    # Design fidelity (prevention): the implemented UI must MATCH the design, not just
    # function. Without this block the engineer only ever saw the tech spec — a live run
    # shipped a working app that looked nothing like design/mockup.html.
    design_block = _read_design(state)
    if design_block:
        extra += design_block

    # Design-owned component kit (alignment by construction): design shipped the real
    # presentational components — the engineer WIRES them and may not modify/recreate
    # them or introduce parallel presentation for the same elements.
    kit_block = _read_kit(state)
    if kit_block:
        extra += kit_block
    if state.get("error_log"):
        extra += f"\n\nPREVIOUS FAILURE (fix this):\n{state['error_log']}"
    if state.get("review_notes"):
        extra += f"\n\nCEO REVIEW FEEDBACK (address this):\n{state['review_notes']}"

    if extend:
        existing = _existing_context(state, tech_spec)
        mode_block = f"""
You are EXTENDING an existing repository. Reuse existing modules/helpers. Match the
existing stack and conventions. Do NOT modify any test file.

{existing}"""
        format_block = """For each EXISTING file you change, output a MINIMAL edit — do NOT rewrite the whole
file. Use one or more search/replace pairs whose SEARCH text matches the current file
EXACTLY and UNIQUELY (copy enough surrounding lines to be unambiguous):

===EDIT: path/to/existing_file.py===
<<<<<<< SEARCH
<exact lines currently in the file>
=======
<the replacement lines>
>>>>>>> REPLACE
===END===

For each NEW file, output its full contents:

===FILE: path/to/new_file.py===
<file content here>
===END===

If an EXISTING file must be REMOVED (e.g. a stale/conflicting leftover such as a
pages/ entry conflicting with app/ in Next.js), output on its own line:

===DELETE: path/to/stale_file.ext==="""
        rules = "- Prefer minimal edits to existing files; only create files that don't exist.\n" \
                "- SEARCH text must match the shown file content exactly.\n" \
                "- Do NOT output any test file or e2e spec — the test author / QA own them."
    else:
        mode_block = "\nAUTHORITATIVE TESTS already exist under tests/ — make them pass; never edit them.\n"
        format_block = """For EACH file output EXACTLY this format:

===FILE: path/to/file.ext===
<file content here>
===END===

If a previously written file must be REMOVED (e.g. a stale/conflicting leftover),
output on its own line:

===DELETE: path/to/stale_file.ext==="""
        rules = "- Ship the app RUNNABLE: include a docker-compose.yml at the project root\n" \
                "  (services named api / frontend / db per the stack; db = postgres:17-alpine\n" \
                "  with a healthcheck; api publishes :8000 and exposes GET /health; frontend\n" \
                "  publishes :3000), plus the Dockerfiles and dependency manifests each\n" \
                "  service needs (requirements.txt / package.json with every test dep).\n" \
                "- Do NOT output anything under tests/ or e2e/ — test author / QA own those."

    context_msg = f"""
{spec_label}:
{tech_spec}
{mode_block}
EXISTING TESTS (must keep passing — do not edit them):
{test_code}

{qa_ctx}{extra}
"""

    project_dir = str(code_root(state))
    # I1: structured file changes through tools (default) — the ===FILE/EDIT===
    # text format caused every mechanical failure class observed live (corruption,
    # stale-SEARCH, destructive re-emits). AGENT_CODEGEN=text is the fallback.
    use_tools = os.environ.get("AGENT_CODEGEN", "tools").strip().lower() != "text"

    if use_tools:
        user_msg = f"""{context_msg}
Implement the changes now, directly in the working copy with your file tools.

Rules:
{_tools_rules(extend)}
- Keep each file focused. No placeholders or TODOs.
"""
        if allow_clarify:
            user_msg += "\n" + _clarify_instruction(CONSULT)
        result = codegen.generate(
            system, user_msg, project_dir, _protected_fn(state), tier="strong",
            apply_when=(lambda t: not _parse_needs_input(t)) if allow_clarify else None)
        questions = _parse_needs_input(result["text"]) if allow_clarify else None
        if questions:
            return {"_clarify": questions}
        code_path = project_dir if extend else str(code_root(state) / "src")
        written = result["written"]
        # ACCUMULATE across fix rounds: a tool-mode retry writes only the minimal
        # diff (a live round wrote just docker-compose.yml) — replacing the list
        # blinded QA to the actual implementation. Deletions drop out.
        code_files_out = sorted((set(state.get("code_files") or []) | set(written))
                                - set(result["deleted"]))
        _ok, security_warnings = scan_security(written)
        # Guard events are surfaced, not silently swallowed (shown at the PR gate).
        security_warnings += [f"GUARD: {v}" for v in result["violations"]]
        if result["violations"]:
            from tools.learnings import emit_feedback
            emit_feedback("engineer", "guard_violation", "; ".join(result["violations"]))
    else:
        questions, raw_output = work_call(system, user_msg_text(context_msg, format_block, rules),
                                          "strong", CONSULT, allow_clarify)
        if questions:
            return {"_clarify": questions}

        raw_output = _complete_if_truncated(system, raw_output)

        if extend:
            code_path, written, edit_failures = _apply_changes(raw_output, state)
            if edit_failures:
                # A stale/ambiguous SEARCH means we must not proceed on a half-applied change.
                # Include the CURRENT content of each failed file: the engineer keeps writing
                # SEARCH blocks from memory of its own past output, not what's on disk — three
                # live runs in a row burned attempts on stale SEARCHes before this was added.
                out = _fail(state, code_path,
                            "EDITS DID NOT APPLY (fix the SEARCH text to match the file exactly):\n"
                            + "\n".join(edit_failures)
                            + _current_content_of_failed(state, edit_failures),
                            qa_log, rounds, written)
                out["security_warnings"] = scan_security(written)[1]
                return out
        else:
            code_path, written = _parse_and_write_files(raw_output, state)

        # Phase 2.3: static security scan over the files we just wrote (non-blocking; surfaced).
        _ok, security_warnings = scan_security(written)
        # text path keeps its original contract: a delete-only round keeps the prior list
        code_files_out = written or state.get("code_files") or []

    # I3: kit-wiring ENFORCEMENT — the engineer is blocked from editing the kit, but a
    # live run simply ignored it and built parallel components (17 missing microcopy
    # strings caught only at integration). Deterministic check, fails the round with
    # the exact rule before any test/integration tokens are spent.
    kit_files = state.get("design_component_files") or []
    if kit_files:
        wired_ok, wiring_msg = check_kit_wiring(project_dir, kit_files)
        if not wired_ok:
            out = _fail(state, code_path,
                        f"KIT WIRING FAILURE (fix before anything else):\n{wiring_msg}",
                        qa_log, rounds, written)
            out["security_warnings"] = security_warnings
            return out

    # Linter: greenfield only — in extend mode we don't fail on the repo's pre-existing lint.
    if not extend:
        # Auto-fix + format FIRST (import-sort, pyupgrade, unused-import cleanup, ruff
        # format): can only make code cleaner and the E,F gate pass more often — never
        # blocks (non-blocking, graceful when ruff is absent).
        format_code(project_dir)
        lint_ok, lint_msg = run_linter(project_dir)
        if not lint_ok:
            out = _fail(state, code_path, f"LINTER FAILURE (fix before tests):\n{lint_msg}", qa_log, rounds, written)
            out["security_warnings"] = security_warnings
            return out

    # Toolchain-aware: detect each layer (pytest for Python, vitest/jest for JS) and run
    # the right tool. extend mode runs the repo's whole suite; greenfield runs tests/.
    test_target = "" if extend else "tests/"
    passed, error_log = run_project_tests(project_dir, test_path=test_target)

    # Advisory code-quality signal (non-blocking) — complexity/type/lint findings on the
    # Python just written + a frontend-tooling check + a dependency-lock check (every
    # third-party import must be a declared dep — kills the hallucinated-dependency drift
    # class). Surfaced at the PR gate like the security scan; scoped to written files so it
    # never reports pre-existing repo debt.
    code_quality = (code_quality_report(project_dir, written)
                    + check_frontend_quality_tooling(project_dir)
                    + check_dependencies(project_dir, written))

    # §2.1/2.2 code-quality soft gate — OPT-IN via QUALITY_GATE, DEFAULT OFF (no change).
    # report (or block): measure line coverage in a SEPARATE Docker run and surface it
    # advisory (coverage is NOT gated on the engineer — it can't edit tests/ to raise it).
    # block: also fail the round on over-budget COMPLEXITY (the engineer CAN refactor that),
    # bounded by MAX_FIX_ATTEMPTS. Only on a green, greenfield run (extend never gates).
    if passed and quality_gate_level():
        cov = measure_coverage(project_dir)
        if cov is not None:
            code_quality.append(f"coverage: {cov}% line (floor {COVERAGE_FLOOR}%)")
    if passed and not extend:
        gate_ok, gate_msg = check_quality_gate(project_dir, written)
        if not gate_ok:
            out = _fail(state, code_path, gate_msg, qa_log, rounds, written)
            out["security_warnings"] = security_warnings
            out["code_quality"] = code_quality
            return out

    return {
        "current_node": "engineer",
        "code_path": code_path,
        # QA reads these to review the implementation (#5); see code_files_out above.
        "code_files": code_files_out,
        "tests_passed": passed,
        "error_log": error_log[-MAX_ERROR_CHARS:] if error_log else None,  # tail = the failures
        "fix_attempts": state.get("fix_attempts", 0) + 1,
        "review_notes": None,   # consumed
        "security_warnings": security_warnings,
        "code_quality": code_quality,
        "qa_log": qa_log,
        "qa_rounds": rounds,
        "ceo_qa_from": None,
    }


def user_msg_text(context_msg: str, format_block: str, rules: str) -> str:
    """The legacy text-format work prompt (AGENT_CODEGEN=text fallback)."""
    return f"""{context_msg}
Generate the code.
{format_block}

Rules:
{rules}
- Keep each file focused. No placeholders or TODOs.
"""


def _tools_rules(extend: bool) -> str:
    base = ("- NEVER create, modify, or delete anything under tests/ or e2e/ (at any "
            "depth) or any design-kit file — the guard discards such changes and "
            "flags them.\n"
            "- Read an existing file before editing it; anchor edits on its ACTUAL "
            "current content.\n"
            "- Delete a stale/conflicting leftover (e.g. pages/ vs app/ duplicates) "
            "instead of working around it.")
    if extend:
        return ("- Make MINIMAL edits to existing files — do not rewrite whole files.\n"
                + base)
    return (base + "\n"
            "- Ship the app RUNNABLE: docker-compose.yml at the project root (services\n"
            "  api / frontend / db per the stack; db = postgres:17-alpine with a\n"
            "  healthcheck; api publishes :8000 and exposes GET /health; frontend\n"
            "  publishes :3000), plus each service's Dockerfile and dependency\n"
            "  manifests (requirements.txt / package.json with every test dep).")


def _protected_fn(state: dict):
    """The guard predicate for codegen sync-back — the oracles (tests/e2e), the
    design-owned kit, and (greenfield) the run's meta-artifact dirs."""
    protected = (set(state.get("test_files") or []) | set(state.get("e2e_files") or [])
                 | set(state.get("design_component_files") or []))
    extend = bool(state.get("target_repo"))

    def is_protected(rel: str) -> bool:
        parts = Path(rel).parts
        if rel in protected or any(p in ("tests", "e2e") for p in parts):
            return True
        # greenfield code_root is workspace/<id>, which also holds the run's
        # meta-artifacts — the engineer has no business touching them.
        if not extend and parts and parts[0] in ("prd", "design"):
            return True
        return False

    return is_protected


def _fail(state, code_path, msg, qa_log, rounds, written=None) -> dict:
    return {
        "current_node": "engineer",
        "code_path": code_path,
        "code_files": written or state.get("code_files") or [],
        "tests_passed": False,
        "error_log": msg[:MAX_ERROR_CHARS],
        "fix_attempts": state.get("fix_attempts", 0) + 1,
        "review_notes": None,
        "qa_log": qa_log,
        "qa_rounds": rounds,
        "ceo_qa_from": None,
    }


# The components manifest is a CONTRACT (props + REQUIRED MICROCOPY the app must show),
# like an API spec — truncating it starved the engineer of the wiring contract and it built
# a parallel component. Read it effectively UNTRUNCATED (24000-char safety ceiling only).
MANIFEST_CAP = 24000
DESIGN_SPEC_CAP = 16000
DESIGN_MOCKUP_CAP = 16000


def _read_kit(state: dict, manifest_cap: int = MANIFEST_CAP) -> str:
    """The design-owned component kit contract: wire these, never rewrite them."""
    files = state.get("design_component_files") or []
    if not files:
        return ""
    manifest = read_artifact(state["components_manifest_path"], manifest_cap) \
        if state.get("components_manifest_path") else "(manifest missing — read the kit files)"
    kit_list = "\n".join(f"- {f}" for f in files)
    return f"""

DESIGN-OWNED COMPONENT KIT — these presentational components ALREADY EXIST; design
owns their pixels and words. Your job is to WIRE them to business logic:
{kit_list}

WIRING MANIFEST (props contracts + the microcopy the app must show):
{manifest}

Kit rules (hard):
- Import and use these components for their UI. Pass data/handlers via their props.
- NEVER modify, rewrite, or re-emit any kit file — they are protected like tests/.
- NEVER build a parallel/duplicate component for something the kit already renders.
- You own: pages/containers, hooks, state, API calls, routing, non-kit chrome."""


def _read_design(state: dict, spec_cap: int = DESIGN_SPEC_CAP, mockup_cap: int = DESIGN_MOCKUP_CAP) -> str:
    """The design spec + HTML mockup, for visual fidelity. Empty when absent
    (quick lane, backend-only) — costs nothing in those cases."""
    parts = []
    if state.get("design_spec_path"):
        spec = read_artifact(state["design_spec_path"], spec_cap)
        if spec:
            parts.append(f"DESIGN SPEC (the UI you build must match this — layout, components, "
                         f"microcopy, states):\n{spec}")
    if state.get("design_mockup_path"):
        mockup = read_artifact(state["design_mockup_path"], mockup_cap)
        if mockup:
            parts.append(f"DESIGN MOCKUP HTML (the visual reference — reproduce its structure, "
                         f"hierarchy and copy in the real frontend; adapt classes to the "
                         f"project's styling system):\n{mockup}")
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def _read_tests(state: dict) -> str:
    """Read the authoritative tests (by the exact files the test author wrote)."""
    root = code_root(state)
    test_files = state.get("test_files") or []
    chunks = []
    if test_files:
        for rel in test_files:
            p = root / rel
            if p.exists():
                chunks.append(f"# === {rel} ===\n{read_artifact(str(p))}")
    else:
        tests_dir = root / "tests"
        if tests_dir.exists():
            for f in sorted(tests_dir.glob("*.py")):
                chunks.append(f"# === {f.name} ===\n{read_artifact(str(f))}")
    return "\n\n".join(chunks) if chunks else "(no tests found)"


def _existing_context(state: dict, tech_spec: str, max_files: int = 6) -> str:
    """In extend mode, show the repo map + the existing files the spec references."""
    target = state["target_repo"]
    parts = ["EXISTING CODEBASE MAP:"]
    if state.get("repo_map_path"):
        parts.append(read_artifact(state["repo_map_path"]))
    else:
        parts.append(repo_tools.build_repo_map(target))

    # Pull file-path-looking tokens from the spec that actually exist in the repo.
    candidates = set(re.findall(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+", tech_spec))
    shown = 0
    excerpts = []
    for rel in sorted(candidates):
        if (Path(target) / rel).is_file():
            excerpts.append(f"# === {rel} (existing — modify in place) ===\n"
                            f"{repo_tools.read_repo_file(target, rel, 2000)}")
            shown += 1
            if shown >= max_files:
                break
    if excerpts:
        parts.append("EXISTING FILES THE FEATURE TOUCHES:\n" + "\n\n".join(excerpts))
    return "\n\n".join(parts)


def _complete_if_truncated(system: str, raw: str) -> str:
    """If output stopped mid-file (more ===FILE: than ===END===), continue. Bounded."""
    cont = 0
    while (raw.count("===FILE:") + raw.count("===EDIT:")) > raw.count("===END===") and cont < MAX_CONTINUATIONS:
        more = call_llm(
            system,
            "Your previous output was cut off mid-file. Continue EXACTLY where you "
            "stopped — do not repeat files already emitted. Resume here:\n\n" + raw[-1500:],
            tier="strong",
        )
        raw += more
        cont += 1
    return raw


def _current_content_of_failed(state: dict, edit_failures: list, per_file_cap: int = 4000) -> str:
    """Append the CURRENT on-disk content of each file whose EDIT failed, so the retry
    can copy an exact SEARCH anchor instead of guessing from memory (#10)."""
    root = Path(state.get("target_repo") or code_root(state))
    seen, chunks = set(), []
    for failure in edit_failures:
        rel = failure.split(":", 1)[0].strip()
        if rel in seen or not rel:
            continue
        seen.add(rel)
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")[:per_file_cap]
        except OSError:
            continue
        chunks.append(f"\n--- CURRENT CONTENT OF {rel} (copy SEARCH text from THIS) ---\n{text}")
    return "".join(chunks)


_EDIT_BLOCK = r"===EDIT: (.+?)===\n(.*?)===END==="
_SEARCH_REPLACE = r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE"
_FILE_BLOCK = r"===FILE: (.+?)===\n(.*?)===END==="
_DELETE_BLOCK = r"===DELETE: (.+?)===\s*$"


def _file_blocks(raw: str) -> list:
    """Parse FILE blocks defensively. The model sometimes re-emits a file WITHOUT
    closing the previous block — the naive regex then swallows the second marker
    INTO the first file's content, writing corrupted source (a live run shipped
    layout.tsx with a literal '===FILE:...===' glued mid-JSX and 4 more like it).
    Rules: a block ends at ===END=== OR at the next ===FILE:/===EDIT: marker; when
    the same path is emitted twice, the LAST emission wins."""
    marker = re.compile(r"===(?:FILE|EDIT): (.+?)===\n")
    out: dict = {}
    for m in re.finditer(r"===FILE: (.+?)===\n", raw):
        rel = m.group(1).strip()
        rest = raw[m.end():]
        nxt = marker.search(rest)
        end = rest.find("===END===")
        cut = min(x for x in (end if end != -1 else len(rest),
                              nxt.start() if nxt else len(rest)))
        out[rel] = rest[:cut].strip()
    return list(out.items())


def _apply_changes(raw: str, state: dict):
    """
    Extend mode (#3 — minimal diffs): apply ===EDIT:=== search/replace blocks to existing
    files, and write ===FILE:=== blocks as new files. Test files are never touched.
    Returns (code_root_path, [written abs paths], [edit failure messages]).
    """
    target = state["target_repo"]
    protected = (set(state.get("test_files") or []) | set(state.get("e2e_files") or [])
                 | set(state.get("design_component_files") or []))  # design owns the kit
    written, failures = [], []

    def _is_test(rel):
        parts = Path(rel).parts
        # tests/ is the Test Author's oracle; e2e/ is QA's user-flow oracle (4.3).
        # ANY segment counts: the split layout puts them at backend/tests/, frontend/tests/.
        return rel in protected or any(p in ("tests", "e2e") for p in parts)

    for rel, body in re.findall(_EDIT_BLOCK, raw, re.DOTALL):
        rel = rel.strip()
        if _is_test(rel):
            continue
        pairs = re.findall(_SEARCH_REPLACE, body, re.DOTALL)
        if not pairs:
            failures.append(f"{rel}: malformed EDIT block (no SEARCH/REPLACE)")
            continue
        applied = False
        for search, replace in pairs:
            ok, msg = repo_tools.apply_edit(target, rel, search, replace)
            if ok:
                applied = True
            else:
                failures.append(msg)
        if applied:
            written.append(str(Path(target) / rel))

    for rel, content in _file_blocks(raw):
        if _is_test(rel):
            continue
        written.append(repo_tools.write_into_repo(target, rel, content))

    # DELETE blocks (#7): a retry may need to REMOVE a stale/conflicting leftover
    # (e.g. pages/index.tsx vs app/page.tsx) instead of accreting files forever.
    for rel in re.findall(_DELETE_BLOCK, raw, re.M):
        rel = rel.strip()
        if _is_test(rel):
            continue   # the oracles are never deletable
        ok, msg = repo_tools.delete_from_repo(target, rel)
        if not ok:
            failures.append(msg)

    return str(code_root(state)), written, failures


def _parse_and_write_files(raw: str, state: dict):
    """
    Write each ===FILE: path===...===END=== block.
    - Test files (state["test_files"] or anything under tests/) are SKIPPED.
    - extend mode: write at the real repo path (guarded). greenfield: under workspace/<id>/.
    Returns (code_root_path, [absolute paths written]).
    """
    extend = bool(state.get("target_repo"))
    protected = (set(state.get("test_files") or []) | set(state.get("e2e_files") or [])
                 | set(state.get("design_component_files") or []))  # design owns the kit
    root = code_root(state)
    written = []
    matches = _file_blocks(raw)

    if not matches:
        if extend:
            written.append(repo_tools.write_into_repo(state["target_repo"], "src/generated.py", raw))
        else:
            write_artifact(state["project_id"], "src", "generated.py", raw)
            written.append(str(root / "src" / "generated.py"))
        return str(root if extend else root / "src"), written

    for rel_path, content in matches:
        parts = Path(rel_path).parts
        if rel_path in protected or any(p in ("tests", "e2e") for p in parts):
            continue  # never clobber the oracles — tests/ or e2e/ at ANY depth
        if extend:
            written.append(repo_tools.write_into_repo(state["target_repo"], rel_path, content))
        else:
            subdir = parts[0] if len(parts) > 1 else "src"
            filename = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
            write_artifact(state["project_id"], subdir, filename, content)
            written.append(str(root / subdir / filename))

    # DELETE blocks (#7): allow a retry to remove a stale/conflicting leftover.
    for rel_path in re.findall(_DELETE_BLOCK, raw, re.M):
        rel_path = rel_path.strip()
        parts = Path(rel_path).parts
        if rel_path in protected or any(p in ("tests", "e2e") for p in parts):
            continue   # the oracles are never deletable
        if extend:
            repo_tools.delete_from_repo(state["target_repo"], rel_path)
        else:
            target = (root / rel_path)
            try:   # same escape guard as the repo writer: stay inside the project root
                target.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            if target.is_file():
                target.unlink()

    return str(root if extend else root / "src"), written
