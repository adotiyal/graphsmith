"""
tools/spec_ledger.py  (Design-fidelity hardening, Work item B — spec-coverage ledger)
------------------------------------------------------------------------------------
Graphsmith converges on BEHAVIOR each run (100% AC coverage on the per-run PRD) but has
NO memory of the CUMULATIVE product spec across runs. On a real multi-run build a whole
spec section (an operator storefront editor, §6.14) silently never got implemented across
7 runs — nothing tracked it because each run only ever saw its own PRD.

This module is that missing memory: a persistent, per-product ledger of the standing spec's
numbered sections and whether each has shipped. It follows tools/product.py's persistence
style (a product/ dir, capped reads that WARN rather than silently head-slice) and
tools/learnings.run_retro's end-of-run pattern (ONE fast-tier call that NEVER raises).

Stored at <root>/product/spec_ledger.md, one line per section:
    - [ ] 6.14 Operator storefront editor
    - [x] 7.2 Checkout — covered 2026-07-09

Wiring is byte-identical when no spec is provided: --spec seeds it (init_ledger), PM sees the
uncovered sections (uncovered_block), and the end-of-run retro marks newly-covered ones
(update_ledger). Absent --spec, none of that runs and no ledger file is created.
"""

import json
import re
from datetime import date
from pathlib import Path

MAX_SPEC_LEDGER_CHARS = 8000

_LEDGER_HEADER = (
    "# Standing Product Spec — coverage ledger\n\n"
    "Numbered sections of the cumulative product spec and whether each has shipped.\n"
    "`[x]` = implemented; `[ ]` = not yet. Managed across runs — do not hand-edit ids.\n\n"
)

_UNCOVERED_HEADER = (
    "STANDING PRODUCT SPEC — sections NOT yet implemented (do not re-implement covered "
    "ones; flag if this feature should cover any):"
)

# Section-number extraction. Precision-biased: only NUMBERED headings / bold lines /
# screen-section lines count — a bare "6.14 percent of users" prose line never matches.
# `## 6.14 Title`, `### 7.2 Title`, `# 12. Title`  (number right after the # run)
_HEADING_NUM = re.compile(r"^\s{0,3}#{1,6}\s+(\d+(?:\.\d+)*)\.?\s+(\S.*?)\s*$")
# `### Screen 12 — Title`, `## Section 3: Dashboard`  (a Screen/Section keyword then a number)
_SCREEN_NUM = re.compile(
    r"^\s{0,3}#{1,6}\s+(?:Screen|Section)\s+(\d+(?:\.\d+)*)\s*[—–\-:.]*\s*(\S.*?)\s*$",
    re.IGNORECASE,
)
# `**6.14** Title`, `**7.2** — Title`
_BOLD_NUM = re.compile(r"^\s{0,3}\*\*(\d+(?:\.\d+)*)\.?\*\*\s*[—–\-:.]*\s*(\S.*?)\s*$")

# Inline parenthetical section ids (review fix): real build specs define screens ONLY as
# inline refs — a flow line (`7. **Storefront** → public `/o/{slug}` (6.14) built from …`)
# or a table row (`| Operator storefront (6.14) | hero, VerificationBadge | GET /o/{slug} |`)
# — never as headings, so §6.14 (the exact section whose silent loss motivated this ledger)
# was missed. Precision rules: the number MUST be dotted (`(12)`, years, counts never
# match); `(§6.2)` is a cross-REFERENCE, not a definition (the digit-right-after-paren
# requirement plus a trailing-§ guard exclude it); fenced code blocks are skipped; and a
# match is DROPPED unless a clean Title-case NAME precedes it — the adjacent name-run
# (bounded by the `|` table-cell separator), else the nearest preceding **bold** span
# (flow lines bold the name). A heading-form definition wins the title on id collision.
_INLINE_NUM = re.compile(r"\((\d+\.\d+(?:\.\d+)*)\)")
_TAIL_NAME = re.compile(r"([A-Za-z][A-Za-z0-9 /&'\-]{1,60})\s*$")
_BOLD_SPAN = re.compile(r"\*\*([^*]+)\*\*")
_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z0-9 /&'\-]{2,60}$")

# A stored ledger line: `- [ ] <id> <title> — <note>` (note optional).
_LEDGER_LINE = re.compile(r"^- \[([ xX])\] (.*)$")


def _cap(text: str, cap: int, label: str) -> str:
    """Cap to `cap` chars but LOUDLY — a silent head-slice that drops the tail of
    authoritative content is the exact failure class this codebase keeps hitting (see
    product._cap). Mirrors that behavior: keep the head, but log the cap-hit."""
    if len(text) > cap:
        print(f"[spec_ledger] WARNING: {label} is {len(text)} chars > cap {cap} — truncated; "
              f"the TAIL is being dropped. Trim the spec or raise the cap.")
        return text[:cap]
    return text


def _clean_name(cand: str) -> str:
    """Normalize an inline-name candidate: collapse whitespace, drop leading non-Title-case
    words (connectors/route junk like "or …", "tap …", "public …" that ride between the real
    name and the parenthesized id), then require a plausible section NAME — Title-case start,
    3–61 name characters (the same class the heading titles use) — else ""."""
    words = " ".join((cand or "").split()).split(" ")
    while words and not words[0][:1].isupper():
        words.pop(0)
    cand = " ".join(words)
    return cand if _NAME_OK.match(cand) else ""


def _inline_name(segment: str) -> str:
    """The section NAME for an inline `<name> (6.14)` id, or "" to DROP the match.
    Tier 1: the name-run immediately before the parens (table cells:
    `| Operator storefront (6.14) |`). Tier 2: the nearest preceding **bold** span (flow
    lines bold the name: `7. **Storefront** → public `/o/{slug}` (6.14) …`). Precision over
    recall — no clean Title-case name means no ledger entry."""
    m = _TAIL_NAME.search(segment)
    if m:
        name = _clean_name(m.group(1))
        if name:
            return name
    for cand in reversed(_BOLD_SPAN.findall(segment)):
        name = _clean_name(cand)
        if name:
            return name
    return ""


def parse_spec(text: str) -> list:
    """Deterministically extract numbered sections from a markdown spec, IN ORDER OF
    APPEARANCE. Recognizes numbered headings (`## 6.14 Title`, `# 12. Title`), bold-number
    lines (`**6.14** Title`), Screen/Section lines (`### Screen 12 — Title`), and inline
    parenthetical ids after a name (`| Operator storefront (6.14) |`,
    `7. **Storefront** → … (6.14)`) — DOTTED numbers only, outside code fences, `(§…)`
    cross-references never match. Returns [{"id": "6.14", "title": "Title"}] — id = the
    number token as written. Deduped by id: first occurrence wins, except a heading-form
    DEFINITION upgrades the title of an earlier inline reference (the heading defines the
    section; the inline mention merely names it). No numbers found → []."""
    out, by_id = [], {}
    in_fence = False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = _SCREEN_NUM.match(line) or _HEADING_NUM.match(line) or _BOLD_NUM.match(line)
        if m:
            sid, title = m.group(1), m.group(2).strip()
            prev = by_id.get(sid)
            if prev is None:
                by_id[sid] = entry = {"id": sid, "title": title}
                out.append(entry)
            elif prev.pop("_inline", None):
                prev["title"] = title   # heading DEFINES the section — beats an inline ref
            continue
        if in_fence:
            continue
        for im in _INLINE_NUM.finditer(line):
            sid = im.group(1)
            if sid in by_id:
                continue                # first occurrence wins (incl. vs a prior heading)
            segment = line[line.rfind("|", 0, im.start()) + 1:im.start()]
            if segment.rstrip().endswith("§"):
                continue                # `§(6.2)`-style cross-reference, not a definition
            title = _inline_name(segment)
            if title:
                by_id[sid] = entry = {"id": sid, "title": title, "_inline": True}
                out.append(entry)
    for e in out:
        e.pop("_inline", None)
    return out


def ledger_path(root) -> Path:
    return Path(root) / "product" / "spec_ledger.md"


def _parse_ledger_line(line: str) -> "dict | None":
    m = _LEDGER_LINE.match(line)
    if not m:
        return None
    done = m.group(1).lower() == "x"
    rest = m.group(2).strip()
    parts = rest.split(None, 1)
    if not parts:
        return None
    sid = parts[0]
    remainder = parts[1] if len(parts) > 1 else ""
    # The note is appended as " — <note>"; split from the RIGHT so a title that itself
    # contains " — " keeps its dash and only the trailing note is peeled off.
    if " — " in remainder:
        title, note = remainder.rsplit(" — ", 1)
        title, note = title.strip(), note.strip()
    else:
        title, note = remainder, ""
    return {"id": sid, "title": title, "done": done, "note": note}


def load_ledger(root) -> list:
    """The persisted ledger as [{"id","title","done","note"}]; missing file → []."""
    path = ledger_path(root)
    if not path.exists():
        return []
    text = _cap(path.read_text(encoding="utf-8", errors="replace"),
                MAX_SPEC_LEDGER_CHARS, "spec_ledger.md")
    entries = []
    for line in text.splitlines():
        e = _parse_ledger_line(line)
        if e:
            entries.append(e)
    return entries


def save_ledger(root, entries: list) -> str:
    """Write entries to <root>/product/spec_ledger.md in the canonical line format
    (note only when non-empty). Same style as product.py persistence."""
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for e in entries or []:
        box = "x" if e.get("done") else " "
        sid = str(e.get("id", "")).strip()
        title = str(e.get("title", "") or "").strip()
        note = str(e.get("note", "") or "").strip()
        line = f"- [{box}] {sid} {title}".rstrip()
        if note:
            line += f" — {note}"
        lines.append(line)
    text = _LEDGER_HEADER + "\n".join(lines) + ("\n" if lines else "")
    path.write_text(text, encoding="utf-8")
    return str(path)


def init_ledger(root, spec_path) -> int:
    """Seed/refresh the ledger from a product spec file. Parses the spec, MERGES with any
    existing ledger (preserving done/note state for ids already tracked, appending new ids
    unchecked, and keeping any prior ids the new spec omits), saves, and returns the number
    of sections parsed from the spec. Unreadable/empty/no-numbers → 0, existing ledger left
    untouched (idempotent, safe to call every run)."""
    try:
        text = Path(spec_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return 0
    sections = parse_spec(text)
    if not sections:
        return 0
    existing = {e["id"]: e for e in load_ledger(root)}
    merged, seen = [], set()
    for s in sections:
        prev = existing.get(s["id"])
        if prev:
            merged.append({"id": s["id"], "title": s["title"] or prev.get("title", ""),
                           "done": prev.get("done", False), "note": prev.get("note", "")})
        else:
            merged.append({"id": s["id"], "title": s["title"], "done": False, "note": ""})
        seen.add(s["id"])
    # Preserve prior tracking for ids the new spec doesn't mention (a partial/renamed spec
    # must not silently drop coverage state we already have).
    for eid, e in existing.items():
        if eid not in seen:
            merged.append(e)
    save_ledger(root, merged)
    return len(sections)


def uncovered_block(root, cap: int = 2000) -> str:
    """A prompt block naming the still-unimplemented spec sections, for the PM. "" when
    there is no ledger or nothing is uncovered. Capped at `cap` chars with a "(+N more)"
    tail so a large backlog can't blow the PM prompt."""
    entries = load_ledger(root)
    undone = [e for e in entries if not e.get("done")] if entries else []
    if not undone:
        return ""
    result = _UNCOVERED_HEADER
    shown = 0
    for e in undone:
        line = f"- {e.get('id', '')} {e.get('title', '')}".rstrip()
        remaining_after = len(undone) - shown - 1
        tail = f"\n(+{remaining_after} more)" if remaining_after > 0 else ""
        # Would this line (plus the tail that would then follow) blow the cap? Always keep
        # at least one line (shown == 0) so the block is never just a header.
        if shown > 0 and len(result + "\n" + line + tail) > cap:
            break
        result += "\n" + line
        shown += 1
    remaining = len(undone) - shown
    if remaining > 0:
        result += f"\n(+{remaining} more)"
    return result


def _first_json_span(text: str) -> "str | None":
    """The first balanced {...} span in `text`, quote/escape aware (mirrors the robust
    brace-scan used by tools/llm._extract_json). None if none found."""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_covered_ids(raw: str) -> list:
    """Defensively pull covered_ids out of an LLM reply. Reuses the brace-scan style rather
    than depending on tools.llm.call_structured (keeps this module dependency-light). Any
    failure (no JSON span, invalid JSON, wrong shape) → []."""
    try:
        span = _first_json_span(raw)
        if span is None:
            return []
        data = json.loads(span)
        ids = data.get("covered_ids", []) if isinstance(data, dict) else []
        return [str(i).strip() for i in ids if str(i).strip()]
    except Exception:
        return []


def update_ledger(root, feature_summary: str, llm_call, today: str = None) -> list:
    """End-of-run coverage marking (mirror learnings.run_retro: ONE call, NEVER raises).
    Given the still-uncovered ledger lines + the shipped feature's PRD/brief summary
    (caller-capped), ask `llm_call(prompt) -> str` (expected to reply with a JSON object
    {"covered_ids": [...]} — parsed defensively here) which sections this feature now
    implements, flip those boxes to `[x]` with note "covered <YYYY-MM-DD>", save, and return
    the marked ids. Any exception → [] (a finished run is never broken by this)."""
    try:
        entries = load_ledger(root)
        undone = [e for e in entries if not e.get("done")] if entries else []
        if not undone:
            return []
        ledger_lines = "\n".join(f"- {e['id']} {e['title']}" for e in undone)
        prompt = (
            "You maintain a STANDING PRODUCT SPEC coverage ledger. Below are the spec "
            "sections NOT yet implemented, then a summary of the feature that just shipped. "
            "Return ONLY the ids of sections that THIS shipped feature now implements (fully "
            "or substantially). Do not guess — include an id only if the summary clearly "
            "delivers that section.\n\n"
            "UNIMPLEMENTED SECTIONS:\n" + ledger_lines +
            "\n\nSHIPPED FEATURE:\n" + (feature_summary or "").strip() +
            '\n\nRespond with ONLY a JSON object: {"covered_ids": ["<id>", ...]} '
            "(empty list if none).")
        covered = _parse_covered_ids(llm_call(prompt))
        valid = {e["id"] for e in undone}
        marked = [cid for cid in covered if cid in valid]
        if not marked:
            return []
        date_str = today or date.today().isoformat()
        marked_set = set(marked)
        for e in entries:
            if e["id"] in marked_set and not e.get("done"):
                e["done"] = True
                e["note"] = f"covered {date_str}"
        save_ledger(root, entries)
        return marked
    except Exception:
        return []
