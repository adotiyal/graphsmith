# AgentPlatform — Improvement Plan

> **Historical record (point-in-time).** Most items here are now shipped — for the
> *current* design and status see `ARCHITECTURE.md`, `CLAUDE.md`, and `AI_NATIVE_ROADMAP.md`.

**Goal:** evolve from a clean waterfall *scaffolding* tool into a system that produces
**production-grade features** that can rival a strong human team.

**Optimization stance:** quality first. We spend tokens where they buy correctness
(reasoning, iteration, independent verification) and claw the cost back where it is
pure waste (prompt caching, redundant calls, ceremony). Net cost can stay flat or
*drop* even as quality rises.

This is a planning document. No code has been changed. Items are ordered by
leverage-per-effort. Each phase is independently shippable.

---

## The core problem we are solving

Today the system has three structural ceilings that cap quality below a human team,
regardless of prompt quality:

1. **It is a waterfall.** The only feedback loop is Engineer⟷QA. A wrong PRD
   propagates all the way down and is never caught.
2. **QA marks its own homework.** The Engineer writes both the code and the tests
   that validate it. Passing tests prove self-consistency, not correctness against intent.
3. **Hard caps starve quality.** 4096 output tokens for an entire app, 6000-char
   reads, 3000-char skills — agents often cannot see or emit enough to do real work.

Everything below attacks these three, plus the cost discipline you asked for.

---

## Phase 0 — Foundation fixes ✅ DONE

These are mostly mechanical and pay for the rest of the plan in token savings.

**Status: implemented.** 0.1 prompt caching (`tools/llm.py`), 0.2 folded the Q&A
probe into the work call via the `NEEDS_INPUT` protocol (`tools/qa_utils.py` + all
agents), 0.3 three model tiers with Architect on Opus and Engineer output at 8192
(`tools/llm.py`, `agents/architect.py`), 0.4 raised context caps to 24000 read /
8000 skill (`tools/file_io.py`).

### 0.1 Turn on prompt caching
- **What:** In `tools/llm.py`, send the system block (identity + skill) as a cached
  block via `cache_control`. The system prompt is stable per agent and is currently
  re-sent in full on every call — including twice per agent run (Q&A probe + work)
  and on every Engineer retry.
- **Why:** Cache reads are ~0.1× input cost. This is the single biggest cost win and
  needs ~10 lines.
- **Files:** `tools/llm.py`
- **Cost impact:** large input-token reduction. **Quality impact:** none (pure savings).
- **Risk:** low.

### 0.2 Fold the Q&A probe into the work call
- **What:** Today every agent makes an *extra* Haiku call (`_generate_questions`) just
  to ask "any questions?" — usually returning `false`. Replace it: have the main work
  call return a structured response containing *both* any clarifying questions *and* the
  artifact. Only pause if questions came back.
- **Why:** Removes ~5 wasted calls per run with no loss of capability.
- **Files:** `tools/qa_utils.py`, each agent's `_do_work`
- **Cost impact:** ~5 fewer calls/run. **Quality impact:** neutral.
- **Risk:** medium (touches the Q&A control flow we just built).

### 0.3 Fix model + token allocation (it is currently inverted)
- **What:**
  - Route **Architect** to a stronger model. Its own docstring calls it "the
    highest-risk node," yet it runs on Haiku. Decomposition and API contracts are the
    highest-leverage reasoning in the system.
  - Raise `MAX_TOKENS["strong"]` for the Engineer — 4096 cannot emit a real app.
  - Introduce a third tier (e.g. `reason`) so we can put an Opus-class model on
    Architect and the future Critic without over-paying elsewhere.
- **Why:** quality is gated by the *weakest model on the highest-leverage step*.
- **Files:** `tools/llm.py`, `agents/architect.py`
- **Cost impact:** moderate increase, largely offset by 0.1. **Quality impact:** high.
- **Risk:** low.

### 0.4 Stop silently truncating context
- **What:** Raise `MAX_READ_CHARS` (6000) and the skills cap (3000), or make them
  per-call. The Engineer literally cannot see a full tech spec for a non-trivial
  feature today. Truncation should be a last resort, not the default.
- **Why:** correctness requires seeing the whole spec.
- **Files:** `tools/file_io.py`
- **Cost impact:** small increase. **Quality impact:** high. **Risk:** low.

---

## Phase 1 — Quality loops ✅ DONE

This phase converts the waterfall into an iterative system with independent
verification. It is the heart of the plan.

**Status: implemented.** 1.1 independent TDD Test Author (`agents/test_author.py`)
writing tests before the engineer, who must pass them and cannot edit `tests/`;
1.2 Critic gate (`agents/critic.py`) reviewing the tech spec on Opus with bounded
retry then CEO escalation; 1.3 blocking PRD + PR approval gates (`agents/prd_gate.py`,
`agents/pr_gate.py`) with reject-loops, and PR creation moved to `agents/ship.py`
behind the gate; 1.4 engineer consumes the authoritative tests and continues past
truncation. Graph rewired in `graph/graph.py`; routing verified by simulation across
happy / PRD-reject / critic-retry / critic-escalate / test-fail / PR-reject /
tests-never-pass — all reach END.

### 1.1 Independent test authoring (kill the "homework" problem)
- **What:** Add a **Test agent** (or move test authorship into QA) that writes tests
  *from the PRD acceptance criteria*, independent of the Engineer. The Engineer then
  must make those tests pass — it no longer writes the tests that judge it.
- **Why:** This is the largest single correctness gain. Tests become an oracle for
  *intent*, not a mirror of the implementation.
- **Files:** new `agents/test_author.py`, `prompts/`, `skills/`, `graph/graph.py`,
  `agents/engineer.py` (consume external tests), `agents/qa.py`
- **Cost impact:** +1 agent. **Quality impact:** very high. **Risk:** medium.

### 1.2 Per-stage critic / review gates (break the waterfall)
- **What:** After each major artifact, a **Critic** checks it against the upstream
  artifact and the acceptance criteria: *does the PRD satisfy the brief? does the spec
  cover every user story? does the design match the PRD?* If gaps are found, loop back
  once (bounded, mirroring the existing `MAX_FIX_ATTEMPTS` pattern).
- **Why:** Introduces the iteration that waterfall lacks and that human teams rely on
  for quality. Catches errors at the cheapest point — before they propagate.
- **Files:** new `agents/critic.py` (generic, parameterized by stage), `graph/graph.py`
  (bounded review loops), `graph/state.py` (review counters)
- **Cost impact:** +1 call per reviewed stage (bounded). **Quality impact:** very high.
- **Risk:** medium — needs careful loop bounding to avoid ping-pong.

### 1.3 CEO approval gates on the artifacts that matter
- **What:** Extend the existing `ceo_qa` interrupt machinery to add *approval* gates:
  CEO reviews and can edit/approve the **PRD** and the **final PR** before it ships.
  Today the pipeline interrupts the CEO for trivia mid-flight but auto-opens a PR
  with no human sign-off.
- **Why:** production shipping needs a human gate at the high-stakes points. Reuses
  infrastructure we already built.
- **Files:** `graph/graph.py`, `main.py`, `agents/qa.py` (don't auto-push before approval)
- **Cost impact:** none. **Quality impact:** high (prevents bad ships). **Risk:** low.

### 1.4 Decompose Engineer output
- **What:** Replace the single all-files call with module-by-module generation driven
  by the Architect's file list, each with its own budget; assemble and then test.
- **Why:** removes the 4K-token ceiling on the whole app; lets each file get real depth.
- **Files:** `agents/engineer.py`
- **Cost impact:** more Engineer tokens (intended — quality first). **Quality impact:**
  high. **Risk:** medium (orchestration + consistency across files).

---

## Validation — test suites + smoke-test (before Phase 2) ✅ DONE

Two test suites added under `tests/` (52 passed, 3 skipped; LLM + Docker mocked):
- **Set 1 — architecture:** `test_architecture.py` (graph compiles, every routing fn,
  Q&A caps/escalation/force-proceed, parsers, gate/ceo_qa nodes) + `test_integration.py`
  (drives the real graph CEO→END, incl. the PRD-reject loop).
- **Set 2 — agents:** `test_agents.py` — each agent's inputs/outputs + the "never blocked,
  always escalates to CEO" guarantee. `test_live_eval.py` adds real-LLM quality checks
  (skipped without a key).

Surfaced and fixed a **release blocker**: `SqliteSaver.from_conn_string()` returns a
context manager in current LangGraph, so `build_graph()` crashed on compile — the live
run would have died immediately. Also added **DevOps escalation** (the one agent that
could be blocked with no path to the CEO) per the "never blocked" principle.

Human-worker review of every agent → `AGENT_AUDIT.md`. Live-run steps → `SMOKE_TEST.md`.

**Audit decisions applied (CEO = CTO).** The human is both CEO and CTO (`skills/ceo.md`),
the universal unblocker for business and technical calls. This resolved two findings:
- **#2 (stack):** no longer hardcoded. The architect proposes a default (FastAPI +
  Next.js + Postgres) and the **CTO confirms/changes it before commit** via a mandatory
  `ceo_qa` escalation (`architect._ask_stack`); the choice is recorded in `tech_stack`.
- **#9 (escalation):** agent-to-agent Q&A caps at 3 rounds → any remaining blocker
  escalates to the human CEO/CTO, who resolves it (wording updated in `qa_utils`/`main`).
Tests grew to 55 passed / 3 skipped.

## 1→10 ergonomics — triage + stack persistence  ✅ DONE

From the architecture review ("can it ship like a startup 0→1 and 1→10?"). Most 1→10 work
is fixes/refactors/chores, not net-new features — so:
- **Triage router** (`agents/triage.py`): classifies the request → `change_type`; feature
  takes the full pipeline, bugfix/refactor/chore take a **quick lane** (Engineer ⇄ QA →
  PR gate → Ship, skipping PRD/design/architecture/TDD + DevOps). Engineer is change-type
  aware and works from the brief when there's no tech spec.
- **Stack persistence**: the confirmed greenfield stack is saved to `product/stack.md`
  and reused, so the architect stops re-asking it every feature.

**Project continuity ✅ DONE:** one persistent project at `workspace/project/`
(`tools/project_ctx.py`) — first run seeds it, later runs auto-extend it (no `--repo`); a
**feature ledger** records each feature and is fed to PM/Design/Architect; the architect
reuses the persisted stack for the managed project. So *build → verify → add next → repeat*
against the same product now works, with the agents accumulating code + history.

**Still open for full 0→10 (from the review):** autonomy/trust modes to reduce per-run
gating; a real deploy→telemetry→backlog loop; namespacing the growing `ProjectState`;
multiple named projects (today: one at a time).

## Phase 4 — Run-and-verify (the app must actually RUN, not just compile)

The system generated config but never executed it; tests were pytest-only so the
default full-stack (FastAPI + Next.js + Postgres) couldn't be verified end-to-end.
Three slices, CEO/CTO-chosen design: deterministic Playwright e2e (not agentic browser),
a new integration stage **after QA / before the PR gate** (failure loops to Engineer),
pinned slim/alpine images (no floating `:latest`).

### 4.1 Stack-matched test toolchain + dockerized default  ✅ DONE
- **What:** `registry.detect_toolchains()` finds each testable layer by language marker
  (root + immediate subdirs, so flat *and* `backend/`+`frontend/` layouts work) and
  `run_project_tests()` runs the matching tool per layer and aggregates: **pytest** in
  `python:3.12-slim` (backend), **vitest/jest** in `node:22-alpine` (frontend). Falls back
  to legacy pytest when nothing is detected (extend mode keeps working). Engineer + QA now
  call the dispatcher. Default stack made explicit (dockerized, pinned slim, per-layer test
  tools) in `architect.DEFAULT_STACK` + persisted `product/stack.md`; engineer/test_author/
  devops skills updated (frontend tests now REQUIRED; pinned-image policy). Images pinned:
  `python:3.12-slim`, `node:22-alpine`, `postgres:17-alpine`.
- **Files:** `tools/registry.py`, `agents/engineer.py`, `agents/architect.py`,
  `skills/{engineer,test_author,devops}.md`, `tests/conftest.py`, tests.
- **This replaces** the old "stack-agnostic skill packs + toolchain-detecting test runner"
  open item — by standardizing on ONE well-supported default stack rather than detecting
  arbitrary ones.

### 4.2 Integration stage — bring the stack UP + smoke  ✅ DONE
- **What:** new deterministic node (`agents/integration.py`, no LLM) after QA, before
  `pr_gate`: `registry.run_compose_integration()` runs `docker compose -p agentplatform-it
  up -d --build` on the app's OWN compose file, polls `compose ps` until every service is
  running/healthy, smoke-checks the conventional endpoints (api `GET :8000/health`,
  frontend `GET :3000/` — service names standardized via skills/engineer.md), captures
  service logs on failure, and ALWAYS tears down (`down -v`). Failure → loops to Engineer
  with the compose/smoke/e2e log in `error_log` (the channel the test loop already uses),
  bounded by `MAX_INTEGRATION_ATTEMPTS=2`, then proceeds to the gate with the red report
  visible (pipeline always completes). External `--repo` without a compose file skips
  gracefully; the managed project MUST ship compose (engineer skill + prompt enforce).
  Report written to `tests/integration_report.md`.
- **Files:** `tools/registry.py`, `agents/integration.py`, `graph/{graph,state}.py`,
  `skills/engineer.md`, `agents/engineer.py`, `main.py`/`live_run.py`, `tests/conftest.py`
  (autouse `no_compose` stub) + routing/behavior tests.

### 4.3 Playwright e2e user-flow + loop-back  ✅ DONE (QA authors the specs)
- **What:** **QA** (not Test Author — CEO/CTO decision) authors the Playwright specs in
  its pass path, AFTER reviewing the code: flows derive from the feature request + PRD
  acceptance criteria (intent), the reviewed code is used ONLY for selectors/routes. This
  keeps the user-flow oracle independent of the implementer (Engineer ≠ QA) while letting
  specs target the real DOM — pre-implementation Playwright would guess selectors blind.
  Specs land in `e2e/*.spec.ts` (engineer is BLOCKED from writing `tests/` AND `e2e/`);
  the integration stage runs them in the pinned `mcr.microsoft.com/playwright` image on
  the compose network (`E2E_BASE_URL=http://frontend:3000`). Backend-only features skip
  authoring (no node layer detected) — the API smoke still covers them. Authoring is
  best-effort: one retry on a spec-less result, then proceed (never blocks the pipeline).
- **Files:** `agents/qa.py` (`_author_e2e_specs`, strong tier), `agents/engineer.py`
  (e2e protection), `tools/registry.py` (`_run_e2e`), `graph/state.py` (`e2e_files`).

## Overseeing the orchestration — evals + overseer (slice 1)  ✅ DONE

With the human mostly out of the loop, two systems watch the agents:
- **Tracing** (`tools/trace.py`): every run → `traces/<id>.jsonl` (nodes + LLM
  calls/tokens/latency, hooked into `call_llm`). Observability foundation.
- **Overseer** (`evals/overseer.py`, runs at the end of every run): deterministic
  invariants (engineer-didn't-touch-tests, feature-has-PRD, stack-confirmed,
  no-silent-red-ship), loop non-convergence, token/call budget → "NEEDS HUMAN REVIEW" on
  a high-severity finding.
- **Triage accuracy eval** (`evals/triage_eval.py` + `datasets/triage.jsonl`): the proof
  the harness works; `evaluate()` testable, `run_live()` runs the real agent.

**Next (documented in `evals/README.md`):** per-agent rubric LLM-judge evals (reuse
critics/validators as graders, run K× for variance), end-to-end golden tasks with a
held-out acceptance oracle, an LLM overseer over the trajectory, per-node real-time
halting, confidence-gated selective human review, CI regression gate on eval scores.

## Phase 2 — Compounding (a real company gets better over time)

### 2.1 Codebase awareness — extend real code, not greenfield  ✅ DONE (v1)
- **What:** Let the pipeline target an existing repository: give agents read access
  (grep/read), and have the Engineer emit changes against existing files rather than
  whole new projects in `workspace/<id>/`.
- **Slice 1 — DONE (understanding + planning):** `tools/repo.py` (read-only list/grep/
  read/repo-map + guarded writer); `agents/surveyor.py` (maps the repo + detects stack;
  no-op in greenfield) wired after the PRD gate; architect proposes the **detected** stack
  at the CTO gate and folds the integration brief into the spec; `--repo` flag in
  `main.py`; state fields `target_repo`/`repo_map_path`/`detected_stack`. 14 new tests
  (`test_repo.py` + surveyor/architect-extend + an extend-mode graph run). Greenfield is
  unchanged.
- **Slice 2 — DONE (write-back):** test-author + engineer write **into the target repo**
  at real paths (guarded by `repo.write_into_repo`); engineer reads the repo map + the
  existing files the spec references, runs the repo's **own** test suite in Docker
  (`test_path=""`), skips the linter on pre-existing code, and never clobbers
  `state["test_files"]`. Meta-artifacts stay in `workspace/<id>/`. (72 tests pass.)
- **Files:** `tools/repo.py`, `agents/surveyor.py`, `agents/architect.py`, `graph/*`,
  `main.py`, `agents/engineer.py`, `agents/test_author.py`, `tools/file_io.py` (`code_root`),
  `tools/registry.py` (`run_tests_in_docker(test_path=)`).
- **Minimal diffs — DONE:** existing files are now patched via `===EDIT:===`
  search/replace blocks applied by `repo.apply_edit` (unique-match-or-fail), instead of
  full-file rewrites. New files still written whole.
- **Remaining v1 caveats (next):** engineer reads only spec-referenced files (no broad
  semantic retrieval); runs the whole repo test suite (could be slow/flaky on large repos).

### Quality follow-ups (post-2.x, from AGENT_AUDIT)  ✅ #5, design upgrade DONE
- **#5 QA reads the code — DONE:** QA reviews the engineer's written files against the
  acceptance criteria in its sign-off (`state["code_files"]`); findings surface at the PR gate.
- **#4 Design upgrade — DONE (thinking, not pixels):** standing **product profile**
  (`tools/product.py`, set once by CEO/CTO, persists across features) feeds Design + PM;
  design now does **discovery** (users/JTBD/brand/goal), asks the CEO/CTO for gaps, gives
  **rationale**, designs **first-run + unhappy paths**, writes **microcopy**, runs on
  `strong`, and is gated by a **design critic** (`critic_design`, generic critic + design
  review-focus). Consumer-app skill rewrite. **Visual mockup — DONE:** Design also emits a
  self-contained `design/mockup.html` (Tailwind CDN) of the key screens + states with real
  microcopy (skipped for backend-only). (perf/a11y QA review still TODO.)
- **Cost impact:** higher (reads existing code; mitigated by caching + targeted reads).
  **Quality impact:** transformational. **Risk:** high.

### 2.2 Cross-run learning  ✅ DONE
- **What:** Feed recurring QA/test failure patterns back into `skills/*.md` (or a
  separate learnings store the agents read). The system should not repeat the same
  mistakes across features.
- **Why:** turns static skills into a compounding asset — the "gets smarter over time"
  claim becomes real instead of manual.
- **Files:** new `tools/learnings.py`, `agents/qa.py`, `tools/file_io.py`
- **Cost impact:** small. **Quality impact:** compounds. **Risk:** medium.

### 2.3 Execution hardening  ✅ DONE
- **What:** Pin/verify dependencies before Docker runs; sandbox more tightly; require
  approval before any `git push`/`gh pr create` (ties to 1.3).
- **Why:** production safety. Generated code currently runs and pushes with minimal guardrails.
- **Files:** `tools/registry.py`, `agents/qa.py`
- **Cost impact:** none. **Quality impact:** safety. **Risk:** low.

---

## Phase 3 — Scale & latency (after quality is proven)

- **3.1 Parallelism** — run independent steps (e.g. Design and parts of Architect, or
  independent file generation) concurrently via LangGraph's `Send` API. Latency, not quality.
- **3.2 RAG over skills** — when skills outgrow the context budget, retrieve relevant
  chunks instead of truncating. Quality + token efficiency at scale.

---

## Token budget: net effect

Quality-first does **not** mean expensive. Rough directional view per feature:

| Change | Direction |
|---|---|
| 0.1 Prompt caching | ⬇⬇ large input savings |
| 0.2 Kill Q&A probe call | ⬇ ~5 fewer calls/run |
| 0.3 Stronger Architect / bigger Engineer | ⬆ moderate |
| 1.1 Independent tests | ⬆ +1 agent |
| 1.2 Critic loops | ⬆ bounded +1/stage |
| 1.4 Decomposed Engineer | ⬆ intended spend |

**Net:** caching + removing the probe call typically offset the added quality calls.
The system can become *higher quality at similar or lower cost*. The principle: cut
ceremony, spend on correctness.

---

## Can it then beat a human team?

With Phase 0–1 it reaches **"ships small/medium production features with light human
review"** — competitive with a solid mid-level team on well-scoped work.

With Phase 2 (codebase awareness + learning) it gains the thing humans have that the
current design lacks: **compounding on an existing product**. That is the threshold
where "better than a human team" becomes a real claim for a meaningful class of work —
high-volume, well-specified features on an established codebase, done faster and more
consistently than humans, with a human gate at the high-stakes points.

It will **not** soon beat a senior team on ambiguous, novel, or deeply cross-cutting
work — that is where human judgment still dominates. The right framing is **augmentation
that approaches replacement on the well-specified middle of the distribution**, not
wholesale replacement.

---

## Suggested execution order

1. **Phase 0** in one pass (foundation; pays for itself).
2. **1.1 + 1.2** together (the quality core; biggest correctness jump).
3. **1.3 + 1.4** (shipping safety + output depth).
4. **Phase 2** as a larger, separate effort (the compounding leap).
5. **Phase 3** once quality is proven and volume justifies it.

> Per project convention: when any of this is implemented, update
> `README.md`, `ARCHITECTURE.md`, and `CLAUDE.md` in the same change.

---

# Phase 5 — Improvement Backlog (prioritized, workflow-consumable)

## The improvement workflow (agreed 2026-06-12)
Session-driven, one item at a time, executed by the operator (Claude) with the CEO/CTO
at decision points:
1. **Pick** the top OPEN item (P0 → P1 → P2; P3 only by explicit CEO request).
2. **Implement** with regression tests; `pytest tests/` must be GREEN before proceeding.
3. **Verify** against the item's own *Verification* criteria (mocked first; live run when
   the criteria demand one).
4. **Close**: set Status → DONE (date), update README/ARCHITECTURE/CLAUDE.md + memory in
   the same session (standing rule).
5. **Re-prioritize** if the item surfaced new findings — new items enter with evidence,
   effort, impact, verification, like every item here.
Next session starts with **I2** (15-min active degradation) then **I1** (class-killer).

Source: the 2026-06-12 end-to-end test (todo + due dates, clean slate → TRUSTWORTHY) plus
the CTO/CEO/User three-role review. Every item carries evidence from live runs.

**Schema:** each item = ID · problem (evidence) · fix · files · effort (S/M/L) · impact ·
**verification** (how the improvement workflow proves it done) · status.
**Ordering rule:** leverage-per-effort first; items with low cost/time benefit *today* sit
in P3 regardless of conceptual appeal.

## P0 — class-killers (do first)

### I1 — Structured tool-call outputs for code-writing agents
- **Problem:** the `===FILE/EDIT/DELETE===` text format caused EVERY mechanical failure
  class observed live: duplicate-block file corruption (5 files), the stale-SEARCH plague
  (≥6 burned attempts across runs), silent no-op rounds, destructive "re-emit" rounds.
  30–40% of engineer attempts died on format mechanics, not engineering.
- **Fix:** define Anthropic tool schemas (`write_file`, `edit_file{path, old, new}`,
  `delete_file`) and have `call_llm` support tool-use loops; engineer (first), then
  test_author/design/qa emit operations as tool calls. Keep guards (oracle protection,
  path escapes) at the tool-executor layer — they get STRONGER (single choke point).
- **Files:** `tools/llm.py`, `agents/engineer.py` (then test_author, design, qa), tests.
- **Effort:** L (engineer slice: M). **Impact:** highest — quality + time + cost together.
- **Verification:** a mocked suite proving corruption/stale-SEARCH cases are impossible
  (duplicate emissions, stale anchors → clean failures w/ current content); one live
  quick-lane bugfix converging with zero mechanical failures.
- **Status:** DONE — engineer slice (2026-06-12). `tools/codegen.py`: file changes via
  REAL tools on a STAGING copy (claude-cli backend = Claude Code's own Read/Write/Edit;
  api backend = Messages tool-use loop, same executor), synced back through ONE guard
  (`sync_back`: oracle/kit/meta protection, escapes, deletion mirror); clarify rounds
  discard staging; `AGENT_CODEGEN=text` = legacy fallback. 17 mocked tests
  (tests/test_codegen.py) prove the failure classes structurally impossible. LIVE: the
  UTC→local overdue bugfix — 1 surgical file in 26s, then THREE retry rounds with ZERO
  mechanical failures incl. two mandated no-ops that wrote 0 files (the text era
  destroyed a working file in this exact scenario). Reds en route were a stale port
  (I6) and QA spec quality (I4) — never the engineer. Run ended ok:true TRUSTWORTHY.
  Remaining slices (test_author / design / qa still emit ===FILE=== text) → I17.
  Bonus fixes en route: `code_files` accumulates across rounds; `_cli_call` retry-once
  on error_max_turns; e2e runner `--workers=1`.

### I2 — Engineer skill exceeds MAX_SKILL_CHARS (active silent degradation)
- **Problem:** `skills/engineer.md` > 8000 chars → tail sections (SEO floor, theme wiring —
  the NEWEST mandates) are silently truncated away on every call. Observed in live logs.
- **Fix:** raise cap for skills (or per-skill override) AND tighten the skill (it has grown
  redundant); add a startup warning/test that no skill exceeds its cap.
- **Files:** `tools/file_io.py`, `skills/engineer.md`, test.
- **Effort:** S. **Impact:** high (mandates currently not reaching the engineer).
- **Verification:** test asserting every `skills/*.md` loads untruncated.
- **Status:** DONE (2026-06-12) — `MAX_SKILL_CHARS` 8000 → 16000 (safety net, never a
  budget); engineer skill's stale Vite-style frontend structure replaced with the real
  Next.js app-router + design-kit layout; guard test `test_all_skills_load_untruncated`
  fails the suite if ANY skill ever crosses the cap again. 197 passed.

### I3 — Kit-wiring ENFORCEMENT (protection ≠ usage)
- **Problem:** engineer ignored the design kit and built parallel components — the live
  microcopy gate caught 17 missing strings. Prompt-only rules don't bind.
- **Fix:** deterministic post-write check (registry): when `design_component_files` exist,
  the page/container source must import from `components/kit/`, and no non-kit component
  file may duplicate a kit component's name/role → fail → engineer with the exact rule.
- **Files:** `tools/registry.py`, `agents/engineer.py` (run check like the linter), tests.
- **Effort:** S/M. **Impact:** high — protects the whole by-construction investment.
- **Verification:** mocked tests (unwired page → fail; wired → pass); next feature run
  wires kit on attempt 1.
- **Status:** DONE (2026-06-12). `registry.check_kit_wiring` — deterministic, post-write,
  runs in the engineer before the linter whenever `design_component_files` exist:
  (1) some non-kit frontend source must import from the kit dir; (2) no non-kit file may
  duplicate a kit component's basename (parallel component). Fail → engineer retry with
  the exact rule in error_log, BEFORE any test/integration tokens. node_modules/.next/
  tests/e2e ignored. 6 mocked tests (tests/test_kit_wiring.py) incl. the full engineer
  fail→fix round-trip; live positive check: the real managed project reports WIRED.
  Remaining live criterion ("next feature run wires kit on attempt 1") rides the next
  feature run — the enforcement is in place either way.

### I4 — QA e2e spec quality pack
- **Problem (live):** specs invented testids/fields (`stats-total`, `priority`), used wrong
  API paths twice despite CEO corrections (authoring prompt never receives `qa_log`), no
  test isolation, `.check()` flake on styled checkboxes, batches accreted per pass.
  **More evidence (2026-06-12 I1 live run):** TWICE in one run QA authored
  `getByLabel(/task/i)` unions that matched the form's aria-label instead of the input
  (4 e2e failures, one full gate loop burned), plus a `.overdue` CSS-class locator that
  doesn't exist in the kit; QA also REWROTE the spec the CTO had just fixed (re-author
  clobbers human repairs — (d) must also preserve/diff, not just wipe). A second spec
  FILE also exposed cross-file DB pollution under parallel Playwright workers
  (FIXED immediately, 2026-06-12: `registry._run_e2e` now passes `--workers=1` —
  spec files share one live backend, serial is the only deterministic choice).
- **Fix:** (a) authoring prompt gets qa_log + the kit manifest's REAL testids as the ONLY
  selector source; (b) deterministic spec-lint before accepting: every `getByTestId` must
  exist in manifest/kit grep, paths must match the served openapi.json, must contain a
  cleanup `beforeEach`; (c) conventions baked in: evaluate-click for checkboxes, settle-on
  input-clear; (d) e2e/ wiped before re-authoring (no accretion); (e) integration e2e
  failure routes to **QA** (spec revision, 1 bounded round) before the engineer.
- **Files:** `agents/qa.py`, `tools/registry.py` (lint), `graph/graph.py` (routing), tests.
- **Effort:** M. **Impact:** high — e2e was the single largest loop-burner.
- **Verification:** mocked lint tests; live run where first-authored specs pass unmodified.
- **Status:** DONE (2026-06-12). Shipped: (a) authoring prompt now carries qa_log + the
  kit's REAL data-testids (static + dynamic prefixes, parsed from the kit source) as the
  ONLY selector source; (b) `registry.lint_e2e_spec` — deterministic gate (invented
  testids, label/CSS-class guessing, guessed API paths, .check() flake, missing
  isolation, stray markdown fences), comment-stripped before matching; one re-author
  with findings, then the file is DROPPED+deleted (a bad oracle never reaches
  integration); (c) conventions baked into the prompt; (d) re-passes keep existing
  specs (no clobber); (e) e2e-stage failure on a healthy app → `e2e_revision_pending` →
  ONE QA spec-revision round (assertions may not be weakened) → back to integration;
  MAX_INTEGRATION_ATTEMPTS 2→3 so the engineer keeps two real shots. 16 mocked tests.
  LIVE: first-authored spec was lint-clean on pass 1 (78s) and mechanically perfect —
  kit testids only, beforeEach API cleanup, evaluate-clicks, exact /api paths; 4/5
  tests passed unmodified; the 5th (hover-revealed delete button needs evaluate-click)
  was fixed surgically by the live (e) revision round in 45s; final live integration
  20/20. Live testing also caught+fixed two latent bugs: lint flagged conventions
  CITED IN COMMENTS (now comment-stripped) and a revision wrapped the file in
  ```markdown fences → SyntaxError (writer now strips fences; revision path now
  runs the lint gate too). Known leftover: pre-I4 spec bugfix-test-layers has a
  flaky stats-count test (fails ~1 in 3 runs) — candidate for a one-off revision.

## P1 — high value, small/medium effort

### I5 — Kill the remaining redundant CEO asks
- **Problem:** PM still asks the stack every run (persisted stack not in its context);
  DevOps asked the same deploy question twice in one run.
- **Fix:** inject persisted stack + "already-answered" qa_log digest into PM/DevOps
  prompts; DevOps repeat-guard (same-topic question already answered → proceed).
- **Files:** `agents/pm.py`, `agents/devops.py`, tests. **Effort:** S. **Impact:** medium
  (latency + CEO attention). **Verification:** live run with zero stack/deploy re-asks.
- **Status:** OPEN

### I6 — Integration robustness pack
- **Problem (live):** stale compose stack on :8000 failed a run (port collision); design-QA
  screenshot of an EMPTY app made populated-state unverifiable; one global MAX_FIX_ATTEMPTS
  is too tight for greenfield full-stack.
- **Fix:** pre-clean conflicting compose projects before `up`; seed sample tasks (incl.
  overdue + completed) via the app's own API before the screenshot; raise/per-layer fix
  attempts (e.g. 4 for greenfield full lane).
- **Files:** `tools/registry.py`, `agents/integration.py`, `graph/graph.py`, tests.
- **Effort:** S/M. **Impact:** medium-high. **Verification:** gauntlet passes from a dirty
  port state; design-QA screenshot shows populated state automatically.
- **Status:** MOSTLY DONE (2026-06-12, pre-e2e hardening): (a) pre-clean — `run_compose_integration`
  now `down -v --remove-orphans` BEFORE `up` and fails fast with an "environment, not code"
  message when a FOREIGN process holds :8000/:3000 (`_foreign_port_holders`, lsof) — the
  engineer never sees a bind error as a code bug again; (b) screenshot seeding —
  `seed_app_data()` discovers the main POST collection from the app's own openapi.json and
  posts 3 entities (OVERDUE / due-soon / later; retries while uvicorn warms); LIVE-verified
  against the real app (3 rows, correct dates). Also: the known-flaky TS stats test was
  robustified (button-click + settle waits, longer timeouts) and critic_design now knows the
  '## Design Directions'/'## Chosen Direction' spec sections (won't flag unchosen options).
  REMAINING (open): per-layer/raised fix attempts for greenfield full lane.

### I7 — Engineer dependency lock
- **Problem (live):** hallucinated react-query/react-hook-form/lucide architectures; deps
  appear in imports but not package.json (or vice versa).
- **Fix:** deterministic check: imports ⊆ declared deps; NEW deps require an explicit
  `===DEPS===`/tool-call declaration that surfaces in the QA report + PR gate.
- **Files:** `tools/registry.py`, `agents/engineer.py`, tests. **Effort:** S/M.
- **Impact:** medium-high (kills the drift class). **Verification:** mocked tests; QA
  report lists any new dep.
- **Status:** OPEN

### I8 — Close colocated-test protection gap
- **Problem (live):** engineer's own `*.test.tsx`/`__tests__/` files dodge oracle-path
  protection, fail the vitest layer, and once sat INSIDE kit/.
- **Fix:** engineer may only write tests under its designated colocated dirs and they run
  in a separate "engineer-tests" bucket that can't block the oracle verdict (or: forbid
  engineer test files entirely — test_author owns frontend tests too).
- **Files:** `agents/engineer.py`, `tools/registry.py`, tests. **Effort:** S/M.
- **Impact:** medium. **Verification:** mocked tests for the placement rules.
- **Status:** OPEN

### I9 — Latency pack
- **Problem:** ~70% of feature wall-time = serial docker builds + serial agent calls
  (~60 min/feature observed).
- **Fix:** BuildKit layer caching flags on compose builds; run design's mockup + kit calls
  in parallel (LangGraph Send or asyncio); skip per-layer unit reruns when a layer's files
  are unchanged (hash manifest).
- **Files:** `tools/registry.py`, `agents/design.py`, `graph/graph.py`. **Effort:** M.
- **Impact:** time (60→~25 min target), no quality change. **Verification:** timed live run.
- **Status:** OPEN

### I10 — Autonomy-rate metric (the number the company manages)
- **Problem:** autonomy is the real cost driver (operator hours dwarf API cost) and is
  currently unmeasured (~0% on the e2e).
- **Fix:** overseer counts human interventions (gate rejects w/ content fixes, manual file
  edits detected via git diff against agent-written set, CTO answers beyond approvals) →
  `autonomy_rate` per run in the trace + overseer report.
- **Files:** `evals/overseer.py`, `tools/trace.py`, tests. **Effort:** S/M.
- **Impact:** strategic — makes every other item measurable. **Verification:** metric
  appears in DONE report; backfilled definition documented.
- **Status:** OPEN

### I17 — Extend structured tool-call outputs beyond the engineer
- **Problem:** I1's engineer slice is live-verified, but test_author, design (kit/mockup),
  and QA (e2e specs) still emit ===FILE=== text — same mechanical-failure exposure
  (QA's spec re-author clobber in the I1 live run is the standing example).
- **Fix:** route their file emission through `codegen.generate` (each with its own
  protected-path predicate); delete the text parsers when the last consumer migrates.
- **Files:** `agents/test_author.py`, `agents/design.py`, `agents/qa.py`, tests.
- **Effort:** M. **Impact:** M/high — finishes killing the class.
- **Verification:** mocked per-agent wiring tests; one live full-lane feature with all
  four agents on the tools path, zero mechanical failures.
- **Status:** DONE (2026-06-15) — `codegen.generate_in_domain` (inverted domain guard) routes test_author (tests/), qa (e2e/), and design (kit/) through the tools path. Read-before-write is structural: live test_author preserved an existing conftest.py while adding a new test; no fences, no blind overwrite. AGENT_CODEGEN=text = fallback. 274 passed.

## P2 — quality breadth (after P0/P1)

### I11 — Human `verify` stage before the PR gate
Leave the stack up + print a clickable checklist (URLs + ACs) at the gate. Files:
`agents/integration.py`/new node, drivers. Effort: S/M. Impact: trust + UX of the
platform itself. Verification: gate pause shows checklist; stack reachable. **OPEN**

### I12 — Mobile-viewport design QA
Screenshot 375px alongside 1280px; judge against the mockup's mobile frames (one extra
vision image, same single-shot call). Effort: S/M. Impact: closes the unverified half of
the dual-surface mandate. Verification: ALIGNED verdict cites both frames. **OPEN**

### I13 — Accessibility gate (axe-core in the Playwright container)
Free-ish deterministic a11y floor (critical violations fail). Effort: M. Impact: quality
breadth; consumer mandate. Verification: gate line in integration report. **OPEN**

### I14 — Lighthouse/CWV budget gate
Performance floor (LCP/CLS budgets) via Lighthouse CI in the e2e container. Effort: M.
Impact: consumer-grade performance discipline. Verification: report line + budget file.
**OPEN**

### I15 — Destructive-action UX pattern (undo)
Design-skill pattern: destructive actions need undo affordance (toast w/ undo) — the
delete-with-no-undo gap a user would feel first. Effort: S (skill) + flows through next
features. Verification: next feature's spec includes the pattern. **OPEN**

### I16 — App observability conventions
Engineer skill: structured logging (request logs, error logs w/ context) + /health detail.
Effort: S. Impact: operability of SHIPPED apps. Verification: logs visible in integration
service-log tail. **OPEN**

### I18 — Port legacy TypeScript e2e specs to Python
- **Problem:** e2e language is hardwired to Python (CTO decision 2026-06-12), but the
  todo app's 3 original specs are @playwright/test TypeScript — two toolchains run at
  integration until they're ported (works, but slower + double maintenance).
- **Fix:** port bugfix-test-layers / overdue-local-date / todo specs to pytest-playwright,
  delete the TS runner path (`registry._run_e2e_ts`).
- **Effort:** S/M. **Impact:** low/medium (cleanup + ~1 min less per integration run).
- **Verification:** live integration green on Python-only specs; TS path deleted.
- **Status:** OPEN

## P3 — parked (low cost/time benefit today — revisit when hosting/scale is real)

- **I17 i18n readiness** (string extraction convention) — expensive retrofit avoided
  later, but zero user value pre-launch.
- **I18 Hosted-readiness pack** (auth, DB backups, alerting, privacy page) — meaningless
  until a real deploy target exists.
- **I19 Multi-state vision QA** (dark-mode + interaction-state screenshots) — adds vision
  cost; current light/desktop single-shot + deterministic theme floor covers the bulk.
- **I20 live_run sleep-proofing** (caffeinate/daemonize) — resume already works; pure
  convenience.
