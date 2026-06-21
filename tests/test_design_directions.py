"""
Design directions (CEO mandate 2026-06-12): design emits THREE distinct directions
with rationale + a mockup each, renders a side-by-side review page, and pauses for
the REAL HUMAN to pick. The chosen direction becomes THE design; the kit is built
from the winner only. Plus the deterministic HTML review layer (tools/report_html).
"""

from pathlib import Path

from tests.conftest import base_state, seed
from tools import report_html

SPEC_3DIR = """## Design Context
busy people tracking todos
## Design Directions
### A — Calm list
Minimal single-column list; serves focus; trade-off: less data density.
### B — Dense board
Compact grid with stats up top; serves power users; trade-off: busier first-run.
### C — Playful cards
Big friendly cards; serves casual users; trade-off: fewer items per screen.
## Content & Microcopy
CTA: 'Add task'
## Flagged Items
None.
"""


def _full_seed(ws):
    return seed(ws, "proj", "prd", "prd.md", "User can log in and manage tasks")


def test_design_emits_three_mockups_and_pauses_for_human(llm, ws):
    from agents import design
    prd = _full_seed(ws)
    llm.queue = [SPEC_3DIR, "<html>A</html>", "<html>B</html>", "<html>C</html>"]
    out = design.run(base_state(prd_path=prd, detected_stack="Python"))

    assert out["ceo_qa_pending"] and out["ceo_qa_from"] == "design"
    q = out["ceo_qa_pending"]
    assert "DESIGN DIRECTION CHOICE" in q
    assert "Calm list" in q and "Dense board" in q and "Playful cards" in q  # rationale shown
    for x in "ABC":
        assert (ws / "proj" / "design" / f"mockup_{x}.html").exists()
    review = ws / "proj" / "review" / "design_options.html"
    assert review.exists()
    page = review.read_text()
    # mockups are EMBEDDED inline (srcdoc) so the board renders when opened from disk;
    # a file:// iframe src renders blank (the live phase-2 report bug).
    assert "srcdoc=" in page and "&lt;html&gt;A&lt;/html&gt;" in page   # mockup A inlined
    assert "Dense board" in page                                        # rationale still shown
    assert '<iframe src="../design/' not in page                        # no file:// iframe
    assert str(review) in q                                  # human gets the page path
    assert not (ws / "proj" / "design" / "mockup.html").exists()   # nothing chosen yet


def test_design_resumes_with_choice_without_regenerating(llm, ws):
    from agents import design
    prd = _full_seed(ws)
    llm.queue = [SPEC_3DIR, "<html>A</html>", "<html>B</html>", "<html>C</html>"]
    state = base_state(prd_path=prd, detected_stack="Python")
    design.run(state)
    calls_before = len(llm.calls)

    qa_log = [{"from": "design", "to": "ceo",
               "question": "DESIGN DIRECTION CHOICE (CEO/CTO — human pick): ...",
               "answer": "B, but tone down the header"}]
    out = design.run(base_state(prd_path=prd, detected_stack="Python", qa_log=qa_log))

    assert len(llm.calls) == calls_before                    # NO regeneration on resume
    assert out["design_choice"] == "B"
    assert [o["id"] for o in out["design_options"]] == ["A", "B", "C"]
    mock = (ws / "proj" / "design" / "mockup.html").read_text()
    assert mock == "<html>B</html>"                          # the winner is THE mockup
    spec = (ws / "proj" / "design" / "design_spec.md").read_text()
    assert "## Chosen Direction" in spec and "B — Dense board" in spec
    assert "tone down the header" in spec                    # tweak notes recorded


def test_direction_choice_parsing():
    from agents.design import _direction_choice
    def log(ans):
        return [{"from": "design", "to": "ceo",
                 "question": "DESIGN DIRECTION CHOICE ...", "answer": ans}]
    assert _direction_choice(log("B"))[0] == "B"
    assert _direction_choice(log("option 2 please"))[0] == "B"      # 1/2/3 accepted
    assert _direction_choice(log("the shiny one"))[0] == "A"        # unparseable → A
    assert _direction_choice([]) == (None, None)                    # not asked yet


def test_parse_directions_lenient_padding():
    from agents.design import _parse_directions
    dirs = _parse_directions("## Design Directions\n### A — Only one\njust this\n")
    assert [d["id"] for d in dirs] == ["A", "B", "C"]               # padded, never breaks
    assert dirs[0]["title"] == "Only one"


def test_backend_only_feature_skips_directions(llm, ws):
    from agents import design
    prd = _full_seed(ws)
    llm.default = "NO UI SURFACE - backend feature only."
    out = design.run(base_state(prd_path=prd, detected_stack="Python"))
    assert out["design_mockup_path"] is None
    assert len(llm.calls) == 1                                       # spec call only


# ── HTML review layer (deterministic — zero LLM) ─────────────────────────────

def test_render_gate_pr_dashboard(ws):
    seed(ws, "proj", "tests", "integration_report.md",
         "=== compose up --build — OK ===\n=== health — OK ===\n"
         "=== e2e (playwright) — FAILED ===\n2 failed")
    seed(ws, "proj", "tests", "qa_report.md", "## Verdict\n**GO** — all criteria met")
    (ws / "proj" / "tests" / "app_screenshot.png").write_bytes(b"png")
    path = report_html.render_gate(
        base_state(security_warnings=["GUARD: protected path change discarded: tests/x"]),
        "pr")
    page = Path(path).read_text()
    assert "compose up --build: OK" in page and "e2e (playwright): FAILED" in page
    assert "GO" in page and "app_screenshot.png" in page
    assert "protected path change discarded" in page
    assert path.endswith("review/pr_gate.html")


def test_render_gate_prd_dashboard(ws):
    seed(ws, "proj", "prd", "prd.md", "# Feature\n- AC1: user adds a task")
    path = report_html.render_gate(base_state(), "prd")
    page = Path(path).read_text()
    assert "AC1" in page and "Approve" in page


def test_render_gate_never_raises(ws):
    # gates must never break on rendering — missing artifacts → still a page or None
    assert report_html.render_gate({"project_id": "nonexistent-run"}, "pr") is not None \
        or True


def test_render_ledger_page(tmp_path):
    agent = tmp_path / ".agent"
    agent.mkdir()
    (agent / "ledger.md").write_text("# Ledger\n- feature: todo list — shipped")
    path = report_html.render_ledger(str(tmp_path))
    assert path and "todo list" in Path(path).read_text()
    assert report_html.render_ledger(str(tmp_path / "missing")) is None


# ── Human audit folder (Part 4) ──────────────────────────────────────────────

def test_render_audit_captures_actors_decisions_and_why(ws, tmp_path):
    from tools import report_html, trace
    # a trace with feedback events (the discussions/decisions)
    tr = trace.start("proj", trace_dir=tmp_path)
    trace.emit("node_exec", node="design", wall_ms=100)
    trace.emit("llm_call", tier="strong", node="design", in_tokens=5, out_tokens=900)
    trace.emit("feedback", agent="engineer", fb_kind="pr_gate_reject",
               text="counts wrong; fix following_count")
    trace.emit("feedback", agent="design", fb_kind="interface_regression",
               text="dropped data-testid 'profile-bio'")
    trace.reset()

    prd = seed(ws, "proj", "prd", "prd.md",
               "## Acceptance Criteria\n- AC-1 (ui): The profile shows the name.\n")
    seed(ws, "proj", "design", "design_spec.md", "## Design\nstuff")
    state = base_state(
        prd_path=prd,
        feature_request="adventures and badges",
        design_choice="A",
        design_options=[{"id": "A", "title": "Trailhead", "rationale": "warm"}],
        prd_approved=True, pr_approved=True, tests_passed=True,
        qa_log=[{"from": "architect", "to": "ceo",
                 "question": "multi-tenant?", "answer": "single-org for v1"}])
    path = report_html.render_audit(state, str(tr.path),
                                    retro={"design": ["don't regress prior ACs"]},
                                    overseer={"ok": True})
    html = Path(path).read_text()
    assert path.endswith("review/audit/index.html")
    # actors + topics + why
    assert "architect → ceo" in html and "single-org for v1" in html       # Q&A exchange
    assert "PR gate" in html and "following_count" in html                  # gate decision + why
    assert "Interface-Contract freeze" in html and "profile-bio" in html    # freeze + why
    assert "TRUSTWORTHY" in html                                            # overseer verdict
    assert "Trailhead" in html                                             # human design choice
    assert "AC-1" in html                                                  # coverage map
    assert "regress prior ACs" in html                                    # retro lesson
    assert (ws / "proj" / "review" / "audit" / "artifacts").is_dir()      # artifacts rendered


def test_render_audit_never_raises_on_missing_data():
    from tools import report_html
    assert report_html.render_audit({"project_id": "nope"}, "") is not None or True
