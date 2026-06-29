# Graphsmith

An AI-native software company where you act as **CEO and CTO** — you own the business
*and* technical calls — and agents do the rest, from requirements all the way to
deployment configuration.

## Quickstart

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. LLM access — DEFAULT backend is the claude CLI (subscription-billed, no API key):
npm install -g @anthropic-ai/claude-code   # + `claude` once to log in
# or opt into the metered API instead:
#   export LLM_BACKEND=api ANTHROPIC_API_KEY=your_key_here

# 3. Pre-flight: Docker must be running (used by the test + integration stages)
docker ps
# `gh auth status` is OPTIONAL — only needed to actually OPEN the PR; without it the
# pipeline still completes and the Ship step no-ops. `workspace/` is created for you.

# 4. Run — interactive (you answer gates/questions at the terminal)
python main.py                          # build/extend the SINGLE persistent project
python main.py --repo /path/to/repo     # extend an EXTERNAL repo instead
python main.py --resume <project-id>    # resume an interrupted run
# reset the project: rm -rf workspace/project
```

### Run it from Claude Code (or any script)

`main.py` blocks on `input()`. For non-interactive / automated driving — including
**letting Claude Code itself drive the whole pipeline** — use `live_run.py`, which runs
one segment per process and prints machine-readable markers:

```bash
python live_run.py start  --feature "Add user login with email + password"
#   → runs to the first pause, prints  PAUSE {json}  (a ceo_qa question or an approval gate)
python live_run.py answer --thread <id> --text "single-org only for v1"   # answer a question
python live_run.py approve --thread <id>                                  # approve a gate
python live_run.py reject  --thread <id> --feedback "..."                 # reject with feedback
python live_run.py resume  --thread <id>                                  # continue after a pause/stall
#   → ends with  DONE {json}  (overseer verdict, autonomy rate, artifact links)
```

State persists in `checkpoints.db` across invocations, so a driver (a human, a script, or
Claude reading the `PAUSE`/`DONE` markers) can answer each pause and resume. This whole repo's
own MadClub demo product was built this way — Claude driving `live_run.py` end-to-end.

### Optional knobs (env vars, default OFF)

| Env | Effect |
|---|---|
| `LLM_BACKEND=api` | use the metered API instead of the zero-cost claude CLI |
| `LLM_WEB_SEARCH=1` | let the architect/surveyor verify current library versions/CVEs via web search |
| `QUALITY_GATE=report` / `block` | measure test coverage (report); also block over-complex code (block) |
| `LLM_THINKING=adaptive` | adaptive reasoning-effort per tier (api backend) |

## How it works

**This is one persistent product, built up over many runs** — not a fresh app each time.
The first run sets up a **product profile** (category, users, brand, goals) and seeds the
project at `workspace/project/`; **every later run auto-extends that same project**, so the
agents accumulate the real code plus a **feature ledger** of what's been built and why. You:
build a feature → verify it → ask for the next one → repeat. Each run pipeline:

```
You (CEO input)
  → CEO agent          prd/ceo_brief.md
  → Triage             feature → full pipeline below; bugfix/refactor/chore → QUICK lane
                       (straight to Engineer, skipping PRD/design/architecture/TDD + DevOps)
  → PM agent           prd/prd.md            ← may pause to ask you questions
  → ✋ PRD approval     you approve or reject the PRD (reject loops back to PM)
  → Surveyor           design/repo_map.md   (extend mode only: maps the existing repo;
                       no-op for greenfield)
  → Design agent       design/design_spec.md + 3 direction mockups
                       designs from discovery (users, brand, job-to-be-done) using your
                       product profile; proposes THREE design directions with rationale
  → ✋ Design choice    you open review/design_options.html and pick A, B or C —
                       the winner becomes THE design and drives the component kit
  → Design Critic      reviews the design vs PRD/user needs (retry or escalate)
  → Architect agent    design/tech_spec.md  proposes the tech stack and asks YOU
                       (as CTO) to confirm/change it before committing the spec
  → Spec Critic        reviews the tech spec vs PRD; sends it back to fix gaps,
                       escalates to you if still failing
  → Test Author        tests/**             writes the authoritative tests FIRST (TDD)
  → Engineer agent     src/**               implements code to pass those tests —
                       changes files through REAL file tools on a staging copy,
                       synced back through a guarded diff (no fragile text
                       formats); per-layer toolchain in Docker: pytest backend,
                       vitest frontend; retries up to 3x
  → QA agent           tests/qa_report.md   diagnoses failures / signs off on pass;
                       writes Playwright e2e specs (e2e/**) for the user flow —
                       selectors come from the design kit's real data-testids
                       (state-suffix components like RelationshipButton are
                       resolved to their rendered `<base>-<suffix>` ids, not the
                       never-rendered base) and every spec must pass a
                       deterministic lint (real testids, exact API paths, cleanup
                       isolation) before it ships
  → Integration        tests/integration_report.md — docker compose up the app's own
                       stack, smoke-check api/frontend, run the e2e specs live;
                       failure loops back to the Engineer (≤2) before any human gate
  → ✋ PR approval      you approve or reject before the PR is opened
  → Ship               opens the GitHub PR (only after your approval)
  → DevOps agent       deploy/**            (Dockerfile, docker-compose, GitHub Actions)
```

Meta-artifacts (brief, PRD, specs, QA report, deploy config) are written to
`workspace/<project-id>/`. In **greenfield** mode the code + tests also go there; in
**extend mode** (`--repo`) the code + tests are written back **into your repo** — new
files in full, and **existing files via minimal search/replace edits** (no whole-file
rewrites), so diffs stay small and reviewable (`git diff`). Any agent may also pause to
ask you a clarifying question.

### Bidirectional Q&A

Agents are not just relay runners — they can ask questions before doing their work.

The question check is folded into each agent's single work call — if an agent is
genuinely blocked it emits a `NEEDS_INPUT` request instead of producing its artifact;
otherwise it just does the work. No wasted "any questions?" round-trip.

**Agent-to-agent:** Before generating code, Engineer might ask Architect about an
ambiguous API contract. These consultations happen silently and synchronously — no
terminal prompt — and the answer feeds back into the agent's retry of its work call.
Each agent can make up to **3 total agent-to-agent consultations** across the pipeline run.

**Agent-to-CEO:** If an agent has a question only you can answer (scope, priority, a
business constraint), the pipeline pauses and you see:

```
==================================================
QUESTION FROM ARCHITECT
==================================================
Should the API support multi-tenancy in v1, or is this single-org only?

CEO>
```

You type your answer and the pipeline resumes. Each agent can ask you up to **3 rounds**
of questions. If the agent exhausts its peer consultations, remaining questions are
automatically escalated to you.

### Quality loops

The pipeline is not a one-pass waterfall — it verifies its own work:

- **TDD:** an independent Test Author writes the test suite from the PRD *before* the
  Engineer writes any code. The Engineer must make those tests pass and cannot edit
  them — so the tests check *intent*, not just self-consistency. The suite is guarded:
  if it contains no runnable `def test_` case (an empty oracle), the Test Author retries
  and then escalates rather than handing the Engineer nothing to satisfy.
- **Critic:** the technical spec is reviewed against the PRD. Gaps are sent back for up
  to 2 revisions; if it still falls short, you're asked how to proceed.
- **QA reads the code:** before signing off, QA reviews the *actual generated code*
  against the acceptance criteria (not just "tests passed"). Its findings are in the
  report shown at the PR gate, so you can reject → engineer fixes.
- **Approval gates:** the pipeline blocks for your explicit sign-off on the PRD and
  again before the PR is opened. Rejecting either loops back with your feedback.

### Gets smarter, and safer

- **Cross-run learning — every agent self-improves:** every piece of feedback in a run
  (gate rejections, critic findings, integration failures, guard violations, design-QA
  verdicts, your own directives) is recorded, and an end-of-run retrospective distils
  generalizable lessons PER AGENT (`learnings/`). Every agent loads its lessons on the
  next run — the whole company stops repeating its mistakes, not just the engineer.
  These local lessons stay on the machine that learned them (gitignored). The **generic,
  stack-agnostic** ones can be **promoted into a committed `learnings/shared/` tier** that
  ships with the harness, so every clone and every project starts with them —
  `python -m tools.learnings list` then `promote --agent <a> --index N --as "<generic
  rewrite>"`. Promotion is human-gated (no auto-commit), keeping stack/product specifics out
  of the shared tier.
- **Standing product invariants:** the code-writing agents (architect, test author,
  engineer, QA) get the product's hard, code-verifiable rules — unique/check constraints,
  computed-not-stored columns, enums, route+auth surface — **statically extracted from the
  backend each run** (`registry.extract_product_invariants`) and injected as canonical
  context that overrides any guess. They live with the product as docs (`docs/DOMAIN_MODEL.md`,
  `docs/AUTH.md`, `docs/decisions/` ADRs) so a fresh human or agent can use them cold.
- **Security scan:** the engineer's generated code is statically scanned (`eval`,
  `shell=True`, hardcoded secrets, unsafe deserialization, …). Findings appear in the QA
  sign-off and are printed at the PR gate so you see them before merge.
- **Code quality:** generated Python is auto-formatted and import-sorted (ruff) before the
  lint gate, then an *advisory* report (type errors, cyclomatic complexity, lint), a
  frontend-tooling check (ESLint / Prettier / strict TS), and a **dependency lock** (every
  third-party import must be a declared dependency — kills the "builds locally, breaks in a
  clean install" class) are surfaced at the PR gate — so the code stays clean, reproducible,
  and debuggable without destabilizing the engineer⇄QA loop. An opt-in soft gate
  (`QUALITY_GATE`) adds a coverage report and a complexity block. Roadmap in `AI_NATIVE_ROADMAP.md`.
- **Reliable routing:** the routing-critical decisions (triage lane, critic pass/fail,
  design-QA verdict) are validated structured objects with a corrective retry and a safe
  default — not regexes over prose — so a malformed reply can never silently misroute a run.
- **Sandboxed tests:** test runs are resource-limited in Docker (memory/CPU/PID caps).
- **Overseer:** every run is traced (`traces/<id>.jsonl`, tokens/latency) and audited at the
  end by a deterministic **overseer** (`evals/`) — invariants (engineer never touched the
  tests, a feature has a PRD + confirmed stack, nothing shipped on red), loop non-convergence,
  and token/call budget. A high-severity finding flags the run **NEEDS HUMAN REVIEW**.
- **Flight recorder** (`review/run.html`): a visual, zero-LLM dashboard of each run — the
  actual node **path with loops**, where **time** went, **model spend by tier**, and every
  **rework** cycle — so you can *see* what the agents did at a glance. The deeper roadmap of
  observability + 2026-Claude capabilities lives in `AI_NATIVE_ROADMAP.md`.
- **Autonomy metric:** every run reports an `autonomy_rate` — how much you had to intervene
  (clarifications + gate rejects + hand-fixes) versus the agents running unaided. **1.0 means
  you only rubber-stamped the mandatory gates;** every reject/clarification/hand-fix drags it
  down. It's the headline KPI on the flight recorder and the number to drive toward 1.0 — the
  single best measure of whether the platform is getting more autonomous over time.

## Pre-flight checklist

| Check | Command |
|---|---|
| Docker running | `docker ps` |
| gh CLI authenticated | `gh auth status` |
| workspace is a git repo | `cd workspace && git status` |
| LLM backend | `claude --version` (CLI default) — or `echo $ANTHROPIC_API_KEY` if `LLM_BACKEND=api` |

## Testing

Two suites under `tests/`, all mocked — **no API key or Docker needed**:

```bash
pip install -r requirements-dev.txt
pytest tests/ -q          # 401 passed, 3 skipped (live evals)
```

- **`test_architecture.py`** — graph compiles, routing, Q&A caps/escalation, gates, parsers.
- **`test_integration.py`** — drives the real graph CEO→END, incl. the PRD-reject loop.
- **`test_agents.py`** — each agent's inputs/outputs + the "never blocked, always escalates" guarantee.
- **`test_live_eval.py`** — real-LLM quality checks; runs only with `ANTHROPIC_API_KEY`.
- **`test_evals.py`** — the overseer + tracer + triage-eval harness (deterministic).

Before a live run, see **`SMOKE_TEST.md`** (runbook), **`AGENT_AUDIT.md`** (per-agent review),
and **`evals/README.md`** (overseeing the orchestration: tracing, overseer, agent evals).

```bash
python -m evals.triage_eval     # real-LLM accuracy of the Triage classifier (needs the key)
```

## Model tiers

| Tier | Model | Used by |
|---|---|---|
| `fast` | `claude-haiku-4-5` | CEO, PM, QA, DevOps, peer consults — cost floor |
| `strong` | `claude-opus-4-8` | GENERATION: Engineer (code), Design (kit/mockup), Test Author (the correctness oracle) |
| `reason` | `claude-opus-4-8` | THINKING: Architect + Critic (Fable retired) |

**Backend:** by default every call goes through the **claude CLI** (`claude -p`,
billed to your Claude subscription — zero marginal cost, vision included; quality
guards keep generation parity with the API). Set `LLM_BACKEND=api` to use the
metered API instead, where the system prompt is sent as a **cached** block so
retries and clarification re-runs reuse it at ~0.1× input cost.

## Adding a new agent

See `ARCHITECTURE.md` for the full extensibility guide.
Short version: 4 files + edits in `graph/graph.py`. No other existing files change.
