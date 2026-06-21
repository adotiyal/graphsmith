"""
I4 verification suite — QA e2e spec quality pack.

Every lint rule and routing change traces to a live failure: invented testids,
getByLabel matching the form's aria-label (twice in one run), /tasks vs /api/tasks
(twice), .check() no-op on styled checkboxes, missing isolation polluting counts,
re-author clobbering a human-repaired spec, and e2e reds burning engineer rounds.
"""

from pathlib import Path

import pytest

from tests.conftest import base_state, seed
from tools.registry import lint_e2e_spec

KIT_IDS = {"task-title-input", "task-due-date-input", "add-task-button", "stats-bar"}
KIT_PREFIXES = ("task-card-",)
KNOWN_PATHS = "fetch('/api/tasks')  '/api/tasks'  '/health'"

GOOD_SPEC = """
import { test, expect } from '@playwright/test';
const BASE = process.env.E2E_BASE_URL || 'http://frontend:3000';
const API = process.env.API_BASE_URL || 'http://api:8000';
test.beforeEach(async ({ request }) => {
  const res = await request.get(`${API}/api/tasks`);
  for (const t of (await res.json()).items ?? []) await request.delete(`${API}/api/tasks/${t.id}`);
});
test('adds a task', async ({ page }) => {
  await page.goto(BASE);
  await page.getByTestId('task-title-input').fill('Buy milk');
  await page.getByTestId('add-task-button').click();
  await expect(page.locator('li[data-testid^="task-card-"]')).toHaveCount(1);
});
"""


# ---------------------------------------------------------------------------
# lint rules
# ---------------------------------------------------------------------------

def test_lint_clean_spec_passes():
    assert lint_e2e_spec(GOOD_SPEC, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS) == []


def test_lint_flags_invented_testid():
    bad = GOOD_SPEC.replace("'stats-bar'", "x").replace(
        "getByTestId('task-title-input')", "getByTestId('stats-total')")
    findings = lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS)
    assert any("stats-total" in f and "no such data-testid" in f for f in findings)


def test_lint_accepts_dynamic_prefix_testid():
    spec = GOOD_SPEC + "\n// page.getByTestId('task-card-abc123')\n".replace("// ", "")
    spec = GOOD_SPEC.replace("getByTestId('add-task-button')",
                             "getByTestId('task-card-abc123')")
    assert lint_e2e_spec(spec, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS) == []


def test_lint_flags_label_and_css_class_when_kit_exists():
    bad = GOOD_SPEC.replace("page.getByTestId('task-title-input')",
                            "page.getByLabel(/title|task/i).first()")
    bad += "\nconst row = page.locator('.overdue');\n"
    findings = lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS)
    assert any("aria-label" in f for f in findings)          # the live failure, named
    assert any("CSS-class" in f for f in findings)
    # without a kit, accessible selectors are fine
    assert lint_e2e_spec(bad, set(), (), KNOWN_PATHS) == []


def test_lint_flags_check_flake():
    bad = GOOD_SPEC + "\n// await box.check();\n".replace("// ", "")
    bad = GOOD_SPEC.replace("await page.getByTestId('add-task-button').click();",
                            "await page.getByTestId('add-task-button').check();")
    findings = lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS)
    assert any("evaluate" in f for f in findings)


def test_lint_ignores_comments():
    # Live false positive: a first-pass spec CITED the convention in a comment
    # ("// .check() can silently no-op") and was dropped for it.
    spec = GOOD_SPEC + (
        "\n// Styled checkbox: .check()/.click() can silently no-op — use evaluate\n"
        "/* also never page.getByLabel(/task/i) — matched the form's aria-label */\n")
    assert lint_e2e_spec(spec, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS) == []


def test_writer_strips_markdown_fences(ws):
    # Live failure: a revision wrapped the spec in ```typescript fences INSIDE the
    # ===FILE=== block → SyntaxError at line 1 → the WHOLE e2e suite red.
    from agents import qa
    raw = "===FILE: e2e/x.spec.ts===\n```typescript\ntest('a', () => {});\n```\n===END==="
    state = base_state()
    files = qa._write_e2e_files(raw, state)
    from tools.file_io import WORKSPACE_ROOT
    text = (ws / "proj" / "e2e" / "x.spec.ts").read_text()
    assert files == ["e2e/x.spec.ts"]
    assert "```" not in text and text.startswith("test('a'")


def test_lint_flags_stray_markdown_fence():
    bad = GOOD_SPEC + "\n```\n"
    findings = lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS)
    assert any("markdown" in f for f in findings)


def test_lint_flags_guessed_api_path():
    # the live bug: ${API}/tasks instead of ${API}/api/tasks
    bad = GOOD_SPEC.replace("${API}/api/tasks", "${API}/tasks")
    findings = lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS)
    assert any("/tasks" in f and "not found" in f for f in findings)
    # templated id suffixes on a KNOWN path are fine (covered by GOOD_SPEC's delete call)


def test_lint_flags_missing_isolation():
    bad = GOOD_SPEC.replace("test.beforeEach", "// no cleanup\nconst _x = ")
    findings = lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS)
    assert any("beforeEach" in f for f in findings)


# ---------------------------------------------------------------------------
# authoring wiring
# ---------------------------------------------------------------------------

def _seed_kit_project(ws, monkeypatch):
    """A project with a frontend marker + a kit component carrying testids."""
    from agents import qa
    root = Path(seed(ws, "proj", "frontend", "package.json",
                     '{"devDependencies": {"vitest": "1"}, "scripts": {"test": "vitest run"}}')).parent.parent
    kit = seed(ws, "proj", "frontend/src/components/kit", "AddTaskForm.tsx",
               'export const F = () => (<form data-testid="add-task-form">'
               '<input data-testid="task-title-input" />'
               '<button data-testid="add-task-button" />'
               '<li data-testid={`task-card-${task.id}`} /></form>);')
    (root / "frontend" / "tests").mkdir(parents=True, exist_ok=True)
    return root, kit


PY_GOOD_SPEC = '''
import os
import pytest
from playwright.sync_api import Page, expect

BASE = os.environ.get("E2E_BASE_URL", "http://frontend:3000")
API = os.environ.get("API_BASE_URL", "http://api:8000")

@pytest.fixture(autouse=True)
def clean_db(page: Page):
    res = page.request.get(f"{API}/api/tasks")
    for t in (res.json().get("items") or []):
        page.request.delete(f"{API}/api/tasks/{t['id']}")
    yield

def test_add_task(page: Page):
    page.goto(BASE)
    page.get_by_test_id("task-title-input").fill("Buy milk")
    page.get_by_test_id("add-task-button").click()
    expect(page.locator('li[data-testid^="task-card-"]')).to_have_count(1)
'''


def test_lint_clean_python_spec_passes():
    assert lint_e2e_spec(PY_GOOD_SPEC, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS) == []


def test_lint_python_rules():
    # invented testid (snake_case API) + label guessing + missing autouse cleanup
    bad = PY_GOOD_SPEC.replace('get_by_test_id("task-title-input")',
                               'get_by_test_id("stats-total")')
    assert any("stats-total" in f for f in lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS))
    bad = PY_GOOD_SPEC.replace('page.get_by_test_id("task-title-input")',
                               'page.get_by_label("title")')
    assert any("label/placeholder" in f for f in lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS))
    bad = PY_GOOD_SPEC.replace("@pytest.fixture(autouse=True)", "@pytest.fixture()")
    assert any("cleanup" in f for f in lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS))
    # f-string dynamic testid with a known prefix is fine; python comments ignored
    ok = PY_GOOD_SPEC + '\n# never use .check() on styled checkboxes\n'
    ok = ok.replace('get_by_test_id("add-task-button")',
                    'get_by_test_id(f"task-card-{tid}")')
    assert lint_e2e_spec(ok, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS) == []
    # guessed f-string API path flagged
    bad = PY_GOOD_SPEC.replace('f"{API}/api/tasks"', 'f"{API}/tasks"')
    assert any("not found" in f for f in lint_e2e_spec(bad, KIT_IDS, KIT_PREFIXES, KNOWN_PATHS))


def test_authoring_prompt_carries_qa_log_kit_ids_and_conventions(llm, ws, monkeypatch):
    from agents import qa
    root, kit = _seed_kit_project(ws, monkeypatch)
    llm.default = "===FILE: e2e/test_flow.py===\n" + PY_GOOD_SPEC + "\n===END==="
    state = base_state(prd_path=seed(ws, "proj", "prd", "prd.md", "AC1"),
                       design_path=seed(ws, "proj", "design", "tech_spec.md", "'/api/tasks'"),
                       feature_request="todo", code_files=[],
                       design_component_files=[str(Path(kit).relative_to(root))])
    qa_log = [{"from": "qa", "to": "ceo", "question": "which path?",
               "answer": "ALWAYS /api/tasks, never /tasks"}]
    files, notes = qa._author_e2e_specs(state, "SYS", "AC1", "code text", qa_log)
    prompt = llm.calls[-1]["user"]
    assert "ALWAYS /api/tasks" in prompt                     # (a) qa_log reaches authoring
    assert "task-title-input" in prompt                      # (a) kit testids listed
    assert "ONLY the kit data-testids" in prompt             # selector mandate
    assert "ISOLATION IS MANDATORY" in prompt                # (c) conventions
    assert "LANGUAGE IS PYTHON" in prompt                    # hardwired e2e language
    assert "autouse=True" in prompt and 'evaluate("el => el.click()")' in prompt
    assert files == ["e2e/test_flow.py"]                     # python file written


def test_authoring_skips_when_specs_already_exist(llm, ws, monkeypatch):
    from agents import qa
    root, kit = _seed_kit_project(ws, monkeypatch)
    seed(ws, "proj", "e2e", "flow.spec.ts", "test('x', () => {}); expect(1)")
    state = base_state(e2e_files=["e2e/flow.spec.ts"],
                       design_component_files=[str(Path(kit).relative_to(root))])
    files, notes = qa._author_e2e_specs(state, "SYS", "prd", "code", [])
    assert files == ["e2e/flow.spec.ts"]
    assert llm.calls == []                                   # (d) no re-author, no clobber
    assert any("kept" in n for n in notes)


def test_lint_gate_drops_still_failing_spec(llm, ws, monkeypatch):
    from agents import qa
    root, kit = _seed_kit_project(ws, monkeypatch)
    bad = ("===FILE: e2e/bad.spec.ts===\nimport { test, expect } from '@playwright/test';\n"
           "test('x', async ({ page }) => {\n"
           "  await page.getByLabel(/task/i).fill('a');\n"
           "  await expect(page.getByTestId('stats-total')).toBeVisible();\n});\n===END===")
    llm.default = bad                                        # authored bad, re-authored bad
    state = base_state(prd_path=seed(ws, "proj", "prd", "prd.md", "AC1"),
                       design_component_files=[str(Path(kit).relative_to(root))])
    files, notes = qa._author_e2e_specs(state, "SYS", "AC1", "'/api/tasks'", [])
    assert files == []                                       # (b) bad oracle never ships
    assert not (root / "e2e" / "bad.spec.ts").exists()       # deleted from disk
    assert any("DROPPED" in n and "lint" in n for n in notes)


# ---------------------------------------------------------------------------
# revision path + routing
# ---------------------------------------------------------------------------

def test_qa_revision_path_fixes_specs_and_returns_to_integration(llm, ws, monkeypatch):
    from agents import qa
    from graph.graph import qa_routing
    root, kit = _seed_kit_project(ws, monkeypatch)
    seed(ws, "proj", "e2e", "flow.spec.ts", "test('x', () => { /* broken selector */ })")
    llm.default = ("===FILE: e2e/flow.spec.ts===\n" + GOOD_SPEC + "\n===END===")
    state = base_state(e2e_files=["e2e/flow.spec.ts"], e2e_revision_pending=True,
                       tests_passed=True,
                       error_log="e2e FAILED: locator resolved to <form ...>",
                       design_component_files=[str(Path(kit).relative_to(root))])
    out = qa.run(state)
    assert out["e2e_revised"] is True and not out["e2e_revision_pending"]
    assert out["tests_passed"] is True
    assert "getByTestId" in (root / "e2e" / "flow.spec.ts").read_text()
    assert qa_routing({**state, **out}) == "integration"     # straight back to verify
    prompt = llm.calls[-1]["user"]
    assert "Do NOT weaken" in prompt                         # assertions stay intact


def test_integration_sets_revision_flag_only_for_e2e_stage(ws, monkeypatch):
    from agents import integration as integ
    monkeypatch.setattr(integ, "run_compose_integration",
                        lambda d, **kw: (False, "=== compose up --build — OK ===\n"
                                                "=== health — OK ===\n=== smoke — OK ===\n"
                                                "=== e2e (playwright) — FAILED ===\n2 failed"))
    state = base_state(e2e_files=["e2e/flow.spec.ts"])
    out = integ.run(state)
    assert out["integration_failed_stage"] == "e2e"
    assert out["e2e_revision_pending"] is True

    # compose-stage failure → engineer's problem, no revision round
    monkeypatch.setattr(integ, "run_compose_integration",
                        lambda d, **kw: (False, "=== compose up --build — FAILED ===\nboom"))
    out = integ.run(base_state(e2e_files=["e2e/flow.spec.ts"]))
    assert out["integration_failed_stage"] == "compose"
    assert "e2e_revision_pending" not in out

    # the bounded round is single-shot: already revised → no second revision
    monkeypatch.setattr(integ, "run_compose_integration",
                        lambda d, **kw: (False, "=== e2e (playwright) — FAILED ===\nstill"))
    out = integ.run(base_state(e2e_files=["e2e/flow.spec.ts"], e2e_revised=True))
    assert "e2e_revision_pending" not in out


def test_compiled_graph_has_integration_to_qa_edge():
    # The routing FN returned "qa" but the compiled edge map didn't include it —
    # a live run crashed with KeyError('qa') at integration. Assert at the
    # compiled-graph level, not just the function level.
    from graph.graph import build_graph
    compiled = build_graph(":memory:")
    edges = {(e.source, e.target) for e in compiled.get_graph().edges}
    assert ("integration", "qa") in edges
    assert ("integration", "engineer") in edges and ("integration", "design_qa") in edges


def test_integration_routing_prefers_qa_revision_round():
    from graph.graph import integration_routing
    assert integration_routing(base_state(integration_passed=True)) == "design_qa"
    assert integration_routing(base_state(integration_passed=False,
                                          e2e_revision_pending=True,
                                          integration_attempts=1)) == "qa"
    assert integration_routing(base_state(integration_passed=False,
                                          integration_attempts=1)) == "engineer"
    assert integration_routing(base_state(integration_passed=False,
                                          integration_attempts=3)) == "pr_gate"


def test_qa_reads_project_tree_when_code_files_empty(ws):
    # A no-op verification round records no files — QA must fall back to the
    # managed project's own tree instead of escalating for a git remote (live).
    from agents import qa
    root = ws / "managed"
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "app" / "adventures.py").write_text("BADGES = 'real code'")
    state = base_state(code_files=[], target_repo=str(root), managed_project=True)
    code = qa._read_code(state)
    assert "real code" in code and "adventures.py" in code
