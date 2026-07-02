"""
QA Agent
--------
DESIGN DECISION: QA does NOT re-run tests. Engineer already ran them (against the
Test Author's authoritative suite — Phase 1.1, so this is no longer "marking its
own homework"; the tests came from an independent agent).
- If passed: QA writes a sign-off report and routes to the PR approval gate.
  The PR itself is opened by the ship node AFTER the CEO approves (Phase 1.3).
- If failed: QA enriches error_log with a structured diagnosis for the engineer.

Q&A: on PASS, QA can ask CEO/PM/Engineer. On FAIL, QA skips Q&A — fast path to
diagnosis.
"""

import os
import re
from pathlib import Path

from graph.state import ProjectState
from tools import codegen
from tools.llm import call_llm
from tools.file_io import load_prompt, load_skill, read_artifact, write_artifact, code_root
from tools.learnings import augment_system, record_learning
from tools.registry import (detect_toolchains, lint_e2e_spec, resolve_kit_testids,
                            kit_state_suffixes)
from tools.qa_utils import run_with_qa, work_call, format_qa_context, product_invariants_block

CONSULT = ["ceo", "pm", "engineer"]
MAX_LINT_RETRIES = 1   # one re-author with lint findings, then drop the failing file
MAX_REVIEW_FILES = 12
# Generous per-file cap: QA reviews the ACTUAL source, so truncation here is a
# silent quality bug — a 3000-char cap cut a 3988-char app.js mid-function and QA
# wrongly flagged the frontend as "incomplete" (false-positive NO-GO). Truncation
# is a last-resort safety net, not the default (cf. file_io MAX_READ_CHARS).
MAX_REVIEW_CHARS_PER_FILE = 16000
# The wiring manifest is a CONTRACT (props + REQUIRED MICROCOPY the e2e selectors depend
# on) — like an API spec. A 4000-char cap starved QA of the contract when authoring specs.
# Read it effectively untruncated (24000-char safety ceiling only).
MANIFEST_CAP = 24000


def run(state: ProjectState) -> dict:
    # I4(e): integration's e2e stage failed while the app itself was healthy —
    # the specs are the prime suspect. ONE bounded spec-revision round.
    if state.get("e2e_revision_pending"):
        return _revise_e2e_specs(state)

    if not state["tests_passed"]:
        return _handle_fail(state)

    return run_with_qa(state, "qa", _do_pass_work, consultable_agents=CONSULT)


def _do_pass_work(state: dict, qa_log: list, rounds: dict, allow_clarify: bool = True) -> dict:
    system = augment_system(load_prompt("qa") + "\n\n" + load_skill("qa"), "qa")
    prd = read_artifact(state["prd_path"])
    qa_ctx = format_qa_context(qa_log, "qa")
    code = _read_code(state)
    security = state.get("security_warnings") or []
    sec_block = ("\n\nSECURITY SCAN FINDINGS (call these out explicitly and factor into "
                 "Go/No-go):\n- " + "\n- ".join(security)) if security else ""
    inv_block = product_invariants_block(state)

    user_msg = f"""
All tests passed. Now REVIEW THE ACTUAL CODE below against the PRD before signing off —
do not rubber-stamp. Look for: logic that passes the tests but violates the acceptance
criteria, missing/weak error handling, unhandled edge cases, and anything a careful
reviewer would block on.

PRD (acceptance criteria are the contract):
{prd}

GENERATED CODE:
{code}

{qa_ctx}{sec_block}{inv_block}

Write a QA sign-off report with these sections:
1. Features verified (map to PRD acceptance criteria; note any AC not satisfied)
2. Code review — tag each finding CRITICAL / MAJOR / MINOR:
   - CRITICAL: logic violates an AC, or missing/broken error handling that would fail in prod
   - MAJOR: unhandled edge case or weak validation that a user could trigger
   - MINOR: style/polish issue that doesn't affect correctness
   Also check: does this code change break any previously-passing behavior? Name it if so.
3. Security review (note any scan findings above; "none" if clean)
4. Known limitations / manual checks still needed
5. Verdict — end your report with exactly one of these lines:
   ===VERDICT: GO===
   ===VERDICT: NO-GO===
   NO-GO if any CRITICAL finding exists. MAJOR findings are noted but don't auto-block.

Keep it under 300 words.
"""

    questions, report = work_call(system, user_msg, "fast", CONSULT, allow_clarify)
    if questions:
        return {"_clarify": questions}

    # Phase 4.3: author the Playwright e2e specs for the integration stage. QA (not the
    # engineer) writes them, so the user-flow oracle stays independent of the implementer.
    e2e_files, e2e_notes = _author_e2e_specs(state, system, prd, code, qa_log)
    if e2e_files:
        report += "\n\n## E2E\nPlaywright user-flow specs written: " + ", ".join(e2e_files)
    if e2e_notes:
        report += "\n" + "\n".join(e2e_notes)

    write_artifact(state["project_id"], "tests", "qa_report.md", report)
    _emit_nogo_feedback(report)   # a NO-GO must teach the retro even if the CTO adjudicates/hand-fixes it

    return {
        "current_node": "qa",
        "tests_passed": True,
        "e2e_files": e2e_files,
        "approval_pending": "pr",   # route to PR approval gate; ship opens the PR after approval
        "qa_log": qa_log,
        "qa_rounds": rounds,
        "ceo_qa_from": None,
    }


def _blocking_finding(report: str) -> str:
    """The blocking parts of a NO-GO sign-off (the verdict + code-review findings) — not
    the AC checklist or the e2e list — so the retro distils the right CLASS of lesson."""
    parts = []
    for sec in ("Go / No-Go", "Go/No-Go", "Code Review", "Findings", "Known Limitations"):
        m = re.search(rf"##\s*{re.escape(sec)}\b.*?(?=\n##\s|\Z)", report, re.DOTALL | re.I)
        if m:
            parts.append(m.group(0).strip())
    return ("\n".join(parts) or report).strip()[:1500]


def _emit_nogo_feedback(report: str) -> bool:
    """A blocking code-review NO-GO is a real bug QA caught that the PASSING tests did
    NOT. Emit it for the end-of-run retro REGARDLESS of the gate outcome — before, only a
    gate REJECT taught anything, so a CTO-adjudicated or hand-fixed NO-GO (a derived field
    not recomputed; an e2e that never crosses the threshold) silently taught nothing. The
    retro GENERALISES the app-specific finding into a product-agnostic rule."""
    # Detect NO-GO via the structured signal first, then fall back to prose scan.
    # The structured signal (===VERDICT: NO-GO===) is unambiguous.
    # The prose fallback strips the "Go / No-Go" section header (which always contains
    # "No-Go") before scanning, so the header alone doesn't trigger a false positive.
    if re.search(r"===VERDICT:\s*NO-GO===", report, re.I):
        pass  # structured signal found — proceed
    else:
        verdict = re.sub(r"go\s*/\s*no[\s-]?go", " ", report, flags=re.I)
        if not re.search(r"\bNO[\s-]?GO\b", verdict, re.I):
            return False
    from tools.learnings import emit_feedback
    finding = _blocking_finding(report)
    emit_feedback("engineer", "qa_code_review_nogo", finding)   # the bug CLASS to prevent
    emit_feedback("qa", "passing_tests_missed_a_bug", finding)  # strengthen the oracle for this class
    return True


def _author_e2e_specs(state: dict, system: str, prd: str, code: str, qa_log: list) -> tuple:
    """
    Write Playwright specs for the feature's USER FLOW (Phase 4.3), to be run by the
    integration stage against the live composed stack. Returns (files, report_notes).

    Flows come from the feature request / PRD acceptance criteria (intent); the code
    QA just reviewed is used ONLY for concrete selectors, routes and API paths — the
    spec must assert what was ASKED FOR, not whatever the code happens to do.

    I4: the prompt carries the qa_log (CEO corrections used to never reach authoring —
    wrong API paths shipped twice), the kit's REAL data-testids as the ONLY selector
    source, and hard-won conventions; every authored file must pass the deterministic
    `lint_e2e_spec` gate (one re-author with findings, then the file is dropped).
    Re-passes never clobber existing specs (a live re-author overwrote a CTO-fixed
    spec with the same bug the fix removed).

    Only authored when the project has a frontend (node layer detected) — backend-only
    features are covered by the integration smoke (api /health). Authoring is
    best-effort: one retry on an empty result, then proceed without e2e rather than
    blocking (the integration smoke still runs; the gap is visible in the QA report).
    """
    project_dir = code_root(state)
    has_frontend = any(l["kind"] == "node" for l in detect_toolchains(str(project_dir)))
    if not has_frontend:
        return [], []

    # I4(d): a pass that already has live spec files does NOT re-author — re-emission
    # clobbered a human-repaired spec in a live run. Spec changes go through the
    # explicit revision path (e2e_revision_pending) instead.
    existing = [f for f in (state.get("e2e_files") or []) if (project_dir / f).exists()]
    if existing:
        return existing, ["E2E specs kept from the previous pass (no re-author on a re-pass)."]

    # The tech spec carries the API contract (routes, request/response shapes) — give it
    # to QA so specs assert the REAL schema instead of guessing (a live run's specs tried
    # body.tasks ?? body.data and failed against the actual response shape).
    tech_spec = read_artifact(state["design_path"]) if state.get("design_path") else ""
    qa_ctx = format_qa_context(qa_log, "qa")
    kit_block, kit_ids, kit_prefixes = _kit_selector_block(state)

    user_msg = f"""
Now write Playwright e2e spec(s) covering the requested USER FLOW end-to-end, to run
against the LIVE app (docker compose). LANGUAGE IS PYTHON: pytest-playwright (sync
API), run with `pytest --browser chromium`. Base URLs come from env:
  BASE = os.environ.get("E2E_BASE_URL", "http://frontend:3000")   # the UI
  API  = os.environ.get("API_BASE_URL", "http://api:8000")        # the backend
Drive UI flows through BASE only (the app calls its API same-origin); use API only for
direct backend checks (page.request.get/post/delete against f"{{API}}..."), with the
EXACT routes and response shapes from the tech spec.

FEATURE REQUEST (the intent your spec must verify):
{state.get("feature_request", "")}

PRD acceptance criteria (assert these through the UI):
{prd}

THE UI ACCEPTANCE CRITERIA YOU MUST COVER (each needs >=1 e2e test that declares
`# covers: <id>` above its `def test_...`):
{_ui_ac_block(prd)}

TECH SPEC (authoritative API contract — routes and response shapes; do not guess):
{tech_spec}

IMPLEMENTED CODE (use ONLY for selectors, routes and API paths — do not derive
expectations from it):
{code[:30000]}

{qa_ctx}{kit_block}

Rules:
- Drive the flow like a user: navigate, fill, click, and assert visible outcomes.
{_selector_rule(bool(kit_ids or kit_prefixes))}
- API paths: copy them EXACTLY as they appear in the tech spec/implementation — never
  shorten or guess (a live run wrote /tasks instead of /api/tasks, twice).
- ISOLATION IS MANDATORY: every spec file has an @pytest.fixture(autouse=True) that
  resets the app's data through the API before each test (list entities via
  page.request, delete each). Unique titles are NOT isolation — the live DB is shared
  and leaked entities pollute other specs' counts.
- Styled checkboxes/toggles AND hover-revealed buttons: .check()/.click() can silently
  no-op — use locator.evaluate("el => el.click()").
- COVERAGE IS THE CONTRACT: every UI acceptance criterion listed above must be covered
  by a test that declares `# covers: AC-N` above its `def test_...` (one journey test
  may cover several — tag all of them).
- SCOPE IS BOUNDED (a live authoring call exceeded the output ceiling writing a test
  per criterion): write ONE spec file with AT MOST 10 focused tests. Compose acceptance
  criteria into end-to-end USER JOURNEYS (e.g. signup → act → verify outcome covers
  several criteria in one test) plus the single most important unhappy path. Keep the
  file under ~350 lines; shared helpers, no repetition.
- Self-contained Python: only `pytest` and `playwright.sync_api` imports
  (from playwright.sync_api import Page, expect). No other deps, no repo fixtures.
- Keep it deterministic: expect(...) assertions with sensible timeouts, no sleeps.
- Emit RAW Python only — no markdown fences.

{_e2e_emit_instruction()}
"""
    files = _emit_e2e(state, system, user_msg)
    if not _has_real_specs(project_dir, files):
        retry = user_msg + ("\n\nYOUR PREVIOUS OUTPUT HAD NO RUNNABLE SPEC — you MUST "
                            "create at least one e2e/test_<flow>.py with def test_...(page) "
                            "cases and expect(...) assertions.")
        files = _emit_e2e(state, system, retry)
        if not _has_real_specs(project_dir, files):
            return [], ["E2E authoring produced no runnable spec — integration smoke only."]

    # I4(b): deterministic lint gate — a broken spec is a broken ORACLE; it burns a
    # whole integration round and points the blame at the engineer.
    return _lint_gate(state, system, user_msg, files, kit_ids, kit_prefixes,
                      known_paths_text=code + "\n" + tech_spec)


def _lint_gate(state: dict, system: str, user_msg: str, files: list,
               kit_ids: set, kit_prefixes: tuple, known_paths_text: str) -> tuple:
    """Lint every authored spec; one re-author with the findings; still-failing files
    are DELETED (a known-bad oracle must not reach integration). Returns (files, notes)."""
    project_dir = code_root(state)
    notes = []
    for attempt in range(MAX_LINT_RETRIES + 1):
        findings = {}
        for rel in files:
            try:
                content = (Path(project_dir) / rel).read_text(encoding="utf-8")
            except OSError:
                continue
            f = lint_e2e_spec(content, kit_testids=kit_ids, testid_prefixes=kit_prefixes,
                              known_paths_text=known_paths_text)
            if f:
                findings[rel] = f
        if not findings:
            return files, notes
        if attempt < MAX_LINT_RETRIES:
            flat = "\n".join(f"- {rel}: {msg}" for rel, msgs in findings.items() for msg in msgs)
            retry = (user_msg + "\n\nYOUR SPECS FAILED THE DETERMINISTIC LINT — fix EXACTLY "
                     "these findings (edit the named files):\n" + flat)
            files = _emit_e2e(state, system, retry) or files
    # still failing after the bounded retry → drop the bad files, keep the clean ones
    clean = [r for r in files if r not in findings]
    for rel in findings:
        try:
            (Path(project_dir) / rel).unlink()
        except OSError:
            pass
        notes.append(f"E2E spec {rel} DROPPED — failed lint: " + "; ".join(findings[rel]))
        from tools.learnings import emit_feedback
        emit_feedback("qa", "e2e_lint_drop", "; ".join(findings[rel]))
    return clean, notes


def _selector_rule(kit: bool) -> str:
    if kit:
        return ("- SELECTORS: use ONLY the kit data-testids listed above "
                "(page.get_by_test_id(...) / page.locator('[data-testid^=\"...\"]')). NEVER "
                "get_by_label/get_by_placeholder/CSS-class guesses — a live getByLabel(/task/i) "
                "matched the FORM's aria-label instead of the input and failed 4 tests.")
    return "- Prefer accessible selectors (get_by_role/get_by_label/get_by_placeholder/get_by_text)."


def _kit_selector_block(state: dict) -> tuple:
    """The design kit's REAL data-testids — the only selector contract that exists by
    construction. Suffix-rendering components (e.g. RelationshipButton renders
    `<base>-add-friend`, NEVER the bare base) are RESOLVED via resolve_kit_testids, so QA
    authors the real ids — not the never-rendered base that failed 3 e2e tests in a live
    phase-2 run. Returns (prompt_block, static_ids, dynamic_prefixes)."""
    root = code_root(state)
    sources = {}
    for rel in state.get("design_component_files") or []:
        p = Path(rel) if Path(rel).is_absolute() else root / rel
        if p.exists():
            sources[rel] = p.read_text(encoding="utf-8", errors="replace")
    static, prefixes = resolve_kit_testids(sources)
    if not (static or prefixes):
        return "", set(), ()
    suffix_note = ""
    suffixers = kit_state_suffixes(sources)
    if suffixers:
        suffix_note = ("State-suffixed components (the rendered id is <base> + ONE of these "
                       "suffixes — never the bare base): "
                       + "; ".join(f"{c} → {'|'.join(s)}"
                                   for c, s in sorted(suffixers.items())) + "\n")
    manifest = read_artifact(state["components_manifest_path"], MANIFEST_CAP) \
        if state.get("components_manifest_path") else ""
    block = ("\n\nKIT SELECTOR CONTRACT (design-owned components — the ONLY selector source):\n"
             "Static data-testids: " + ", ".join(sorted(static)) + "\n"
             + ("Dynamic data-testid prefixes (entity ids appended): "
                + ", ".join(sorted(prefixes)) + "\n" if prefixes else "")
             + suffix_note
             + (f"\nWIRING MANIFEST:\n{manifest}\n" if manifest else ""))
    return block, static, tuple(sorted(prefixes))


def _revise_e2e_specs(state: dict) -> dict:
    """I4(e): the app passed compose/health/smoke but the e2e RUN failed — revise the
    SPECS (selector/path/isolation mechanics), not the app. One bounded round; the
    revised specs go straight back to integration (tests_passed stays True). If the
    failures are genuinely app defects the next integration round still fails and
    routes to the engineer as before — nothing is masked."""
    system = augment_system(load_prompt("qa") + "\n\n" + load_skill("qa"), "qa")
    project_dir = code_root(state)
    kit_block, kit_ids, kit_prefixes = _kit_selector_block(state)

    current = []
    for rel in (state.get("e2e_files") or []):
        p = Path(project_dir) / rel
        if p.exists():
            current.append(f"===CURRENT {rel}===\n{p.read_text(encoding='utf-8')}")

    user_msg = f"""
The application itself is HEALTHY (compose up, health and smoke checks all passed) but
your Playwright e2e specs FAILED against the live app. The most likely defects are in
the SPECS: wrong selectors, guessed API paths, or missing isolation.

FAILING E2E OUTPUT:
{state.get("error_log", "")}

CURRENT SPECS:
{chr(10).join(current)}
{kit_block}
Fix ONLY spec mechanics (selectors, paths, isolation, waits). Do NOT weaken, remove,
or invert any assertion that expresses a PRD acceptance criterion — if you believe a
failure is a real APP bug, leave that assertion exactly as is.
{_selector_rule(bool(kit_ids or kit_prefixes))}

Re-emit EVERY file you change, in full, in the SAME LANGUAGE the file already uses
(.py = pytest-playwright sync API, .spec.ts = legacy @playwright/test), no markdown fences:
===FILE: e2e/<same filename>===
<content>
===END===
"""
    from tools.learnings import emit_feedback
    emit_feedback("qa", "e2e_revision_needed", (state.get("error_log") or "")[:900])
    files = _emit_e2e(state, system, user_msg)
    # The revised files pass the same deterministic gate as first-authored ones —
    # a live revision emitted a markdown-fenced (SyntaxError) file before this.
    if files:
        files, _notes = _lint_gate(state, system, user_msg, files, kit_ids, kit_prefixes,
                                   known_paths_text="")
    merged = sorted(set(state.get("e2e_files") or []) | set(files))
    return {
        "current_node": "qa",
        "tests_passed": True,            # qa_routing → integration re-runs the e2e
        "e2e_files": merged,
        "e2e_revision_pending": False,
        "e2e_revised": True,             # the one bounded round is spent
        "error_log": None,
        "qa_log": list(state.get("qa_log") or []),
        "qa_rounds": dict(state.get("qa_rounds") or {}),
        "ceo_qa_from": None,
    }


def _use_tools() -> bool:
    return os.environ.get("AGENT_CODEGEN", "tools").strip().lower() != "text"


def _ui_ac_block(prd: str) -> str:
    """The UI acceptance criteria QA must cover with e2e, listed by id."""
    from tools import contract
    ui = [a for a in contract.parse_acs(prd) if a["surface"] == "ui"]
    if not ui:
        return "(no UI acceptance criteria parsed — cover every user-visible criterion)"
    return "\n".join(f"  {a['id']}: {a['text']}" for a in ui)


def _e2e_emit_instruction() -> str:
    if _use_tools():
        return ("Write/edit the spec files DIRECTLY with your file tools, under e2e/ "
                "(test_<flow>.py). READ an existing spec before you change it — edit it, "
                "never blind-rewrite. You may ONLY touch files under e2e/.")
    return ("For EACH file output EXACTLY:\n===FILE: e2e/test_<flow>.py===\n"
            "<content>\n===END===")


def _emit_e2e(state: dict, system: str, user_msg: str) -> list:
    """I17: author/revise e2e specs through the codegen tools path (Read-before-write,
    e2e/-only domain, no text parsing). Returns the e2e/ relpaths now on disk."""
    root = code_root(state)
    if not _use_tools():
        raw = call_llm(system, user_msg, tier="strong")
        return _write_e2e_files(raw, state)
    codegen.generate_in_domain(system, user_msg, str(root),
                               allowed_segments={"e2e"}, tier="strong")
    e2e_dir = root / "e2e"
    if not e2e_dir.is_dir():
        return []
    return sorted(f"e2e/{p.name}" for p in e2e_dir.glob("test_*.py"))


def _write_e2e_files(raw: str, state: dict) -> list:
    pattern = r"===FILE: (.+?)===\n(.*?)===END==="
    written = []
    root = code_root(state)
    for rel_path, content in re.findall(pattern, raw, re.DOTALL):
        rel_path = rel_path.strip()
        name = Path(rel_path).name
        # Python (the authoring language) or legacy TS (revision of old specs only)
        if not ((name.startswith("test_") and name.endswith(".py"))
                or name.endswith(".spec.ts") or name.endswith(".spec.js")):
            continue
        content = _strip_md_fences(content.strip())
        dest = root / "e2e" / name      # everything lands flat under e2e/
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content + "\n", encoding="utf-8")
        written.append(f"e2e/{name}")
    return written


from tools.file_io import strip_md_fences as _strip_md_fences  # shared de-fencer


def _has_real_specs(project_dir, files: list) -> bool:
    for rel in files:
        try:
            text = (Path(project_dir) / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        if ("expect(" in text and
                (re.search(r"\btest\s*\(", text) or re.search(r"^def test_", text, re.M))):
            return True
    return False


def _read_code(state: dict) -> str:
    """Read the files the engineer wrote so QA can actually review the implementation (#5).
    VERIFICATION-RUN FALLBACK: a no-op engineer round records no files (a live QA
    escalated asking for a git remote because its context was empty) — on a managed/
    extend run, fall back to the project tree's own source files, newest first."""
    files = state.get("code_files") or []
    if not files and (state.get("target_repo") or state.get("managed_project")):
        root = code_root(state)
        candidates = [p for pat in ("backend/app/*.py", "frontend/src/components/**/*.tsx",
                                    "frontend/src/app/**/*.tsx", "src/**/*.py")
                      for p in root.glob(pat)
                      if p.is_file() and "kit" not in p.parts and p.name != "__init__.py"]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        files = [str(p) for p in candidates]
    chunks = []
    for p in files[:MAX_REVIEW_FILES]:
        content = read_artifact(p, MAX_REVIEW_CHARS_PER_FILE)
        if content:
            chunks.append(f"# === {Path(p).name} ===\n{content}")
    return "\n\n".join(chunks) if chunks else "(no code files were recorded to review)"


def _handle_fail(state: ProjectState) -> dict:
    """
    Diagnose the failure so the engineer gets structured context, not raw noise.
    Also distil a generalizable LESSON and record it (Phase 2.2) so future engineer
    runs don't repeat this class of bug — folded into the same call (no extra cost).
    """
    system = augment_system(load_prompt("qa") + "\n\n" + load_skill("qa"), "qa")

    user_msg = f"""
Test run failed. Diagnose this error output and give the engineer
a structured fix hint in max 150 words.

Format:
ROOT CAUSE: one sentence
FIX: specific code change needed
FILES LIKELY AFFECTED: list file names

Then, on a final separate line, output one GENERALIZABLE rule (not specific to this
feature) that would prevent this whole class of failure next time, prefixed exactly:
LESSON: <the rule>

Error output (the tail contains the actual failures — diagnose those, not warnings):
{state.get("error_log", "No error log captured.")}
"""

    diagnosis = call_llm(system, user_msg, tier="fast")

    # Record the generalizable lesson for the engineer (deduped + capped in learnings.py).
    m = re.search(r"LESSON:\s*(.+)", diagnosis)
    if m:
        record_learning("engineer", m.group(1))

    # Diagnosis first (head), raw output tail-sliced — a 500-char cap here once cut the
    # real assertion mid-line and QA "diagnosed" a truncated test that didn't exist.
    raw = state.get("error_log", "")
    enriched = f"{diagnosis}\n\nRAW OUTPUT (tail):\n{raw[-4000:]}"

    out = {
        "current_node": "qa",
        "tests_passed": False,
        "error_log": enriched[:6000],
        "qa_log": list(state.get("qa_log") or []),
        "qa_rounds": dict(state.get("qa_rounds") or {}),
    }

    # Quick lane at the retry cap routes to pr_gate (the CEO decides on the imperfect
    # diff) — mark the approval as pending so the driver PAUSES for a human. Without
    # this, a live run sailed through the gate interrupt unprompted and pr_gate's old
    # default-approve shipped red code with zero human sign-off.
    from graph.graph import MAX_FIX_ATTEMPTS
    if (state.get("fix_attempts", 0) >= MAX_FIX_ATTEMPTS
            and (state.get("change_type") or "feature") != "feature"):
        out["approval_pending"] = "pr"
    return out
