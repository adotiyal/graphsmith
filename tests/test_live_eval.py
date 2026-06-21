"""
LIVE eval — real LLM calls. Skipped automatically unless ANTHROPIC_API_KEY is set.

This is the "through the eye of a human worker" check at the quality level: it runs the
real spec-producing agents and asserts their output has the substance a human reviewer
would demand (the PRD has testable acceptance criteria, the architect names real
endpoints, etc.). It does NOT need Docker — it stops before the engineer's test run.

Run with:  ANTHROPIC_API_KEY=... pytest tests/test_live_eval.py -q -s
"""

import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live eval needs ANTHROPIC_API_KEY",
)

from conftest import base_state, seed   # noqa: E402


def test_pm_produces_testable_acceptance_criteria(ws):
    from agents import pm
    brief = seed(ws, "live", "prd", "ceo_brief.md",
                 "Build a URL shortener: users paste a long URL and get a short link "
                 "they can share; visiting the short link redirects to the original.")
    out = pm.run(base_state("live", prd_path=brief))
    prd = (ws / "live" / "prd" / "prd.md").read_text().lower()
    assert "acceptance criteria" in prd
    assert out["approval_pending"] == "prd"
    # A human PM would not leave it without concrete, numbered criteria.
    assert any(prd.strip().count(f"{n}.") for n in (1, 2)), "no enumerated criteria"


def test_architect_names_real_endpoints(ws):
    from agents import architect
    prd = seed(ws, "live", "prd", "prd.md",
               "## Acceptance Criteria\n1. POST a long URL, receive a short code\n"
               "2. GET the short code redirects to the original URL\n3. invalid code -> 404")
    design = seed(ws, "live", "design", "design_spec.md", "NO UI SURFACE - backend feature only.")
    architect.run(base_state("live", prd_path=prd, design_path=design))
    spec = (ws / "live" / "design" / "tech_spec.md").read_text().lower()
    assert "endpoint" in spec or "/" in spec
    assert "post" in spec and "get" in spec       # both operations specified


def test_test_author_covers_each_criterion(ws):
    from agents import test_author
    prd = seed(ws, "live", "prd", "prd.md",
               "## Acceptance Criteria\n1. POST /shorten returns a code\n"
               "2. GET /{code} redirects (302)\n3. unknown code returns 404")
    spec = seed(ws, "live", "design", "tech_spec.md",
                "## API Endpoints\nPOST /shorten -> 200\nGET /{code} -> 302\n")
    test_author.run(base_state("live", prd_path=prd, design_path=spec))
    files = list((ws / "live" / "tests").glob("*.py"))
    assert files, "no tests written"
    body = "\n".join(f.read_text().lower() for f in files)
    assert "def test" in body and ("404" in body or "302" in body)
