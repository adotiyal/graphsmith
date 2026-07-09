"""
Work item B — spec-coverage ledger (tools/spec_ledger.py).

Kills cross-phase scope decay: a persistent, per-product ledger of the standing spec's
numbered sections and whether each has shipped. These tests pin the deterministic
parse/persist/merge behavior, the never-raises end-of-run coverage marking, the PM prompt
injection, and the absent-spec byte-identical no-op.

LLM is mocked (MockLLM); no key/Docker needed.
"""

from conftest import base_state, seed
from tools import spec_ledger


# ── parse_spec — deterministic numbered-section extraction ───────────────────────────────

def test_parse_spec_dotted_and_plain_headings():
    txt = ("## 6.14 Operator storefront editor\n"
           "### 7.2 Checkout flow\n"
           "# 12. Admin dashboard\n")
    assert spec_ledger.parse_spec(txt) == [
        {"id": "6.14", "title": "Operator storefront editor"},
        {"id": "7.2", "title": "Checkout flow"},
        {"id": "12", "title": "Admin dashboard"},
    ]


def test_parse_spec_bold_and_screen_section_lines():
    txt = ("**6.14** Storefront editor\n"
           "### Screen 12 — Product grid\n"
           "## Section 3: Settings\n")
    out = spec_ledger.parse_spec(txt)
    assert {"id": "6.14", "title": "Storefront editor"} in out
    assert {"id": "12", "title": "Product grid"} in out
    assert {"id": "3", "title": "Settings"} in out


def test_parse_spec_dedupe_keeps_first():
    assert spec_ledger.parse_spec("## 6.14 First\n## 6.14 Second\n") == [
        {"id": "6.14", "title": "First"}]


def test_parse_spec_no_numbers_returns_empty():
    # A bare number inside prose must NOT match (precision over recall).
    assert spec_ledger.parse_spec("# Overview\nSome prose about 6.14 percent of users.\n") == []
    assert spec_ledger.parse_spec("") == []
    assert spec_ledger.parse_spec(None) == []


def test_parse_spec_preserves_appearance_order():
    txt = "## 9.1 Late\n## 1.2 Early\n**3.3** Middle\n"
    assert [s["id"] for s in spec_ledger.parse_spec(txt)] == ["9.1", "1.2", "3.3"]


# ── inline parenthetical ids (review fix — the real failure class) ────────────────────────
# On the real build spec, §6.14 exists ONLY as inline parenthetical references — a flow
# line and a table row — never as a heading, so the ledger missed the exact section whose
# silent loss motivated it. Fixture mimics both REAL formats (hermetic, inline text).

def test_parse_spec_inline_parenthetical_flow_and_table_rows():
    txt = (
        "## 5. Build order\n"
        "7. **Storefront** → public `/o/{slug}` (6.14) built from `storefront` jsonb → "
        "**on-platform only**; guides + verified certs; all their Events.\n"
        "\n"
        "| Screen (PRD) | Key components | API |\n"
        "|---|---|---|\n"
        "| Operator storefront (6.14) | hero, VerificationBadge, ProductCard | `GET /o/{slug}` |\n"
        "| Checkout flow (6.15) | CartSheet, PayButton | `POST /api/checkout` |\n"
        "\n"
        "Fees follow the payments appendix (§6.2); RBAC is enforced server-side (§2).\n"
        "Launched in (12) markets.\n"
    )
    ids = {e["id"]: e["title"] for e in spec_ledger.parse_spec(txt)}
    assert "6.14" in ids and "storefront" in ids["6.14"].lower()   # the motivating section
    assert ids["6.15"] == "Checkout flow"          # table-cell name, cell-scoped by `|`
    assert "6.2" not in ids and "2" not in ids     # §-prefixed cross-references are skipped
    assert "12" not in ids                         # undotted parenthetical never matches


def test_parse_spec_inline_dedupes_against_heading():
    txt = ("## 6.14 Operator storefront editor\n"
           "| Storefront (6.14) | hero | `GET /o/{slug}` |\n")
    assert spec_ledger.parse_spec(txt) == [
        {"id": "6.14", "title": "Operator storefront editor"}]


def test_parse_spec_heading_wins_title_over_earlier_inline_ref():
    # Appearance order is kept (the inline ref came first) but the heading DEFINES the
    # section — its title wins on id collision. Real case: `admin decides (7.3)` appears
    # in a flow line BEFORE the `### 7.3 Admin` heading.
    txt = ("| Storefront (6.14) | hero | `GET /x` |\n"
           "## 6.14 Operator storefront editor\n"
           "## 9.1 Reports\n")
    out = spec_ledger.parse_spec(txt)
    assert [e["id"] for e in out] == ["6.14", "9.1"]
    assert out[0] == {"id": "6.14", "title": "Operator storefront editor"}


def test_parse_spec_inline_skips_fences_and_nonname_segments():
    txt = ("```\n"
           "Retry (2.5) backoff example\n"
           "```\n"
           "See also: config reference (1.5) for details.\n")
    # fenced code never yields sections; a segment with no clean Title-case name
    # ("config reference" is lowercase prose, not a section definition) is dropped.
    assert spec_ledger.parse_spec(txt) == []


# ── init / load / save round-trip + idempotent merge ─────────────────────────────────────

def test_init_load_save_roundtrip(tmp_path):
    root = str(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("## 6.14 Storefront editor\n## 7.2 Checkout\n")
    assert spec_ledger.init_ledger(root, str(spec)) == 2
    entries = spec_ledger.load_ledger(root)
    assert [e["id"] for e in entries] == ["6.14", "7.2"]
    assert [e["title"] for e in entries] == ["Storefront editor", "Checkout"]
    assert all(e["done"] is False and e["note"] == "" for e in entries)
    assert spec_ledger.ledger_path(root).exists()


def test_save_load_preserves_done_and_note(tmp_path):
    root = str(tmp_path)
    spec_ledger.save_ledger(root, [
        {"id": "6.14", "title": "Editor", "done": True, "note": "covered 2026-07-09"},
        {"id": "7.2", "title": "Checkout", "done": False, "note": ""},
    ])
    got = spec_ledger.load_ledger(root)
    assert got == [
        {"id": "6.14", "title": "Editor", "done": True, "note": "covered 2026-07-09"},
        {"id": "7.2", "title": "Checkout", "done": False, "note": ""},
    ]


def test_reinit_preserves_done_state_and_appends_new(tmp_path):
    root = str(tmp_path)
    spec1 = tmp_path / "s1.md"
    spec1.write_text("## 6.14 Editor\n## 7.2 Checkout\n")
    spec_ledger.init_ledger(root, str(spec1))
    # cover 6.14
    spec_ledger.update_ledger(root, "shipped the editor",
                              lambda p: '{"covered_ids": ["6.14"]}', today="2026-07-09")
    # re-init from a SUPERSET spec
    spec2 = tmp_path / "s2.md"
    spec2.write_text("## 6.14 Editor\n## 7.2 Checkout\n## 8.1 Reports\n")
    assert spec_ledger.init_ledger(root, str(spec2)) == 3
    entries = {e["id"]: e for e in spec_ledger.load_ledger(root)}
    assert set(entries) == {"6.14", "7.2", "8.1"}
    assert entries["6.14"]["done"] is True                    # preserved across re-init
    assert entries["6.14"]["note"] == "covered 2026-07-09"    # note preserved
    assert entries["8.1"]["done"] is False                    # new section, unchecked


def test_init_no_numbers_returns_zero_and_untouched(tmp_path):
    root = str(tmp_path)
    spec = tmp_path / "prose.md"
    spec.write_text("# Overview\nJust prose, no numbered sections.\n")
    assert spec_ledger.init_ledger(root, str(spec)) == 0
    assert not spec_ledger.ledger_path(root).exists()


def test_init_unreadable_spec_returns_zero(tmp_path):
    assert spec_ledger.init_ledger(str(tmp_path), str(tmp_path / "does-not-exist.md")) == 0
    assert not spec_ledger.ledger_path(str(tmp_path)).exists()


def test_init_bad_spec_leaves_existing_ledger_untouched(tmp_path):
    root = str(tmp_path)
    spec_ledger.save_ledger(root, [
        {"id": "6.14", "title": "Editor", "done": True, "note": "covered x"}])
    before = spec_ledger.ledger_path(root).read_text(encoding="utf-8")
    assert spec_ledger.init_ledger(root, str(tmp_path / "missing.md")) == 0
    assert spec_ledger.ledger_path(root).read_text(encoding="utf-8") == before


# ── uncovered_block — prompt block for the PM ────────────────────────────────────────────

def test_uncovered_block_content_excludes_covered(tmp_path):
    root = str(tmp_path)
    spec_ledger.save_ledger(root, [
        {"id": "6.14", "title": "Editor", "done": False, "note": ""},
        {"id": "7.2", "title": "Checkout", "done": True, "note": "covered x"},
    ])
    block = spec_ledger.uncovered_block(root)
    assert "STANDING PRODUCT SPEC" in block
    assert "6.14 Editor" in block
    assert "Checkout" not in block          # covered sections are not listed


def test_uncovered_block_empty_when_no_ledger_or_all_done(tmp_path):
    root = str(tmp_path)
    assert spec_ledger.uncovered_block(root) == ""            # no ledger file
    spec_ledger.save_ledger(root, [
        {"id": "6.14", "title": "Editor", "done": True, "note": "covered x"}])
    assert spec_ledger.uncovered_block(root) == ""            # nothing uncovered


def test_uncovered_block_caps_with_more_tail(tmp_path):
    root = str(tmp_path)
    spec_ledger.save_ledger(root, [
        {"id": f"{i}.0", "title": f"Section number {i} with a fairly long title",
         "done": False, "note": ""} for i in range(1, 40)])
    block = spec_ledger.uncovered_block(root, cap=200)
    assert len(block) <= 200
    assert "(+" in block and "more)" in block
    assert "STANDING PRODUCT SPEC" in block


# ── update_ledger — end-of-run coverage marking (never raises) ────────────────────────────

def test_update_ledger_marks_covered_ids(tmp_path):
    root = str(tmp_path)
    spec_ledger.save_ledger(root, [
        {"id": "6.14", "title": "Storefront editor", "done": False, "note": ""},
        {"id": "7.2", "title": "Checkout", "done": False, "note": ""},
    ])
    marked = spec_ledger.update_ledger(
        root, "shipped an operator storefront editor",
        lambda p: 'Here you go: {"covered_ids": ["6.14"]} done',   # tolerant of prose around JSON
        today="2026-07-09")
    assert marked == ["6.14"]
    entries = {e["id"]: e for e in spec_ledger.load_ledger(root)}
    assert entries["6.14"]["done"] is True
    assert entries["6.14"]["note"] == "covered 2026-07-09"
    assert entries["7.2"]["done"] is False                   # untouched


def test_update_ledger_ignores_hallucinated_ids(tmp_path):
    root = str(tmp_path)
    spec_ledger.save_ledger(root, [
        {"id": "6.14", "title": "Editor", "done": False, "note": ""}])
    marked = spec_ledger.update_ledger(
        root, "x", lambda p: '{"covered_ids": ["99.9", "6.14"]}', today="2026-07-09")
    assert marked == ["6.14"]                                # 99.9 not in ledger → dropped


def test_update_ledger_malformed_json_no_change(tmp_path):
    root = str(tmp_path)
    spec_ledger.save_ledger(root, [
        {"id": "6.14", "title": "Editor", "done": False, "note": ""}])
    before = spec_ledger.load_ledger(root)
    assert spec_ledger.update_ledger(root, "x", lambda p: "sorry, no JSON here") == []
    assert spec_ledger.load_ledger(root) == before           # ledger unchanged


def test_update_ledger_never_raises_when_llm_raises(tmp_path):
    root = str(tmp_path)
    spec_ledger.save_ledger(root, [
        {"id": "6.14", "title": "Editor", "done": False, "note": ""}])

    def boom(_prompt):
        raise RuntimeError("llm backend down")

    assert spec_ledger.update_ledger(root, "x", boom) == []   # never raises
    assert spec_ledger.load_ledger(root)[0]["done"] is False  # unchanged


def test_update_ledger_empty_when_nothing_uncovered(tmp_path):
    root = str(tmp_path)
    spec_ledger.save_ledger(root, [
        {"id": "6.14", "title": "Editor", "done": True, "note": "covered x"}])
    # never even calls the llm — no undone sections
    called = []
    assert spec_ledger.update_ledger(root, "x", lambda p: called.append(1) or "{}") == []
    assert called == []


# ── PM prompt injection ──────────────────────────────────────────────────────────────────

def test_pm_injects_standing_spec_block_when_uncovered(llm, ws, tmp_path):
    from agents import pm
    repo = str(tmp_path / "repo")
    spec_ledger.save_ledger(repo, [
        {"id": "6.14", "title": "Operator storefront editor", "done": False, "note": ""},
        {"id": "7.2", "title": "Checkout", "done": True, "note": "covered 2026-07-01"},
    ])
    brief = seed(ws, "proj", "prd", "ceo_brief.md", "BRIEF: build the profile page")
    llm.default = "## Feature\nx\n## Acceptance Criteria\n1. ..."
    pm.run(base_state(prd_path=brief, target_repo=repo))
    txt = llm.user_texts()
    assert "STANDING PRODUCT SPEC" in txt
    assert "6.14 Operator storefront editor" in txt
    assert "Checkout" not in txt                              # covered section not injected


def test_pm_no_spec_block_without_ledger(llm, ws, tmp_path):
    from agents import pm
    brief = seed(ws, "proj", "prd", "ceo_brief.md", "BRIEF: build the profile page")
    llm.default = "## Feature\nx"
    # target_repo set but no ledger file, then no target_repo at all — both → no block
    pm.run(base_state(prd_path=brief, target_repo=str(tmp_path / "empty")))
    assert "STANDING PRODUCT SPEC" not in llm.user_texts()
    llm.calls.clear()
    pm.run(base_state(prd_path=brief))                        # target_repo defaults to None
    assert "STANDING PRODUCT SPEC" not in llm.user_texts()


# ── Runner no-op: absent --spec → no ledger file ever created ────────────────────────────

def test_absent_spec_creates_no_ledger(tmp_path):
    # If init_ledger is never called (no --spec), the ledger file must not exist and
    # every read is a benign empty — byte-identical prior behavior.
    root = str(tmp_path)
    assert spec_ledger.load_ledger(root) == []
    assert spec_ledger.uncovered_block(root) == ""
    assert not spec_ledger.ledger_path(root).exists()
