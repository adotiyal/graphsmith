"""
Design Agent (consumer-app product design)
-------------------------------------------
Designs from DISCOVERY, not a feature list. It reads the standing PRODUCT PROFILE
(category, users, use cases, brand/tone, goals — set once by the CEO/CTO) plus the PRD,
and for material gaps it asks the CEO/CTO (folded clarification) rather than guessing.

Output is an enriched spec: design context + rationale, flows (incl. unhappy paths +
first-run), screens/components, and the actual microcopy. Reviewed by a design critic
(critic_design) before architecture.

Runs on the `strong` tier — consumer design is high-leverage reasoning, not boilerplate.
Q&A peers: CEO/CTO (product/brand/scope), PM (scope).
"""

import re

from graph.state import ProjectState
from tools.llm import call_llm
from tools.file_io import load_prompt, load_skill, read_artifact, write_artifact, code_root, strip_md_fences
from tools.registry import validate_components
import os

from tools import codegen, product, report_html
from tools import repo as repo_tools
from tools.file_io import WORKSPACE_ROOT
from tools.qa_utils import run_with_qa, work_call, format_qa_context

CONSULT = ["ceo", "pm"]


def run(state: ProjectState) -> dict:
    # Regenerating after a design-critic gap: apply the notes, skip a fresh Q&A round.
    if state.get("review_notes"):
        return _do_work(
            state,
            list(state.get("qa_log") or []),
            dict(state.get("qa_rounds") or {}),
            allow_clarify=False,
        )
    return run_with_qa(state, "design", _do_work, consultable_agents=CONSULT)


_STACK_Q = ("DESIGN STACK CONFIRMATION (CTO call): to design with real components I need "
            "the frontend stack now. Default: Next.js (React + TypeScript + Tailwind) "
            "frontend + FastAPI (Python) backend + Postgres, dockerized. Reply 'default' "
            "to confirm, or name a different stack.")

_DEFAULT_STACK = ("Dockerized full stack — FastAPI (backend, Python) + Next.js (frontend, "
                  "TypeScript, React, Tailwind) + Postgres, docker-compose, pinned slim "
                  "images. Tests: pytest (backend), vitest (frontend), Playwright (e2e).")


def _do_work(state: dict, qa_log: list, rounds: dict, allow_clarify: bool = True) -> dict:
    identity = load_prompt("design")
    skill = load_skill("design")
    from tools.learnings import augment_system
    system = augment_system(f"{identity}\n\n{skill}" if skill else identity, "design")

    # Stack-at-design-time: emitting REAL components needs the frontend stack settled
    # NOW, not at the architect. If no stack is persisted/detected yet (first greenfield
    # run), ask the CTO once — 'default' confirms Next.js/FastAPI/Postgres. The answer is
    # PERSISTED, so the architect's later mandatory confirmation auto-skips (no re-ask).
    if not product.load_stack() and not state.get("detected_stack"):
        answer = _stack_answer(qa_log)
        if answer is None and allow_clarify:
            return {"_clarify": {"ceo": _STACK_Q}}
        if answer is not None:
            text = answer.strip()
            product.save_stack(_DEFAULT_STACK if re.search(
                r"\b(default|confirm|yes|ok)\b", text, re.I) or not text else text)

    prd = read_artifact(state["prd_path"])
    profile = state.get("product_profile") or "(no product profile on file — ask the CEO/CTO if you need product/user/brand context)"
    qa_ctx = format_qa_context(qa_log, "design")

    # —— RESUME PATH (direction-choice round-trip): the spec + the 3 direction mockups
    # already exist on disk for THIS run and we're re-entered after the CEO's choice
    # (or to ask for it). Never regenerate them — finalize the chosen direction.
    run_design = WORKSPACE_ROOT / state["project_id"] / "design"
    if (not state.get("review_notes")
            and (run_design / "design_spec.md").exists()
            and all((run_design / f"mockup_{x}.html").exists() for x in "ABC")):
        spec = (run_design / "design_spec.md").read_text(encoding="utf-8")
        directions = _parse_directions(spec)
        choice, notes = _direction_choice(qa_log)
        if choice is None and allow_clarify:
            review = report_html.render_design_options(
                state["project_id"],
                [{**d, "mockup_file": f"mockup_{d['id']}.html"} for d in directions])
            return {"_clarify": {"ceo": _direction_question(directions, review)}}
        return _finalize(state, system, spec, str(run_design / "design_spec.md"),
                         profile, directions, choice or "A", notes, qa_log, rounds)

    ledger = state.get("project_ledger")
    ledger_block = f"\n\nPROJECT HISTORY (features already designed — keep the UX consistent with what exists):\n{ledger}" if ledger else ""

    # Design memory: the persisted design system + the kit components that already exist.
    # Successive features must FEEL like the same product — same type scale, spacing,
    # components, voice — so the user never senses a disconnect between features.
    ds = product.load_design_system()
    ds_block = (f"\n\nESTABLISHED DESIGN SYSTEM (the product's visual/UX law — follow it; "
                f"extend it for new needs, never contradict it):\n{ds}") if ds else ""
    kit_existing = _existing_kit(state)
    kit_block = (f"\n\nEXISTING KIT COMPONENTS (already built — REUSE these; only design "
                 f"new components for genuinely new needs):\n{kit_existing}") if kit_existing else ""

    feedback = state.get("review_notes")
    feedback_block = f"\n\nDESIGN CRITIC FOUND GAPS (fix every one):\n{feedback}" if feedback else ""

    user_msg = f"""
PRODUCT PROFILE (standing context — who/what/brand/goals):
{profile}

PRD (the feature):
{prd}
{ledger_block}{ds_block}{kit_block}

{qa_ctx}{feedback_block}

Produce the UI/UX design spec with ONLY these sections. Do discovery first; if something
material about the user, brand, or goal is missing and not above, ask the CEO/CTO instead
of guessing.

## Design Context
Who it's for, the job-to-be-done, the success metric, and the brand/tone you're designing
to. 3–5 lines, grounded in the product profile + PRD.

## Design Directions
Propose EXACTLY three distinct, viable design directions — different visual/UX takes on
the SAME flows and content (layout density, navigation pattern, visual personality,
information hierarchy). The CEO/CTO (a real human) will pick ONE. Format each EXACTLY:
### A — <short name>
<3-5 lines: the concept, WHY it serves these users/this brand (the rationale), and its
key trade-off vs the other two.>
### B — <short name>
<same>
### C — <short name>
<same>
Write the rest of this spec anchored to direction A (your recommended choice). Where B
or C would differ meaningfully (different nav pattern, different component, different
flow), add a brief parenthetical: "(Direction B: use X instead)". This lets the CEO
understand the real cost of each choice before they pick.

## User Flows
Entry point → steps → success. Include the FIRST-RUN/empty experience and the unhappy
paths (error, empty, loading, permission/edge). Max ~12 steps total.

## Screens & Components
Per screen: purpose + the components (from your library) and the data fields they
show/collect. Design all 4 states (loading/success/error/empty) where relevant.

## Content & Microcopy
The actual words: primary button labels, empty-state copy, key error messages, first-run
text, and any confirmation copy. Voice matches the brand/tone.

## Accessibility, Responsive & Theming
Keyboard/contrast rules; the mobile (375px) vs desktop (1280px) layout differences per
screen; and the light/dark token pairs this feature uses (contrast AA in BOTH modes).

## SEO & Discoverability
Semantic structure for this feature: the page's title + meta description, the single H1,
landmark structure, and the JSON-LD schema.org type that fits (e.g. WebApplication,
Product, FAQPage). Content critical to search/AI answers must be server-rendered.

## Design System
The COMPLETE, CURRENT design system (carry forward the established one above, extended
with anything this feature adds — never contradict it): type scale + fonts, color tokens,
spacing scale, component inventory (name → purpose), UX patterns (empty/loading/error
conventions, optimistic UI, touch targets), and microcopy voice rules. This section is
PERSISTED and becomes the law for every future feature.

## Flagged Items
Ambiguities, missing data fields, or CUSTOM COMPONENT NEEDED. If none, write "None."

If this feature has NO user-facing surface, write:
"NO UI SURFACE - backend feature only." and stop.
"""

    questions, spec = work_call(system, user_msg, "strong", CONSULT, allow_clarify)
    if questions:
        return {"_clarify": questions}

    valid, tool_msg = validate_components(spec)
    if not valid:
        spec += f"\n\n---\n⚠️ COMPONENT VALIDATION WARNING:\n{tool_msg}"

    path = write_artifact(state["project_id"], "design", "design_spec.md", spec)

    # Persist the (extended) design system — the next feature designs from it.
    m = re.search(r"##\s*Design System\s*\n(.*?)(?:\n##\s|\Z)", spec, re.DOTALL)
    if m and m.group(1).strip():
        product.save_design_system(m.group(1).strip())

    # Backend-only feature: no mockups, no direction choice.
    if "NO UI SURFACE" in spec.upper():
        return {
            "current_node": "design",
            "design_path": path,
            "design_spec_path": path,
            "design_mockup_path": None,
            "design_component_files": [],
            "components_manifest_path": None,
            "review_notes": None,
            "review_action": None,
            "qa_log": qa_log,
            "qa_rounds": rounds,
            "ceo_qa_from": None,
        }

    # THREE DESIGN DIRECTIONS, HUMAN CHOICE (CEO mandate 2026-06-12): emit a mockup per
    # direction, render the side-by-side review page, and pause for the REAL human to
    # pick — design taste is a human call, not an agent's.
    directions = _parse_directions(spec)
    choice, notes = _direction_choice(qa_log)
    if choice is None:
        options = []
        for d in directions:
            html = _build_mockup(system, spec, profile, direction=d)
            write_artifact(state["project_id"], "design", f"mockup_{d['id']}.html", html)
            options.append({**d, "mockup_file": f"mockup_{d['id']}.html"})
        review = report_html.render_design_options(state["project_id"], options)
        if allow_clarify:
            return {"_clarify": {"ceo": _direction_question(directions, review)}}
        choice = "A"   # cannot pause (shouldn't happen on the first pass) — safe default

    return _finalize(state, system, spec, path, profile, directions, choice, notes,
                     qa_log, rounds)


def _parse_directions(spec: str) -> list:
    """The 3 directions from the spec's '## Design Directions' section. Lenient: if the
    format drifted, pad to exactly A/B/C so the choice gate never breaks."""
    section = ""
    m = re.search(r"##\s*Design Directions\s*\n(.*?)(?:\n##\s|\Z)", spec, re.DOTALL)
    if m:
        section = m.group(1)
    found = re.findall(r"###\s*([ABC])\s*[—:\-]\s*(.+?)\n(.*?)(?=\n###\s*[ABC]|\Z)",
                       section, re.DOTALL)
    out = [{"id": i, "title": t.strip(), "rationale": body.strip()[:600]}
           for i, t, body in found]
    have = {d["id"] for d in out}
    for x in "ABC":
        if x not in have:
            out.append({"id": x, "title": f"Direction {x}",
                        "rationale": section.strip()[:300] or "(see design spec)"})
    return sorted(out, key=lambda d: d["id"])[:3]


def _direction_question(directions: list, review_html: str | None) -> str:
    lines = [f"  {d['id']} — {d['title']}: {d['rationale'][:160]}" for d in directions]
    review = (f"OPEN THE REVIEW PAGE (all 3 side by side, with mockups):\n  {review_html}\n"
              if review_html else "")
    return ("DESIGN DIRECTION CHOICE (CEO/CTO — human pick): I prepared 3 design "
            "directions for this feature.\n" + review + "\n".join(lines)
            + "\nReply A, B, or C (optionally add tweaks, e.g. 'B, but tone down the header').")


def _direction_choice(qa_log: list):
    """(choice, tweak_notes) from the CEO's answer to the direction question; (None, None)
    when not asked/answered yet. An unparseable answer defaults to A — never blocked."""
    for entry in reversed(qa_log or []):
        if (entry.get("from") == "design" and entry.get("to") == "ceo"
                and "DESIGN DIRECTION CHOICE" in (entry.get("question") or "")):
            answer = entry.get("answer")
            if answer is None:
                return None, None
            m = re.search(r"\b([ABC])\b", answer.upper()) or re.search(r"\b([123])\b", answer)
            choice = ("ABC"["123".index(m.group(1))] if m and m.group(1) in "123"
                      else (m.group(1) if m else "A"))
            notes = answer.strip() if len(answer.strip()) > 2 else None
            return choice, notes
    return None, None


def _finalize(state: dict, system: str, spec: str, spec_path: str, profile: str,
              directions: list, choice: str, notes, qa_log: list, rounds: dict) -> dict:
    """The human chose a direction: promote its mockup to THE mockup, record the choice
    (+ tweak notes) in the spec, and build the kit from the WINNER only."""
    chosen = next((d for d in directions if d["id"] == choice), directions[0])
    run_design = WORKSPACE_ROOT / state["project_id"] / "design"
    chosen_file = run_design / f"mockup_{chosen['id']}.html"
    if chosen_file.exists():
        html = chosen_file.read_text(encoding="utf-8")
    else:   # critic-feedback re-run: spec changed, regenerate the chosen direction only
        html = _build_mockup(system, spec, profile, direction=chosen)
    mockup_path = write_artifact(state["project_id"], "design", "mockup.html", html)

    if "## Chosen Direction" not in spec:
        spec += (f"\n\n## Chosen Direction\n{chosen['id']} — {chosen['title']} "
                 f"(picked by the CEO/CTO)."
                 + (f"\nCEO tweak notes (apply them): {notes}" if notes else ""))
        write_artifact(state["project_id"], "design", "design_spec.md", spec)

    component_files: list = []
    manifest_path = None
    if _react_stack_known(state):
        component_files, manifest_path = _build_components(system, spec, html, state)

    return {
        "current_node": "design",
        "design_path": spec_path,
        # design_path gets OVERWRITTEN by the architect (tech spec) — keep a stable
        # pointer so the engineer can still read the design itself (visual fidelity).
        "design_spec_path": spec_path,
        "design_mockup_path": mockup_path,
        "design_component_files": component_files,
        "components_manifest_path": manifest_path,
        "design_options": [{k: d[k] for k in ("id", "title", "rationale")} for d in directions],
        "design_choice": chosen["id"],
        "review_notes": None,     # consumed
        "review_action": None,    # reset routing signal before entering the design critic
        "qa_log": qa_log,
        "qa_rounds": rounds,
        "ceo_qa_from": None,
    }


def _stack_answer(qa_log: list):
    """The CTO's reply to the design-time stack question, or None if not asked/answered."""
    for entry in reversed(qa_log or []):
        if (entry.get("from") == "design" and entry.get("to") == "ceo"
                and "DESIGN STACK CONFIRMATION" in (entry.get("question") or "")):
            return entry.get("answer") or ""
    return None


def _existing_kit(state: dict) -> str:
    """Names of kit components already in the repo — design reuses, never duplicates."""
    kit_dir = code_root(state) / "frontend" / "src" / "components" / "kit"
    if not kit_dir.is_dir():
        return ""
    names = sorted(p.name for p in kit_dir.glob("*.tsx"))
    return "\n".join(f"- {n}" for n in names)


def _react_stack_known(state: dict) -> bool:
    """Design can only emit real components when the frontend framework is already a
    settled fact — the persisted CTO stack (managed project, run 2+) or the detected
    stack (extend mode). Before that, the stack is confirmed AFTER design (architect),
    so the first greenfield run falls back to mockup-guided implementation."""
    known = f"{product.load_stack()} {state.get('detected_stack') or ''}".lower()
    return any(k in known for k in ("react", "next"))


def _use_tools() -> bool:
    return os.environ.get("AGENT_CODEGEN", "tools").strip().lower() != "text"


def _build_components(system: str, spec: str, mockup_html: str, state: dict) -> tuple:
    """Emit the design-owned presentational component kit into the code root
    (frontend/src/components/kit/) + a manifest the engineer wires against."""
    user_msg = f"""You already designed this feature (spec + mockup below). Now emit the REAL
presentational React components an engineer will wire to business logic — you own the
pixels and words; the engineer may NOT modify these files.

DESIGN SPEC (authoritative microcopy/layout/states):
{spec[:8000]}

YOUR MOCKUP (the visual truth — reproduce its structure and copy exactly):
{mockup_html[:8000]}

Rules:
- TypeScript + React function components + Tailwind classes. PRESENTATIONAL ONLY:
  all data and callbacks arrive via typed props (no fetch, no state beyond trivial
  local UI state like a controlled input's text).
- SELF-CONTAINED: import ONLY react (and other kit files). Do NOT import
  @/components/ui/*, shadcn, or any component library — style plain HTML elements
  with Tailwind directly. (A live kit imported unscaffolded shadcn paths and broke
  the build.) Inline any icon as a small SVG.
- Exact microcopy from the spec — every label, placeholder, empty-state line, error
  message, counter format. Stable kebab-case data-testid attributes on every
  interactive/assertable element.
- RESPONSIVE: mobile-first with sm:/md: breakpoints implementing the spec's
  mobile-vs-desktop differences — both surfaces are first-class.
- THEME-AWARE: every color utility carries its dark: counterpart from the spec's
  light/dark token pairs. Include a ThemeToggle kit component
  (data-testid="theme-toggle", props: {{theme, onToggle}}).
- Files live under frontend/src/components/kit/ — one component per file. On a
  feature that EXTENDS an existing kit, READ the existing components first and EDIT
  them in place (add props/fields) — never rewrite a shared file (e.g. types.ts,
  icons.tsx) from scratch and drop its existing exports.
- ALSO write the wiring manifest to frontend/src/components/kit/MANIFEST.md:
  # Design Component Kit — wiring manifest
  ## Components
  - <Name> (frontend/src/components/kit/<Name>.tsx): props {{...}} — what it renders
  ## REQUIRED MICROCOPY (must appear verbatim in the running app)
  - "<string 1>"
{_kit_emit_instruction()}
"""
    root = code_root(state)

    def _emit(extra_msg=""):
        if _use_tools():
            result = codegen.generate_in_domain(
                system, user_msg + extra_msg, str(root),
                allowed_prefixes=["frontend/src/components/kit/"], tier="strong")
            if result["violations"]:
                from tools.learnings import emit_feedback
                emit_feedback("design", "kit_domain_violation", "; ".join(result["violations"]))
        else:
            _write_kit_text(call_llm(system, user_msg + extra_msg, tier="strong"), state)

    _emit()
    _enforce_interface_additive(state, _emit)
    _enforce_testid_uniqueness(state, _emit)
    return _collect_kit(state)


def _enforce_testid_uniqueness(state: dict, reemit) -> None:
    """Responsive dual-layout hazard: a shared component rendered in BOTH the desktop and
    mobile layouts puts the SAME data-testid in the DOM twice → a Playwright strict-mode
    failure the engineer CANNOT fix (the kit is design-owned). Force ONE fix round to scope
    per-layout testids; advisory if it persists (never hard-blocks the pipeline)."""
    from tools import registry
    kit_dir = code_root(state) / "frontend" / "src" / "components" / "kit"
    findings = registry.check_kit_testid_uniqueness(kit_dir)
    if not findings:
        return
    from tools.learnings import emit_feedback
    emit_feedback("design", "kit_testid_dup", "; ".join(findings)[:900])
    reemit("\n\nDUPLICATE-TESTID FIX (Playwright strict mode WILL fail on these, and the "
           "engineer cannot fix the kit):\n- " + "\n- ".join(findings) + "\n\nGive every "
           "interactive element a data-testid that is UNIQUE in the rendered DOM. When a "
           "component renders in BOTH the desktop and mobile layouts, pass a per-layout "
           "suffix prop (e.g. scope=\"-card\" on the mobile instance) so the two testids "
           "differ, or render a single responsive layout. Keep the desktop bare testids.")


def _enforce_interface_additive(state: dict, reemit) -> None:
    """Interface Contract FREEZE: the kit's testids + required microcopy may only GROW
    across phases — never drop a prior-phase guarantee that existing e2e specs rely on
    (the phase-3 regression class: dropped card bio, renamed profile testids). On a
    drop, force ONE restore round; persist the additive union on success."""
    from tools import registry
    kit_dir = code_root(state) / "frontend" / "src" / "components" / "kit"
    manifest = (kit_dir / "MANIFEST.md").read_text(encoding="utf-8", errors="replace") \
        if (kit_dir / "MANIFEST.md").exists() else ""
    ids, prefixes, micro = registry.extract_kit_interface(kit_dir, manifest)
    prior = product.load_interface_contract()
    ok, msg, merged = registry.check_interface_additive(prior, ids, prefixes, micro)
    if not ok:
        from tools.learnings import emit_feedback
        emit_feedback("design", "interface_regression", msg)
        reemit("\n\n" + msg + "\n\nRestore every dropped item above (re-add the "
               "data-testid / microcopy to the relevant kit component) — DO NOT remove "
               "anything a prior phase shipped. Keep all your new work too.")
        manifest = (kit_dir / "MANIFEST.md").read_text(encoding="utf-8", errors="replace") \
            if (kit_dir / "MANIFEST.md").exists() else ""
        ids, prefixes, micro = registry.extract_kit_interface(kit_dir, manifest)
        ok, msg, merged = registry.check_interface_additive(prior, ids, prefixes, micro)
    product.save_interface_contract(merged)   # additive union becomes the new floor


def _kit_emit_instruction() -> str:
    if _use_tools():
        return ("\nWrite every kit file (.tsx components + the MANIFEST.md) DIRECTLY with "
                "your file tools under frontend/src/components/kit/. You may ONLY touch "
                "that directory. Emit raw code — no markdown fences.")
    return ("\nFor EACH component output:\n===FILE: frontend/src/components/kit/<Name>.tsx==="
            "\n<complete file>\n===END===\nThen the manifest as "
            "===FILE: frontend/src/components/kit/MANIFEST.md===\n<manifest>\n===END===")


def _write_kit_text(raw: str, state: dict) -> None:
    """Legacy text-path kit writer (AGENT_CODEGEN=text fallback)."""
    root = state.get("target_repo")
    for rel, content in re.findall(r"===FILE: (.+?)===\n(.*?)===END===", raw, re.DOTALL):
        rel = rel.strip()
        if rel == "MANIFEST":
            rel = "frontend/src/components/kit/MANIFEST.md"
        if "/kit/" not in rel:
            continue
        content = strip_md_fences(content)
        if root:
            repo_tools.write_into_repo(root, rel, content)
        else:
            (code_root(state) / rel).parent.mkdir(parents=True, exist_ok=True)
            (code_root(state) / rel).write_text(content + "\n", encoding="utf-8")


def _collect_kit(state: dict) -> tuple:
    """Gather the kit component relpaths + persist the manifest to the meta design/ dir.
    Single source of truth = what's on disk after emission (tools or text path)."""
    root = code_root(state)
    kit_dir = root / "frontend" / "src" / "components" / "kit"
    files = sorted(f"frontend/src/components/kit/{p.name}"
                   for p in kit_dir.glob("*.tsx")) if kit_dir.is_dir() else []
    manifest = None
    mpath = kit_dir / "MANIFEST.md"
    if mpath.exists():
        from tools import registry
        text = strip_md_fences(mpath.read_text())
        # Append a DETERMINISTICALLY-generated testid section so the manifest is the
        # complete, accurate interface contract (the LLM's prose is never the source
        # of truth for selectors — the kit source is).
        ids, prefixes, _ = registry.extract_kit_interface(kit_dir, text)
        if (ids or prefixes) and "## TESTIDS" not in text:
            text += ("\n\n## TESTIDS (the ONLY selectors e2e may use — generated from the kit)\n"
                     + "\n".join(f"- {t}" for t in sorted(ids))
                     + ("\n## TESTID PREFIXES (entity id appended)\n"
                        + "\n".join(f"- {p}" for p in sorted(prefixes)) if prefixes else ""))
        manifest = write_artifact(state["project_id"], "design",
                                  "components_manifest.md", text)
    return files, manifest


def _build_mockup(system: str, spec: str, profile: str, direction: dict = None) -> str:
    """Generate a single self-contained HTML/Tailwind mockup from the design spec —
    optionally for ONE specific design direction (the 3-options human choice)."""
    dir_block = (f"""
DESIGN DIRECTION — this mockup must follow THIS direction (not the other ones in the spec):
{direction['id']} — {direction['title']}
{direction['rationale']}
""" if direction else "")
    user_msg = f"""Turn this design into a SINGLE self-contained HTML mockup the CEO/CTO can
open in a browser to review the design — a clean, labeled design board, not a working app.
{dir_block}
PRODUCT CONTEXT (match the brand/tone):
{profile}

DESIGN SPEC (build the key screens + states from this; use its REAL microcopy):
{spec}

Rules:
- One complete HTML document. Load Tailwind via CDN:
  <script src="https://cdn.tailwindcss.com"></script>
- Render the KEY screens and their important states (first-run/empty, success, error)
  as clearly-labeled sections stacked on the page.
- DUAL SURFACE: show each key screen at BOTH widths — a 375px mobile frame AND a
  ~1280px desktop frame — labeled, with the layout differences the spec defines.
- DUAL THEME: show each key screen in BOTH light and dark mode (wrap dark variants in
  a dark-styled container using the spec's dark tokens; label "Light" / "Dark").
  Include the ThemeToggle in the chrome.
- Use the actual labels, empty-state copy, error messages and CTAs from the spec.
- No external JS beyond the Tailwind CDN.
- Output ONLY the HTML document. No prose, no markdown fences."""
    return _extract_html(call_llm(system, user_msg, tier="strong"))


def _extract_html(text: str) -> str:
    """Return the complete HTML document from the model output. Robust to the model
    wrapping a SNIPPET in ``` fences despite the 'no fences' rule (e.g. a code example in
    the SEO section): the old non-greedy ```...``` match grabbed that tiny MIDDLE slice and
    shipped a 4KB fragment instead of the 60KB board (live phase-3: 2 of 3 mockups broke)."""
    # 1) the real document — greedy to the LAST </html>, ignoring fences/preamble/trailing.
    m = re.search(r"(?is)<(?:!doctype html|html)\b.*</html>", text)
    if m:
        return m.group(0).strip()
    # 2) doctype/<html> present but no close (output truncated) — best-effort from there on.
    m = re.search(r"(?is)<(?:!doctype html|html)\b.*", text)
    if m:
        return m.group(0).strip()
    # 3) no document at all — strip a single wrapping fence (GREEDY to the last ```).
    m = re.search(r"```(?:html)?\s*\n(.*)\n?```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()
