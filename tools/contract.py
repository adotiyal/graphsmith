"""
tools/contract.py — the Feature Contract spine (zero-drift coordination).

THE PROBLEM (measured live): a PRD's acceptance criteria are the only canonical
statement of "what the feature is", but they were written ONCE in prose and every
downstream agent re-derived its understanding — so design/tests/e2e referenced ZERO
acceptance criteria by id. Nobody could prove "is AC7 implemented and tested?".

THE FIX — decide-once, reference-everywhere, verify-at-the-boundary:
- PM assigns each acceptance criterion a STABLE id (AC-1, AC-2, …) + a surface tag
  (ui | backend). This is the shared spine every agent references.
- test_author tags each test with the AC ids it covers; QA tags each e2e the same.
- A deterministic COVERAGE check proves every AC has a test, and every UI AC has an
  e2e — 100% coverage by construction, not by hope. No LLM; never raises.
"""

import re

# A criterion is UI iff it asserts something the USER SEES/DOES — keyed on display/
# interaction VERBS (and page/screen surfaces), NOT on nouns like "badge"/"card"
# that appear just as often in backend logic ("the badge is CALCULATED from count").
# The surface decides whether an e2e is REQUIRED, so over-tagging nouns inflates the
# e2e requirement falsely; PM tags explicitly and this only fires as a fallback.
_UI_HINTS = ("display", "displays", "shown", "shows", "show ", "visible", "appears",
             "page", "screen", "click", "clicks", "navigate", "render", "renders",
             "landing", "dialog", "see ", "sees", "view ", "views", "select",
             "redirect", "highlighted", "shown as", "as their avatar")


def parse_acs(prd_text: str) -> list:
    """Extract the acceptance criteria as [{id, text, surface}]. Tolerant: recognizes
    explicit `AC-1`/`AC1` ids and an optional `(ui)`/`(backend)` tag; falls back to
    numbering bullet/numbered items under the Acceptance Criteria heading by ordinal,
    inferring surface from keywords."""
    if not prd_text:
        return []
    m = re.search(r"##\s*Acceptance Criteria\s*\n(.*?)(?:\n##\s|\Z)", prd_text,
                  re.DOTALL | re.I)
    section = m.group(1) if m else prd_text
    acs, ordinal = [], 0
    for raw in section.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # a list item: "- ...", "* ...", "1. ...", or "AC-1: ..."
        item = re.match(r"^(?:[-*]\s+|\d+[.)]\s+)?(.*)$", line)
        body = item.group(1).strip() if item else line
        idm = re.match(r"(?:\*\*)?AC[-\s]?(\d+)(?:\*\*)?\s*", body)
        if idm:
            num = int(idm.group(1))
            body = body[idm.end():].lstrip(":：-—  ").strip()
        elif re.match(r"^(?:[-*]\s+|\d+[.)]\s+)", line):
            ordinal += 1
            num = ordinal
        else:
            continue   # prose line, not a criterion
        tag = re.match(r"\((ui|backend|api|frontend)\)\s*[:\-—]?\s*", body, re.I)
        if tag:
            surface = "ui" if tag.group(1).lower() in ("ui", "frontend") else "backend"
            body = body[tag.end():].strip()
        else:
            surface = "ui" if any(h in body.lower() for h in _UI_HINTS) else "backend"
        body = re.sub(r"\s*\((ui|backend|api|frontend)\)\s*$", "", body, flags=re.I).strip()
        if body:
            acs.append({"id": f"AC-{num}", "text": body, "surface": surface})
    # de-dup by id (keep first)
    seen, out = set(), []
    for a in acs:
        if a["id"] not in seen:
            seen.add(a["id"]); out.append(a)
    return out


def extract_ac_refs(text: str) -> set:
    """All AC ids a test/spec/artifact references (AC-1, AC1, AC 1 → AC-1)."""
    return {f"AC-{n}" for n in re.findall(r"\bAC[-\s]?(\d+)\b", text or "", re.I)}


def coverage(acs: list, unit_texts, e2e_texts) -> dict:
    """Map every AC to its coverage. Returns
    {map: {ac_id: {surface, text, unit, e2e}}, uncovered: [...], ui_without_e2e: [...]}.
    Rule: every AC needs >=1 test reference; a UI AC must be referenced by >=1 e2e."""
    unit_refs, e2e_refs = set(), set()
    for t in unit_texts or []:
        unit_refs |= extract_ac_refs(t)
    for t in e2e_texts or []:
        e2e_refs |= extract_ac_refs(t)
    cov, uncovered, ui_without_e2e = {}, [], []
    for a in acs:
        u, e = a["id"] in unit_refs, a["id"] in e2e_refs
        cov[a["id"]] = {"surface": a["surface"], "text": a["text"], "unit": u, "e2e": e}
        if not (u or e):
            uncovered.append(a["id"])
        elif a["surface"] == "ui" and not e:
            ui_without_e2e.append(a["id"])
    return {"map": cov, "uncovered": uncovered, "ui_without_e2e": ui_without_e2e,
            "total": len(acs)}


def coverage_report(cov: dict) -> tuple:
    """(ok, human_message) from a coverage() result. ok iff nothing uncovered and no
    UI AC lacks an e2e."""
    ok = not cov["uncovered"] and not cov["ui_without_e2e"]
    if not cov["total"]:
        return True, "no acceptance criteria parsed — coverage check skipped"
    lines = []
    for ac_id, c in cov["map"].items():
        marks = ("U" if c["unit"] else "·") + ("E" if c["e2e"] else "·")
        flag = "" if (c["unit"] or c["e2e"]) and not (c["surface"] == "ui" and not c["e2e"]) else "  ← GAP"
        lines.append(f"  [{marks}] {ac_id} ({c['surface']}): {c['text'][:70]}{flag}")
    head = (f"AC coverage {cov['total'] - len(cov['uncovered'])}/{cov['total']} tested"
            + (f"; UNCOVERED: {', '.join(cov['uncovered'])}" if cov["uncovered"] else "")
            + (f"; UI ACs missing e2e: {', '.join(cov['ui_without_e2e'])}"
               if cov["ui_without_e2e"] else ""))
    return ok, head + "\n" + "\n".join(lines)
