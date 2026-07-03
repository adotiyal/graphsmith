"""
SET 2 — Agent behavioral tests.

For each agent, viewed as a human worker would be judged:
  - Does it receive the inputs it needs? (the upstream artifact reaches its prompt)
  - Does it produce the expected output artifact + state?
  - Is it ever BLOCKED? It must escalate to the CEO instead of guessing — the
    core principle: agents are never stuck; the CEO unblocks any decision.

LLM + Docker are mocked. Quality of the prose itself is evaluated by the live
eval harness (test_live_eval.py), not here.
"""

import pytest
from conftest import base_state, seed, NEEDS_CEO

FILES_OUTPUT = (
    "===FILE: src/main.py===\n"
    "from fastapi import FastAPI\napp = FastAPI()\n"
    "===END===\n"
    "===FILE: requirements.txt===\nfastapi\nhttpx\n===END===\n"
)


def full_seed(ws, pid="proj"):
    """Seed every upstream artifact and return absolute paths."""
    return {
        "brief": seed(ws, pid, "prd", "ceo_brief.md", "BRIEF: build secure user login"),
        "prd": seed(ws, pid, "prd", "prd.md", "## Acceptance Criteria\n1. User can log in"),
        "design": seed(ws, pid, "design", "design_spec.md", "## Screens\nLogin screen"),
        "tech": seed(ws, pid, "design", "tech_spec.md", "## API\nPOST /login → 200"),
        "tests": seed(ws, pid, "tests", "test_login.py", "def test_login():\n    assert True"),
    }


# ── CEO ───────────────────────────────────────────────────────────────────────

def test_ceo_writes_brief_from_request(llm, ws):
    from agents import ceo
    llm.default = "BRIEF: login feature, scope X"
    out = ceo.run(base_state(feature_request="let users log in"))
    assert "let users log in" in llm.user_texts()      # input reaches the prompt
    assert (ws / "proj" / "prd" / "ceo_brief.md").exists()
    assert out["prd_path"]


# ── Triage (change-type router) ───────────────────────────────────────────────

@pytest.mark.parametrize("reply,expected", [
    ('{"change_type":"feature"}', "feature"),
    ('{"change_type":"bugfix"}', "bugfix"),
    ('{"change_type":"refactor"}', "refactor"),
    ('{"change_type":"chore"}', "chore"),
    ('{"change_type":"BugFix"}', "bugfix"),              # enum match is case-insensitive
    ("I cannot produce JSON, sorry", "feature"),         # unparseable → safe default (full lane)
])
def test_triage_classifies(llm, ws, reply, expected):
    from agents import triage
    brief = seed(ws, "proj", "prd", "ceo_brief.md", "some request")
    llm.default = reply
    assert triage.run(base_state(prd_path=brief))["change_type"] == expected


# ── Engineer quick lane (bugfix/refactor/chore — no tech spec) ─────────────────

def test_engineer_quick_lane_works_from_brief(llm, ws, no_docker):
    from agents import engineer
    brief = seed(ws, "proj", "prd", "ceo_brief.md", "BUG: header logo 404s on mobile")
    llm.default = "===FILE: src/header.py===\nlogo = '/logo.svg'\n===END==="
    # No design_path — the quick lane works straight from the request.
    out = engineer.run(base_state(prd_path=brief, change_type="bugfix"))
    txt = llm.user_texts()
    assert "header logo 404s" in txt                # worked from the brief
    assert "BUGFIX" in txt                          # change-type-aware instruction
    assert out["tests_passed"] is True


# ── PM ────────────────────────────────────────────────────────────────────────

def test_pm_reads_brief_writes_prd_and_requests_approval(llm, ws):
    from agents import pm
    paths = full_seed(ws)
    llm.default = "## Feature\nLogin\n## Acceptance Criteria\n1. ..."
    out = pm.run(base_state(prd_path=paths["brief"]))
    assert "build secure user login" in llm.user_texts()
    assert (ws / "proj" / "prd" / "prd.md").exists()
    assert out["approval_pending"] == "prd"            # routes to PRD gate
    assert out.get("ceo_qa_pending") is None


def test_pm_escalates_when_blocked(llm, ws):
    from agents import pm
    paths = full_seed(ws)
    llm.default = NEEDS_CEO
    out = pm.run(base_state(prd_path=paths["brief"]))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "pm"   # never blocked
    assert out.get("approval_pending") is None


def test_pm_system_prompt_loads_the_pm_skill(llm, ws):
    # C11 lives in skills/pm.md — it must actually reach the model (the skill was a dead file).
    from agents import pm
    paths = full_seed(ws)
    llm.default = "## Feature\nx\n## Acceptance Criteria\n1. ..."
    pm.run(base_state(prd_path=paths["brief"]))
    assert "Journey ACs" in llm.system_texts()


def test_pm_uses_product_profile(llm, ws):
    from agents import pm
    paths = full_seed(ws)
    llm.default = "## Feature\nlogin"
    pm.run(base_state(prd_path=paths["brief"], product_profile="Users: small-business owners"))
    assert "small-business owners" in llm.user_texts()


def test_pm_uses_project_ledger(llm, ws):
    from agents import pm
    paths = full_seed(ws)
    llm.default = "## Feature\nx"
    pm.run(base_state(prd_path=paths["brief"], project_ledger="## built the dashboard last week"))
    assert "built the dashboard" in llm.user_texts()    # PM scopes consistently with history


def test_pm_applies_ceo_rejection_feedback(llm, ws):
    from agents import pm
    paths = full_seed(ws)
    llm.default = "## Feature\nrevised"
    out = pm.run(base_state(prd_path=paths["brief"], review_notes="add SSO support"))
    assert "add SSO support" in llm.user_texts()       # feedback reaches the prompt
    assert out["review_notes"] is None                  # consumed


# ── Surveyor (Phase 2.1, codebase awareness) ──────────────────────────────────

def _make_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "requirements.txt").write_text("fastapi\n")
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (root / "app" / "auth.py").write_text("def login():\n    return 'token'\n")
    return root


def test_surveyor_is_noop_in_greenfield(llm, ws):
    from agents import surveyor
    out = surveyor.run(base_state())                # no target_repo
    assert out["current_node"] == "surveyor"
    assert llm.calls == []                          # no LLM cost in greenfield
    assert "repo_map_path" not in out


def test_surveyor_noops_on_empty_managed_repo(llm, ws, tmp_path):
    # First run of a managed project: target_repo exists but is EMPTY — that's
    # greenfield. (A live run surveyed it: burned an Opus call, asked the CTO a
    # pointless question, and set detected_stack='unknown' which suppressed the
    # design-time stack ask + component kit.)
    from agents import surveyor
    empty = tmp_path / "project"
    empty.mkdir()
    out = surveyor.run(base_state(target_repo=str(empty), managed_project=True))
    assert out == {"current_node": "surveyor"}      # no detected_stack, no map
    assert llm.calls == []                          # zero LLM cost


def test_surveyor_maps_repo_and_detects_stack(llm, ws, tmp_path):
    from agents import surveyor
    target = _make_repo(tmp_path / "repo")
    paths = full_seed(ws)
    llm.default = "## Stack & Conventions\nFastAPI in app/main.py\n## Where The Feature Plugs In\napp/auth.py"
    out = surveyor.run(base_state(prd_path=paths["prd"], target_repo=str(target)))
    assert out["detected_stack"] and "Python" in out["detected_stack"]
    assert out["repo_map_path"]
    assert (ws / "proj" / "design" / "repo_map.md").exists()
    assert "app/main.py" in llm.user_texts()        # the repo map reached the prompt


def test_surveyor_escalates_when_blocked(llm, ws, tmp_path):
    from agents import surveyor
    target = _make_repo(tmp_path / "repo")
    paths = full_seed(ws)
    llm.default = NEEDS_CEO
    out = surveyor.run(base_state(prd_path=paths["prd"], target_repo=str(target)))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "surveyor"


def test_architect_proposes_detected_stack_in_extend_mode(llm, ws):
    from agents import architect
    paths = full_seed(ws)
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"],
                                   detected_stack="Next.js, Python"))
    assert out["ceo_qa_from"] == "architect"
    assert "existing codebase" in out["ceo_qa_pending"].lower()
    assert "Next.js, Python" in out["ceo_qa_pending"]


def test_architect_confirm_uses_detected_stack(llm, ws):
    from agents import architect
    paths = full_seed(ws)
    llm.default = "## Stack\nextending existing app"
    log = [{"from": "architect", "to": "ceo",
            "question": "TECH STACK DECISION (CTO call): ...",
            "answer": "confirm"}]
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"],
                                   detected_stack="Go, Postgres", qa_log=log))
    assert out["tech_stack"] == "Go, Postgres"      # affirmative → the detected stack


# ── Design ────────────────────────────────────────────────────────────────────

# Pre-answered direction choice: design pauses for the HUMAN to pick one of 3 design
# directions; seeding the answer lets single-pass tests run the legacy-shaped flow
# (spec → one mockup → kit). The pause itself is covered in test_design_directions.py.
_CHOICE = {"from": "design", "to": "ceo",
           "question": "DESIGN DIRECTION CHOICE (CEO/CTO — human pick): ...",
           "answer": "A"}

def test_design_reads_prd_writes_spec(llm, ws):
    from agents import design
    paths = full_seed(ws)
    llm.default = "## Screens\nLogin\n## Components per Screen\nButton, Input"
    out = design.run(base_state(prd_path=paths["prd"], detected_stack="Python", qa_log=[_CHOICE]))
    assert "User can log in" in llm.user_texts()
    assert (ws / "proj" / "design" / "design_spec.md").exists()
    assert out["design_path"]


def test_design_escalates_when_blocked(llm, ws):
    from agents import design
    paths = full_seed(ws)
    llm.default = NEEDS_CEO
    out = design.run(base_state(prd_path=paths["prd"]))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "design"


def test_design_produces_html_mockup(llm, ws):
    # The design now emits a visual mockup grounded in the spec.
    from agents import design
    paths = full_seed(ws)
    spec = "## Design Context\nfor users\n## Content & Microcopy\nPrimary CTA: 'Start free'"
    html = "<!doctype html><html><body><button>Start free</button></body></html>"
    llm.queue = [spec, html]          # 1st call = spec, 2nd call = mockup HTML
    out = design.run(base_state(prd_path=paths["prd"], detected_stack="Python", qa_log=[_CHOICE]))
    assert out["design_mockup_path"]
    mock = ws / "proj" / "design" / "mockup.html"
    assert mock.exists() and "Start free" in mock.read_text()
    assert "DESIGN SPEC" in llm.user_texts()       # the spec fed the mockup call


def test_design_strips_markdown_fences_from_mockup(llm, ws):
    from agents import design
    paths = full_seed(ws)
    llm.queue = ["## Design Context\nx", "```html\n<html>clean</html>\n```"]
    design.run(base_state(prd_path=paths["prd"], detected_stack="Python", qa_log=[_CHOICE]))
    text = (ws / "proj" / "design" / "mockup.html").read_text()
    assert text.startswith("<html>") and "```" not in text


def test_design_skips_mockup_for_backend_feature(llm, ws):
    from agents import design
    paths = full_seed(ws)
    llm.default = "NO UI SURFACE - backend feature only."
    out = design.run(base_state(prd_path=paths["prd"], detected_stack="Python"))
    assert out["design_mockup_path"] is None
    assert not (ws / "proj" / "design" / "mockup.html").exists()
    assert len(llm.calls) == 1                      # no second (mockup) call


def test_design_uses_product_profile(llm, ws):
    # The standing product context (brand/users) must reach the designer's prompt.
    from agents import design
    paths = full_seed(ws)
    llm.default = "## Design Context\nfor users\n## Screens & Components\nButton"
    design.run(base_state(prd_path=paths["prd"], detected_stack="Python",
                          product_profile="Brand: playful and bold; Users: gen-z creators"))
    txt = llm.user_texts()
    assert "playful and bold" in txt and "gen-z creators" in txt


def test_design_uses_project_ledger(llm, ws):
    from agents import design
    paths = full_seed(ws)
    llm.queue = ["## Design Context\nx", "<html></html>"]   # spec call, then mockup call
    design.run(base_state(prd_path=paths["prd"], detected_stack="Python", project_ledger="## built the onboarding flow"))
    assert "built the onboarding flow" in llm.user_texts()


def test_design_regenerates_on_critic_notes(llm, ws):
    from agents import design
    paths = full_seed(ws)
    llm.default = "## Design Context\nrevised design"
    out = design.run(base_state(prd_path=paths["prd"], review_notes="first-run empty state missing"))
    assert "first-run empty state missing" in llm.user_texts()
    assert out["review_notes"] is None and out["review_action"] is None


def test_critic_design_reviews_design_vs_prd(llm, ws):
    from agents import critic
    paths = full_seed(ws)
    spec = seed(ws, "proj", "design", "design_spec.md", "## Screens\nLogin screen")
    llm.default = '{"verdict":"fail","gaps":"1. no empty/first-run state designed"}'
    out = critic.run(base_state(prd_path=paths["prd"], design_path=spec), stage="design")
    assert out["review_action"] == "retry" and "empty" in out["review_notes"].lower()
    assert "first-run" in llm.user_texts().lower()     # the design-specific review focus was applied


# ── Architect ─────────────────────────────────────────────────────────────────

def test_architect_confirms_stack_with_cto_before_committing(llm, ws):
    # First architecture pass: the stack is a CTO decision — escalate, don't assume.
    from agents import architect
    paths = full_seed(ws)
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"]))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "architect"
    assert "tech stack" in out["ceo_qa_pending"].lower()
    assert llm.calls == []                          # no spec generated until the CTO confirms
    assert "tech_stack_confirmed" not in out or out.get("tech_stack_confirmed") is not True


def test_architect_proceeds_after_cto_confirms_stack(llm, ws):
    from agents import architect
    paths = full_seed(ws)
    llm.default = "## Stack\nFastAPI + Next.js + Postgres\n## API Endpoints\nPOST /login"
    # CEO/CTO answered the stack question in the log; architect should now build.
    log = [{"from": "architect", "to": "ceo",
            "question": "TECH STACK DECISION (CTO call): proposed default is ...",
            "answer": "confirm"}]
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"], qa_log=log))
    assert (ws / "proj" / "design" / "tech_spec.md").exists()
    assert out["tech_stack_confirmed"] is True
    assert "Next.js" in out["tech_stack"]


def test_architect_uses_a_custom_stack_chosen_by_cto(llm, ws):
    from agents import architect
    paths = full_seed(ws)
    llm.default = "## Stack\nDjango spec"
    log = [{"from": "architect", "to": "ceo",
            "question": "TECH STACK DECISION (CTO call): ...",
            "answer": "Use Django + HTMX + SQLite instead"}]
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"], qa_log=log))
    assert out["tech_stack"] == "Use Django + HTMX + SQLite instead"
    assert "Use EXACTLY this CEO/CTO-confirmed stack" in llm.user_texts()
    assert "Django + HTMX + SQLite" in llm.user_texts()


def test_architect_reads_prd_and_design_writes_tech_spec(llm, ws):
    from agents import architect
    paths = full_seed(ws)
    llm.default = "## Stack\nFastAPI\n## API Endpoints\nPOST /login"
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"],
                                   tech_stack_confirmed=True, tech_stack="FastAPI + Next.js + Postgres"))
    txt = llm.user_texts()
    assert "User can log in" in txt and "Login screen" in txt   # both inputs present
    assert (ws / "proj" / "design" / "tech_spec.md").exists()
    assert out["design_path"].endswith("tech_spec.md")


def test_architect_reuses_persisted_stack_greenfield(llm, ws, product_root):
    # Stack persists across features: a confirmed stack is reused without re-asking.
    from agents import architect
    from tools import product
    product.save_stack("Django + HTMX + SQLite")
    paths = full_seed(ws)
    llm.default = "## Stack\nspec body"
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"]))
    assert out.get("ceo_qa_pending") is None        # NOT re-asked
    assert out["tech_stack"] == "Django + HTMX + SQLite"
    assert (ws / "proj" / "design" / "tech_spec.md").exists()


def test_architect_persists_stack_on_first_confirm(llm, ws, product_root):
    from agents import architect
    from tools import product
    assert product.load_stack() == ""
    llm.default = "## Stack\nspec body"
    paths = full_seed(ws)
    log = [{"from": "architect", "to": "ceo",
            "question": "TECH STACK DECISION (CTO call): ...", "answer": "confirm"}]
    architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"], qa_log=log))
    assert "FastAPI" in product.load_stack()         # default persisted for next time


def test_architect_managed_project_reuses_persisted_stack(llm, ws, product_root, tmp_path):
    # Continuity: in the managed project the stack is NOT re-asked every feature.
    from agents import architect
    from tools import product
    product.save_stack("FastAPI + Next.js + Postgres")
    repo = tmp_path / "proj"; repo.mkdir()
    paths = full_seed(ws)
    llm.default = "## Stack\nspec body"
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"],
                                   target_repo=str(repo), managed_project=True, detected_stack="Go"))
    assert out.get("ceo_qa_pending") is None                    # reused — not re-asked
    assert out["tech_stack"] == "FastAPI + Next.js + Postgres"


def test_architect_external_repo_ignores_persisted_stack(llm, ws, product_root, tmp_path):
    # An external --repo is not our product: detect its stack, never reuse ours.
    from agents import architect
    from tools import product
    product.save_stack("FastAPI + Next.js + Postgres")
    repo = tmp_path / "otherrepo"; repo.mkdir()
    paths = full_seed(ws)
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"],
                                   target_repo=str(repo), managed_project=False, detected_stack="Go, Postgres"))
    assert out["ceo_qa_from"] == "architect"                    # asks, doesn't reuse ours
    assert "Go, Postgres" in out["ceo_qa_pending"]


def test_architect_uses_project_ledger(llm, ws):
    from agents import architect
    paths = full_seed(ws)
    llm.default = "## Stack\nspec"
    architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"],
                             tech_stack_confirmed=True, project_ledger="## built user login\n- files: auth.py"))
    assert "built user login" in llm.user_texts()


def test_architect_escalates_when_blocked(llm, ws):
    from agents import architect
    paths = full_seed(ws)
    llm.default = NEEDS_CEO
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"],
                                   tech_stack_confirmed=True))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "architect"


def test_architect_regenerates_on_critic_notes(llm, ws):
    from agents import architect
    paths = full_seed(ws)
    llm.default = "## Stack\nrevised spec"
    out = architect.run(base_state(prd_path=paths["prd"], design_path=paths["design"],
                                   tech_stack_confirmed=True, review_notes="AC#3 has no endpoint"))
    assert "AC#3 has no endpoint" in llm.user_texts()
    assert out["review_notes"] is None and out["review_action"] is None


# ── Critic ────────────────────────────────────────────────────────────────────

def test_critic_pass(llm, ws):
    from agents import critic
    paths = full_seed(ws)
    llm.default = '{"verdict":"pass","gaps":null}'
    out = critic.run(base_state(prd_path=paths["prd"], design_path=paths["tech"]), stage="architect")
    assert out["review_action"] == "pass"


def test_critic_fails_open_on_unparseable_reply(llm, ws):
    # call_structured retries once, then returns the safe default (pass) — a parse error
    # must never BLOCK the pipeline (fail-open), but it no longer silently passes on the
    # FIRST malformed reply: the corrective retry gets a real verdict when the model can.
    from agents import critic
    paths = full_seed(ws)
    llm.default = "I think the spec looks mostly fine, honestly."   # no JSON ever
    out = critic.run(base_state(prd_path=paths["prd"], design_path=paths["tech"]), stage="architect")
    assert out["review_action"] == "pass"


def test_critic_retry_then_escalate(llm, ws):
    from agents import critic
    from agents.critic import MAX_REVIEW_ATTEMPTS
    paths = full_seed(ws)
    llm.default = '{"verdict":"fail","gaps":"1. missing rate limiting"}'
    # First failure → retry with notes
    out1 = critic.run(base_state(prd_path=paths["prd"], design_path=paths["tech"]), stage="architect")
    assert out1["review_action"] == "retry" and out1["review_notes"]
    # After exhausting attempts → escalate to CEO (never silently ships a bad spec)
    out2 = critic.run(base_state(prd_path=paths["prd"], design_path=paths["tech"],
                                 review_attempts={"architect": MAX_REVIEW_ATTEMPTS}), stage="architect")
    assert out2["review_action"] == "escalate"
    assert out2["ceo_qa_pending"] and out2["ceo_qa_from"] == "architect_critic"


# ── Test Author ───────────────────────────────────────────────────────────────

def test_test_author_writes_tests_from_prd_and_spec(llm, ws):
    from agents import test_author
    paths = full_seed(ws)
    llm.default = "===FILE: tests/test_login.py===\ndef test_x():\n    assert True\n===END==="
    out = test_author.run(base_state(prd_path=paths["prd"], design_path=paths["tech"]))
    txt = llm.user_texts()
    assert "User can log in" in txt and "POST /login" in txt    # PRD + spec both feed it
    assert out["test_path"]
    assert (ws / "proj" / "tests" / "test_login.py").exists()


def test_test_author_escalates_when_blocked(llm, ws):
    from agents import test_author
    paths = full_seed(ws)
    llm.default = NEEDS_CEO
    out = test_author.run(base_state(prd_path=paths["prd"], design_path=paths["tech"]))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "test_author"


def test_test_author_writes_into_repo_in_extend_mode(llm, ws, tmp_path):
    # Slice 2: tests are written INTO the target repo, following existing conventions.
    from agents import test_author
    target = _make_repo(tmp_path / "repo")
    (target / "tests").mkdir()
    (target / "tests" / "test_existing.py").write_text("def test_old_thing():\n    assert True\n")
    paths = full_seed(ws)
    llm.default = "===FILE: tests/test_logout.py===\ndef test_logout():\n    assert True\n===END==="
    out = test_author.run(base_state(prd_path=paths["prd"], design_path=paths["tech"],
                                     target_repo=str(target)))
    assert (target / "tests" / "test_logout.py").exists()        # written into the repo
    assert out["test_files"] == ["tests/test_logout.py"]         # recorded for protection
    assert "test_old_thing" in llm.user_texts()                  # saw existing conventions
    assert not (ws / "proj" / "tests" / "test_logout.py").exists()  # NOT in workspace


CONFTEST_ONLY = (
    "===FILE: tests/conftest.py===\n"
    "import pytest\n@pytest.fixture\ndef client():\n    return object()\n"
    "===END==="
)
REAL_TEST = (
    "===FILE: tests/test_todos.py===\n"
    "# covers: AC-1\n"
    "def test_add_task():\n    assert 1 == 1\n"
    "===END==="
)


def test_test_author_retries_when_suite_has_no_real_tests(llm, ws):
    # Guard: a suite of only fixtures/conftest is not an oracle — retry once, recover.
    from agents import test_author
    paths = full_seed(ws)
    llm.queue = [CONFTEST_ONLY, REAL_TEST]   # first emits only fixtures, retry emits a real test
    out = test_author.run(base_state(prd_path=paths["prd"], design_path=paths["tech"]))
    assert not out.get("ceo_qa_pending")                      # recovered, no escalation
    assert "tests/test_todos.py" in out["test_files"]
    assert (ws / "proj" / "tests" / "test_todos.py").exists()
    assert len(llm.calls) == 2                                # original + one corrective retry


def test_test_author_escalates_when_oracle_stays_empty(llm, ws):
    # If it still produces no runnable tests after the retry, escalate — never pass an
    # empty oracle downstream (which would make the engineer fail against nothing).
    from agents import test_author
    paths = full_seed(ws)
    llm.default = CONFTEST_ONLY                               # always fixtures-only
    out = test_author.run(base_state(prd_path=paths["prd"], design_path=paths["tech"]))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "test_author"


def test_qa_reads_full_code_file_without_truncation(ws):
    # Regression: a 3000-char per-file cap silently cut a ~4KB app.js mid-function,
    # so QA wrongly flagged the frontend "incomplete" (false-positive NO-GO).
    from agents.qa import _read_code, MAX_REVIEW_CHARS_PER_FILE
    big = ws / "app.js"
    body = "// header\n" + "x();\n" * 800 + "function deleteTodo(){ return 1; }\n"  # ~4KB
    assert len(body) > 3000 and len(body) < MAX_REVIEW_CHARS_PER_FILE
    big.write_text(body, encoding="utf-8")
    out = _read_code({"code_files": [str(big)]})
    assert "function deleteTodo()" in out          # the tail survives
    assert "truncated" not in out


# ── Engineer ──────────────────────────────────────────────────────────────────

def test_engineer_implements_against_tests_and_passes(llm, ws, no_docker):
    from agents import engineer
    paths = full_seed(ws)
    llm.default = FILES_OUTPUT
    out = engineer.run(base_state(design_path=paths["tech"]))
    assert "POST /login → 200" in llm.user_texts()      # reads the spec
    assert "def test_login" in llm.user_texts()         # reads the authoritative tests
    assert (ws / "proj" / "src" / "main.py").exists()   # wrote code
    assert out["tests_passed"] is True


def test_engineer_does_not_overwrite_tests(llm, ws, no_docker):
    from agents import engineer
    paths = full_seed(ws)
    original = (ws / "proj" / "tests" / "test_login.py").read_text()
    # Engineer tries to emit a tests/ file — it must be ignored.
    llm.default = (FILES_OUTPUT +
                   "===FILE: tests/test_login.py===\ndef test_login():\n    assert False\n===END===")
    engineer.run(base_state(design_path=paths["tech"]))
    assert (ws / "proj" / "tests" / "test_login.py").read_text() == original  # protected


def test_engineer_continues_on_truncated_output(llm, ws, no_docker):
    from agents import engineer
    paths = full_seed(ws)
    # First response is cut off mid-file (no ===END===); continuation closes it.
    llm.queue = ["===FILE: src/main.py===\nprint('hi')\n", "===END===\n"]
    llm.default = ""
    engineer.run(base_state(design_path=paths["tech"]))
    assert (ws / "proj" / "src" / "main.py").exists()


def test_engineer_escalates_when_blocked(llm, ws, no_docker):
    from agents import engineer
    paths = full_seed(ws)
    llm.default = NEEDS_CEO
    out = engineer.run(base_state(design_path=paths["tech"]))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "engineer"


def test_engineer_writes_into_repo_and_protects_tests_in_extend_mode(llm, ws, no_docker, tmp_path):
    # Slice 2: code is written INTO the target repo at real paths; tests are never clobbered.
    from agents import engineer
    target = _make_repo(tmp_path / "repo")
    (target / "tests").mkdir()
    (target / "tests" / "test_auth.py").write_text("def test_auth():\n    assert True\n")
    paths = full_seed(ws)
    llm.default = ("===FILE: app/logout.py===\ndef logout():\n    return True\n===END===\n"
                   "===FILE: tests/test_auth.py===\ndef test_auth():\n    assert False  # sabotage\n===END===")
    out = engineer.run(base_state(design_path=paths["tech"], target_repo=str(target),
                                  test_files=["tests/test_auth.py"]))
    assert (target / "app" / "logout.py").read_text().startswith("def logout")  # wrote into repo
    assert "assert True" in (target / "tests" / "test_auth.py").read_text()      # test protected
    assert "sabotage" not in (target / "tests" / "test_auth.py").read_text()
    assert out["tests_passed"] is True
    assert not (ws / "proj" / "src").exists()                                    # nothing in workspace


def test_engineer_applies_minimal_edits_in_extend_mode(llm, ws, no_docker, tmp_path):
    # #3: existing files are edited via search/replace, not rewritten wholesale.
    from agents import engineer
    target = _make_repo(tmp_path / "repo")   # app/auth.py: def login(): return 'token'
    paths = full_seed(ws)
    llm.default = (
        "===EDIT: app/auth.py===\n"
        "<<<<<<< SEARCH\ndef login():\n    return 'token'\n"
        "=======\ndef login():\n    return 'token-v2'\n>>>>>>> REPLACE\n===END===\n"
        "===FILE: app/logout.py===\ndef logout():\n    return True\n===END==="
    )
    out = engineer.run(base_state(design_path=paths["tech"], target_repo=str(target)))
    assert "token-v2" in (target / "app" / "auth.py").read_text()        # minimal edit applied
    assert "app = FastAPI()" in (target / "app" / "main.py").read_text() # untouched file preserved
    assert (target / "app" / "logout.py").exists()                      # new file created
    assert out["tests_passed"] is True


def test_engineer_extend_edit_failure_triggers_retry(llm, ws, no_docker, tmp_path):
    # A stale/unmatched SEARCH must fail loudly (no half-applied change), so the engineer retries.
    from agents import engineer
    target = _make_repo(tmp_path / "repo")
    paths = full_seed(ws)
    llm.default = ("===EDIT: app/auth.py===\n<<<<<<< SEARCH\nthis is not in the file\n"
                   "=======\nwhatever\n>>>>>>> REPLACE\n===END===")
    out = engineer.run(base_state(design_path=paths["tech"], target_repo=str(target)))
    assert out["tests_passed"] is False
    assert "EDITS DID NOT APPLY" in out["error_log"]


def test_engineer_runs_whole_repo_suite_in_extend_mode(llm, ws, monkeypatch, tmp_path):
    from agents import engineer
    target = _make_repo(tmp_path / "repo")
    paths = full_seed(ws)
    captured = {}
    monkeypatch.setattr(engineer, "run_project_tests",
                        lambda d, timeout=300, test_path="tests/": captured.update(dir=d, test_path=test_path) or (True, "ok"))
    llm.default = "===FILE: app/x.py===\nx = 1\n===END==="
    engineer.run(base_state(design_path=paths["tech"], target_repo=str(target)))
    assert captured["dir"] == str(target)        # tests run in the repo
    assert captured["test_path"] == ""           # the repo's whole suite, not just tests/


# ── Integration (run-and-verify, Phase 4.2/4.3) ──────────────────────────────

def test_integration_pass_routes_clean(ws, monkeypatch):
    from agents import integration
    captured = {}
    monkeypatch.setattr(integration, "run_compose_integration",
                        lambda d, **kw:
                        captured.update(dir=d, require=kw.get("require_compose", True)) or (True, "all green"))
    out = integration.run(base_state())
    assert out["integration_passed"] is True
    assert out["integration_attempts"] == 1
    assert "error_log" not in out
    assert captured["require"] is True          # managed/greenfield must ship compose
    rpt = (ws / "proj" / "tests" / "integration_report.md").read_text()
    assert "all green" in rpt and "AC coverage" in rpt


def test_integration_fail_loops_engineer_with_log(ws, monkeypatch):
    from agents import integration
    monkeypatch.setattr(integration, "run_compose_integration",
                        lambda d, **kw:
                        (False, "api container exited 1"))
    out = integration.run(base_state(integration_attempts=0))
    assert out["integration_passed"] is False
    assert out["integration_attempts"] == 1
    assert "INTEGRATION FAILURE" in out["error_log"]
    assert "api container exited 1" in out["error_log"]


def test_integration_external_repo_does_not_require_compose(ws, monkeypatch, tmp_path):
    from agents import integration
    captured = {}
    monkeypatch.setattr(integration, "run_compose_integration",
                        lambda d, **kw:
                        captured.update(require=kw.get("require_compose", True)) or (True, "skipped"))
    integration.run(base_state(target_repo=str(tmp_path), managed_project=False))
    assert captured["require"] is False         # external --repo: graceful skip


E2E_SPEC = (
    "===FILE: e2e/todo-flow.spec.ts===\n"
    "import { test, expect } from '@playwright/test';\n"
    "test('adds a task', async ({ page }) => {\n"
    "  await page.goto(process.env.E2E_BASE_URL || 'http://frontend:3000');\n"
    "  await expect(page.getByText('Todo')).toBeVisible();\n"
    "});\n"
    "===END==="
)


def _frontend_layer(ws, pid="proj"):
    """Give the project a node layer so QA's e2e authoring triggers."""
    fe = ws / pid / "frontend"
    fe.mkdir(parents=True, exist_ok=True)
    (fe / "package.json").write_text('{"devDependencies": {"vitest": "^2"}}')


def test_qa_pass_authors_playwright_specs_for_ui(llm, ws):
    from agents import qa
    paths = full_seed(ws)
    _frontend_layer(ws)
    llm.queue = ["QA sign-off: GO", E2E_SPEC]    # report call, then e2e authoring call
    out = qa.run(base_state(tests_passed=True, prd_path=paths["prd"],
                            feature_request="todo app"))
    assert out["e2e_files"] == ["e2e/todo-flow.spec.ts"]
    spec = (ws / "proj" / "e2e" / "todo-flow.spec.ts").read_text()
    assert "test(" in spec and "expect(" in spec
    assert "Playwright user-flow specs written" in (ws / "proj" / "tests" / "qa_report.md").read_text()
    # the authoring call carries intent (feature request) + says code is selectors-only
    assert "todo app" in llm.calls[1]["user"]
    assert "selectors" in llm.calls[1]["user"]


def test_qa_system_prompt_loads_the_qa_skill(llm, ws):
    # C9 lives in skills/qa.md — it must actually reach the model (the skill was a dead file).
    from agents import qa
    paths = full_seed(ws)
    llm.default = "QA sign-off: GO"
    qa.run(base_state(tests_passed=True, prd_path=paths["prd"]))
    assert "Evidence rule" in llm.system_texts()


def test_qa_pass_skips_e2e_for_backend_only(llm, ws):
    from agents import qa
    paths = full_seed(ws)                        # no frontend layer
    llm.default = "QA sign-off: GO"
    out = qa.run(base_state(tests_passed=True, prd_path=paths["prd"]))
    assert out["e2e_files"] == []
    assert len(llm.calls) == 1                   # no second authoring call


def test_qa_e2e_retries_then_proceeds_without_specs(llm, ws):
    # Empty/spec-less authoring output → one retry → still empty → proceed (never block).
    from agents import qa
    paths = full_seed(ws)
    _frontend_layer(ws)
    llm.queue = ["QA sign-off: GO", "no spec here", "still no spec"]
    out = qa.run(base_state(tests_passed=True, prd_path=paths["prd"]))
    assert out["e2e_files"] == []
    assert out["approval_pending"] == "pr"       # pipeline continues to the gate
    assert len(llm.calls) == 3                   # report + authoring + one retry


def test_engineer_blocked_from_nested_tests_dirs(llm, ws, no_docker):
    # Split layout: oracle dirs live at backend/tests/ and frontend/tests/ — protection
    # must match a tests/e2e segment at ANY depth, not just the path root. (A live run's
    # engineer wrote backend/tests/__init__.py; the overseer flagged it.)
    from agents import engineer
    paths = full_seed(ws)
    llm.default = (
        "===FILE: backend/tests/test_sneaky.py===\nhacked\n===END===\n"
        "===FILE: frontend/tests/setup.ts===\nhacked\n===END===\n"
        "===FILE: backend/src/main.py===\nx = 1\n===END===\n"
    )
    engineer.run(base_state(design_path=paths["tech"]))
    assert not (ws / "proj" / "backend" / "tests" / "test_sneaky.py").exists()
    assert not (ws / "proj" / "frontend" / "tests" / "setup.ts").exists()
    assert (ws / "proj" / "backend" / "src" / "main.py").exists()


def test_engineer_error_log_keeps_the_tail(llm, ws, monkeypatch):
    # pytest prints failures at the END; a head-slice once fed QA only deprecation
    # warnings and the loop burned 3 attempts fixing the wrong thing.
    from agents import engineer
    paths = full_seed(ws)
    monkeypatch.setattr(engineer, "run_linter", lambda d: (True, "ok"))
    long_log = ("WARNING: noise\n" * 500) + "FAILED tests/test_x.py - assert 200 == 422"
    monkeypatch.setattr(engineer, "run_project_tests",
                        lambda d, timeout=300, test_path="tests/": (False, long_log))
    llm.default = FILES_OUTPUT
    out = engineer.run(base_state(design_path=paths["tech"]))
    assert "assert 200 == 422" in out["error_log"]          # the tail survived


def test_qa_fail_diagnosis_keeps_raw_tail(llm, ws):
    from agents import qa
    llm.default = "ROOT CAUSE: x\nFIX: y\nLESSON: keep logs"
    long_err = ("warning noise\n" * 300) + "E  assert 200 == 422"
    out = qa.run(base_state(tests_passed=False, error_log=long_err))
    assert "assert 200 == 422" in out["error_log"]          # tail reaches the engineer
    assert "ROOT CAUSE" in out["error_log"]


def test_engineer_delete_block_removes_stale_file_greenfield(llm, ws, no_docker):
    # #7: a retry must be able to REMOVE a conflicting leftover (pages/ vs app/ in
    # Next.js) instead of accreting files forever.
    from agents import engineer
    paths = full_seed(ws)
    stale = ws / "proj" / "frontend" / "pages" / "index.tsx"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale pages-router entry")
    llm.default = (
        "===FILE: frontend/app/page.tsx===\nexport default function P(){return null}\n===END===\n"
        "===DELETE: frontend/pages/index.tsx===\n"
    )
    engineer.run(base_state(design_path=paths["tech"]))
    assert not stale.exists()                                        # deleted
    assert (ws / "proj" / "frontend" / "app" / "page.tsx").exists()  # new file written


def test_engineer_delete_block_in_extend_mode_and_protections(llm, ws, no_docker, monkeypatch, tmp_path):
    from agents import engineer
    target = _make_repo(tmp_path / "repo")
    (target / "stale.py").write_text("old")
    (target / "tests").mkdir(exist_ok=True)
    (target / "tests" / "test_keep.py").write_text("def test_k(): assert True")
    paths = full_seed(ws)
    monkeypatch.setattr(engineer, "run_project_tests",
                        lambda d, timeout=300, test_path="tests/": (True, "ok"))
    llm.default = (
        "===FILE: src/new.py===\nx = 1\n===END===\n"
        "===DELETE: stale.py===\n"
        "===DELETE: tests/test_keep.py===\n"          # oracle — must be refused
        "===DELETE: ../outside.txt===\n"              # traversal — must be refused
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    engineer.run(base_state(design_path=paths["tech"], target_repo=str(target)))
    assert not (target / "stale.py").exists()              # legit delete applied
    assert (target / "tests" / "test_keep.py").exists()    # oracle survived
    assert outside.exists()                                # traversal blocked


def test_engineer_edit_failure_includes_current_file_content(llm, ws, no_docker, tmp_path):
    # #10: a stale SEARCH must come back with the file's CURRENT content so the retry
    # copies an exact anchor instead of guessing from memory (3 live runs burned on this).
    from agents import engineer
    target = _make_repo(tmp_path / "repo")
    (target / "app.py").write_text("REAL_CURRENT_CONTENT = 42\n")
    paths = full_seed(ws)
    llm.default = (
        "===EDIT: app.py===\n<<<<<<< SEARCH\nSTALE TEXT THAT ISN'T THERE\n=======\nnew\n"
        ">>>>>>> REPLACE\n===END==="
    )
    out = engineer.run(base_state(design_path=paths["tech"], target_repo=str(target)))
    assert out["tests_passed"] is False
    assert "SEARCH block not found" in out["error_log"]
    assert "REAL_CURRENT_CONTENT = 42" in out["error_log"]   # current content attached


def test_engineer_delete_only_round_keeps_code_files(llm, ws, no_docker, monkeypatch, tmp_path):
    # #13: a round that only DELETEs writes nothing — QA must still see the prior files.
    from agents import engineer
    target = _make_repo(tmp_path / "repo")
    (target / "stale.py").write_text("old")
    paths = full_seed(ws)
    monkeypatch.setattr(engineer, "run_project_tests",
                        lambda d, timeout=300, test_path="tests/": (True, "ok"))
    llm.default = "===DELETE: stale.py===\n"
    prior = ["/abs/backend/src/main.py"]
    out = engineer.run(base_state(design_path=paths["tech"], target_repo=str(target),
                                  code_files=prior))
    assert not (target / "stale.py").exists()
    assert out["code_files"] == prior                        # not clobbered to []


def test_qa_e2e_prompt_includes_tech_spec_contract(llm, ws):
    # #12 (spec half): QA's e2e authoring must receive the tech spec so it asserts the
    # real API shapes instead of guessing.
    from agents import qa
    paths = full_seed(ws)
    _frontend_layer(ws)
    llm.queue = ["QA sign-off: GO", E2E_SPEC]
    qa.run(base_state(tests_passed=True, prd_path=paths["prd"], design_path=paths["tech"]))
    assert "POST /login" in llm.calls[1]["user"]             # tech spec content present
    assert "authoritative API contract" in llm.calls[1]["user"]


def test_design_sets_stable_design_spec_path(llm, ws):
    # The architect overwrites design_path with the tech spec; design_spec_path must
    # survive so the engineer can still see the actual design (visual fidelity).
    from agents import design
    paths = full_seed(ws)
    llm.queue = ["## Screens\nA clean header", "<html><body>mock</body></html>"]
    out = design.run(base_state(prd_path=paths["prd"], detected_stack="Python", qa_log=[_CHOICE]))
    assert out["design_spec_path"] == out["design_path"]
    assert out["design_spec_path"].endswith("design_spec.md")


def test_engineer_prompt_includes_design_spec_and_mockup(llm, ws, no_docker):
    # Prevention half of the design-fidelity gap: the engineer must SEE the design.
    # (A live run shipped a working app that looked nothing like the mockup — the
    # engineer had only ever received the tech spec.)
    from agents import engineer
    paths = full_seed(ws)
    spec = seed(ws, "proj", "design", "design_spec.md",
                "## Microcopy\nHeader says: My Tasks Today")
    mockup = seed(ws, "proj", "design", "mockup.html",
                  "<html><h1>My Tasks Today</h1></html>")
    llm.default = FILES_OUTPUT
    engineer.run(base_state(design_path=paths["tech"], design_spec_path=spec,
                            design_mockup_path=mockup))
    prompt = llm.calls[0]["user"]
    assert "My Tasks Today" in prompt                      # design content reached it
    assert "DESIGN SPEC" in prompt and "DESIGN MOCKUP HTML" in prompt
    assert "must match" in prompt


def test_engineer_quick_lane_skips_design_block(llm, ws, no_docker):
    from agents import engineer
    paths = full_seed(ws)
    llm.default = FILES_OUTPUT
    engineer.run(base_state(design_path=paths["tech"], change_type="bugfix"))
    assert "DESIGN MOCKUP" not in llm.calls[0]["user"]     # nothing to match, no cost


def test_engineer_parser_survives_unclosed_duplicate_file_blocks(llm, ws, no_docker):
    # A live run re-emitted layout.tsx WITHOUT closing the first block — the naive
    # regex glued the second '===FILE:' marker INTO the file (5 corrupted sources).
    # The hardened parser ends a block at the next marker and keeps the LAST emission.
    from agents import engineer
    paths = full_seed(ws)
    llm.default = (
        "===FILE: src/layout.tsx===\n"
        "const TRUNCATED_V1 = (\n"                      # no ===END=== — model error
        "===FILE: src/layout.tsx===\n"
        "export const GOOD_V2 = 'complete';\n"
        "===END===\n"
        "===FILE: src/other.py===\nok = 1\n===END===\n"
    )
    engineer.run(base_state(design_path=paths["tech"]))
    text = (ws / "proj" / "src" / "layout.tsx").read_text()
    assert "===FILE:" not in text                      # no marker corruption
    assert "GOOD_V2" in text and "TRUNCATED_V1" not in text   # last emission wins
    assert (ws / "proj" / "src" / "other.py").read_text().strip() == "ok = 1"


def test_engineer_never_writes_e2e_specs(llm, ws, no_docker):
    from agents import engineer
    paths = full_seed(ws)
    llm.default = (
        "===FILE: e2e/todo-flow.spec.ts===\nhacked spec\n===END===\n"
        "===FILE: src/ok.py===\nx = 1\n===END===\n"
    )
    out = engineer.run(base_state(design_path=paths["tech"],
                                  e2e_files=["e2e/todo-flow.spec.ts"]))
    assert not (ws / "proj" / "e2e" / "todo-flow.spec.ts").exists()  # blocked
    assert (ws / "proj" / "src" / "ok.py").exists()                  # app code fine


# ── Design QA (vision verification, app vs mockup) ───────────────────────────

def _design_qa_setup(ws, monkeypatch):
    from agents import design_qa
    mock = ws / "proj" / "design" / "mockup.html"
    mock.parent.mkdir(parents=True, exist_ok=True)
    mock.write_text("<html>mock</html>")
    shot = ws / "proj" / "tests" / "app_screenshot.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG fake")
    def fake_render(mockup_path, out_png):
        from pathlib import Path
        Path(out_png).parent.mkdir(parents=True, exist_ok=True)
        Path(out_png).write_bytes(b"\x89PNG fake mockup")
        return True, "ok"
    monkeypatch.setattr(design_qa, "render_mockup_screenshot", fake_render)
    return design_qa, str(mock), str(shot)


def test_design_qa_misaligned_loops_engineer_with_findings(llm, ws, monkeypatch):
    design_qa, mock, shot = _design_qa_setup(ws, monkeypatch)
    paths = full_seed(ws)
    llm.default = ('{"verdict":"MISALIGNED","findings":'
                   '"- title: expected \'Today\'s List\' / got \'Task Manager\'"}')
    out = design_qa.run(base_state(design_mockup_path=mock, app_screenshot_path=shot,
                                   design_spec_path=paths["design"]))
    assert out["design_qa_passed"] is False
    assert out["design_qa_attempts"] == 1
    assert "Today's List" in out["review_notes"]            # findings reach the engineer
    assert llm.calls[0]["images"] and len(llm.calls[0]["images"]) == 2  # both screenshots sent
    assert "Login screen" in llm.calls[0]["user"]           # design spec in the prompt
    assert "MISALIGNED" in (ws / "proj" / "tests" / "design_qa.md").read_text()


def test_design_qa_aligned_proceeds_clean(llm, ws, monkeypatch):
    design_qa, mock, shot = _design_qa_setup(ws, monkeypatch)
    llm.default = '{"verdict":"ALIGNED","findings":null}'
    out = design_qa.run(base_state(design_mockup_path=mock, app_screenshot_path=shot))
    assert out["design_qa_passed"] is True
    assert "review_notes" not in out


def test_design_qa_skips_without_mockup_or_screenshot(llm, ws):
    from agents import design_qa
    # quick lane / backend-only: no mockup → pass-through, no LLM cost
    out = design_qa.run(base_state())
    assert out["design_qa_passed"] is True and llm.calls == []
    # mockup exists but integration captured no screenshot → graceful skip
    mock = ws / "proj" / "design" / "mockup.html"
    mock.parent.mkdir(parents=True, exist_ok=True)
    mock.write_text("<html>m</html>")
    out2 = design_qa.run(base_state(design_mockup_path=str(mock)))
    assert out2["design_qa_passed"] is True and llm.calls == []


def test_integration_requests_screenshot_when_mockup_exists(ws, monkeypatch):
    from agents import integration
    captured = {}
    monkeypatch.setattr(integration, "run_compose_integration",
                        lambda d, **kw:
                        captured.update(shot=kw.get("screenshot_to")) or (True, "ok"))
    mock = ws / "proj" / "design" / "mockup.html"
    mock.parent.mkdir(parents=True, exist_ok=True)
    mock.write_text("<html>m</html>")
    integration.run(base_state(design_mockup_path=str(mock)))
    assert captured["shot"] and captured["shot"].endswith("app_screenshot.png")
    integration.run(base_state())                           # no mockup → no screenshot ask
    assert captured["shot"] is None or "app_screenshot" not in str(captured.get("shot"))


# ── Design-owned component kit (alignment by construction) ───────────────────

KIT_OUTPUT = (
    "===FILE: frontend/src/components/kit/TaskForm.tsx===\n"
    "export const TaskForm = ({ onAdd }: { onAdd: (t: string) => void }) => "
    "<input placeholder=\"What needs to get done?\" data-testid=\"task-input\" />;\n"
    "===END===\n"
    "===FILE: MANIFEST===\n"
    "# Design Component Kit — wiring manifest\n"
    "## Components\n"
    "- TaskForm (frontend/src/components/kit/TaskForm.tsx): props {onAdd}\n"
    "## REQUIRED MICROCOPY (must appear verbatim in the running app)\n"
    "- \"What needs to get done?\"\n"
    "- \"Today's List\"\n"
    "===END===\n"
)


def test_design_emits_component_kit_when_stack_known(llm, ws):
    from agents import design
    from tools import product
    product.save_stack("FastAPI + Next.js (React) + Postgres")     # persisted CTO stack
    paths = full_seed(ws)
    llm.queue = ["## Screens\nTask form", "<html>mockup</html>", KIT_OUTPUT]
    out = design.run(base_state(prd_path=paths["prd"], qa_log=[_CHOICE]))
    assert out["design_component_files"] == ["frontend/src/components/kit/TaskForm.tsx"]
    kit = ws / "proj" / "frontend" / "src" / "components" / "kit" / "TaskForm.tsx"
    assert kit.exists() and "What needs to get done?" in kit.read_text()
    manifest = (ws / "proj" / "design" / "components_manifest.md").read_text()
    assert "REQUIRED MICROCOPY" in manifest


def test_design_skips_kit_when_stack_unknown(llm, ws):
    # Stack settled but NOT react (e.g. server-rendered Python) → no kit, no 3rd call.
    from agents import design
    paths = full_seed(ws)
    llm.queue = ["## Screens\nTask form", "<html>mockup</html>"]
    out = design.run(base_state(prd_path=paths["prd"], detected_stack="Python, server-rendered", qa_log=[_CHOICE]))
    assert out["design_component_files"] == []
    assert len(llm.calls) == 2                                     # spec + mockup only


def test_engineer_cannot_touch_design_kit_and_gets_wiring_contract(llm, ws, no_docker):
    from agents import engineer
    paths = full_seed(ws)
    kit_rel = "frontend/src/components/kit/TaskForm.tsx"
    kit_abs = ws / "proj" / kit_rel
    kit_abs.parent.mkdir(parents=True, exist_ok=True)
    kit_abs.write_text("DESIGN OWNED")
    manifest = seed(ws, "proj", "design", "components_manifest.md",
                    "## Components\n- TaskForm: props {onAdd}")
    llm.default = (
        f"===FILE: {kit_rel}===\nhacked\n===END===\n"
        "===FILE: frontend/src/app/page.tsx===\nwired\n===END===\n"
    )
    engineer.run(base_state(design_path=paths["tech"],
                            design_component_files=[kit_rel],
                            components_manifest_path=manifest))
    assert kit_abs.read_text() == "DESIGN OWNED"                   # kit untouched
    assert (ws / "proj" / "frontend" / "src" / "app" / "page.tsx").exists()
    prompt = llm.calls[0]["user"]
    assert "DESIGN-OWNED COMPONENT KIT" in prompt and "NEVER modify" in prompt
    assert "props {onAdd}" in prompt                               # manifest reached it


def test_integration_passes_required_microcopy(ws, monkeypatch):
    from agents import integration
    captured = {}
    monkeypatch.setattr(integration, "run_compose_integration",
                        lambda d, **kw:
                        captured.update(copy=kw.get("required_microcopy")) or (True, "ok"))
    manifest = seed(ws, "proj", "design", "components_manifest.md",
                    '## REQUIRED MICROCOPY (must appear verbatim)\n'
                    '- "Today\'s List"\n- "What needs to get done?"\n')
    integration.run(base_state(components_manifest_path=manifest))
    assert captured["copy"] == ["Today's List", "What needs to get done?"]


def test_check_required_microcopy_fails_gracefully_offline():
    from tools.registry import check_required_microcopy
    ok, msg = check_required_microcopy(["x"], url="http://localhost:59999/")
    assert not ok and "could not fetch" in msg


# ── Design-time stack ask + design-system memory + SEO gate ──────────────────

def test_design_asks_cto_for_stack_when_unknown(llm, ws):
    # First greenfield run: no persisted/detected stack → design escalates ONCE to the
    # CTO so it can emit real components from feature #1.
    from agents import design
    paths = full_seed(ws)
    out = design.run(base_state(prd_path=paths["prd"]))
    assert out["ceo_qa_from"] == "design"
    assert "DESIGN STACK CONFIRMATION" in out["ceo_qa_pending"]
    assert "default" in out["ceo_qa_pending"].lower()
    assert llm.calls == []                       # deterministic ask — no LLM spent


def test_design_persists_default_stack_and_emits_kit_after_cto_confirms(llm, ws):
    # CTO replies 'default' → Next.js stack persisted → kit emitted in the SAME run;
    # the architect's later mandatory stack ask auto-skips (persisted stack).
    from agents import design
    from tools import product
    paths = full_seed(ws)
    log = [{"from": "design", "to": "ceo",
            "question": "DESIGN STACK CONFIRMATION (CTO call): ...", "answer": "default"}]
    llm.queue = ["## Screens\nForm", "<html>mock</html>", KIT_OUTPUT]
    out = design.run(base_state(prd_path=paths["prd"], qa_log=log + [_CHOICE]))
    assert "Next.js" in product.load_stack()     # persisted — architect won't re-ask
    assert out["design_component_files"]         # kit emitted on feature #1


def test_design_persists_named_stack_verbatim(llm, ws):
    from agents import design
    from tools import product
    paths = full_seed(ws)
    log = [{"from": "design", "to": "ceo",
            "question": "DESIGN STACK CONFIRMATION (CTO call): ...",
            "answer": "Use SvelteKit + Go + SQLite"}]
    llm.queue = ["## Screens\nForm", "<html>mock</html>"]
    out = design.run(base_state(prd_path=paths["prd"], qa_log=log + [_CHOICE]))
    assert "SvelteKit" in product.load_stack()   # CTO's words are the record
    assert out["design_component_files"] == []   # non-react → no kit, mockup-guided


def test_design_system_persisted_and_recalled(llm, ws):
    # Feature 1 writes the design system; feature 2's prompt carries it — the
    # coherence memory that stops per-feature UX drift.
    from agents import design
    from tools import product
    paths = full_seed(ws)
    spec = ("## Screens\nx\n## Design System\nFont: Inter; accent: indigo-600; "
            "spacing: 4px rhythm; voice: calm, second person.\n## Flagged Items\nNone.")
    llm.queue = [spec, "<html>m</html>"]
    design.run(base_state(prd_path=paths["prd"], detected_stack="Python", qa_log=[_CHOICE]))
    assert "indigo-600" in product.load_design_system()          # persisted
    llm.queue = ["## Screens\ny", "<html>m2</html>"]
    design.run(base_state(prd_path=paths["prd"], detected_stack="Python", qa_log=[_CHOICE]))
    assert "ESTABLISHED DESIGN SYSTEM" in llm.calls[-2]["user"]  # spec call of run 2
    assert "indigo-600" in llm.calls[-2]["user"]                 # tokens recalled


def test_design_sees_existing_kit_inventory(llm, ws):
    from agents import design
    paths = full_seed(ws)
    kit = ws / "proj" / "frontend" / "src" / "components" / "kit"
    kit.mkdir(parents=True)
    (kit / "TaskForm.tsx").write_text("existing")
    llm.queue = ["## Screens\nx", "<html>m</html>"]
    design.run(base_state(prd_path=paths["prd"], detected_stack="Python"))
    prompt = llm.calls[0]["user"]
    assert "EXISTING KIT COMPONENTS" in prompt and "TaskForm.tsx" in prompt


def test_design_mockup_and_kit_prompts_mandate_dual_surface_and_theme(llm, ws):
    # The mandate must reach BOTH generation calls: mockup (both widths + both modes)
    # and the component kit (dark: variants + ThemeToggle + breakpoints).
    from agents import design
    from tools import product
    product.save_stack("Next.js + FastAPI + Postgres")
    paths = full_seed(ws)
    llm.queue = ["## Screens\nForm", "<html>mock</html>", KIT_OUTPUT]
    design.run(base_state(prd_path=paths["prd"], qa_log=[_CHOICE]))
    mockup_prompt = llm.calls[1]["user"]
    assert "375px" in mockup_prompt and "1280" in mockup_prompt     # dual surface
    assert "dark" in mockup_prompt.lower()                          # dual theme
    kit_prompt = llm.calls[2]["user"]
    assert "dark:" in kit_prompt and "ThemeToggle" in kit_prompt
    assert "theme-toggle" in kit_prompt


def test_theme_findings_detects_missing_toggle_and_dark_variants():
    from tools.registry import _theme_findings
    bare = '<html><body><h1>App</h1></body></html>'
    misses = _theme_findings(bare)
    assert len(misses) == 2
    themed = ('<html class="dark:bg-gray-950"><body>'
              '<button data-testid="theme-toggle">🌙</button>'
              '<div class="bg-white dark:bg-gray-900">x</div></body></html>')
    assert _theme_findings(themed) == []
    # Token-based theming (attribute-switched custom properties) counts too — a
    # data-theme mechanism false-failed the "dark:"-only heuristic on a live run.
    token_themed = ('<html data-theme="light"><head><script>var m=window.matchMedia('
                    '"(prefers-color-scheme: dark)");</script></head><body>'
                    '<button data-testid="theme-toggle">Dark mode</button></body></html>')
    assert _theme_findings(token_themed) == []
    # A toggle alone without ANY dark-mode mechanism still fails the mechanism check.
    toggle_only = '<body><button data-testid="theme-toggle">Dark</button></body>'
    assert len(_theme_findings(toggle_only)) == 1


def test_seo_findings_detects_all_misses_and_passes_full_page():
    from tools.registry import _seo_findings
    assert len(_seo_findings("<html><body>hi</body></html>")) == 6   # everything missing
    good = ('<html lang="en"><head><title>Today\'s List</title>'
            '<meta name="description" content="A simple task app">'
            '<meta name="viewport" content="width=device-width">'
            '<script type="application/ld+json">{"@type":"WebApplication"}</script>'
            '</head><body><h1>Today\'s List</h1></body></html>')
    assert _seo_findings(good) == []


def test_integration_runs_seo_gate_only_for_ui_features(ws, monkeypatch):
    from agents import integration
    captured = {}
    monkeypatch.setattr(integration, "run_compose_integration",
                        lambda d, **kw: captured.update(seo=kw.get("check_seo")) or (True, "ok"))
    mock = seed(ws, "proj", "design", "mockup.html", "<html>m</html>")
    integration.run(base_state(design_mockup_path=mock))
    assert captured["seo"] is True
    integration.run(base_state())                                    # backend-only
    assert captured["seo"] is False


# ── QA ────────────────────────────────────────────────────────────────────────

def test_qa_pass_writes_report_and_requests_pr_approval(llm, ws):
    from agents import qa
    paths = full_seed(ws)
    llm.default = "QA sign-off: GO"
    out = qa.run(base_state(prd_path=paths["prd"], tests_passed=True))
    assert (ws / "proj" / "tests" / "qa_report.md").exists()
    assert out["approval_pending"] == "pr"              # PR gated, not auto-opened
    assert "pr_url" not in out                          # QA no longer opens the PR


def test_qa_reads_and_reviews_the_code(llm, ws):
    # #5: QA must actually read the engineer's code, not rubber-stamp the tests.
    from agents import qa
    paths = full_seed(ws)
    code = seed(ws, "proj", "src", "main.py",
                "def login(pw):\n    if pw == 'admin':  # hardcoded backdoor\n        return True")
    llm.default = "QA sign-off: NO-GO — hardcoded credential in login()"
    qa.run(base_state(prd_path=paths["prd"], tests_passed=True, code_files=[code]))
    assert "hardcoded backdoor" in llm.user_texts()      # the code reached the review prompt
    assert "main.py" in llm.user_texts()


def test_qa_fail_enriches_error_log(llm, ws):
    from agents import qa
    llm.default = "ROOT CAUSE: import error\nFIX: add module"
    out = qa.run(base_state(tests_passed=False, error_log="ImportError: foo"))
    assert out["tests_passed"] is False
    assert "ROOT CAUSE" in out["error_log"]             # diagnosis prepended
    assert out.get("approval_pending") is None


def test_qa_escalates_when_blocked(llm, ws):
    from agents import qa
    paths = full_seed(ws)
    llm.default = NEEDS_CEO
    out = qa.run(base_state(prd_path=paths["prd"], tests_passed=True))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "qa"


# ── DevOps ────────────────────────────────────────────────────────────────────

def test_devops_writes_deploy_files(llm, ws):
    from agents import devops
    paths = full_seed(ws)
    llm.default = "===FILE: Dockerfile===\nFROM python:3.11\n===END==="
    out = devops.run(base_state(design_path=paths["tech"], tests_passed=True))
    assert out["deploy_path"]
    assert out["deployed"] is False                     # generates IaC, doesn't execute


def test_devops_escalates_when_blocked(llm, ws):
    from agents import devops
    paths = full_seed(ws)
    llm.default = NEEDS_CEO
    out = devops.run(base_state(design_path=paths["tech"], tests_passed=True))
    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "devops"  # no agent is blocked


# ── C7: deploy-target persistence (I5) ───────────────────────────────────────────────────
def test_devops_injects_persisted_deploy_target_and_forbids_asking(llm, ws):
    from agents import devops
    from tools import product
    paths = full_seed(ws)
    product.save_deploy_target("local compose only, dry-run manifests")
    llm.default = "===FILE: Dockerfile===\nFROM python:3.11\n===END==="
    devops.run(base_state(design_path=paths["tech"], tests_passed=True))
    prompt = llm.user_texts()
    assert "STANDING DEPLOY TARGET" in prompt
    assert "local compose only" in prompt
    assert "DO NOT ask about the deploy target" in prompt


def test_devops_persists_a_ceo_deploy_answer(llm, ws):
    from agents import devops
    from tools import product
    paths = full_seed(ws)
    assert product.load_deploy_target() == ""
    llm.default = "===FILE: Dockerfile===\nFROM python:3.11\n===END==="
    # simulate the resume where the CEO answered the deploy-target question
    state = base_state(design_path=paths["tech"], tests_passed=True)
    state["qa_log"] = [{"from": "devops", "to": "ceo",
                        "question": "What is the deploy target / hosting?",
                        "answer": "Cloud Run for the api, dry-run only for now."}]
    devops.run(state)
    assert "Cloud Run" in product.load_deploy_target()


def test_devops_absent_target_is_current_behavior(llm, ws):
    from agents import devops
    from tools import product
    paths = full_seed(ws)
    llm.default = "===FILE: Dockerfile===\nFROM python:3.11\n===END==="
    devops.run(base_state(design_path=paths["tech"], tests_passed=True))
    assert "STANDING DEPLOY TARGET" not in llm.user_texts()
    assert product.load_deploy_target() == ""


def test_devops_does_not_persist_non_deploy_ceo_answer(llm, ws):
    # A CEO answer to an unrelated devops question that merely mentions "target"/"cloud"/"infra"
    # (a latency target, a cloud storage bucket) must NOT be captured as the standing deploy
    # target — the trigger requires an explicit deploy/hosting term.
    from agents import devops
    from tools import product
    paths = full_seed(ws)
    assert product.load_deploy_target() == ""
    llm.default = "===FILE: Dockerfile===\nFROM python:3.11\n===END==="
    state = base_state(design_path=paths["tech"], tests_passed=True)
    state["qa_log"] = [{"from": "devops", "to": "ceo",
                        "question": "What latency target should the healthcheck allow, "
                                    "and which cloud storage bucket holds build artifacts?",
                        "answer": "200ms, and the artifacts bucket in us-central1."}]
    devops.run(state)
    assert product.load_deploy_target() == ""
