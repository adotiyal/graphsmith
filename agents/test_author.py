"""
Test Author Agent (Phase 1.1 — TDD; Phase 2.1 — extend mode)
------------------------------------------------------------
DESIGN DECISION: tests are written BEFORE the engineer, by a different agent.
- Kills "marking your own homework": the engineer doesn't write the tests judging it.
- The engineer must make these pass without modifying them (protected via test_files).

Greenfield: writes tests under workspace/<id>/tests/.
Extend mode: writes test files INTO the target repo at the paths the LLM specifies,
following the repo's existing test conventions (which it is shown), and records the
exact relpaths it wrote in state["test_files"] so the engineer can't clobber them.

Q&A: may consult CEO/CTO or Architect if a criterion is too ambiguous to test.
"""

import os
import re
from pathlib import Path

from graph.state import ProjectState
from tools.file_io import (
    load_prompt, load_skill, read_artifact, write_artifact, code_root, WORKSPACE_ROOT,
)
from tools import codegen, contract, repo as repo_tools
from tools.learnings import augment_system
from tools.qa_utils import run_with_qa, work_call, format_qa_context, product_invariants_block

CONSULT = ["ceo", "architect"]


def _use_tools() -> bool:
    # I17: default path routes file emission through the codegen tools (Read-before-
    # write, no text parsing). AGENT_CODEGEN=text keeps the legacy path for the
    # mocked unit tests (which stub call_llm, not the tools backend).
    return os.environ.get("AGENT_CODEGEN", "tools").strip().lower() != "text"


def _emit_instruction(extend: bool) -> str:
    if _use_tools():
        loc = ("the repo's existing test location/conventions" if extend
               else "tests/test_<area>.py")
        return (f"Write the test files DIRECTLY with your file tools, under {loc} "
                "(READ any existing tests first and extend them; never rewrite a file "
                "you haven't read). You may ONLY create/edit test files.")
    return ("For EACH file output EXACTLY this format:\n\n"
            + ("===FILE: <path matching the repo's test layout>===" if extend
               else "===FILE: tests/test_<area>.py===")
            + "\n<file content here>\n===END===")


def run(state: ProjectState) -> dict:
    return run_with_qa(state, "test_author", _do_work, consultable_agents=CONSULT)


def _do_work(state: dict, qa_log: list, rounds: dict, allow_clarify: bool = True) -> dict:
    identity = load_prompt("test_author")
    skill = load_skill("test_author")
    system = augment_system(f"{identity}\n\n{skill}" if skill else identity, "test_author")

    prd = read_artifact(state["prd_path"])
    tech_spec = read_artifact(state["design_path"])
    qa_ctx = format_qa_context(qa_log, "test_author")
    extend = bool(state.get("target_repo"))

    conventions = _existing_test_conventions(state) if extend else ""

    acs = contract.parse_acs(prd)
    ac_block = "\n".join(f"  {a['id']} ({a['surface']}): {a['text']}" for a in acs)

    inv_block = product_invariants_block(state)

    user_msg = f"""
PRD (acceptance criteria are the contract you must test):
{prd}

THE ACCEPTANCE-CRITERIA CONTRACT — you must cover EVERY one of these by id:
{ac_block or "(no AC ids parsed — cover every acceptance criterion in the PRD)"}

Technical Spec (API contracts, data models, file structure to import from):
{tech_spec}
{conventions}{inv_block}
{qa_ctx}

Write the authoritative pytest suite. {_emit_instruction(extend)}

Rules:
- COVERAGE IS THE CONTRACT: every AC above must be covered by >=1 test, and each test
  must declare which it covers with a comment `# covers: AC-N` (e.g. `# covers: AC-1, AC-3`)
  on the line above the `def test_...`. Backend ACs are yours to fully verify here;
  UI ACs get at least a logic-level test here (QA adds the e2e).
- Every acceptance criterion maps to at least one test.
- {"Place tests where this repo keeps them and follow its conventions." if extend
   else "Tests live ONLY under tests/."} Do not write application code.
- Note test-only deps in a top comment (e.g. # requires: httpx).
- Complete, real assertions. No placeholders.
"""

    # Test authoring is the correctness ORACLE for the whole system; it runs on the
    # `strong` tier so a full suite + fixtures can't be truncated away.
    if _use_tools():
        test_files = _author_via_tools(state, system, user_msg, extend, allow_clarify)
        if isinstance(test_files, dict):   # a clarify request
            return test_files
    else:
        questions, raw = work_call(system, user_msg, "strong", CONSULT, allow_clarify)
        if questions:
            return {"_clarify": questions}
        test_files = _write_test_files(raw, state, extend)

    # Guard: a suite of only fixtures/conftest is NOT an oracle. If no runnable
    # `def test_` case was produced, retry once with an explicit correction; if it
    # STILL has none, escalate to the CEO/CTO rather than passing an empty oracle
    # downstream (which makes the engineer "fail" against tests that don't exist).
    if not _has_real_tests(state, test_files, extend):
        correction = user_msg + (
            "\n\nYOUR PREVIOUS OUTPUT HAD NO RUNNABLE TESTS — only fixtures/markers. "
            "You MUST write at least one tests/test_<area>.py containing real "
            "`def test_...` functions with assertions, in addition to any conftest.py. "
            "Do not output only fixtures."
        )
        if _use_tools():
            test_files = _author_via_tools(state, system, correction, extend,
                                           allow_clarify=False)
        else:
            _, raw = work_call(system, correction, "strong", CONSULT, allow_clarify=False)
            test_files = _write_test_files(raw, state, extend)
        if not _has_real_tests(state, test_files, extend):
            return {"_clarify": {"ceo":
                "Test Author could not produce a runnable test suite (no `def test_` "
                "cases) for this feature, even after a retry. The acceptance criteria "
                "may be too ambiguous to test as written — please clarify the expected "
                "testable behaviors, or simplify the scope."
            }}

    # COVERAGE SELF-CHECK (zero-drift): every acceptance criterion must be referenced
    # by >=1 test. Catching a gap HERE (one extra authoring round) is far cheaper than
    # discovering it at the gate or in production (the AC8/avatar-on-card class).
    missing = _uncovered_acs(state, prd, test_files)
    if missing:
        gap = user_msg + (
            "\n\nCOVERAGE GAP — these acceptance criteria have NO test that declares "
            "`# covers: <id>`: " + ", ".join(missing) + ". Add tests (or the missing "
            "`# covers:` tags to existing tests) so EVERY listed AC is covered. Keep all "
            "existing tests.")
        if _use_tools():
            test_files = _author_via_tools(state, system, gap, extend, allow_clarify=False)
        else:
            _, raw = work_call(system, gap, "strong", CONSULT, allow_clarify=False)
            test_files = _write_test_files(raw, state, extend) or test_files

    return {
        "current_node": "test_author",
        "test_path": str(code_root(state) / "tests"),
        "test_files": test_files,
        "qa_log": qa_log,
        "qa_rounds": rounds,
        "ceo_qa_from": None,
    }


def _uncovered_acs(state: dict, prd: str, test_files: list) -> list:
    """Acceptance criteria with no `# covers: AC-N` reference in the written tests.
    Reads files from disk (post-write). Empty when fully covered or no ACs parsed."""
    acs = contract.parse_acs(prd)
    if not acs:
        return []
    root = code_root(state)
    texts = []
    for rel in test_files or []:
        p = root / rel
        if p.exists():
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
    cov = contract.coverage(acs, texts, [])
    return cov["uncovered"]


def _has_real_tests(state: dict, test_files: list, extend: bool) -> bool:
    """True iff at least one written file is a test_*.py / *_test.py with a `def test_`.

    Reads the files back from disk (they were just written) so the check reflects what
    the engineer + pytest will actually see, not just the raw LLM text.
    """
    base = Path(state["target_repo"]) if extend else (WORKSPACE_ROOT / state["project_id"])
    for rel in test_files:
        name = Path(rel).name
        if not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        try:
            text = (base / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r"^\s*def test_\w+", text, re.M):
            return True
    return False


def _existing_test_conventions(state: dict, max_files: int = 3) -> str:
    """Show the engineer-of-tests how this repo already writes tests."""
    target = state["target_repo"]
    hits = repo_tools.grep(target, r"def test_|describe\(|it\(", max_hits=30)
    seen, chunks = set(), []
    for relpath, _ln, _line in hits:
        if relpath in seen:
            continue
        seen.add(relpath)
        chunks.append(f"# === {relpath} ===\n{repo_tools.read_repo_file(target, relpath, 1200)}")
        if len(seen) >= max_files:
            break
    if not chunks:
        return "\nEXISTING TESTS: none found — establish a sensible tests/ layout.\n"
    return "\nEXISTING TEST CONVENTIONS (match these — layout, imports, fixtures):\n" + "\n\n".join(chunks) + "\n"


def _author_via_tools(state: dict, system: str, user_msg: str, extend: bool,
                      allow_clarify: bool):
    """I17: write the test suite through the codegen tools path — the author READS
    existing tests before changing them and may ONLY touch tests/ (everything else is
    guard-protected). Returns the written tests/ relpaths, or a {"_clarify": ...} dict."""
    from tools.qa_utils import _clarify_instruction, _parse_needs_input
    root = code_root(state)
    msg = user_msg
    if allow_clarify:
        msg = f"{user_msg}\n\n{_clarify_instruction(CONSULT)}"
    result = codegen.generate_in_domain(
        system, msg, str(root), allowed_segments={"tests"}, tier="strong",
        apply_when=(lambda t: not _parse_needs_input(t)) if allow_clarify else None)
    if allow_clarify:
        q = _parse_needs_input(result["text"])
        if q:
            return {"_clarify": q}
    return [str(Path(p).relative_to(root)) for p in result["written"]]


def _write_test_files(raw: str, state: dict, extend: bool) -> list:
    """Write each ===FILE:===END=== block; return the list of relpaths written."""
    pattern = r"===FILE: (.+?)===\n(.*?)===END==="
    matches = re.findall(pattern, raw, re.DOTALL)
    written = []

    if not matches:
        if extend:
            repo_tools.write_into_repo(state["target_repo"], "tests/test_generated.py", raw)
            return ["tests/test_generated.py"]
        write_artifact(state["project_id"], "tests", "test_generated.py", raw)
        return ["tests/test_generated.py"]

    for rel_path, content in matches:
        rel_path = rel_path.strip()
        content = content.strip()
        if extend:
            repo_tools.write_into_repo(state["target_repo"], rel_path, content)
            written.append(rel_path)
        else:
            filename = Path(rel_path).parts[-1]
            write_artifact(state["project_id"], "tests", filename, content)
            written.append(f"tests/{filename}")
    return written
