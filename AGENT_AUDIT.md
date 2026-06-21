# Agent Audit — Through the Human-Worker Lens

Each agent is judged as you'd judge a human in that role: **does it get the inputs it
needs, produce the right outputs, have the interactions it needs — and is it ever
*blocked*?** The governing principle (your call): an agent must never be stuck on a
decision; it escalates to the CEO, who unblocks it. The CEO is the universal unblocker.

Verified by the test suites in `tests/` (52 tests, all mocked — no API key/Docker).
This doc records what those tests confirmed plus the design-level gaps they exposed.

---

## Escalation guarantee (the "never blocked" principle)

| Agent | Escalates to CEO? | How | Test |
|---|---|---|---|
| PM | ✓ | `NEEDS_INPUT` in work call | `test_pm_escalates_when_blocked` |
| Design | ✓ | `NEEDS_INPUT` | `test_design_escalates_when_blocked` |
| Architect | ✓ | `NEEDS_INPUT` | `test_architect_escalates_when_blocked` |
| Test Author | ✓ | `NEEDS_INPUT` | `test_test_author_escalates_when_blocked` |
| Engineer (first run) | ✓ | `NEEDS_INPUT` | `test_engineer_escalates_when_blocked` |
| QA (pass path) | ✓ | `NEEDS_INPUT` | `test_qa_escalates_when_blocked` |
| **DevOps** | ✓ (added this pass) | `NEEDS_INPUT` | `test_devops_escalates_when_blocked` |
| Critic | ✓ | escalates after bounded retries | `test_critic_retry_then_escalate` |

**Fixed this pass:** DevOps previously had *no* escalation path (it never used
`run_with_qa`) — it would silently guess the deploy target/region/secrets. It now
escalates like every other agent. This was the one true "blocked" agent.

**Remaining non-escalating paths (by design, not blocking):** Engineer on *retry*
(it has the error log and is fixing, not deciding) and QA on *failure* (it is
diagnosing, not deciding). These are fix/diagnose loops, not decision points.

**Open tension for your decision:** after `MAX_QA_ROUNDS = 3` CEO rounds in a single
stage, an agent is *forced to proceed* with assumptions rather than asking a 4th time.
That bounds infinite loops but technically lets an agent proceed un-unblocked. If the
principle is absolute ("CEO unblocks *any* decision, always"), we should raise or
remove this cap. Currently capped — flagged in cross-cutting limitations (#9).

---

## Per-agent review

### CEO
- **Gets:** your one-line `feature_request`. **Produces:** a scoped brief.
- **Human would also have:** company strategy, product context. **But** the human *is*
  the CEO here — this agent only reformats your words. No escalation needed (it's the source).
- **Verdict:** fine for its role.

### PM
- **Gets:** CEO brief. **Produces:** PRD (user stories + acceptance criteria).
- **A real PM also has:** user research, product analytics, stakeholder interviews,
  competitor analysis, the existing backlog. **None of these exist here** — the PM
  reasons purely from the brief and escalates gaps to the CEO.
- **Missing interaction:** cannot talk to real users. Acceptable for v1; escalation covers
  *decisions*, but not *discovery*. **Severity: medium** (Phase 2: feed analytics/user data).

### Design  — UPGRADED (consumer-app design)
- **Now gets:** the standing **product profile** (category, users, brand/tone, goals;
  `tools/product.py`) **+** PRD, and asks the CEO/CTO for feature-specific gaps. It does
  **discovery first** (who/JTBD/brand/success-metric), explores then commits **with
  rationale**, designs the **first-run + unhappy paths**, writes **microcopy**, and is
  reviewed by a **design critic** (`critic_design`). Runs on the `strong` tier. This closed
  the "no discovery / craft-only" gap the audit flagged.
- **Visual output — DONE:** Design also emits a **self-contained HTML/Tailwind mockup**
  (`design/mockup.html`) of the key screens + states with the real microcopy, so the
  CEO/CTO can *see* the design in a browser, not just read it. Skipped for backend-only
  features. The brittle `validate_components` heuristic (#7) still remains.
- **Severity: Resolved** — both the thinking and a visual rendering are now produced.

### Architect
- **Gets:** PRD + design spec. **Produces:** technical spec. Runs on **Opus** (Phase 0).
- **A real architect also has:** the existing codebase, infra/cloud constraints,
  non-functional requirements (scale, latency, SLAs, compliance), a cost budget.
  **Missing:** all of the above.
- **~~Artificial limitation: hardcoded stack~~ — RESOLVED.** The stack is now a **CTO
  decision**. On the first architecture pass the architect proposes a default
  (FastAPI + Next.js + Postgres) and escalates a **mandatory** stack-confirmation to the
  human CEO/CTO before committing the spec; the human confirms or specifies a different
  stack, which is recorded in `state["tech_stack"]` and used downstream (engineer +
  devops). See `agents/architect.py::_ask_stack` and the tests
  `test_architect_confirms_stack_with_cto_*`.

### Critic
- **Gets:** PRD + tech spec. **Produces:** pass/fail + specific gaps; loops or escalates.
- **Verdict:** strong. Only wired for the tech spec today — adding PRD/design critics is
  a few lines. **Severity: low.**

### Test Author
- **Gets:** PRD (acceptance criteria) + tech spec (contracts). **Produces:** the
  authoritative test suite. **Verdict:** good — it has what it needs to encode intent.
- **Coordination risk:** it and the engineer agree on import paths only via the tech
  spec. If the spec is vague about file structure, tests and code can mismatch on imports.
  **Severity: medium.** Mitigation: spec File-Structure section is mandatory (already is);
  consider a shared "module map" artifact.

### Engineer
- **Gets:** tech spec + the authoritative tests (TDD). **Produces:** code that must pass
  them. **Verdict:** good inputs.
- **A real engineer also has:** the existing codebase, an interactive run/debug loop,
  docs/package search, freedom to refactor across many files.
- **Artificial limitations:** greenfield only (no existing code); **<200 lines/file** cap
  (real files are often larger); one-shot generation (+bounded continuation); no
  interactive debugging beyond the test loop. **Severity: high** (greenfield) — this is
  the Phase 2.1 codebase-awareness item.

### QA
- **Gets:** test pass/fail + PRD (on pass). **Produces:** a sign-off report.
- **A real QA also does:** reads the code, exploratory/manual testing, security scanning,
  performance and accessibility audits. **This QA does none of those** — it only confirms
  pytest passed and writes prose. It is the **shallowest agent vs. its human counterpart.**
- **Severity: high.** Recommendation: add static analysis (ruff is present; add `bandit`),
  let QA read the diff, and add a11y/perf checks. Note: the "marking own homework" problem
  is already fixed (tests come from the independent Test Author).

### DevOps
- **Gets:** tech spec + tests_passed. **Produces:** IaC (Dockerfile, compose, GH Actions).
  Now escalates to CEO. **Verdict:** good for v1.
- **Limitations (documented v1 boundaries):** generates IaC but does **not execute** the
  deploy; target hardcoded to GCP Cloud Run + Firebase (escalation now lets it ask, but
  the prompt still defaults). **Severity: medium**, Phase 2.3.

---

## Cross-cutting limitations (artificial limits on "best work")

| # | Limitation | Severity | Escalation helps? | Fix |
|---|---|---|---|---|
| 1 | ~~Greenfield only~~ — **RESOLVED (2.1 v1)**: extend mode surveys the repo, plans against it, and writes code/tests back into it | — | Yes | done (`--repo`; v1 caveats: full-file rewrites, whole-suite run) |
| 2 | ~~Hardcoded stack~~ — **RESOLVED**: stack is now CTO-confirmed at architecture time | — | Yes | done (`architect._ask_stack`) |
| 3 | File-size / output caps can truncate real work | Med | No | Decompose per-module; raise caps (partly mitigated: 8192 + truncation continuation) |
| 4 | ~~Design weak / text-only~~ — **RESOLVED**: discovery + product profile + rationale + microcopy + design critic, **and a self-contained HTML/Tailwind mockup** you can open in a browser | — | Yes | done (`design/mockup.html`) |
| 5 | QA is shallow — no perf, a11y | Low (was High) | No | **QA now reads + reviews the code (#5)** and runs a security scan (2.3); perf/a11y still TODO |
| 6 | `read_artifact` 24k cap can still truncate huge specs | Med | No | RAG / chunked reads (Phase 3) |
| 7 | `validate_components` false positives | Low | n/a | Match against a real component list, not regex |
| 8 | ~~No cross-run learning~~ — **RESOLVED (2.2)**: QA records generalizable lessons; agents load them next run | — | n/a | done (`tools/learnings.py`) |
| 9 | Q&A round cap (3) then force-proceed | Med | n/a | **Decided**: agent-to-agent caps at 3 → escalate to human CEO/CTO who resolves (see below) |
| 10 | Agents escalate only during their work phase, not after output | Low | n/a | Post-hoc review hook |

**Decisions applied (CEO = CTO):** The human is both CEO and CTO — the universal
unblocker for business *and* technical decisions (`skills/ceo.md`). #2 is resolved: the
stack is finalized by the CTO, not hardcoded. #9 is settled per that principle:
agent-to-agent Q&A stays capped at 3 rounds, after which any remaining blocker escalates
to the human CEO/CTO, who resolves it (escalations are framed as CEO/CTO in
`tools/qa_utils.py` and `main.py`). The 3-round CEO cap then force-proceeds using the
human's accumulated directives (never the agent's own guess), as a loop-safety valve.

**None of the remaining items block the smoke test** — they are quality ceilings (Phase 2/3).

---

## Bug found and fixed while wiring up the tests

`graph/graph.py` used `SqliteSaver.from_conn_string(db_path)`, which in the installed
LangGraph returns a **context manager**, not a saver — `compile()` raised
`TypeError: Invalid checkpointer`. The pipeline could not start at all. Fixed to own a
long-lived `sqlite3` connection. This would have killed the live smoke test on the first
line; the architecture test (`test_graph_compiles_with_all_nodes`) now guards it.
