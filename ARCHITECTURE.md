# Architecture

## Purpose

Graphsmith is a multi-agent pipeline built on LangGraph that automates the
full lifecycle of building a SaaS feature — from your plain-language requirement
to deployed infrastructure configuration. You act as **CEO and CTO** — the single human
authority over both business scope and technical direction (the tech stack is yours to
finalize). Every other role (PM, Designer, Architect, Engineer, QA, DevOps) is an AI agent.

The system is designed around three constraints:
- **Minimal tokens** — state holds paths, not content; two models (Opus=think, Sonnet=code)
- **KISS** — one LLM call per agent work phase, flat state schema, no conversation history
- **Extensible** — adding an agent touches 4 new files and a few lines in one existing file

---

## Directory Structure

```
graphsmith/
│
├── main.py                  Entry point. Your CEO interface.
├── requirements.txt
├── README.md
├── ARCHITECTURE.md          This file.
│
├── graph/
│   ├── state.py             Shared state schema (TypedDict, paths + Q&A fields)
│   └── graph.py             Pipeline wiring — the only file that knows agent order
│
├── agents/
│   ├── ceo.py               Formats your input into a structured brief
│   ├── ceo_qa.py            Shared interrupt node — stores CEO answers into qa_log
│   ├── triage.py            Classifies the request → full feature lane vs quick lane
│   ├── pm.py                Expands brief into PRD with user stories + criteria
│   ├── prd_gate.py          Blocking CEO approval of the PRD (Phase 1.3)
│   ├── surveyor.py          Maps an existing repo in extend mode; no-op greenfield (2.1)
│   ├── design.py            Produces UI/UX component spec
│   ├── architect.py         Produces technical spec (data models, API, file structure)
│   ├── critic.py            Reviews the tech spec vs PRD; bounded retry then escalate (1.2)
│   ├── test_author.py       Writes the authoritative test suite BEFORE the engineer (1.1)
│   ├── engineer.py          Implements code to pass the tests; runs them in Docker
│   ├── qa.py                Diagnoses failures, signs off on passing runs
│   ├── pr_gate.py           Blocking CEO approval before the PR is opened (Phase 1.3)
│   ├── ship.py              Opens the GitHub PR after CEO approval (Phase 1.3)
│   └── devops.py            Generates Dockerfile, docker-compose, GitHub Actions
│
├── prompts/
│   └── <agent>.txt          Identity prompt for each agent (5–8 lines, rarely changes)
│
├── skills/
│   └── <agent>.md           Domain knowledge for each agent (principles, patterns, rules)
│
├── tools/
│   ├── llm.py               Single LLM caller — all agents go through here
│   ├── file_io.py           All disk I/O — read/write artifacts, load prompts/skills
│   ├── registry.py          Deterministic tools: linter, test runner, validators
│   ├── qa_utils.py          Bidirectional Q&A — run_with_qa(), consult(), format_qa_context()
│   ├── repo.py              Read-only codebase access for extend mode (2.1)
│   ├── design_source.py     Reuse an external design (local dir / git URL of HTML mockups)
│   ├── learnings.py         Cross-run learning — local + committed-shared tiers, promote CLI (2.2)
│   ├── product.py           Persistent product profile + confirmed stack
│   ├── project_ctx.py       The single persistent project (workspace/project) + feature ledger
│   └── trace.py             Per-run trace (nodes + LLM calls/tokens/latency) → traces/<id>.jsonl
│
├── evals/                   Overseeing the orchestration (offline evals + online overseer)
│   ├── overseer.py          Deterministic invariants / loop / budget checks on a finished run
│   ├── triage_eval.py       Accuracy + confusion for the Triage classifier
│   └── datasets/            Labeled eval cases (triage.jsonl, …)
│
├── tests/                  Platform test suites (mocked LLM/Docker — no key needed)
│   ├── conftest.py         MockLLM, isolated workspace, Docker stubs
│   ├── test_architecture.py  Set 1: graph wiring, routing, Q&A flow, parsers
│   ├── test_integration.py   Set 1: full graph CEO→END incl. reject loop
│   ├── test_agents.py        Set 2: per-agent IO + escalation guarantee
│   ├── test_repo.py          Phase 2.1: read-only repo tools + guarded writer
│   └── test_live_eval.py     Real-LLM quality checks (skipped without API key)
│
└── workspace/
    └── <project-id>/
        ├── prd/             CEO brief + PRD
        ├── design/          design spec + mockup.html + tech spec (+ repo_map.md in extend mode)
        ├── src/             Generated application code
        ├── tests/           Authoritative tests (Test Author) + QA report
        └── deploy/          Dockerfile, docker-compose, GitHub Actions workflow
```

See `SMOKE_TEST.md` for the live-run runbook and `AGENT_AUDIT.md` for the human-worker
review of each agent (inputs/outputs/limitations).

---

## The Pipeline

```
CEO ─► Triage ─(feature)─► PM ─► [PRD gate] ─► Surveyor ─► Design ─► Critic ─► Architect ─► Critic ─► Test Author ─► Engineer ⇄ QA
      └─(bugfix/refactor/chore — QUICK lane)───────────────────────────────────────────────────────────────────► Engineer ⇄ QA
                           (no-op greenfield;       (design)            (spec)
                            maps the repo)
        ↑        │ reject              ↑  │ retry                              │
        └────────┘                     └──┘ (escalate → ceo_qa → Test Author) │
                                                                   ┌──────────┤
                                                       pass ▼      ▼ fail(<max)→ Engineer
                                                  Integration ─fail(≤2)─► Engineer
                                            (compose up + smoke + e2e)
                                                       pass ▼  (or cap hit, red report)
                                                   Design QA ─misaligned─► Engineer
                                            (vision: live app vs mockup, single-shot)
                                                       pass ▼
                                                     [PR gate] ─approve─► Ship ─► DevOps ─► END
                                                         │ reject
                                                         └──► Engineer
```

Each arrow is a LangGraph edge defined in `graph/graph.py`.
Agents have no knowledge of what comes before or after them.

Three kinds of pause (all `interrupt_before` nodes):
- **ceo_qa** — shared clarification node; any agent (or the Critic) can pause to
  ask the CEO a question and resume where it left off.
- **prd_gate / pr_gate** — blocking approval gates; the CEO approves or rejects the
  PRD and the PR. Reject loops back to the producing agent with feedback.

Agent-to-agent questions are handled synchronously inside each agent's `run()` —
they never appear as graph edges.

---

## Key Design Decisions

### 1. State holds paths, not content

`ProjectState` is a flat `TypedDict`. Every field is either a primitive
(`str`, `bool`, `int`) or a file path (`Optional[str]`).

Artifact content — PRDs, code, specs — lives on disk in `workspace/`.
Agents read what they need from disk via `read_artifact()`. This means
LangGraph's checkpoint never serializes a 3000-word PRD, keeping the
checkpoint small and subsequent agent prompts lean.

### 2. Three-layer skill embedding per agent

Every agent composes three layers at runtime:

```
system prompt = identity (prompts/<agent>.txt)
              + domain knowledge (skills/<agent>.md)

user message  = feature-specific artifact content (from disk)
              + Q&A context (from qa_log)
              + structured output instructions
```

- **Identity** (`prompts/`) — who the agent is, output contract. Short, rarely changes.
- **Knowledge** (`skills/`) — design principles, coding patterns, tool constraints.
  Editable without touching Python. This is how the system gets smarter over time.
- **Context** — read from disk at runtime. Changes every feature run.

### 3. Tools are deterministic executors, not LLM calls

`tools/registry.py` contains plain Python functions that do real work:
validate component names, check REST conventions, run the linter, run tests.
They return `(bool, str)` — success flag and output message. The test runner is
**toolchain-detecting** (Phase 4.1): `detect_toolchains()` finds each testable layer by
language marker (root + immediate subdirs → flat *and* `backend/`+`frontend/` layouts),
and `run_project_tests()` runs the matching tool per layer in a pinned slim container —
**pytest** in `python:3.12-slim`, **vitest/jest** in `node:22-alpine` — and aggregates
pass/fail (falling back to pytest-only when nothing else is detected).

Tools run *after* the LLM generates output, not before. Validation warnings
are appended to artifact files so downstream agents can see them, without
triggering an expensive LLM re-call.

### 4. One LLM call per agent work phase, no conversation history

Each agent's work phase is a stateless function: read inputs → call LLM once → write output.
No chat history is passed between agents. The previous agent's *artifact file*
is the handoff mechanism, not a message thread. Q&A rounds use separate LLM calls
(question generation + optional consult calls) before the main work call.

### 5. Model allocation (two models, split by workload)

`tools/llm.py` routes calls to **two models, split by workload** (2026-07-02) — Fable 5 for
thinking/decision/analysis (anything non-coding), Opus 4.8 for hands-on coding. Three tier keys
map onto the two models (keys kept so call sites/tests don't churn); `MAX_TOKENS` is 8192 on
every tier:
- `fast` → `claude-fable-5` — lighter DECISION/ANALYSIS: CEO, PM, Triage, QA review+diagnosis,
  peer consults, retro (was Haiku, now retired)
- `strong` → `claude-opus-4-8` — CODING: Engineer (code gen + fix loop), Design kit/mockup,
  QA e2e specs, DevOps config
- `reason` → `claude-fable-5` — DEEP THINKING + the oracle: Architect, Critic, Test Author
  (test-case design is thinking, per the CEO/CTO), Design spec reasoning, and the design-QA
  vision verdict

The oracle (Test Author) and the vision verdict are analysis, so they stay on the thinking
model (`reason`), never on the coding tier. `claude-fable-5` was re-verified live on the CLI
backend on 2026-07-02 (it was once disabled there); if it ever 404s again, fall back the two
thinking tiers to `claude-opus-4-8`.

**Prompt caching (Phase 0):** the system block (identity + skill) is stable per
agent and sent as a cached block (`cache_control`). Cache reads are ~0.1× input
cost, so clarification re-runs and Engineer retries reuse the cached system prompt
nearly for free. This offsets the cost of the stronger Architect model.

### 6. SqliteSaver checkpoints

LangGraph persists the state after every node into a local SQLite file
(`checkpoints.db`). If any node crashes mid-run, `python main.py --resume <id>`
picks up from the last successful node without re-running earlier agents.

> Implementation note: `build_graph()` owns a long-lived `sqlite3` connection and
> passes `SqliteSaver(conn)` to `compile()`. Do **not** use
> `SqliteSaver.from_conn_string(...)` directly — in current LangGraph it returns a
> context manager, not a saver, and `compile()` rejects it. (Caught by
> `tests/test_architecture.py::test_graph_compiles_with_all_nodes`.)

### 7. Engineer retry loop is bounded

The Engineer → QA → Engineer loop has a hard cap (`MAX_FIX_ATTEMPTS = 3`
in `graph/graph.py`). When the cap is hit, the pipeline proceeds to DevOps
with `tests_passed=False`. DevOps generates a dry-run manifest with a warning
header. The pipeline always completes — you always get reviewable artifacts.

### 8. Bidirectional Q&A

Agents are not just relay runners. Any agent (PM, Design, Architect, Test Author,
Engineer, QA) can ask questions before committing to its output, via `run_with_qa()`
in `tools/qa_utils.py`.

**Folded clarification (Phase 0 — no separate probe call):** the question check
is folded into the agent's single work call. The work prompt carries a
CLARIFICATION PROTOCOL — the LLM either produces its artifact, or, if genuinely
blocked, emits a `===NEEDS_INPUT===` JSON block naming who it needs to ask.
Common case (no questions) = **one call**, down from two (the old design always
made a dedicated "any questions?" call first).

**Agent-to-agent consultations** are resolved synchronously inside the asking
agent's run: the target runs a lightweight `consult()` (no artifact, just an
answer), the answer is added to `qa_log`, and the work call is retried with it.
Cap: **10 total agent-to-agent calls** per agent (`MAX_AGENT_INTERACTIONS`). Once
the cap is hit, remaining agent questions are **escalated to CEO**, never dropped.

**Agent-to-CEO questions** trigger a graph interrupt via the shared `ceo_qa` node.
The pipeline pauses, CEO types the answer in the terminal, the answer is injected
into `qa_log` via `graph.update_state`, and the pipeline resumes. Cap: **10 rounds**
of CEO Q&A per agent (`MAX_QA_ROUNDS`).

**Q&A state fields** (`graph/state.py`):
```
qa_log            list   All Q&A entries: {from, to, question, answer, round}
qa_rounds         dict   CEO Q&A rounds used per agent {agent_name: int}
agent_qa_counts   dict   Agent-to-agent calls made per agent {agent_name: int}
ceo_qa_pending    str    Questions waiting for CEO (set by agent, cleared by ceo_qa node)
ceo_qa_from       str    Which agent is waiting (used by routing to return after CEO answers)
ceo_qa_answer     str    CEO's answer, injected by main.py via graph.update_state
```

**Which agents can consult which:**
| Agent | Can ask CEO | Can consult agents |
|---|---|---|
| PM | ✓ | — (runs first) |
| Design | ✓ | PM |
| Architect | ✓ | PM, Design |
| Test Author | ✓ | Architect |
| Engineer | ✓ | Architect, Design |
| QA (pass only) | ✓ | PM, Engineer |

The Critic does not use the Q&A pre-step; on a persistent gap it escalates directly
to the CEO via `ceo_qa` (`ceo_qa_from = "architect_critic"`, which routes forward).

Engineer skips Q&A on retry runs (fix_attempts > 0) — it already has the error log.
QA skips Q&A on failing runs — fast path to diagnosis for engineer.
PM and Architect also skip the Q&A pre-step when regenerating after a reject/critic
gap (they consume `review_notes` and rebuild directly).

### 9. Quality loops (Phase 1)

The waterfall is now an iterative, independently-verified pipeline.

**9.1 Independent tests (TDD).** `agents/test_author.py` writes the authoritative
test suite from the PRD's acceptance criteria *before* the engineer runs. The
engineer reads those tests and must make them pass; it may not write or modify
anything under `tests/` (enforced in `_parse_and_write_files`). The tests encode
*intent*, not the implementation — this removes the "marking your own homework"
problem where the engineer graded its own code. It runs on the `strong` tier
(the oracle must not be truncated — on `fast`/2048-tok it silently dropped the
actual `test_*.py` and emitted only fixtures) and is protected by a **non-empty-
oracle guard** (`_has_real_tests`): the written suite must contain ≥1 `test_*.py`
with a `def test_`; otherwise it retries once, then escalates to the CEO/CTO
instead of handing the engineer an empty oracle to "fail" against.

**9.2 Critic review gate.** `agents/critic.py` judges the technical spec against
the PRD on the `reason` (Opus) tier — the highest-risk artifact, caught at the
cheapest point. Behavior: **retry** (send back with specific gaps, bounded by
`MAX_REVIEW_ATTEMPTS = 2`), then **escalate** to the CEO via `ceo_qa` if still
failing, then proceed forward. Generic and parameterized by `stage` — adding a
critic for design or PRD is a few lines (see the header of `graph/graph.py`).

**9.3 Blocking approval gates.** `prd_gate` and `pr_gate` pause for explicit CEO
sign-off on the two high-stakes artifacts. Reject loops back with feedback
(`review_notes`). The PR is no longer opened inside QA — `agents/ship.py` opens it
only *after* `pr_gate` approval, so nothing is pushed without a human gate.

**9.3b Run-and-verify (Phase 4.2/4.3).** After QA passes, the **integration** node
(`agents/integration.py` — deterministic, no LLM) proves the app actually RUNS:
`registry.run_compose_integration()` brings the app's own `docker-compose.yml` up
(`-p graphsmith-it`, pinned slim images), polls until every service is
running/healthy, smoke-checks the conventions (api `GET :8000/health`, frontend
`GET :3000/`), then runs **QA-authored Playwright specs** (`e2e/*.spec.ts`) in the
pinned `mcr.microsoft.com/playwright` image on the compose network
(`E2E_BASE_URL=http://frontend:3000`), and always tears down (`down -v`). Because the
e2e suite drives every rate-limited write endpoint from ONE runner IP, the stage writes
an IT-only `docker-compose.it-override.yml` (`api: RATE_LIMIT_ENABLED: "0"`, merged via
`-f` for every compose call, removed in `finally`) so a per-IP limit can't 429 the suite —
the shipped compose keeps its production limit. QA writes
the specs in its pass path *after* reviewing the code — flows from the feature
request/PRD (intent), code only for selectors — keeping the user-flow oracle
independent of the implementer; the engineer is blocked from writing `tests/` AND
`e2e/`. Selector resolution is **semantic, not a string grep**:
`registry.resolve_kit_testids` (shared by `extract_kit_interface`,
`check_testid_contract`, and QA's selector block) resolves design-kit components that
re-emit their `data-testid` prop with a STATE suffix — `RelationshipButton` renders
`${base}-add-friend`/`-requested`/`-accept`/`-friends`/`-edit`, never the bare base — to
the REAL `<base>-<suffix>` ids and suppresses the never-rendered bare base, so a
base-only assertion is FLAGGED instead of silently passing because the base string sits
in the source as the prop literal (a live phase-2 multi-loop failure). Hybrids that also
forward the base verbatim onto a real element (`UsernameField`) keep the base.
Failure loops to the engineer through `error_log`, bounded by
`MAX_INTEGRATION_ATTEMPTS = 2`, then proceeds to the gate with the red
`tests/integration_report.md` visible — the pipeline always completes.

**9.4 Engineer output depth.** Output cap raised to 8192 (Phase 0); a bounded
continuation (`MAX_CONTINUATIONS = 2`) resumes generation if the output is cut off
mid-file, so large apps aren't lost to the token ceiling.

**9.5 The human is CEO *and* CTO; the tech stack is a CTO decision.** The single human
holds both business and technical authority (`skills/ceo.md`) — they are the universal
unblocker. The architect does **not** hardcode a stack: on its first pass it proposes a
default (FastAPI + Next.js + Postgres) and escalates a **mandatory** confirmation to the
CEO/CTO via `ceo_qa` before committing the spec (`agents/architect.py::_ask_stack`). The
confirmed stack is recorded in `state["tech_stack"]` (sticky across critic retries) and
drives the engineer and devops. Agent-to-agent Q&A stays capped at 10, after which any
blocker — business or technical — escalates to the CEO/CTO who resolves it.

**New state fields:** `test_path`, `review_attempts`, `review_notes`,
`review_action`, `prd_approved`, `pr_approved`, `approval_pending`,
`approval_decision`, `approval_feedback`, `tech_stack`, `tech_stack_confirmed`.

### 10. Codebase awareness — extend mode (Phase 2.1)

By default the pipeline builds **greenfield** under `workspace/<id>/`. Pass
`python main.py --repo /path/to/repo` to run in **extend mode** against an existing
codebase. `state["target_repo"]` switches the behavior:

- **Surveyor** (`agents/surveyor.py`) runs after PRD approval. In greenfield it is a
  **no-op pass-through** (zero LLM cost). In extend mode it reads the repo via
  `tools/repo.py` (file tree, detected stack, keyword-relevant file excerpts) and writes
  an **integration brief** (`design/repo_map.md`), setting `repo_map_path` and
  `detected_stack`. Runs on the `reason` tier — understanding an unfamiliar codebase is
  high-leverage.
- **Architect** proposes the *detected* stack (not the default) at the CTO confirmation
  gate, and folds the integration brief into the spec so the File Structure lists real
  files to **modify or create**.
- **Test Author** writes test files **into the target repo** at paths matching the repo's
  existing conventions (which it is shown), and records the exact relpaths in
  `state["test_files"]`.
- **Engineer (I1 — structured file changes, `tools/codegen.py`, DEFAULT):** the model
  changes files **through real file tools against a staging copy** of the project; the
  diff is then synced back through ONE guarded choke point (`codegen.sync_back`: oracle/
  kit/meta protection, path escapes, deletion mirroring). There is **no text format and
  no parsing** — the historic mechanical failure classes (duplicate-block corruption,
  stale-SEARCH, destructive re-emit rounds) are structurally impossible. On the
  `claude-cli` backend the session uses Claude Code's own Read/Write/Edit tools (Edit
  enforces exact-unique anchors and self-heals mismatches IN-SESSION); on the `api`
  backend `codegen._api_codegen` runs a Messages tool-use loop with the same executor
  semantics. A clarify (`NEEDS_INPUT`) round **discards its staging** — never
  half-applied. `code_files` **accumulates** across fix rounds (minus deletions) so QA
  always sees the full implementation, not just the last minimal diff.
  `AGENT_CODEGEN=text` falls back to the legacy `===FILE/EDIT/DELETE===` path:
  - **New files** → full contents via `repo.write_into_repo` (refuses paths escaping root).
  - **Existing files** → **minimal `===EDIT:===` search/replace blocks** (#3) applied by
    `repo.apply_edit`, which requires the SEARCH text to match **exactly once** — an
    ambiguous or stale snippet fails loudly and the engineer retries, rather than a
    whole-file rewrite silently corrupting the file.
  Either way the engineer runs the repo's **own** test suite in Docker and skips the
  linter in extend mode (the repo's CI owns pre-existing lint). Meta-artifacts always
  stay in `workspace/<id>/`; only code + tests land in the repo.
- `tools/repo.py` is read-only except the guarded `write_into_repo()` / `apply_edit()`.

**QA reads the code (#5):** on a passing run, QA reads the engineer's written files
(`state["code_files"]`) and reviews the implementation against the acceptance criteria in
its sign-off — not just "tests passed." The report is surfaced at `pr_gate`, so a blocking
finding reaches the CEO/CTO, who can reject → the engineer fixes (existing pr_gate loop).

**New state fields:** `target_repo`, `repo_map_path`, `detected_stack`, `test_files`,
`code_files`.

### 11. Cross-run learning (Phase 2.2)

The company gets smarter over time. `tools/learnings.py` is a persistent store
(`learnings/<agent>.md`, outside `workspace/`) separate from the curated `skills/`:

- When QA diagnoses a test failure it also distils one **generalizable lesson** (folded
  into the same diagnosis call — no extra cost) and records it for the engineer.
  Lessons are **deduped** and the store is **char-capped** (oldest trimmed first).
- The engineer, architect, and test author load their learnings into the system prompt
  (`augment_system`) on every run, so past failures stop recurring across *different*
  features.
- **Two tiers — local vs shared (committed):** the retro writes the gitignored
  `learnings/<agent>.md` store, which is machine-accumulated, may be stack/product-specific,
  and is **local to one installation**. A second store `learnings/shared/<agent>.md` is
  **committed** (un-ignored in `.gitignore`), so its lessons ship with the harness to *every*
  clone and project. `augment_system` injects both (shared first, then local). A lesson
  reaches the shared tier only by human-gated **promotion** — `learnings.promote_learning`
  and the CLI `python -m tools.learnings list` / `promote --agent <a> (--index N --as
  "<generic rewrite>" | --text "...")` — and **must be product- AND stack-agnostic** (stack
  specifics live only as a `(Default stack: …)` example). Promote-by-index *graduates* the
  candidate: it is removed from the local store so it isn't injected twice. The shared tier is
  kept separate from hand-authored `skills/` so a promoted machine lesson can never corrupt a
  curated skill, and from the local store so raw candidates are never shipped blindly.
- **Feedback events + end-of-run retro:** every failure choke point emits a `feedback`
  event into the run trace (`learnings.emit_feedback`); at DONE, `learnings.run_retro`
  distils ≤2 **product-agnostic** lessons per agent and records them for *all* producing
  agents. Crucially, a QA code-review **NO-GO** emits even when the CTO *adjudicates* or
  *hand-fixes* it rather than rejecting (`qa._emit_nogo_feedback`) — otherwise the highest-
  value lessons (the ones a human had to step in on) are lost — and the operator can log an
  out-of-band hand-fix with `live_run.py feedback`. Genericness is enforced by the retro
  prompt (no feature-specific names/values), so learnings apply to ANY product.

### 11b. Standing product invariants (knowledge-base wiring)

Learnings are heuristic and *advisory*; they don't tell an agent the product's hard,
code-verifiable rules. The generation agents (architect, test author, engineer, QA) had
**no** standing product context at all — they reasoned from per-run artifacts alone.

- `registry.extract_product_invariants(root)` **statically** parses the backend's
  `models/*.py` (unique/check constraints, computed-not-stored columns, enums) and
  `routers/*.py` (route + auth surface) **off disk** — deliberately NOT the runtime
  `openapi.json` (which only exists when compose is healthy, so it would silently go stale
  on the common compose-fail path). Result → `state["product_invariants"]`, loaded in
  `main.py`/`live_run.py` from `target_repo`; `""`/None on a non-Python or undetected repo,
  so extend-mode and greenfield never crash.
- `qa_utils.product_invariants_block(state)` injects it into those four agents' **work**
  prompts under an OVERRIDES-any-learned-lesson label, so a code-verified fact (e.g.
  `spots_remaining` is computed-never-stored) beats a contradicting guess at prompt-assembly
  time. This is the **canonical > learned** provenance rule made to act where it matters.
- The human-readable canon lives **with the product** under `workspace/project/docs/`
  (DOMAIN_MODEL, AUTH, ADRs in `decisions/`, `INDEX.md`) + `README.md` +
  `product/api_contract.md`, so a fresh human, a fresh agent, or an external `--repo` run can
  all use it cold. **New state field:** `product_invariants`. Pinned by
  `tests/test_product_invariants.py`.

### 12. Execution hardening (Phase 2.3)

- **Static security scan** (`registry.scan_security`) runs over the exact files the
  engineer wrote — deterministic, dependency-free checks for `eval`/`exec`,
  `shell=True`, `os.system`, pickle/yaml deserialization, `verify=False`, and hardcoded
  secrets. Findings are non-blocking but **surfaced**: into the QA sign-off and printed
  at the **PR approval gate** so the CEO/CTO sees them before merge (`security_warnings`).
- **Docker resource limits** (`--memory`, `--cpus`, `--pids-limit`) so a runaway or
  fork-bomb in generated code can't take the host down.
- **No push without approval** (already enforced): PR creation lives in `ship`, gated by
  `pr_gate`.
- **Code-quality layer** (additive, non-blocking) — augments the blocking E,F linter
  without replacing it: `registry.format_code` auto-fixes + formats the generated Python
  (ruff `--fix` import-sort/pyupgrade/cleanup, then `ruff format`) BEFORE the lint gate, so
  the gate passes more often and the code is consistently styled. `registry.code_quality_report`
  produces **advisory** findings (ruff bug/style families + mccabe complexity C90 + mypy
  static types, scoped to the files the engineer wrote) and `registry.check_frontend_quality_tooling`
  deterministically (no Node) flags a frontend missing ESLint / Prettier / a strict
  `tsconfig` / a typecheck script. All findings land in `code_quality`, surfaced at the PR
  gate alongside the security scan. Every function degrades gracefully when ruff/mypy are
  absent and never raises. (`ruff`/`mypy` are pinned in `requirements-dev.txt`.)

**New state fields:** `security_warnings`, `code_quality`.

### 12c. Anti-drift / self-fighting hardening (2026-07)

A batch of surgical fixes for failure modes where the pipeline burned its bounded retry
budget on self-inflicted or environment noise rather than real defects — each augments an
existing mechanism (no new blocking gate) and is pinned by a regression test:

- **Convention over invocation (node test runner)** — `registry._run_node_layer` runs the
  layer's OWN `test` script (`npm test --silent`) when `package.json` defines a non-blank one,
  falling back to `npx vitest run` / `npx jest --ci` only when it doesn't. A DB-dependent
  integration test then never runs inside the DB-less unit container. `_node_env_hint`
  appends a test-ENVIRONMENT hint (not a code-bug verdict) when the output matches infra-noise
  patterns (missing platform binary / `ECONNREFUSED`/`P1001` / no database).
- **Kit-wiring container allowance** — `registry.check_kit_wiring` no longer flags a non-kit
  file that shares a kit component's basename WHEN that file imports from the kit (a
  legitimate wrapper/container); a true parallel reimplementation still fails, now with
  actionable rename/wrap guidance.
- **Design-input caps** (`agents/engineer.py`, `agents/qa.py`) — the design spec and mockup
  read at generous caps and the components manifest reads **untruncated** (≤24000 safety
  bound): the manifest is the wiring CONTRACT, and truncating it caused parallel/unwired UI.
  The loud file_io cap-warning is preserved.
- **Per-service integration logs + healthcheck hint** (`registry.run_compose_integration`) —
  `_assemble_service_logs` captures logs per service, app services first, the database last
  and hard-capped (15 lines), so DB init noise can't evict the app error. On a
  running-but-unhealthy health-wait timeout, `_healthcheck_hint` appends a generic hint to
  probe `127.0.0.1` (not `localhost`, which may resolve to IPv6 `::1`) with a `start_period`.
- **Pinned design-system tier** (`tools/product.py`) — `load_design_system()` returns
  `product/design_system.pinned.md` (optional, human-authored, agent-immutable) concatenated
  BEFORE the managed `design_system.md`; `save_design_system()` writes only the managed file.
  Human standing mandates survive the agent's memory-compaction rewrites.
- **Deploy-target persistence** (`tools/product.py` + `agents/devops.py`) — the CEO/CTO
  deploy-target decision is saved (`product/deploy_target.md`) and reused, injected as the
  standing decision with a do-not-ask directive; the persist trigger requires an explicit
  deploy/hosting term so an unrelated devops question is never captured as the target.
- **Live skill mandates** — `agents/pm.py` and `agents/qa.py` now load `skills/pm.md` /
  `skills/qa.md` into the system prompt (they previously never did), so the PM journey-AC
  mandate and the QA evidence rule / environment-vs-code classification actually reach the
  model.

### 13. Design as a real designer (consumer-app design)

The design agent was upgraded from "list screens + components" to how strong designers
actually work — discovery first, then design.

- **Standing product profile** (`tools/product.py`, `product/profile.md`): product
  category, target users/customer base, key use cases, brand & tone, business goals. The
  CEO/CTO sets it **once** (captured in `main.py` on first run) and it **persists across
  features** — like the tech-stack decision. Loaded into `state["product_profile"]` and
  read by **Design and PM**. `product/` is gitignored by default.
- **Discovery-first design:** the design skill (`skills/design.md`) makes the agent
  establish who/JTBD/brand/success-metric before screens, ask the CEO/CTO for material
  gaps rather than guessing, explore briefly then commit **with rationale**, design the
  **first-run + unhappy paths**, and write the actual **microcopy**. Output sections:
  Design Context · Approach & Rationale · User Flows · Screens & Components · Content &
  Microcopy · Accessibility & Responsive · Flagged Items. Runs on the `strong` tier.
- **Design critic gate** (`critic_design`): reuses the generic critic with a
  design-specific *review focus* (coverage of stories, all states, unhappy paths,
  components/data/microcopy, a11y). Bounded retry → escalate to CEO/CTO, same as the
  architect critic. Wired `design → critic_design → architect`.
- **Visual mockup:** after the spec, Design generates a **self-contained HTML/Tailwind
  mockup** (`design/mockup.html`, Tailwind via CDN) of the key screens and their states
  with the real microcopy — a reviewable design board the CEO/CTO opens in a browser.
  A second `strong`-tier call grounded in the just-written spec; skipped for backend-only
  features (`NO UI SURFACE`). Path in `state["design_mockup_path"]`; `main.py` prints it.
- **Reuse an external design (Change 1):** `--design-source <dir|git-url>`
  (`state["design_source"]`, `tools/design_source.py`) lets Design REUSE an existing design
  instead of generating one. A local dir is used as-is; a git URL is shallow-cloned; the
  primary HTML mockup is picked by a `design_manifest.md` or the shallowest `*.html`. When a
  usable mockup exists, `design._do_imported` writes a spec that MATCHES it, uses the imported
  HTML as `mockup.html` (the design-QA baseline + engineer visual truth), builds the kit from
  it through the SAME additive-interface + testid-uniqueness guards, and skips the 3-directions
  human pick. Absent/unusable → normal generate flow. Figma is deferred (interactive OAuth).

**New state fields:** `product_profile`, `design_mockup_path`, `design_source`.

### 14. Triage / change-type routing + stack persistence (1→10 ergonomics)

Real day-to-day work is mostly bug fixes, refactors, and chores — not net-new features.
Running all of them through the full PRD→design→architecture→TDD pipeline is wrong-sized.

- **Triage** (`agents/triage.py`) runs right after CEO and classifies the request into
  `change_type` ∈ {feature, bugfix, refactor, chore} (one cheap `fast` call; safe default
  = feature). Routing:
  - **feature → full lane** (PM → gates → surveyor → design → critics → architect →
    test author → engineer ⇄ qa → PR gate → ship → devops).
  - **bugfix/refactor/chore → quick lane:** straight to **Engineer ⇄ QA → [PR gate] →
    Ship → END** — skips PRD, design, architecture, the TDD scaffolding, and DevOps.
- The **Engineer is change-type aware**: in the quick lane it works directly from the
  brief (no tech spec), is told to make the *smallest* change (bugfix = root-cause fix /
  refactor = no behavior change / chore = scoped), and existing tests must still pass.
- **Stack persists across features.** The CEO/CTO-confirmed greenfield stack is saved to
  `product/stack.md` (`tools/product.py`) and reused, so the architect stops re-asking the
  stack on every feature. Extend mode still detects the stack from the target repo.

> This is the first slice of "ship like a startup 1→10": right-sized process per change
> + no repeated stack confirmation. Still open (see review): autonomy/trust modes, and a
> real deploy→telemetry loop. (Toolchain-detecting test runner + dockerized default stack
> landed in Phase 4.1; product continuity landed earlier.)

**New state fields:** `change_type`.

### 15. Project continuity (one persistent product across runs)

The core workflow is *build a feature → verify → add the next → repeat* against the **same
product**. So a "project" is now first-class, not a fresh throwaway per run.

- **One persistent project** at `workspace/project/` (`tools/project_ctx.py`), a real git
  repo = the product. By default **every run targets it**: the first run seeds it, later
  runs **auto-extend** it (the platform sets `target_repo = workspace/project` and
  `managed_project = True` — you never type `--repo`). So the Surveyor maps the accumulated
  code and the Engineer writes minimal diffs back into it.
- **Feature ledger** (`project/.agent/ledger.md`): after each run, `append_ledger` records
  the feature + type + stack + files (deterministic, no LLM). On the next run it's loaded
  into `state["project_ledger"]` and fed to **PM, Design, and Architect**, so they build
  *consistently with what already exists* and don't re-propose it — the agents get the
  history, not just the current code.
- **Stack reuse for the managed project**: the architect reuses the persisted stack (no
  re-ask) for the managed project and greenfield; only an **external `--repo`** detects its
  stack per run (`external = target_repo and not managed_project`).
- **Ship targets the real code** (`code_root(state)`), so commits land in the project repo.
- Single default project (one product at a time); reset by deleting `workspace/project/`.
  An external `--repo` run is *not* the managed project (no ledger, detect-its-own-stack).

**New state fields:** `managed_project`, `project_ledger`.

### 16. Overseeing the orchestration (evals + overseer)

With the human mostly out of the loop, two separate systems watch the agents — **offline
evals** ("is the output good?") and an **online overseer** ("did this run behave?").

- **Tracing** (`tools/trace.py`): every run is recorded to `traces/<id>.jsonl` — node
  transitions and every LLM call with real token usage + latency (hooked into `call_llm`,
  emitted from `main.py`). No-op when no run is active (unit tests). This is the
  observability foundation everything else builds on.
- **Flight recorder** (`report_html.render_run` → `review/run.html`): the VISUAL companion to
  the audit page, rendered at END (best-effort) from the trace via `run_stats.aggregate`.
  Deterministic, zero-LLM. Shows the **actual node path with loops** (engineer⟷QA bounces,
  ceo_qa pauses and critic retries appear as repeated chips), **where wall-time went** per node,
  **model spend by tier** (the cost flame), and a **loops/rework** summary, cross-linking the
  gate + audit pages. The audit answers *who decided what and why*; the flight recorder answers
  *what the run DID and where the time/tokens went*. Pinned by `tests/test_run_stats.py`.
- **Autonomy metric** (`run_stats.compute_autonomy`): the number a software company actually
  manages — **human interventions per run**. Deterministic from the trace + final state:
  `clarifications` (CEO answers) + `rejections` (gate rejects) + `manual_edits` (CTO hand-fixes,
  logged as `cto_handfix` feedback) = interventions; `autonomy_rate = approvals / (approvals +
  interventions)` (1.0 = the human only rubber-stamped the mandatory gates). Surfaced as an INFO
  overseer finding and the headline KPI on the flight recorder. Pinned by `tests/test_autonomy.py`.
- **Overseer** (`evals/overseer.py`): deterministic, runs at the end of *every* run
  (`main.py`). **Invariants** — engineer never authored tests, a full-lane feature has a
  PRD + a confirmed stack, nothing ships on red below the retry cap. **Loop detection** —
  engineer⟷QA hit the cap without converging, critic escalated. **Budget** — token/call
  ceilings. A HIGH-severity failure means *NEEDS HUMAN REVIEW* even if the pipeline
  "completed" — this is the out-of-band counterpart to the in-band critics.
- **Offline evals** (`evals/triage_eval.py`): Triage is a classifier → labeled dataset →
  accuracy + confusion (`evals/datasets/triage.jsonl`); `evaluate()` is pure/testable,
  `run_live()` runs the real agent. Turn a production miss into a regression case by
  appending to the dataset.

**Documented next steps (not built):** per-agent rubric LLM-judge evals (reuse the
critics/validators as graders, run K× for variance), end-to-end golden tasks with a
**held-out** acceptance oracle, an LLM overseer reviewing the trajectory, per-node
real-time halting, confidence-gated selective human review, and a CI regression gate on
eval scores. See `evals/README.md`.

---

### 17. The AI-native layer (code quality + 2026 model capabilities)

A layer that hardens *output quality* and *agent coordination* without destabilizing the
proven engineer⟷QA loop. The guiding rule throughout: **augment the proven gates with
deterministic checks and safe defaults; never bolt on a new blocking gate that can thrash
the loop.** Roadmap + rationale in `AI_NATIVE_ROADMAP.md`.

- **Dependency lock** (`registry.check_dependencies`): every third-party import in the
  engineer's WRITTEN files must be a declared dependency (`requirements.txt`/`pyproject.toml`
  for Python via `ast`; `package.json` for JS/TS). Folded into `code_quality` (advisory),
  scoped to written files. Kills the "builds locally, breaks in a clean install" class.
- **Code-quality layer** (`registry.format_code` / `code_quality_report` /
  `check_frontend_quality_tooling`): auto-format (ruff) runs before the blocking E,F lint
  gate; advisory mccabe-complexity + mypy + frontend-tooling findings surface at the PR gate.
- **Opt-in soft gate** (`QUALITY_GATE` env): `report` measures line coverage
  (`measure_coverage`, a separate best-effort Docker pass that never touches the correctness
  run); `block` additionally fails the engineer round on over-budget cyclomatic complexity
  (`check_quality_gate`). Default OFF — report-first → gate-later. Coverage is NOT gated on the
  engineer (it can't edit `tests/`); mypy stays advisory (false-positive risk).
- **Structured control-plane signals** (`tools/llm.call_structured`): the routing-critical
  decisions (triage change-type, critic verdict, design-QA verdict) are VALIDATED objects, not
  regexes over prose — a strict "emit ONLY this JSON" contract, robust extraction, schema
  coercion, one corrective retry, and a SAFE default on failure (a traced fallback, never a
  silent misroute). Marker-in-artifact signals (`NEEDS_INPUT`, the QA GO/NO-GO) keep robust
  markers. Backend-agnostic.
- **Web search** (`call_llm(..., web_search=True)`, OPT-IN via `LLM_WEB_SEARCH`): lets the
  THINKING agents (architect, surveyor) verify current library versions / APIs / CVEs instead
  of training-cutoff memory. Falls back to a plain call on any failure, so enabling it can
  never break a run.
- **Kit testid uniqueness** (`registry.check_kit_testid_uniqueness`, in
  `design._enforce_testid_uniqueness`): the dual-surface design mandate (desktop table + mobile
  card) can render the same `data-testid` twice in the DOM → a Playwright strict-mode failure
  the engineer CAN'T fix (the kit is design-owned). This catches it at design time (re-emit
  once), and `skills/design.md` mandates per-layout-unique testids.
- **Adaptive thinking** (`tools/llm.EFFORT`, OPT-IN via `LLM_THINKING=adaptive`): maps each
  tier to a reasoning-effort level on the api backend; default OFF.

---

## How to Add a New Agent

This is the full checklist. No existing agent files change except `graph/graph.py`.

**Example: adding a Security agent between Test Author and Engineer.**

### Step 1 — Create the agent

Follow the current contract: `run()` delegates to `run_with_qa(state, name, _do_work,
consultable_agents=CONSULT)`, and `_do_work(state, qa_log, rounds, allow_clarify=True)`
makes its LLM call through `work_call(...)` so clarification is folded in (it returns
`{"_clarify": questions}` when blocked, without writing artifacts).

```python
# agents/security.py
from graph.state import ProjectState
from tools.file_io import load_prompt, load_skill, read_artifact, write_artifact
from tools.qa_utils import run_with_qa, work_call, format_qa_context

CONSULT = ["ceo", "architect"]

def run(state: ProjectState) -> dict:
    return run_with_qa(state, "security", _do_work, consultable_agents=CONSULT)

def _do_work(state: dict, qa_log: list, rounds: dict, allow_clarify: bool = True) -> dict:
    identity = load_prompt("security")
    skill = load_skill("security")
    system = f"{identity}\n\n{skill}" if skill else identity

    tech_spec = read_artifact(state["design_path"])
    qa_ctx = format_qa_context(qa_log, "security")
    user_msg = f"Tech spec:\n{tech_spec}\n\n{qa_ctx}\n\nIdentify security issues."

    questions, report = work_call(system, user_msg, "reason", CONSULT, allow_clarify)
    if questions:
        return {"_clarify": questions}

    path = write_artifact(state["project_id"], "design", "security_review.md", report)
    return {"current_node": "security", "design_path": path,
            "qa_log": qa_log, "qa_rounds": rounds, "ceo_qa_from": None}
```

### Step 2 — Add identity prompt

```
# prompts/security.txt
You are a senior application security engineer.
You review technical specs for OWASP Top 10 vulnerabilities before code is written.
Flag issues with severity (critical / high / medium). Be specific, not generic.
Output only the review. No preamble.
```

### Step 3 — Add domain knowledge

```markdown
# skills/security.md
## Identity
...auth patterns, injection risks, secrets management rules, etc.
```

### Step 4 — Add state field (if needed)

```python
# graph/state.py — only if security produces a new artifact path
security_path: Optional[str]
```

### Step 5 — Wire into the graph

```python
# graph/graph.py

from agents import ..., security

builder.add_node("security", security.run)

# Redirect test_author → security → engineer (was test_author → engineer)
builder.add_conditional_edges("test_author", _needs_ceo_qa("test_author", "security"), {"ceo_qa": "ceo_qa", "security": "security"})
builder.add_conditional_edges("security",    _needs_ceo_qa("security",    "engineer"), {"ceo_qa": "ceo_qa", "engineer": "engineer"})

# Add "security": "security" to BOTH the ceo_qa_return_routing mapping and the
# ceo_qa conditional-edges dict, so a CEO question from this agent returns to it.
```

That is the complete change. Five files touched, four of them new.

> After any graph change, re-run the routing simulation in your head (or as a script)
> for the happy path plus every loop/reject/escalate branch, and confirm each reaches
> END. The Phase 1 implementation was verified this way.

---

## Known Limitations

| Limitation | Where | Planned fix |
|---|---|---|
| Docker pip install is slow on first run | `tools/registry.py` | Pre-built base image |
| DevOps generates IaC but doesn't execute it | `agents/devops.py` | v2: cloud-CLI execution |
| No Ops/monitoring + deploy→telemetry→backlog loop | `graph/graph.py` | next agent + MCP connectors |
| Sequential Design → Architect (not parallel) | `graph/graph.py` | LangGraph Send() / subagents |
| Engineer still one call (+continuation), not per-module | `agents/engineer.py` | deeper decomposition |
| QA authors only the current phase's e2e — prior specs not retained | `agents/qa.py` | additive-e2e contract |
| Generated kit forms don't always render server-set field errors | design kit (generated) | design/engineer skill fix |
| `live_run.py` segment can exit early mid-pipeline (needs `resume`) | `live_run.py` | investigate stream exit |

> **Done since (see `IMPROVEMENT_PLAN.md` + `AI_NATIVE_ROADMAP.md`):** independent TDD,
> critic gates (design + architect), blocking PRD/PR gates, extend-mode codebase awareness,
> cross-run learning, run-and-verify integration + vision design-QA, design-owned component
> kit, toolchain-detecting test runner, web search, structured control-plane signals,
> dependency lock, opt-in quality soft-gate, autonomy metric, kit-testid hardening.
