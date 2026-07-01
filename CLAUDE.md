# Graphsmith — Claude Code Guide

## What this project is

A multi-agent pipeline built on LangGraph that automates the full feature-development lifecycle. The user acts as **CEO and CTO** (one human, both business and technical authority — the universal unblocker), and 7 AI agents (CEO, PM, Design, Architect, Engineer, QA, DevOps) take a plain-English feature request through to deployment configuration artifacts. Agents ask each other questions (capped at 10 rounds) and escalate anything unresolved — business or technical — to the CEO/CTO.

**Tech stack is a CTO decision:** the architect proposes a default (FastAPI + Next.js + Postgres) and escalates a mandatory confirmation to the CEO/CTO before committing the spec; the confirmed stack lives in `state["tech_stack"]` and drives engineer + devops. See `agents/architect.py::_ask_stack`.

## Running the project

```bash
export ANTHROPIC_API_KEY=your_key_here
python main.py                          # build/extend the SINGLE persistent project (workspace/project)
python main.py --repo /path/to/repo     # extend an EXTERNAL repo instead
python main.py --resume <project-id>    # resume an interrupted run
# reset the project: delete workspace/project/
```

**Project continuity:** by default every run targets one persistent project at
`workspace/project/` — the first run seeds it, later runs **auto-extend** it (Surveyor maps
the real code, Engineer writes diffs back). A **feature ledger** (`project/.agent/ledger.md`,
`tools/project_ctx.py`) records each feature and is fed to PM/Design/Architect as
`state["project_ledger"]`, so successive runs have the accumulated code **and** history.
`managed_project=True` for these runs (reuses the persisted stack; external `--repo` does not).

Pre-flight: Docker running, `gh auth status` ok, `workspace/` is a git repo.

**Operator-driven runs (non-interactive):** `main.py` blocks on `input()`. For driving the
human-in-the-loop pipeline programmatically (one segment per process), use `live_run.py`:
`start --feature "..."` runs to the first pause; it prints a `PAUSE {json}` marker for a
ceo_qa/approval, which you answer with `answer --thread <id> --text ...`,
`approve/reject --thread <id>`, or continue a fixed crash with `resume --thread <id>`.
State persists in `checkpoints.db` (keyed by thread_id) across invocations.

## Testing

```bash
pip install -r requirements-dev.txt
pytest tests/ -q          # 401 passed, 3 skipped — all mocked, no key/Docker needed
```

`pytest.ini` sets `pythonpath = .`, so the bare `pytest tests/` and `python -m pytest
tests/` both work (without it, collection fails on `No module named 'tools'/'evals'`).
Run from the project virtualenv (`.venv/bin/pytest …`) — the system Python here has a
stale `typing_extensions` that breaks the `anthropic` import. The 3 skips are the live
evals (need `ANTHROPIC_API_KEY`); a 4th skip appears if `ruff` isn't on `PATH`.

- `tests/test_architecture.py` (Set 1) — graph build, routing fns, Q&A flow, parsers, gates
- `tests/test_integration.py` (Set 1) — real graph CEO→END + PRD-reject loop
- `tests/test_agents.py` (Set 2) — per-agent IO + "never blocked, always escalates"
- `tests/test_live_eval.py` — real-LLM quality checks, skipped unless `ANTHROPIC_API_KEY` set
- `tests/conftest.py` — `MockLLM`, `base_state()`, `seed()`, `no_docker` fixtures

Live run: see `SMOKE_TEST.md`. Per-agent human-worker review: see `AGENT_AUDIT.md`.

**Gotcha:** `build_graph()` must pass `SqliteSaver(sqlite3.connect(...))`, NOT
`SqliteSaver.from_conn_string()` (the latter returns a context manager in current
LangGraph and breaks `compile()`). When you add/rename a node, add a routing test and
re-run `pytest tests/`.

**Gotcha:** `write_artifact(project_id, subdir, filename, ...)` accepts a `filename` that
itself contains nested dirs (e.g. DevOps' `.github/workflows/deploy.yml`); it now `mkdir`s
the file's full parent chain, not just `subdir`. (A live run crashed here before the fix.)

**Gotcha:** in managed/extend mode `state["code_path"]` is the repo **directory**, not a
file. `qa_utils._get_artifact_for_agent` (peer-consult context for the engineer) reads
`state["code_files"]` when the path is a dir — calling `read_artifact` on a directory
raises `IsADirectoryError` (a live QA-consult crashed here).

**Gotcha:** QA reviews the ACTUAL source (`qa._read_code`), so its per-file read cap is a
silent quality lever: a 3000-char cap truncated a 3988-char `app.js` mid-function and QA
issued a false-positive NO-GO ("frontend incomplete"). Cap is now 16000 chars × 12 files.

**Gotcha (truncation, 3rd occurrence):** the Engineer⇄QA fix loop's whole signal travels in
`error_log`. At 500 chars HEAD-sliced, QA saw only pytest's deprecation warnings (the real
assertion failure is at the TAIL) — a live run burned all 3 attempts fixing warnings, then QA
hallucinated a "truncated test". Now: engineer keeps `error_log[-6000:]` (tail), QA's fail
diagnosis keeps `[:6000]` with the raw tail at 4000. **Rule: never head-slice a test log.**

**Gotcha:** oracle protection must match `tests`/`e2e` as a path segment at ANY depth — the
split full-stack layout puts them at `backend/tests/`, `frontend/tests/` (a live engineer
wrote `backend/tests/__init__.py` straight past the old `parts[0] == "tests"` check; the
overseer caught it).

**Gotcha (FIXED, suffix-renderer testids):** kit-testid checks assumed a component's
rendered `data-testid` equals the base prop it's given. FALSE for a STATE-SUFFIX renderer:
`RelationshipButton` takes `data-testid="profile-relationship"` as a BASE and renders
`${base}-add-friend`/`-requested`/… — so QA's e2e (authored against the bare base) matched
no element, yet `check_testid_contract` passed because the base string IS in the bundle as
the prop literal (a live phase-2 multi-loop failure). `registry.resolve_kit_testids` now
resolves this semantically (emit `<base>-<suffix>`, suppress the bare base; HYBRIDS like
`UsernameField` that ALSO forward verbatim keep the base — do NOT suppress, or you
false-positive the real input). Pinned by `tests/test_kit_testid_suffix.py`. **Rule: a kit
testid is what an ELEMENT renders, never just a string found in the source.**

**Gotcha (FIXED, kit duplicate-testid across responsive layouts):** the design skill MANDATES
dual-surface layouts (desktop table → mobile cards); a SHARED kit sub-component (e.g. a
row-actions menu) rendered in BOTH layouts puts the SAME `data-testid` in the DOM twice (both
present, one only CSS-hidden) → Playwright strict mode fails ("resolved to 2 elements") and the
ENGINEER CAN'T FIX IT (kit is design-owned) — the integration→engineer loop spins. The
testid-CONTRACT gate misses it (the id IS rendered, just twice). Surfaced live in the MadClub
phase-1 rebuild. Now: `skills/design.md` mandates UNIQUE testids across the two layouts (scope the
mobile instance, e.g. `scope="-card"`, or render one layout), and `registry.check_kit_testid_uniqueness`
(deterministic, precise — flags a testid-rendering component used 2+ times IDENTICALLY, but NOT
a per-usage-differentiated one like `scope="-card"`) runs in `design._enforce_testid_uniqueness`
(re-emit ONCE, advisory after). Pinned by `tests/test_kit_testid_uniqueness.py`. **Rule: every
rendered data-testid must be UNIQUE in the DOM; a responsive second layout must suffix its testids.**

**Gotcha (FIXED, mockup HTML extraction):** `design._extract_html` pulled the mockup out of a
```html fence with a NON-GREEDY `(.*?)` — if the model emitted a stray fenced snippet (e.g. a
code example in the SEO section) despite the "no fences" rule, it grabbed that tiny MIDDLE
slice and shipped a 4-6KB FRAGMENT instead of the 60KB board (live phase-3: 2 of 3 mockups
came out blank/garbled). Now it extracts the real `<!doctype …</html>` document span (greedy to
the last `</html>`, ignoring fences/preamble), best-effort on truncation. Pinned by
`tests/test_mockup_html_extract.py`. **Rule: extract the DOCUMENT, not a fence span.**

**Gotcha (FIXED, I2):** a skill exceeding `MAX_SKILL_CHARS` is silently truncated — the
engineer skill's newest mandates never reached it. Cap now 16000 and
`test_all_skills_load_untruncated` fails the suite if any `skills/*.md` crosses it.

**Generalization note (recurring learnings → STACK-AGNOSTIC skill principles, 2026-06-22):**
when a recurring bug class is committed into a skill (so it survives the gitignored/evictable
`learnings/` store and ships with the repo), state it as a TRANSFERABLE PRINCIPLE — usable for
any product on any stack — with the default-stack form as a parenthetical *(Default stack: …)*
example. Do NOT bolt on a per-stack deterministic guard for it: the deterministic layer is
already FastAPI/SQLAlchemy/Postgres-coupled, and growing that coupling makes the platform less
portable; prefer the EXISTING generic mechanisms (e.g. the integration stage brings the app up
on the real backend, which catches the whole "tests pass / app doesn't boot" class regardless
of stack). Worked example: the Postgres enum trap (generic `sa.Enum` silently ignores
`create_type=False` → `DuplicateObject` at `alembic upgrade head`, invisible to sqlite unit
tests) is encoded as the GENERIC engineer/test_author principle "tests passing ≠ the app boots;
migrations must run on the production engine" — its Postgres specifics live only as the example.
**Rule: a learning injected into an agent must be product- AND stack-agnostic; keep stack
specifics as `(Default stack: …)` examples, and reuse the existing generic gates over new
per-stack ones.**

**Gotcha (mostly retired by I1):** the `===FILE/EDIT===` TEXT path (now only behind
`AGENT_CODEGEN=text`) parses via `engineer._file_blocks` (block ends at next marker; LAST
emission of a path wins) — the naive regex once corrupted 5 files, and text re-emission
rounds are DESTRUCTIVE. The DEFAULT tools path (`tools/codegen.py`) has no parsing: a
live no-op round wrote 0 files (verified), and a stale edit anchor errors back with the
file's CURRENT content. The guard choke point is `codegen.sync_back` (oracles/kit/meta,
escapes, deletions).

## Pipeline (Phase 1)

```
CEO → Triage ─(feature)→ PM → [prd_gate] → Surveyor → Design → critic_design → Architect → critic_architect → Test Author
     └─(bugfix/refactor/chore → QUICK lane)──────────────────────────────────────────────────────────────────► Engineer
  full lane:  … Test Author → Engineer ⟷ QA → Integration → Design QA → [pr_gate] → Ship → DevOps → END
  quick lane:                Engineer ⟷ QA → Integration → Design QA → [pr_gate] → Ship → END
                          ↑ reject loops to Engineer; prd_gate reject loops to PM
  Integration (4.2/4.3): docker compose up the app's own stack + smoke (api :8000/health,
  frontend :3000) + QA's Playwright e2e — e2e-stage fail on a healthy app → QA revises the
  SPECS once (I4); other fails → Engineer (≤3 integration attempts total), then gate w/ red report.
  Design QA (vision): integration captures tests/app_screenshot.png while the stack is up;
  design_qa renders mockup.html → PNG and ONE vision call (strong) compares both + design spec
  → ===VERDICT: ALIGNED|MISALIGNED===; misaligned → Engineer w/ findings in review_notes
  (SINGLE-SHOT: MAX_DESIGN_QA_ATTEMPTS=1, then gate w/ red tests/design_qa.md). Skips free when
  no mockup (quick lane/backend-only) or no screenshot — never blocks on tooling. Cost order:
  design-owned component kit (free, by construction) → integration's deterministic REQUIRED
  MICROCOPY gate (free) → one vision confirmation.
```

- **Triage** (`agents/triage.py`): classifies the request → `change_type` ∈
  feature/bugfix/refactor/chore. Feature = full pipeline; the rest take the **quick lane**
  (straight to Engineer working from the brief — no PRD/design/spec/test-author, no DevOps).
  Safe default = feature. Engineer is change-type-aware (minimal fix / preserve behavior / chore).
- **Stack persists** across features: the CEO/CTO-confirmed greenfield stack is saved
  (`tools/product.py` → `product/stack.md`) and reused, so the architect doesn't re-ask it
  every feature. (Extend mode still detects from the target repo.) **Default stack (Phase 4.1)
  is the dockerized full stack** — FastAPI + Next.js + Postgres via docker-compose, pinned
  slim/alpine images (`python:3.12-slim`, `node:22-alpine`, `postgres:17-alpine`), no floating
  `:latest`. Tests run **per layer via a toolchain-detecting runner** (`registry.detect_toolchains`
  → `run_project_tests`): pytest (backend) + vitest (frontend), aggregated; falls back to
  pytest-only when nothing else is detected. Engineer + QA call `run_project_tests`.

- **Design directions + HUMAN choice (CEO mandate 2026-06-12):** design proposes
  **3 distinct directions** (`## Design Directions`, `### A/B/C — name` + rationale each),
  emits a mockup per direction (`design/mockup_{A,B,C}.html`), renders
  `review/design_options.html` (side-by-side mockups embedded inline via iframe `srcdoc`,
  NOT file:// `src` — a file:// page renders a file:// iframe BLANK, so the board was empty
  when opened from disk; `report_html._mockup_iframe`), and pauses via the
  EXISTING `ceo_qa` interrupt with a deterministic "DESIGN DIRECTION CHOICE" question —
  the REAL human picks (reply `A`/`B`/`C`, tweaks allowed; recorded into the spec as
  `## Chosen Direction`). The winner becomes `mockup.html`; the KIT is built from the
  winner only. Resume never regenerates (artifacts on disk are the round-trip state).
  Critic re-runs keep the recorded choice and regenerate only the chosen mockup.
- **HTML review layer (`tools/report_html.py`, dual-surface decision):** `.md` stays the
  canonical agent-readable artifact (token-cheap, regex-parseable); DETERMINISTIC
  templating (zero LLM) renders the human pages: `review/{prd,pr}_gate.html`
  (integration badges, app-vs-mockup screenshots, QA report, security findings) emitted
  by live_run/main at every approval pause (PAUSE payload carries `review_html`),
  `review/design_options.html`, the project's `.agent/ledger.html`, and the per-run
  **`review/audit/` folder** (`report_html.render_audit` at DONE) — a browsable record of
  every actor, discussion (who/what/feedback/conclusion/WHY from the trace's feedback
  events + qa_log), the AC-coverage map, links to every rendered artifact, and the
  retro lessons. Built for human audit; deterministic, zero LLM.
- **Design (consumer-app)**: reads the standing **product profile** (`tools/product.py`,
  set once by CEO/CTO via `main.py`, stored `product/profile.md`) + PRD; does discovery
  (users/JTBD/brand/goal), asks CEO/CTO for material gaps, outputs context+rationale,
  flows incl. unhappy/first-run, components, and **microcopy**; then emits a self-contained
  **`design/mockup.html`** (Tailwind CDN) of the key screens/states (2nd strong call; skipped
  for `NO UI SURFACE`). Reviewed by `critic_design` (generic critic + design review focus).
  PM also reads the profile. **Design fidelity (prevention):** design also sets
  `design_spec_path` — a stable pointer that survives the architect overwriting
  `design_path` with the tech spec — and the engineer's prompt includes the design spec +
  mockup HTML with match-the-design rules (`engineer._read_design`). Before this, the
  engineer only ever saw the tech spec and a live run shipped a working app that looked
  nothing like the mockup. **Verification half (built):** the `design_qa` node — see the
  pipeline diagram. `call_llm(..., images=[(label, png_path), ...])` supports vision calls.
  **Alignment by construction (the cost-rational fix):** when the frontend stack is already
  known (persisted stack on managed runs 2+/detected in extend mode), Design emits the REAL
  presentational components (`design._build_components` → `frontend/src/components/kit/`,
  exact microcopy + data-testids, props-only) plus `design/components_manifest.md` (wiring
  contracts + REQUIRED MICROCOPY). The engineer **wires** them (`engineer._read_kit` contract)
  and is blocked from editing kit files (protected like `tests/`/`e2e/` via
  `design_component_files`); **I3 enforces USAGE too** — `registry.check_kit_wiring`
  (deterministic, post-write, pre-lint) fails the engineer round unless a non-kit
  frontend source imports from `kit/` and no file duplicates a kit component's name. Engineer re-synthesis of UI from prose was the root drift cause —
  handing it finished components removes the drift class. Three-tier checking, cheapest first:
  (1) construction (free), (2) integration's deterministic REQUIRED-MICROCOPY gate against the
  served HTML (`registry.check_required_microcopy`, free, runs before any vision tokens),
  (3) vision design_qa demoted to SINGLE-SHOT confirmation (`MAX_DESIGN_QA_ATTEMPTS=1`).
  **Stack-at-design-time:** if no stack is persisted/detected yet (first greenfield run),
  Design escalates ONE deterministic CTO question ("DESIGN STACK CONFIRMATION… reply
  'default'"); the answer is PERSISTED via `product.save_stack` — so the kit is emitted
  from feature #1 and the architect's mandatory stack ask auto-skips thereafter.
  **Design-system memory (coherence across features):** `product/design_system.md`
  (`product.load/save_design_system`) — fonts/tokens/spacing, component inventory, UX
  patterns, microcopy voice. Design's spec has a mandatory `## Design System` section
  (carry forward + extend) that is extracted and persisted each run, and the prompt
  carries the established system + the EXISTING kit inventory (reuse, don't duplicate).
  **SEO/AEO (consumer app):** design spec has a mandatory `## SEO & Discoverability`
  section (title/description/H1/landmarks/JSON-LD schema type, SSR-critical content);
  the engineer skill implements it (Next Metadata API, JSON-LD, sitemap/robots);
  integration enforces a FREE deterministic floor via `registry.check_seo_basics`
  (title, meta description, h1, html lang, viewport, JSON-LD — `check_seo=True` whenever
  the feature has a mockup).

- **Extend mode** (`--repo <path>`): `surveyor` maps the existing repo (`tools/repo.py`),
  writes `design/repo_map.md`, sets `detected_stack`; architect proposes the detected
  stack and the spec extends real files; **test_author + engineer write back INTO the
  repo** — new files in full, existing files via **minimal `===EDIT:===` search/replace
  blocks** applied by `repo.apply_edit` (unique-match-or-fail; a stale SEARCH fails loudly
  → engineer retries). Engineer reads existing files + runs the repo's own test suite,
  never clobbers `state["test_files"]`. Meta-artifacts still go to `workspace/<id>/`.
  Greenfield = surveyor no-ops, full-file writes to `workspace/<id>/`. The engineer can also
  emit `===DELETE: path===` to remove a stale/conflicting leftover (`repo.delete_from_repo`,
  guarded: never tests/e2e, never outside the root) — without it, retries accreted files
  (a live run ended with conflicting Next.js `pages/index.tsx` AND `app/page.tsx`).
- **QA reads the code (#5):** on a passing run, QA reads the engineer's written files
  (`state["code_files"]`) and reviews them against the acceptance criteria in its sign-off
  (not just tests-passed). The report is shown at `pr_gate`, so blocking findings reach the
  CEO/CTO who can reject → engineer fixes.

- `integration` (4.2/4.3): deterministic node (no LLM) after QA — `registry.run_compose_integration`
  PRE-CLEANS (down -v + foreign-port fail-fast marked "environment, not code"), brings the
  app's own docker-compose stack up (`-p graphsmith-it`), waits healthy, smokes
  api/frontend, runs QA's e2e (python `test_*.py` + legacy `*.spec.ts`) on the compose
  network, SEEDS representative data via the app's openapi.json (incl. an OVERDUE entity)
  before the design-QA screenshot, always tears down. Fail → engineer via `error_log` (cap `MAX_INTEGRATION_ATTEMPTS=2`, then gate
  shows the red `tests/integration_report.md`). External `--repo` w/o compose skips gracefully.
  **Rate-limit override (shared-IP e2e, live phase-2 fix):** the e2e suite drives every
  rate-limited write endpoint from ONE runner IP, so a per-IP limit 429s mid-suite (a
  flaky shared-IP TEST artifact, not a product bug). `run_compose_integration` writes an
  IT-ONLY `docker-compose.it-override.yml` (`api: RATE_LIMIT_ENABLED: "0"`) that `_compose`
  merges via `-f` for every bring-up/teardown call, then removes it in `finally` — the
  SHIPPED compose keeps its production limit. Engineer skill mandates env-gated rate limiting.
- **Interface Contract — additive freeze (zero-drift, 2026-06-15):** the design kit's
  testids + required microcopy are persisted (`product/interface_contract.md`) and may
  only GROW across phases — `design._enforce_interface_additive` (via
  `registry.check_interface_additive`) fails+forces a restore round if a rework drops a
  prior-phase testid/microcopy that existing e2e specs depend on (the phase-3 regression
  class: dropped card bio, renamed profile testids). A prior BARE base testid is NOT counted
  as "dropped" once the current kit renders its suffixed children (`profile-relationship` →
  `profile-relationship-add-friend`…) — the suffix-renderer fix replaced the base, and the
  guarantee lives on in the children e2e actually use. The manifest's `## TESTIDS` section is
  generated deterministically from the kit source — the single selector source for QA.
  **Suffix-renderer resolution (zero-drift, live phase-2 TrailTribe fix):** the testid
  extraction is SEMANTIC, not a flat string grep. `registry.resolve_kit_testids` (shared by
  `extract_kit_interface`, `check_testid_contract`, and qa's `_kit_selector_block`)
  understands components that re-emit their `data-testid` prop with a STATE suffix —
  `RelationshipButton` renders `${base}-add-friend`/`-requested`/`-accept`/`-friends`/`-edit`,
  never the bare base. For each usage it emits the `<base>-<suffix>` variants and SUPPRESSES
  the bare base (a prop literal that no element renders), so QA authors the real ids and a
  base-only e2e assertion is FLAGGED instead of silently passing (the base string is present
  in the source as the prop value, which fooled the old grep). A HYBRID that also forwards the
  base verbatim onto a real element (`UsernameField` → `data-testid={testId}` on the input PLUS
  `${base}-checking`/`-available` sub-spans) keeps the bare base too. A pure verbatim forwarder
  (`FollowButton` → `data-testid={testId}`) is unchanged — its base renders as-is. The
  per-state suffix set is surfaced to QA's authoring prompt via `registry.kit_state_suffixes`.
- **Feature Contract / AC coverage spine (zero-drift, 2026-06-15):** PM emits stable,
  surface-tagged acceptance criteria (`AC-1 (ui|backend): …`); test_author tags each
  test and QA each e2e with `# covers: AC-N`; `tools/contract.py` parses ids + refs.
  test_author SELF-CHECKS coverage (one extra authoring round on a gap, before the
  gate); integration runs a FREE deterministic `AC coverage` stage (every AC tested,
  every UI AC has an e2e) — a UI gap on a healthy app routes to QA's revision round
  (like I4e), the AC→test map shows in the report. 100% coverage by construction.
- **QA authors the e2e specs** (4.3) in its pass path, *after* code review: flows from feature
  request/PRD (intent), code only for selectors. Engineer can never write `tests/` **or `e2e/`**.
  Backend-only (no node layer) → no specs, API smoke covers it. Best-effort: retry once → proceed.
  **E2E LANGUAGE IS PYTHON (CTO decision 2026-06-12):** QA authors pytest-playwright
  files (`e2e/test_*.py`, sync API, run serially via `playwright/python` image +
  pinned `pytest-playwright`); legacy `*.spec.ts` keep running until ported
  (`registry._run_e2e` runs both flavors, aggregated). Unit-test pytest runs pass
  `--ignore=e2e` so a whole-suite run never collects the e2e specs.
  **I4 quality pack:** the authoring prompt carries the `qa_log` (CEO corrections now reach
  authoring) + the kit's REAL data-testids as the ONLY selector source + hard conventions
  (API paths verbatim, mandatory beforeEach cleanup, evaluate-click for styled checkboxes);
  every file must pass `registry.lint_e2e_spec` (invented testids, label/CSS-class guessing,
  guessed API paths, .check() flake, missing isolation) — one re-author w/ findings, then the
  file is DROPPED (a known-bad oracle never reaches integration). Re-passes never re-author
  existing specs (a live re-author clobbered a CTO-fixed spec). e2e-stage failures on a
  healthy app route ONE bounded revision round back to QA (`e2e_revision_pending` →
  `integration_routing` → qa → revise mechanics, never weaken assertions → integration).
- `prd_gate` / `pr_gate`: **blocking** CEO approval interrupts (approve/reject+feedback).
- `critic_architect`: reviews tech spec vs PRD; retry ≤2 then escalate to CEO, then proceed.
- `test_author`: writes authoritative tests BEFORE engineer (TDD); engineer must pass them, can't edit `tests/`. Runs on the **`strong`** tier (it authors the correctness oracle; on `fast`/2048-tok it silently truncated the actual `test_*.py` away). A **non-empty-oracle guard** verifies the written suite has ≥1 `test_*.py` containing `def test_`; if not it retries once, then escalates to the CEO/CTO rather than passing an empty oracle downstream.
- `ceo_qa`: shared clarification interrupt; any agent (or the critic) can pause.
- Agent-to-agent consultations happen silently/synchronously inside each agent's run().
- DevOps can also escalate to CEO (wrapped in `run_with_qa`) — no agent is ever blocked.
- **Cross-run learning (2.2) + UNIVERSAL SELF-IMPROVEMENT (2026-06-13):** every feedback
  moment is emitted into the run trace as a `feedback` event at its choke point —
  gate rejects (pr→engineer, prd→pm), critic retries (→design/architect), integration
  failures (→engineer, stage-tagged), codegen guard violations (→engineer), e2e lint
  drops + revision triggers (→qa), design-QA misalignment (→design+engineer), and a QA
  code-review **NO-GO** (→engineer+qa, REGARDLESS of the gate outcome — `qa._emit_nogo_feedback`:
  a NO-GO the CTO ADJUDICATES or HAND-FIXES still teaches; before, only a gate REJECT did, so an
  adjudicated NO-GO like the phase-3 badge-tier bug taught nothing) — via `learnings.emit_feedback`.
  The CTO can also log an out-of-band hand-fix the pipeline can't observe:
  `live_run.py feedback --thread <id> --agent <a> --text "<class of mistake>"`. At every run's END,
  `learnings.run_retro` (one fast call, never raises) distils ≤2 GENERALIZABLE, product-AGNOSTIC
  lessons per agent (its prompt forbids feature-specific names/values) from those events + the CEO's
  recorded directives, and records them per agent. ALL producing agents (pm, design, architect,
  test_author, engineer, qa, devops) load their lessons into the system prompt via `augment_system`.
  QA's unit-test-failure distillation also remains. Deduped + capped per agent; `learnings/`
  gitignored. Pinned by `tests/test_self_improvement.py` + `tests/test_retro_feedback.py`.
- **Two learning tiers — LOCAL (per-installation) vs SHARED (committed, ships with the
  harness) (2026-06-27):** the retro writes to the gitignored `learnings/<agent>.md` store,
  which is machine-accumulated, may be stack/product-specific, and never leaves the clone
  that learned it. A second tier `learnings/shared/<agent>.md` is **committed** (un-ignored in
  `.gitignore`) so its lessons propagate to EVERY clone and project. A lesson reaches it only
  by human-gated PROMOTION and MUST be product- AND stack-agnostic (keep stack specifics as a
  `(Default stack: …)` example). `augment_system` loads BOTH (shared first, then local);
  `promote_learning` + the CLI `python -m tools.learnings list` / `promote --agent <a>
  (--index N --as "<generic rewrite>" | --text "...")` graduate a candidate (promote-by-index
  REMOVES it from local so it isn't injected twice). Kept separate from hand-authored
  `skills/` so promoted machine lessons never corrupt a curated skill. Pinned by
  `tests/test_shared_learnings.py`. **Rule: only product- AND stack-agnostic lessons go in the
  committed shared tier; raw/stack-specific candidates stay in the local store.**
- **Standing product invariants (knowledge-base wiring, 2026-06-17):** the generation agents
  (architect, test_author, engineer, qa) carried ZERO standing product context — they reasoned
  about the product from per-run artifacts alone. Now `registry.extract_product_invariants(root)`
  STATICALLY parses the backend's `models/*.py` (unique/check constraints, computed-not-stored
  columns, enums) + `routers/*.py` (route+auth surface) OFF DISK — never the runtime `openapi.json`
  (which only exists when compose is healthy) — into `state["product_invariants"]` (loaded in
  `main.py`/`live_run.py` from `target_repo`; `""`/None on a non-Python or undetected repo, so it
  no-ops safely). `qa_utils.product_invariants_block` injects it into those four agents' WORK
  prompts labeled "OVERRIDE any learned lesson or per-run guess", so e.g. the engineer never adds a
  `spots_remaining` column. Lives WITH the product as canonical docs (`workspace/project/docs/`:
  DOMAIN_MODEL, AUTH, ADRs in `decisions/`, `INDEX.md`; `README.md`; `product/api_contract.md`).
  Also: `product.load_profile`/`load_design_system` no longer **silently** head-slice over the cap
  (`product._cap` warns). Pinned by `tests/test_product_invariants.py`.
- **Execution hardening (2.3):** `registry.scan_security` scans the engineer's written files (eval/exec/shell=True/secrets/…); findings → `security_warnings`, shown in the QA report and at the PR gate. Docker test runs have `--memory/--cpus/--pids-limit`.
- **Code-quality layer (additive, non-blocking):** `registry.format_code` (ruff `--fix`
  import-sort/pyupgrade/cleanup + `ruff format`) runs in the engineer BEFORE the blocking
  E,F lint gate — it can only make code cleaner and the gate pass more often, never blocks.
  `registry.code_quality_report` (advisory ruff bug/style + mccabe complexity C90 + mypy
  static types, scoped to written files), `registry.check_frontend_quality_tooling`
  (deterministic, no Node: ESLint/Prettier/strict-tsconfig/typecheck presence) and
  `registry.check_dependencies` (dependency lock §2.3/I7 — see below) produce
  `state["code_quality"]`, surfaced at the PR gate like `security_warnings`. All degrade
  gracefully when ruff/mypy are absent and NEVER raise. `ruff`/`mypy` pinned in
  `requirements-dev.txt`; engineer skill carries a `## Code quality` mandate. Pinned by
  `tests/test_code_quality.py`. **Rule: augment the proven E,F gate with auto-fix +
  advisory reports; don't bolt on a new blocking gate that destabilizes the engineer⇄QA
  loop.**
- **Code-quality soft gate (§2.1/2.2, 2026-06-19) — OPT-IN, default OFF:** `QUALITY_GATE` env
  (`registry.quality_gate_level`) gates the engineer in three levels — unset/`off` = no change;
  `report` = ALSO measure + surface line coverage; `block` = additionally FAIL the engineer round
  on over-budget cyclomatic COMPLEXITY (`registry.check_quality_gate`, bounded by `MAX_FIX_ATTEMPTS`,
  auto-fix still runs first). **Coverage (§2.2) is report-only** — `registry.measure_coverage` runs
  `pytest --cov` in a SEPARATE best-effort Docker pass (never touches the correctness run, never
  raises) and the engineer appends `coverage: NN%` to `code_quality`; it is NOT gated on the
  engineer (which is blocked from `tests/` and so can't raise it — a coverage floor belongs on
  `test_author`, deferred). **mypy stays advisory even at `block`** (false positives without
  per-project config would cause loop-burn). Follows report-first→gate-later: calibrate with
  `report`, watch the autonomy rate (§3.3), then flip to `block`. Pinned by `tests/test_code_quality.py`.
- **Dependency lock (§2.3/I7, 2026-06-19):** `registry.check_dependencies(project_dir, files)`
  flags every third-party import in the engineer's WRITTEN files that is NOT a declared
  dependency — `requirements.txt`/`pyproject.toml` for Python (AST imports; stdlib via
  `sys.stdlib_module_names`, first-party modules and relative imports excluded; import→dist
  aliases like `yaml`→`PyYAML` + PEP503 boundary-prefix matching so `psycopg2`⊆`psycopg2-binary`)
  and `package.json` for JS/TS (scoped/sub-path specifiers reduced to the package; node
  builtins/path-aliases/relative excluded). Advisory, FOLDED INTO `code_quality` with a
  `deps:` label (no new state field/gate), scoped to written files so extend-mode never
  flags a repo's pre-existing imports, biased toward PRECISION (a missed flag beats noise),
  no manifest → silent, never raises. Kills the hallucinated-`react-query` drift class
  ("builds locally, breaks in a clean install"). Pinned by `tests/test_dependency_lock.py`.
- Routing verified by `tests/` across happy/reject/retry/escalate/never-pass paths; all reach END.
- **Overseer (evals/):** every run is traced (`tools/trace.py` → `traces/<id>.jsonl`, tokens/latency via `call_llm`); at the end `main.py` runs `overseer.oversee(final_state, totals, autonomy)` — deterministic invariants (engineer didn't touch tests, feature has PRD, stack confirmed, no silent red ship), loop non-convergence, token/call budget. HIGH-severity failing → "NEEDS HUMAN REVIEW". Offline: `python -m evals.triage_eval` (real-LLM accuracy). See `evals/README.md`.
- **Autonomy metric (§3.3/I10, 2026-06-19):** `run_stats.compute_autonomy(events, state, manual_edits=0)` — the number a software company actually manages: **human interventions per run**. Deterministic from the trace + final state: `clarifications` (CEO answers, `qa_log` to=ceo answered) + `rejections` (gate rejects via `prd_gate_reject`/`pr_gate_reject` feedback events) + `manual_edits` (CTO hand-fixes logged as `cto_handfix` feedback + an optional git-diff count) = `interventions`; `autonomy_rate = approvals / (approvals + interventions)` (1.0 = the human only rubber-stamped the mandatory gates; every reject/clarification/hand-fix drags it down). `agent_steps`/`pauses` from `node_exec`. Surfaced as an INFO overseer finding (never fails a run — a metric to drive down, not a gate) + a KPI card in the flight recorder. Computed once in `main.py`/`live_run.py`, passed to both. On real traces: clean runs 1.0, the 8-reject bugfix run 0.2. Pinned by `tests/test_autonomy.py`.
- **Flight recorder (`report_html.render_run` → `review/run.html`):** the VISUAL companion to
  the audit page — a deterministic (zero-LLM) per-run dashboard built from the trace via
  `run_stats.aggregate`: the actual **node path with loops** (engineer⇄QA bounces, ceo_qa
  pauses, critic retries shown as repeated chips), **where wall-time went** (per-node bars),
  **model spend by tier** (the cost flame), and a **loops/rework** summary; cross-links the PR/
  PRD gates + the audit page. Rendered at END in `main.py`/`live_run.py` next to `render_audit`,
  best-effort (never breaks a run). Leads with the **Autonomy** KPI card (rate + intervention
  breakdown, see above). Pinned by `tests/test_run_stats.py`. Where the audit answers
  *who decided what and why*, the flight recorder answers *what the run DID and where time/tokens went*.

## Key files

| File | Role |
|---|---|
| `main.py` | Entry point, handles both initial CEO interrupt and Q&A interrupts in a loop |
| `graph/state.py` | Flat `TypedDict` — all state fields including Q&A fields |
| `graph/graph.py` | **Only file that knows agent order** — all edges and routing defined here |
| `tools/llm.py` | Single LLM gateway — `call_llm(system, user_msg, tier)`; **`call_structured(...,  schema, default)`** = validated structured control-plane decisions (§4.1) |
| `tools/file_io.py` | All disk I/O — `read_artifact`, `write_artifact`, `load_prompt`, `load_skill` |
| `tools/registry.py` | Deterministic tools — linter, **toolchain-detecting** test runner (`detect_toolchains`/`run_project_tests`: pytest+vitest per layer), validators, **`extract_product_invariants`** (static models/routers → standing product context), **code-quality layer** (`format_code`, `code_quality_report`, `check_frontend_quality_tooling`, **`check_dependencies`** = dependency lock §2.3/I7, **`measure_coverage`**/**`check_quality_gate`** = opt-in soft gate §2.1/2.2) |
| `tools/qa_utils.py` | Bidirectional Q&A — `run_with_qa()`, `consult()`, `format_qa_context()`, **`product_invariants_block`** (injects code-derived invariants into architect/test_author/engineer/qa) |
| `tools/repo.py` | Read-only existing-codebase access for extend mode (list/grep/read/map + guarded write) |
| `tools/codegen.py` | I1/I17 — ALL code-writing agents (engineer, test_author, qa-e2e, design-kit) change files via REAL tools on a staging copy + guarded sync-back. `generate_in_domain` = inverted guard (agent may only touch its own domain; Read-before-write prevents blind overwrites that dropped exports/self-deleted modules) |
| `tools/learnings.py` | Cross-run learning — `load_learnings`/`record_learning`/`augment_system` (2.2); **two tiers**: gitignored LOCAL `learnings/<agent>.md` + COMMITTED `learnings/shared/<agent>.md` (`load_shared_learnings`/`promote_learning` + `python -m tools.learnings list`/`promote` CLI = human-gated graduation of GENERIC lessons into the harness) |
| `tools/contract.py` | Feature Contract spine — parse PRD AC ids (`parse_acs`), extract `# covers:` AC refs, deterministic `coverage` (every AC tested, every UI AC has an e2e) |
| `tools/product.py` (+`registry`) | Interface Contract — persisted cumulative kit testids + microcopy (`load/save_interface_contract`); `registry.extract_kit_interface` + `check_interface_additive` enforce ADDITIVE-ONLY across phases (no dropped guarantee). `registry.resolve_kit_testids` resolves STATE-SUFFIX kit components (`${base}-add-friend`) so the REAL rendered ids are surfaced and a base-only e2e assertion is caught; `kit_state_suffixes` feeds the variants to QA authoring |
| `tools/product.py` | Persistent product profile (category/users/brand/goals) + persisted stack; `_cap` makes over-cap reads WARN (no silent head-slice) |
| `tools/project_ctx.py` | Single persistent project (`workspace/project`) + feature ledger (continuity) |
| `tools/trace.py` | Per-run trace (nodes + LLM calls/tokens/latency) → `traces/<id>.jsonl` |
| `evals/overseer.py` | Runtime overseer — deterministic invariants/loops/budget on the finished run |
| `evals/triage_eval.py` | Accuracy + confusion eval for Triage (`datasets/triage.jsonl`) |
| `agents/triage.py` | Classifies the request → `change_type`; routes feature (full) vs quick lane |
| `agents/surveyor.py` | Maps the target repo in extend mode; no-op in greenfield |
| `agents/critic.py` | Generic spec reviewer, `run(state, stage)`; wired for `design` + `architect` |
| `agents/<name>.py` | Each agent: `run(state) -> dict`, plus `_do_work(state, qa_log, rounds, allow_clarify=True) -> dict` |
| `agents/ceo_qa.py` | Shared Q&A interrupt node — stores CEO's answer into `qa_log`, clears pending state |
| `agents/prd_gate.py` / `pr_gate.py` | Blocking approval interrupt nodes (record approve/reject) |
| `agents/test_author.py` | Writes authoritative tests from PRD before engineer (TDD) |
| `agents/integration.py` | Deterministic run-and-verify node — compose up + smoke + Playwright e2e (4.2/4.3) |
| `agents/design_qa.py` | Vision design verification — live-app screenshot vs mockup + spec, strict ALIGNED/MISALIGNED |
| `agents/ship.py` | Opens the PR after `pr_gate` approval |
| `skills/ceo.md` | Establishes the human as CEO **and** CTO (loaded by `agents/ceo.py`) |
| `prompts/<name>.txt` | Identity system prompt per agent (short, rarely changes) |
| `skills/<name>.md` | Domain knowledge injected into system prompt (evolves over time) |

## Model tiers (TWO models, split by WORKLOAD — set in `tools/llm.py`, 2026-06-27)

**Only two models run everything: Opus 4.8 = deep thinking/decision/analysis; Sonnet 5 =
hands-on coding.** The three tier KEYS are kept (so call sites/tests don't churn); each maps
to one of the two models. `MAX_TOKENS` is 8192 on every tier.

- `fast` → `claude-opus-4-8` — lighter DECISION/ANALYSIS: CEO, PM, Triage, QA review+diagnosis, peer consults, retro (was Haiku — retired; cap raised 2048→8192 so a PRD/QA report can't truncate)
- `strong` → `claude-sonnet-5` — CODING: Engineer (code gen + fix loop), Design kit/mockup, QA e2e specs, DevOps config
- `reason` → `claude-opus-4-8` — DEEP THINKING + the oracle (must NOT ride the coding model): Architect, Critic, Test Author (the correctness oracle), Design spec reasoning, design_qa VISION verdict (cap raised 4096→8192 for the test suite + spec)

**Rule: the Test Author (oracle) and the design_qa vision verdict are analysis — keep them on
Opus (`reason`), never on the Sonnet coding tier.** Verify Sonnet's codegen completeness on a
live run (the engineer's historical failure is truncation; `CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000`
is the ceiling). `claude-sonnet-5` is the confirmed Sonnet 5 id.

**Adaptive thinking (OPT-IN):** `tools/llm.EFFORT` maps each tier to an effort level
(architect/critic/engineer `high`, cost-floor agents `standard`). Set `LLM_THINKING=adaptive`
to send `thinking={"type":"adaptive","effort":…}` on the **api** backend — the model self-budgets
its reasoning (interleaved thinking on automatically; `budget_tokens` is deprecated on 4.7+).
DEFAULT (unset) = exact current behavior. **Safe by design:** `_api_call` falls back to a plain
request if the param is rejected, and text extraction takes the first TEXT block (robust to
thinking blocks). Verify reasoning quality on a live run before relying on it; CLI-backend support
is a follow-up. Pinned by `tests/test_llm_thinking.py`.

**Web search (§4.2, OPT-IN):** the spec agents pinned versions/APIs from training-cutoff memory;
`call_llm(..., web_search=True)` (threaded via `work_call`) lets the **architect** (pins the stack)
and **surveyor** (flags outdated/CVE'd deps) VERIFY current versions/deprecations/CVEs. Set
`LLM_WEB_SEARCH=1` to enable (DEFAULT OFF = exact current behavior). CLI backend → `claude -p
--allowed-tools WebSearch` (+`_SEARCH_GUARD`, higher max-turns); api backend → the `web_search`
server tool. **Safe by design:** `call_llm` falls back to a plain memory-grounded call on ANY search
failure and skips search on vision calls — enabling it can never break a run. PM intentionally NOT
wired (a PRD is product, not version facts). **Verify spec quality on a live run** (the CLI WebSearch
path can't be exercised from a sandbox). Pinned by `tests/test_web_search.py`.

**Backends (`LLM_BACKEND` env):** `claude-cli` (DEFAULT — headless `claude -p`, billed to
the Claude subscription: zero marginal $, plan-quota bounded; binary auto-discovered incl.
nvm paths, override with `CLAUDE_CLI_BIN`) or `api` (metered via `ANTHROPIC_API_KEY`, opt
in with `LLM_BACKEND=api`). Vision (design_qa) also goes through the CLI: images are
COPIED INTO the call's cwd and read with the CLI's Read tool (outside-cwd reads hit
permission friction and burn the turn budget — live `error_max_turns`). With the cli
default the pipeline needs NO API key at all; Opus-on-generation costs nothing extra.

**CLI quality parity (must stay ≥ api backend):** each CLI call runs from a NEUTRAL empty
cwd (else Claude Code loads this repo's CLAUDE.md into every pipeline call — ~6K tokens of
contamination) with `--strict-mcp-config` (no MCP tool defs); text calls get an appended
anti-brevity/anti-tool-use guard (`_TEXT_GUARD` — Claude Code's harness prompt biases
toward concise chat answers and tool use, both poison for "emit COMPLETE files as plain
text"); output ceiling `CLAUDE_CODE_MAX_OUTPUT_TOKENS=16000` (above every api tier cap).
Live-verified: 161-line module + 14 tests emitted complete, zero preamble, no tool
derailment; vision verdict ALIGNED. Pinned by `test_cli_call_slimming_and_quality_guards`.

Prompt caching is on: `call_llm` sends the system block with `cache_control`, so it's
reused cheaply across clarification re-runs and Engineer retries.

## Bidirectional Q&A — how it works

The producing agents (PM, Design, Architect, Test Author, Engineer, QA-on-pass) wrap their `run()` in `run_with_qa()` from `tools/qa_utils.py`. CEO, DevOps, the gate nodes (`prd_gate`/`pr_gate`), `ship`, `critic`, and `ceo_qa` do not — the critic escalates to CEO directly by setting `ceo_qa_pending`.

**Folded clarification (no separate probe call):**
- Each agent's `_do_work(state, qa_log, rounds, allow_clarify=True)` calls `work_call(...)`, which appends a CLARIFICATION PROTOCOL to the work prompt
- The LLM either produces its artifact, or emits a `===NEEDS_INPUT=== {json} ===END===` block when blocked
- If blocked, `_do_work` returns `{"_clarify": questions}` WITHOUT writing artifacts; `run_with_qa` handles resolution
- Common case = one call; old design always made a probe call first

**Structured control-plane signals (§4.1, 2026-06-19):** the routing-critical decisions are
VALIDATED objects, not regexes over prose. `tools/llm.call_structured(system, user, schema,
tier, images, default, retries)` appends a strict "emit ONLY this JSON" contract, extracts the
JSON robustly (quote-aware brace scanner, fence-/prose-tolerant `_extract_json`), validates +
coerces against a lightweight dependency-free `schema` (enum/string/bool/int + required via
`_coerce_schema`), retries once with a corrective, and returns a SAFE `default` (a traced
`structured_fallback`, not a silent misroute) if the model never complies. Backend-agnostic
(rides `call_llm`). Migrated: **triage** change-type (default feature), **critic** verdict
(no longer silently fails-open on the FIRST malformed JSON — it retries), **design_qa** verdict
(vision, default MISALIGNED). **NOT migrated** — `===NEEDS_INPUT===` and the QA GO/NO-GO verdict
are markers embedded in a LARGE produced artifact (and QA's verdict is informational, not
routing), so they keep robust marker extraction. **Rule: structured output for pure-decision
calls; robust markers for marker-in-artifact calls.** Pinned by `tests/test_structured.py`.

**Agent-to-agent (synchronous):**
- Peer questions resolved via `consult(target, question, context)` — lightweight LLM call, no artifact; answer added to `qa_log`, then the work call is retried
- Hard cap: **10 total agent-to-agent calls** per agent (`MAX_AGENT_INTERACTIONS`)
- If cap reached, remaining agent questions are **escalated to CEO** automatically

**Agent-to-CEO (graph interrupt):**
- If the agent has CEO questions (original or escalated), it sets `ceo_qa_pending` and returns
- Graph routes to `ceo_qa` node, `interrupt_before` fires, pipeline pauses
- `main.py` prints the questions, reads CEO's answer, calls `graph.update_state({"ceo_qa_answer": answer})`
- Pipeline resumes; `ceo_qa` node stores the answer in `qa_log` and routes back to the asking agent
- Hard cap: **10 CEO Q&A rounds** per agent (`MAX_QA_ROUNDS`)

**Q&A state fields:**
```
qa_log            — all Q&A entries: [{from, to, question, answer, round}]
qa_rounds         — CEO rounds used per agent
agent_qa_counts   — agent-to-agent calls made per agent
ceo_qa_pending    — question text waiting for CEO
ceo_qa_from       — which agent is waiting (routing uses this)
ceo_qa_answer     — CEO's answer, injected by main.py
```

## Adding a new agent (complete checklist)

1. `agents/<name>.py` — `run(state) -> dict` returning `run_with_qa(state, "<name>", _do_work, consultable_agents=CONSULT)`, plus `_do_work(state, qa_log, rounds, allow_clarify=True) -> dict` that calls `work_call(...)` and returns `{"_clarify": questions}` when blocked
2. `prompts/<name>.txt` — identity (5–8 lines)
3. `skills/<name>.md` — domain knowledge
4. `graph/state.py` — add path/flag fields if needed
5. `graph/graph.py` — add import, `add_node`, replace one existing `add_conditional_edges`, add new one, add agent to `ceo_qa_return_routing` options

No other files change.

## Design rules to preserve

- State holds **paths only** for artifacts — never file content in state
- **One LLM call per agent work phase** — no multi-turn history, no conversation threads between agents
- Tools run **after** LLM output, append warnings to artifact — don't re-call LLM
- `graph/graph.py` is the **only** file that defines edges
- Engineer retry loop is hard-capped at `MAX_FIX_ATTEMPTS = 3`
- Pipeline **always completes** — DevOps emits dry-run manifest even if tests fail
- Agent-to-agent cap is hard at 10 — overflow escalates to CEO, never silently dropped
- **TDD invariant:** `test_author` owns `tests/`; engineer must never write/edit it (enforced in `engineer._parse_and_write_files`)
- **PR only after approval:** PR creation lives in `ship.py`, gated by `pr_gate` — never auto-open
- Critic loop bounded by `MAX_REVIEW_ATTEMPTS=2`; approval rejects loop back with `review_notes`
- When adding a graph node, re-run the routing simulation mentally for all loop/reject/escalate paths
- **Always update README.md, ARCHITECTURE.md, and CLAUDE.md when making code changes**

## Workspace layout

```
workspace/<project-id>/
  prd/       — ceo_brief.md, prd.md
  design/    — design_spec.md, mockup.html, tech_spec.md (repo_map.md in extend mode)
  src/       — generated application code
  tests/     — authoritative tests (Test Author, TDD), qa_report.md
  deploy/    — Dockerfile, docker-compose.yml, .github/workflows/deploy.yml
```
