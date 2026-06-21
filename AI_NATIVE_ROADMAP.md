# AI-Native Roadmap — making AgentPlatform the best AI-native software company

**Date:** 2026-06-19 · **Author:** operator review for the CEO/CTO
**Scope:** the latest Claude capabilities (mid-2026) mapped onto *this* repo, plus a concrete
code-quality / static-analysis layer and agent-observability layer. Cost stance per the
CEO/CTO: **respect the zero-marginal-cost `claude-cli` backend; paid/API-only items are
tagged `[PAID]`.**

> This complements — does **not** repeat — `IMPROVEMENT_PLAN.md` (items I1–I20) and
> `AGENT_HARNESS_REVIEW.md`. Those are excellent and largely about *pipeline structure* and
> *prompt quality*. This document adds three things they predate or under-cover:
> **(1)** the model/tool capabilities Anthropic shipped *after* that plan was written
> (Opus 4.6→4.8, adaptive thinking, structured outputs, memory + context editing,
> programmatic tool calling, tool search, Agent-SDK hooks/subagents);
> **(2)** a real **static-analysis / code-quality** layer (your gates today are correctness +
> design-conformance, not code cleanliness); **(3)** **agent observability artifacts** that
> turn `traces/*.jsonl` into something a human can *see*.
> Where an idea overlaps an existing backlog item I cite it by ID (e.g. *extends I7*).

---

## 0. TL;DR

You have built something genuinely rare: a 7-agent, human-as-CEO/CTO pipeline with
independent TDD, critic gates, run-and-verify integration, design-by-construction, a
feedback→retro learning loop, and a deterministic overseer. The structure is ahead of most
"AI software company" demos. The ceiling now is **not** the graph — it's three things:

1. **The agents still reason from training-cutoff memory and parse each other through brittle
   `===MARKER===` text.** The 2026 primitives (structured outputs, web search, memory/context
   editing, programmatic tool calling) remove whole classes of failure you currently fight.
2. **You can't *see* what the agents did.** A run emits a rich trace but the only human view is
   the gate HTML. A per-run "flight recorder" would 10× your debugging and trust.
3. **Code is verified for correctness and design, never for cleanliness.** No type check, no
   complexity budget, no coverage floor, no dependency lock. (This session ships the first
   layer of that — see §3 and §8.)

**The five highest-leverage moves**, ranked:

| # | Move | Area | Effort | Cost | Why it's #1-5 |
|---|------|------|--------|------|----------------|
| 1 | **Structured outputs for the control-plane signals** (NEEDS_INPUT / VERDICT / triage / critic JSON) | Tooling | M | `[PAID]`* | Kills the single largest *mechanical* failure class left after I1 — regex-parsing the model's prose. |
| 2 ✅ | **Per-run "flight recorder" HTML** from `traces/*.jsonl` | Observability | S/M | Free | You cannot improve what you cannot see; this is the cheapest trust/debug win. **Shipped this session.** |
| 3 | **Web search for Architect + Surveyor** | Tooling | S | Free on CLI | Agents stop guessing library versions/APIs from a 2025 memory; grounds specs in reality. |
| 4 ✅ | **Adaptive thinking + `effort`** replacing fixed token tiers | Model | S | Free | Right-sizes reasoning per call; more thinking on architect/critic, less ceremony on PM/QA. **Shipped opt-in this session.** |
| 5 | **Code-quality layer** (type/complexity/coverage + frontend lint) | Code quality | M | Free | Makes the *output* maintainable, not just passing. **Layer 1 shipped this session.** |

`*` Structured outputs are an API feature; on the `claude-cli` backend you get most of the
benefit by moving these signals onto the **tools path** (as I1 did for code-writing). See §5.1.

---

## 1. The 2026 capability lens → where each plugs into your pipeline

Your `tools/llm.py` already runs Opus 4.8 on generation/thinking and Haiku 4.5 on the cost
floor. These are the capabilities layered *on top* of the models that your current design
doesn't yet use:

| Capability | One-liner | Plugs into | Cost |
|---|---|---|---|
| **Adaptive thinking + `effort`** (standard/high/xhigh/max; `budget_tokens` deprecated on 4.7+) | Model decides how much to think, calibrated by an effort knob | `tools/llm.py` tiers → architect/critic/test-author high, PM/QA standard | Free |
| **Interleaved thinking** (auto with adaptive on 4.6+) | Think *between* tool calls, reason on results | engineer fix loop, integration-failure triage | Free |
| **Structured outputs** (`output_format` JSON-schema / `strict:true` tools) | Inference-engine-enforced JSON; no defensive parsing | triage, critic, design-QA verdict, NEEDS_INPUT, QA verdict | `[PAID]` API |
| **Memory tool + context editing** (`context-management-2025-06-27`; ~84% token cut on long agentic runs) | Persistent memory files + auto-compaction of stale turns | extend-mode long runs, `tools/learnings.py` (productizes your hand-rolled memory) | `[PAID]` API |
| **Code execution tool** (sandboxed bash/python; free *with* web search/fetch) | Server-side run-and-iterate sandbox | engineer inner loop, data-seeding, the integration glue | `[PAID]` API |
| **Programmatic tool calling** (PTC, Nov 2025; `allowed_callers`) | Model writes code that calls your tools in a loop, filtering data before it hits context | integration/seed/e2e orchestration, multi-file fan-out | `[PAID]` API |
| **Tool Search Tool** (~85% MCP-token cut) | Discover tool defs on demand vs loading all upfront | when you add many MCP connectors (§5.3) | `[PAID]` API |
| **1M context window** (Opus 4.6 / Sonnet 4.6 beta) | Whole-repo in context | surveyor + engineer on large extend repos (your v1 caveat) | `[PAID]` / metered |
| **Prompt caching 1h TTL** (`ENABLE_PROMPT_CACHING_1H`; reads ~0.1×) | Longer-lived cache prefix across a long pipeline | the stable identity+skill block you already cache | Free (CLI) |
| **Agent SDK hooks** (PreToolUse, PostToolUse, SubagentStop, … 30 events) | Deterministic code at lifecycle points | turn `codegen.sync_back` guards into PreToolUse; auto-format on PostToolUse; retro on SubagentStop | Free |
| **Subagents** (own context/tools/model) | Delegated child agents, parallel + isolated | surveyor fan-out, design's 3 directions in parallel (*extends I9*) | Free (CLI) |
| **MCP connectors** | First-class GitHub/Sentry/Postgres/Linear tools | ship (GitHub), telemetry loop (Sentry), schema introspection (Postgres) | Free + connector |

The rest of this document turns the high-value rows into concrete, file-level work.

---

## 2. Area A — Code quality & static analysis (clean · readable · maintainable · debuggable)

**Today:** `registry.run_linter` runs `ruff check --select=E,F` (errors + undefined names) as a
blocking gate; `scan_security` does regex SAST; design-conformance gates (kit/testid/microcopy/
SEO/theme) are strong. **Missing:** type checking, complexity/dead-code, coverage, formatting,
dependency-lock, and *any* frontend lint/type check. So the system can ship a passing app that is
untyped, over-complex, and inconsistently formatted.

### 2.0 Shipped this session — the code-quality layer (Layer 1) ✅
See §8 for details. In short: `registry.format_code` (ruff `--fix` + `ruff format`) runs before
the lint gate; `registry.code_quality_report` (advisory ruff bug/style + **mccabe complexity** +
**mypy** types, scoped to written files) and `check_frontend_quality_tooling` (ESLint/Prettier/
strict-tsconfig/typecheck presence) surface `state["code_quality"]` at the PR gate. Non-blocking,
graceful, pinned by `tests/test_code_quality.py`. This is the *foundation* the rest builds on.

### 2.1 Promote type + complexity to a **soft gate** — `[Free]` `Effort S` `Impact High` — ✅ SHIPPED 2026-06-19 (opt-in, complexity only)
**Shipped** as a flag-gated soft gate (the config-flag discipline the item itself prescribes):
`registry.check_quality_gate` + `quality_gate_level()` read `QUALITY_GATE` (unset/`off` → no
change; `report` → adds coverage; `block` → fails the engineer round on over-budget COMPLEXITY,
bounded by `MAX_FIX_ATTEMPTS`). Auto-fix (`format_code`) still runs first. **mypy stays advisory
even at `block`** — without per-project config its false positives would cause loop-burn (deliberate).
**Coverage is NOT gated on the engineer** — it can't edit `tests/` to raise it (see §2.2). *Files:*
`tools/registry.py`, `agents/engineer.py`. *Default OFF* — calibrate with `report` over a few runs,
watch the autonomy rate (§3.3 now measures the loop-burn), then flip to `block`. Pinned by
`tests/test_code_quality.py`.

### 2.2 Coverage floor — `[Free]` `Effort S/M` `Impact Med-High` — ✅ SHIPPED 2026-06-19 (report-only)
**Shipped** as report-only line coverage: `registry.measure_coverage` runs `pytest --cov` in a
SEPARATE best-effort Docker pass (own invocation, defensive `pytest-cov` install, `|| true`
everywhere) so it can NEVER fail the correctness run; `_parse_coverage` reads the `TOTAL … NN%`
row; the engineer surfaces `coverage: NN%` in `code_quality` at the PR gate when `QUALITY_GATE≥report`.
**Gating coverage is deliberately deferred** — a coverage floor belongs on `test_author` (which owns
`tests/`), NOT the engineer (blocked from writing tests, so it can't raise coverage). The test author
already writes the oracle, so when gated, coverage is *earned*, not gamed. *Files:*
`tools/registry.py`, `agents/engineer.py`. *Discipline:* report first, gate later — same as 2.1.
Pinned by `tests/test_code_quality.py`.

### 2.3 Dependency lock — `[Free]` `Effort S/M` `Impact Med-High` — *implements I7* — ✅ SHIPPED 2026-06-19
Deterministic check: every import ⊆ declared deps (`requirements.txt`/`pyproject.toml` /
`package.json`); an undeclared dep is surfaced at the PR gate. Kills the hallucinated-`react-query`
drift class called out in your own backlog. **Shipped:** `registry.check_dependencies(project_dir,
files)` — Python imports via `ast` (stdlib via `sys.stdlib_module_names`, first-party + relative
imports excluded; import→dist aliases like `yaml`→`PyYAML` + PEP503 boundary-prefix matching so
`psycopg2`⊆`psycopg2-binary`); JS/TS via regex (scoped/sub-path specifiers reduced to the package,
node builtins/path-aliases/relative excluded). Advisory, FOLDED INTO `code_quality` with a `deps:`
label (no new state field/gate), scoped to written files so extend-mode never flags pre-existing
imports, biased toward precision, silent with no manifest, never raises. *Files:* `tools/registry.py`,
`agents/engineer.py`, `skills/engineer.md`; pinned by `tests/test_dependency_lock.py` (17 tests).

### 2.4 Real SAST + secret scan — `[Free]` `Effort M` `Impact Med`
Your regex `scan_security` is good but shallow. Add `bandit` (Python AST SAST) and `gitleaks`/
`detect-secrets` over the written set, merged into the existing `security_warnings` surface. In the
**DevOps** CI template add `pip-audit` / `npm audit` and `trivy image` (your DevOps harness review
flags all three as missing). *Files:* `registry.scan_security`, `skills/devops.md`.

### 2.5 Frontend quality that actually **runs** — `[Free]` `Effort M` `Impact Med-High`
§2.0 checks the tooling is *present*; the next step is to *run* `next lint`, `tsc --noEmit`, and
`prettier --check` inside the existing `node:22-alpine` vitest container and aggregate the result
(advisory first). This closes the biggest gap: today the TS frontend has zero static enforcement.
*Files:* `registry.detect_toolchains` / `run_project_tests`, engineer skill.

### 2.6 Accessibility + performance budgets — `[Free-ish]` `Effort M` — *implements I13/I14*
axe-core (a11y critical violations) and Lighthouse CWV budgets in the Playwright container at
integration. These are consumer-grade quality floors your design mandates imply but never verify.

---

## 3. Area B — Agent observability & visual artifacts

You already emit a **structured trace** (`tools/trace.py`: `llm_call` with tier/tokens/latency,
`node_exec` with wall-ms) and a **feedback event stream** (`tools/learnings.py`). You render
*gate* HTML but there is **no per-run view of what the agents actually did**. This is the cheapest
high-trust win in the whole document and it's 100% deterministic (zero LLM) — on-brand with your
"`.md` canonical, deterministic HTML for humans" decision in `report_html.py`.

### 3.1 Per-run **flight recorder** — `[Free]` `Effort S/M` `Impact High` — ✅ SHIPPED THIS SESSION
A single `review/run.html` rendered at END from `traces/<id>.jsonl` + the feedback stream:
- **Timeline** of nodes (the *actual* path taken), each with wall-time and token cost.
- **Per-agent cards:** what it read, what it produced (artifact links), its Q&A, and **why** it
  looped (gate reject / critic retry / integration fail / NO-GO) pulled from the feedback events.
- **Token/cost flame** by node and by tier (where is the spend going?).
- **Decision log:** every CEO interrupt, every escalation, every retry — the human-in-the-loop story.
- **Diffs between engineer attempts** (you keep `code_files`; diff attempt N vs N-1).
*Files:* extend `tools/report_html.py` (new `render_run`), call from `main.py`/`live_run.py` at END.
This is largely assembling data you already capture.

### 3.2 Live **Cowork artifact** dashboard — `[Free]` `Effort S` `Impact Med`
A persisted HTML artifact over `traces/` + `workspace/project/` that the CEO/CTO re-opens to see
run history, autonomy rate, cost per feature, and the current ledger — refreshing on open. (This is
exactly the "turn a one-off view into a re-openable page" pattern.)

### 3.3 **Autonomy-rate** metric — `[Free]` `Effort S/M` — *implements I10* — ✅ SHIPPED 2026-06-19
The number a software company actually manages: human interventions per run. **Shipped:**
`run_stats.compute_autonomy(events, state, manual_edits=0)` — deterministic from the trace + final
state: `clarifications` (CEO answers, `qa_log` to=ceo answered) + `rejections` (`prd_gate_reject`/
`pr_gate_reject` feedback events) + `manual_edits` (CTO `cto_handfix` feedback events — the
out-of-band hand-fix log turned out to be a trace-observable manual-edit signal — PLUS an optional
git-diff count hook) = `interventions`; `autonomy_rate = approvals / (approvals + interventions)`
(1.0 = the human only rubber-stamped the mandatory gates). Surfaced as an INFO overseer finding
(never fails a run) + a KPI card leading the flight recorder; computed once in `main.py`/`live_run.py`,
passed to both. Validated on real traces (clean runs 1.0, the 8-reject bugfix run 0.2). *Files:*
`evals/run_stats.py`, `evals/overseer.py`, `tools/report_html.py`, `main.py`, `live_run.py`; pinned
by `tests/test_autonomy.py`. **Makes every other item measurable.** (Git-diff detection of UN-logged
hand-edits remains a hook — the `manual_edits` param — for a future wire-up.)

### 3.4 Mermaid of the **path taken** — `[Free]` `Effort S` `Impact Low-Med`
Render the actual node path (incl. loops) as a `.mermaid` artifact per run — instant visual of
"this run bounced engineer⇄QA three times then passed." You already declared `.mermaid` a
first-class artifact type.

### 3.5 LLM overseer over the trajectory — `[PAID]` `Effort M` — *extends `evals/`*
Your deterministic overseer is great; add an *optional* one-call (strong tier) judge that reads the
trace and flags "this run technically passed but the spec drifted from the brief" — the class of
error determinism misses. Already on your `evals/README` "next" list; the trace makes it feasible.

---

## 4. Area C — Expanded agent tooling

The agents are mostly **single text-in/text-out LLM calls** (plus the I1 codegen tools path). The
2026 way is to give them *real* tools. Ordered by leverage:

### 4.1 Structured outputs for the **control plane** — `Effort M` `Impact High` — *finishes I1/I17* — ✅ SHIPPED 2026-06-19 (3 of 5 — the pure-decision calls)
I1 moved *code-writing* to tools and killed the `===FILE===` corruption class. The **signalling**
markers were text-parsed and brittle — each a regex away from a misroute. **Shipped:**
`tools/llm.call_structured(system, user, schema, tier, images, default, retries)` — appends a strict
"emit ONLY this JSON" contract, extracts the JSON robustly (quote-aware brace scanner, fence-/prose-
tolerant), validates + coerces against a lightweight dependency-free schema (enum/string/bool/int +
required), retries once with a corrective on failure, and returns a SAFE DEFAULT (a traced fallback,
not a silent misroute) if the model never complies. Backend-agnostic (rides `call_llm`, so it works
on the default `claude-cli` backend too — no `[PAID]` API `output_format` needed). Migrated the three
**pure-decision** signals: **triage** change-type (`agents/triage.py`, default feature = safe full
lane), **critic** verdict (`agents/critic.py` — closes a real hole: the FIRST malformed JSON used to
silently fail-open to "pass", now it retries first), **design-QA** verdict (`agents/design_qa.py`,
vision call, default MISALIGNED). Pinned by `tests/test_structured.py` (13 tests) + the migrated
agent tests.
**Deliberately NOT migrated** (the other 2 of 5): `===NEEDS_INPUT===` (`qa_utils`) and the QA
GO/NO-GO verdict are **markers embedded in a LARGE produced artifact** (the agent's work output / the
QA sign-off report) — forcing JSON-only output there would destroy the artifact, and the QA verdict
is informational, not routing (QA always routes to `pr_gate`). Those keep robust marker extraction
(QA's is already structured-signal-first with a prose fallback). **Design boundary: structured output
is for pure-decision calls; marker-in-artifact calls keep robust markers.**

### 4.2 **Web search** for Architect, Surveyor, PM — `Effort S` `Impact High` `[Free on CLI]` — ✅ SHIPPED 2026-06-19 (opt-in; architect + surveyor)
The architect pinned library versions/API shapes from training-cutoff memory; web search lets the
spec agents VERIFY current versions, deprecations, and CVEs. **Shipped** OPT-IN behind
`LLM_WEB_SEARCH` (default OFF = exact current behavior — same discipline as adaptive thinking §5.1):
`call_llm(..., web_search=True)` threads through `work_call`; the **architect** (it pins the stack —
highest value) and **surveyor** (flag outdated/CVE'd deps) request it. CLI backend → `claude -p
--allowed-tools WebSearch` (+ a `_SEARCH_GUARD`, higher max-turns); api backend → the `web_search`
server tool. **SAFE BY DESIGN:** `call_llm` falls back to a plain memory-grounded call on ANY search
failure, and search is skipped on vision calls — so enabling it can never break a run. **PM skipped**
(a PRD is product/business requirements, not version facts — web grounding would add noise, not
signal). *Files:* `tools/llm.py`, `tools/qa_utils.py`, `agents/architect.py`, `agents/surveyor.py`,
architect/surveyor skills. Pinned by `tests/test_web_search.py`. **VERIFY spec quality on a live run
before relying on it** — the CLI WebSearch tool path can't be exercised from a sandbox (the tests
cover the gating, command wiring, and fallback; not a real search).

### 4.3 **MCP connectors** — `Effort M` `Impact High` `[Free + connector]`
Replace shell-outs and close open loops with first-class tools:
- **GitHub MCP** → `ship.py` opens/updates PRs, reads CI status, comments — instead of `gh` shell.
- **Sentry (or similar) MCP** → the **deploy→telemetry→backlog loop** your IMPROVEMENT_PLAN lists as
  still-open: DevOps reads post-deploy errors, files them back as triage input. This is the
  "compounds over time" claim made real.
- **Postgres MCP** → architect/QA introspect a *real* schema (migrations, constraints) instead of
  parsing models off disk (*augments `extract_product_invariants`*).
- **Linear/Jira MCP** → the ledger becomes a real backlog.
When connectors multiply, add **Tool Search** (§1) so they don't bloat context.

### 4.4 **Memory tool + context editing** — `[PAID]` `Effort M` `Impact High (extend mode)`
Your `learnings.py` + `product/*.md` + ledger is a hand-rolled memory system — exactly what
Anthropic productized. On long extend-mode runs, the memory tool + context-editing compaction cut
tokens ~84% on their 100-turn agentic eval while *improving* quality (39%). Use it for the
accumulating cross-feature context; keep your deterministic `product_invariants` as the
"OVERRIDE any learned lesson" floor. *Files:* `tools/llm.py`, `tools/learnings.py`.

### 4.5 **Programmatic tool calling + code execution** — `[PAID]` `Effort M` `Impact Med`
The integration seeding, e2e orchestration, and multi-file fan-out are exactly PTC's sweet spot:
let the model write code that drives your tools in a sandbox, filtering data before it hits
context (lower latency *and* tokens). Pairs with giving the engineer a real "run it and see" inner
loop instead of write-then-Docker.

### 4.6 **Subagents** for parallel exploration — `Effort M` — *extends I9 (latency)*
Agent-SDK subagents (own context/tools/model) let the surveyor fan out across a big repo, or design
generate its 3 directions concurrently, without polluting the main context. Latency + isolation,
not just quality.

---

## 5. Area D — Model usage & architecture

### 5.1 Adaptive thinking + `effort` replaces fixed token tiers — `[Free]` `Effort S` `Impact High` — ✅ SHIPPED (OPT-IN) THIS SESSION
`MAX_TOKENS = {fast:2048, strong:8192, reason:4096}` is a 2025 mental model. On 4.6+ you set an
**effort** level (standard/high/xhigh/max) and the model self-budgets, with interleaved thinking
on automatically. Map: architect/critic/test-author → **high/xhigh**; engineer → **high**;
PM/QA/CEO → **standard**. Better reasoning where it matters, less ceremony where it doesn't.
*Files:* `tools/llm.py` (swap `max_tokens` plumbing for `thinking:{type:"adaptive",effort}`).

### 5.2 Interleaved thinking in the fix/triage loops — `[Free]` `Effort S`
The engineer fix loop and integration-failure triage are precisely "reason about a tool result,
then act" — turn on interleaved thinking there so the model reasons on the *tail* of the error log
before editing (reinforces your "real failures are at the tail" gotcha).

### 5.3 1M context for the surveyor/engineer on big repos — `[PAID/metered]` `Effort S`
Your extend-mode v1 caveat is "engineer reads only spec-referenced files." With 1M context the
surveyor can map the whole repo and the engineer can see every file it touches — directly attacks
the "no broad semantic retrieval" limitation. Gate it to large repos to control cost.

### 5.4 Prompt cache 1h TTL on the stable prefix — `[Free on CLI]` `Effort S`
You already cache the system block. A single feature run spans many calls over many minutes; the
default 5-min TTL (regressed in early 2026) lets the prefix expire mid-run. Set
`ENABLE_PROMPT_CACHING_1H` for the identity+skill prefix (1h write is 2× but reads are ~0.1× and
you get many reads per run). Pure savings.

### 5.5 Agent-SDK **hooks** as deterministic guardrails — `[Free]` `Effort M`
Your `codegen.sync_back` guard (oracle/kit/escape protection) is conceptually a **PreToolUse**
hook; a **PostToolUse** hook could auto-run `format_code`/lint after every write; a **SubagentStop**
hook could trigger the retro. Hooks make the guarantees structural rather than agent-cooperative.

### 5.6 Eval-driven tool development — `[Mixed]` `Effort M` — *extends `evals/`*
Anthropic's "writing tools for agents" prescribes **prototype → evaluate → collaborate**: every new
tool (§4) ships with a small eval measuring whether agents use it correctly. You already have the
harness culture (`evals/`, triage eval); extend it so new tools are *measured*, not vibes.

---

## 6. Sequenced plan

**Wave 1 — this week · free · low-risk (see what's happening, right-size thinking):**
code-quality layer ✅ (§2.0) · flight-recorder HTML ✅ (§3.1) · adaptive thinking + effort
✅ opt-in (§5.1) · interleaved thinking in loops (§5.2) · 1h prompt cache (§5.4).
**Wave 1 is now substantially shipped — see §8.**

**Wave 2 — grounding + cleanliness — ✅ COMPLETE (2026-06-19):** web search for architect/surveyor
✅ (§4.2 — opt-in, verify live) · structured control-plane signals ✅ (§4.1 — 3/5) · coverage +
complexity soft-gates ✅ (§2.1–2.2 — opt-in) · dependency lock ✅ (§2.3, I7) · autonomy-rate metric
✅ (§3.3, I10). All five shipped; the opt-in items (web search, soft-gate) await live calibration.

**Wave 3 — compounding (the "gets better over time" leap):** GitHub + Sentry + Postgres MCP
(§4.3) incl. the deploy→telemetry→backlog loop · memory tool + context editing (§4.4) ·
subagents/parallelism (§4.6, I9) · promote the soft gates to blocking.

**Wave 4 — frontier polish:** programmatic tool calling + code-execution inner loop (§4.5) ·
a11y/perf budgets (§2.6, I13/I14) · real SAST + CI scans (§2.4) · LLM trajectory overseer (§3.5).

---

## 7. Free vs paid — at a glance

| Free on `claude-cli` (subscription) | `[PAID]` API / metered (flag before adopting) |
|---|---|
| Code-quality layer, soft/blocking gates, coverage, dep-lock, SAST | Structured outputs (or do it free via the tools path) |
| Flight recorder, dashboards, autonomy metric, mermaid | Memory tool + context editing |
| Web search (Claude Code `WebSearch`), MCP connectors | Code execution + programmatic tool calling |
| Adaptive thinking + effort, interleaved thinking, 1h cache | 1M context (metered by tokens) |
| Agent-SDK hooks, subagents/parallelism | Tool Search Tool, LLM trajectory overseer |

Note: the Agent SDK now includes a **separate monthly credit** on subscription plans
($20 Pro / $100 Max-5× / $200 Max-20×, from 2026-06-15) — worth confirming against your run
volume if you lean on SDK-metered features.

---

## 8. What shipped in this session (code-quality layer, Layer 1)

A non-blocking **code-quality layer** that augments — never destabilizes — the proven E,F lint gate.

**New in `tools/registry.py`:**
- `format_code(project_dir)` — `ruff check --fix` (import-sort, pyupgrade, unused-import cleanup)
  then `ruff format`. Runs in the engineer **before** the lint gate, so the blocking gate passes
  more often and code is consistently styled. Non-blocking; graceful if `ruff` is absent.
- `code_quality_report(project_dir, files)` — **advisory** findings: ruff bug/style families +
  **mccabe cyclomatic complexity (C90)** + **mypy** static types, scoped to the files the engineer
  wrote (so extend-mode runs never report a big repo's pre-existing debt).
- `check_frontend_quality_tooling(project_dir)` — deterministic (no Node): flags a frontend missing
  **ESLint / Prettier / a strict `tsconfig` / a typecheck script**.
- Pure, unit-tested parsers `_parse_ruff_statistics`, `_mypy_error_count`.

**Wired in:** `agents/engineer.py` (auto-format before lint; compute `code_quality` on the pass
path) · `graph/state.py` (`code_quality` field) · `tools/report_html.py` (advisory card at the PR
gate, beside Security findings) · `skills/engineer.md` (`## Code quality` mandate: type hints,
docstrings, complexity <10, frontend tooling) · `requirements-dev.txt` (**pins `ruff`** — previously
assumed-but-unpinned — and adds `mypy`).

**Design rule honored:** *augment the proven gate with auto-fix + advisory reports; don't bolt on a
new blocking gate that destabilizes the engineer⇄QA loop.* Promotion to a gate is §2.1, deliberately
deferred until the numbers prove safe.

**Tests:** `tests/test_code_quality.py` — pure parsers, graceful degradation (no ruff/mypy), the
fully-deterministic frontend check, and engineer surfacing (mirrors
`test_hardening.py::test_engineer_surfaces_security_warnings`).

**Verification note:** the sandbox here has no PyPI access, so the full `pytest tests/` suite could
not be executed in-session. The new logic was verified with a **stdlib-only harness** (syntax of all
edited files; parser correctness; graceful no-tool paths; the deterministic frontend check across
five cases). **Run `pip install -r requirements-dev.txt && pytest tests/ -q` in your environment to
confirm the full suite + the new tests are green before relying on it.**

---

### Also shipped this session — Wave 1 observability + model knob

- **Flight recorder (§3.1)** — `tools/report_html.render_run` → `review/run.html`: a deterministic,
  zero-LLM visual dashboard built from the trace via `run_stats.aggregate` — the actual node
  **path with loops** (engineer⇄QA bounces, ceo_qa pauses, critic retries as repeated chips),
  **where wall-time went** per node, **model spend by tier** (cost flame), and a **loops/rework**
  summary, cross-linking the gate + audit pages. Wired at END in `main.py` and `live_run.py` next
  to `render_audit` (best-effort, never breaks a run). Verified by rendering real `traces/*.jsonl`
  in-session; pinned by `tests/test_run_stats.py`. This is the visual answer to "let me *see* what
  the agents did" — the audit covers *who/why*, the recorder covers *what/where/how-long*.
- **Adaptive thinking + effort (§5.1, opt-in)** — `tools/llm.EFFORT` + `LLM_THINKING=adaptive`:
  sends `thinking={"type":"adaptive","effort":…}` on the api backend, mapping **high** effort to
  architect/critic/engineer and **standard** to the cost floor. **DEFAULT OFF** (zero behavior
  change); `_api_call` falls back to a plain request if the backend rejects the param, and text
  extraction is now robust to thinking blocks. Pinned by `tests/test_llm_thinking.py`.
  **Verify on a live run before enabling** — the exact param shape couldn't be checked against a
  live backend from this sandbox.

## 9. Sources (research, June 2026)

- Building effective agents — https://www.anthropic.com/research/building-effective-agents
- Multi-agent research system — https://www.anthropic.com/engineering/multi-agent-research-system
- Writing effective tools for agents — https://www.anthropic.com/engineering/writing-tools-for-agents
- Advanced tool use (tool search + PTC) — https://www.anthropic.com/engineering/advanced-tool-use
- Extended/adaptive thinking — https://platform.claude.com/docs/en/build-with-claude/extended-thinking
- Structured outputs — https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- Memory tool — https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- Context editing — https://platform.claude.com/docs/en/build-with-claude/context-editing
- Code execution tool — https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/code-execution-tool
- Programmatic tool calling — https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling
- Prompt caching — https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Agent SDK overview — https://code.claude.com/docs/en/agent-sdk/overview
- Agent SDK subagents — https://platform.claude.com/docs/en/agent-sdk/subagents
- Claude Code hooks reference — https://www.morphllm.com/claude-code-hooks
