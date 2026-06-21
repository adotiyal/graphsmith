"""
Feature Contract spine (zero-drift): stable AC ids parsed from the PRD, AC refs
extracted from tests/e2e, and a deterministic coverage proof (every AC tested,
every UI AC has an e2e). The mechanism that makes "same understanding, 100%
coverage" a checked invariant instead of a hope.
"""

from tools import contract

PRD = """# Feature
TrailTribe adventures.

## Acceptance Criteria
- AC-1 (backend): A user can create an adventure with a required title.
- AC-2 (ui): The profile displays adventures newest-first with a count.
- AC-3: A user's badge is calculated from their adventure count.
- AC-4 (ui): The badge is shown on the discover card.

## Out of Scope
- cloud storage
"""


def test_parse_acs_ids_and_surface():
    acs = contract.parse_acs(PRD)
    assert [a["id"] for a in acs] == ["AC-1", "AC-2", "AC-3", "AC-4"]
    by = {a["id"]: a for a in acs}
    assert by["AC-1"]["surface"] == "backend"
    assert by["AC-2"]["surface"] == "ui"
    assert by["AC-3"]["surface"] == "backend"      # inferred (no UI keywords + no tag)
    assert by["AC-4"]["surface"] == "ui"           # inferred from "card"/"shown"
    assert "required title" in by["AC-1"]["text"] and "(backend)" not in by["AC-1"]["text"]


def test_parse_acs_tolerates_plain_numbered_list():
    plain = ("## Acceptance Criteria\n"
             "1. The user logs in with email and password.\n"
             "2. The dashboard page shows their name.\n")
    acs = contract.parse_acs(plain)
    assert [a["id"] for a in acs] == ["AC-1", "AC-2"]
    assert acs[1]["surface"] == "ui"               # "page"/"shows" → ui


def test_extract_ac_refs_variants():
    assert contract.extract_ac_refs("# covers: AC-1, AC2 and AC 3") == {"AC-1", "AC-2", "AC-3"}
    assert contract.extract_ac_refs("nothing here") == set()


def test_coverage_full_pass():
    acs = contract.parse_acs(PRD)
    unit = ["def test_create(): pass  # AC-1", "def test_badge(): pass  # AC-3"]
    e2e = ["def test_profile(page): ...  # AC-2",
           "def test_card(page): ...  # AC-4 badge on card"]
    cov = contract.coverage(acs, unit, e2e)
    ok, msg = contract.coverage_report(cov)
    assert ok and not cov["uncovered"] and not cov["ui_without_e2e"]
    assert "4/4 tested" in msg


def test_coverage_flags_uncovered_and_ui_without_e2e():
    acs = contract.parse_acs(PRD)
    # AC-3 has no test at all; AC-4 (ui) only has a UNIT ref, no e2e
    unit = ["# AC-1", "# AC-2", "# AC-4 only a unit test, not an e2e"]
    e2e = ["# AC-2 profile flow"]
    cov = contract.coverage(acs, unit, e2e)
    ok, msg = contract.coverage_report(cov)
    assert not ok
    assert cov["uncovered"] == ["AC-3"]
    assert cov["ui_without_e2e"] == ["AC-4"]
    assert "AC-3" in msg and "GAP" in msg


def test_coverage_no_acs_is_skip():
    cov = contract.coverage([], [], [])
    ok, msg = contract.coverage_report(cov)
    assert ok and "skipped" in msg


# ── integration coverage gate + agent wiring ─────────────────────────────────

def test_integration_coverage_gate_routes_ui_gap_to_qa(ws, monkeypatch):
    from agents import integration as integ
    from tests.conftest import base_state, seed
    # a UI AC with no e2e tag → coverage FAILS, routes to QA's revision round
    prd = seed(ws, "proj", "prd", "prd.md",
               "## Acceptance Criteria\n- AC-1 (ui): The profile page shows the user's name.\n")
    seed(ws, "proj", "tests", "test_x.py", "# covers: AC-1 (unit only)\ndef test_x(): pass")
    seed(ws, "proj", "e2e", "test_flow.py", "def test_flow(page):\n    pass\n")  # no AC tag
    monkeypatch.setattr(integ, "run_compose_integration",
                        lambda d, **kw: (True, "=== smoke — OK ==="))   # app all green
    out = integ.run(base_state(prd_path=prd, e2e_files=["e2e/test_flow.py"]))
    assert out["integration_passed"] is False              # coverage gap fails the gate
    assert out["integration_failed_stage"] == "coverage"
    assert out.get("e2e_revision_pending") is True         # → QA adds the missing e2e
    rpt = (ws / "proj" / "tests" / "integration_report.md").read_text()
    assert "AC coverage" in rpt and "AC-1" in rpt


def test_integration_coverage_passes_when_ui_ac_has_e2e(ws, monkeypatch):
    from agents import integration as integ
    from tests.conftest import base_state, seed
    prd = seed(ws, "proj", "prd", "prd.md",
               "## Acceptance Criteria\n- AC-1 (ui): The profile page shows the name.\n")
    seed(ws, "proj", "e2e", "test_flow.py", "# covers: AC-1\ndef test_flow(page):\n    pass\n")
    monkeypatch.setattr(integ, "run_compose_integration",
                        lambda d, **kw: (True, "ok"))
    out = integ.run(base_state(prd_path=prd, e2e_files=["e2e/test_flow.py"]))
    assert out["integration_passed"] is True


def test_pm_prompt_mandates_ac_ids(llm, ws):
    from agents import pm
    from tests.conftest import base_state, seed
    prd = seed(ws, "proj", "prd", "ceo_brief.md", "build login")
    llm.default = "## Acceptance Criteria\n- AC-1 (backend): x"
    pm.run(base_state(prd_path=prd))
    prompt = llm.calls[0]["user"]
    assert "AC-1" in prompt and "(ui)" in prompt and "(backend)" in prompt
