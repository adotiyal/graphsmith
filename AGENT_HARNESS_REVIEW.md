# Agent Harness Review

Review of every agent's prompt + skill, thinking like the best human for each role.
Each section: what's working, what's missing or broken, concrete fixes.

---

## PM — Product Manager

**Thinking like:** a senior PM who writes PRDs engineers actually ship from.

### What works
- AC format with stable IDs and surface tags `(ui|backend)` is excellent — it's the backbone of the coverage gate.
- "Never invent features" / "stay within the brief" prevents scope creep.
- `review_notes` loop brings CEO feedback back correctly.

### Gaps

**No success metric.** The PRD has Feature, User Stories, ACs, OOS, Open Questions — but nothing that defines "did this feature succeed?" A great PM always answers: what does winning look like? Without this, Design and QA have no north star beyond "it passes tests." Add a `## Success Metric` section: one quantifiable signal (activation rate, task completion, p50 latency, etc.).

**The `project_ledger` arrives with no instruction.** It's injected into the prompt but the PM is told nothing about what to do with it. The obvious intent is: don't reinvent features already built. Make it explicit — "check the ledger; if this feature overlaps something already shipped, call out the delta explicitly and don't re-specify what already exists."

**"Max 9 ACs" is the wrong guard rail.** Capping at 9 tells the PM to stop writing rather than to reconsider scope. Better rule: "If you find yourself needing more than 9 ACs, the feature is too large — split it or escalate to the CEO." Same issue with "max 5 user stories."

**No priority tier on ACs.** When scope gets cut under time pressure, all ACs look equal. Add `(must|should|nice)` or `P0/P1/P2` to each AC. The test author and engineer can use this to decide what to implement first in constrained situations.

**No persona capture.** Design does discovery later, but the PM phase is where the user is first defined. The PM prompt should produce a one-line user segment ("authenticated users on mobile who just signed up") so Design isn't starting from scratch.

### Fixes
```
## Success Metric
One quantifiable signal that confirms this feature worked. Example: "% of new users 
who complete the first action within 24h increases by X%." If unknown, escalate — 
don't write "TBD."

## User Segment
One line: who this is for, their device/context, and their frequency of use.
```

Add to AC rules: "If you reach 9 ACs without full coverage, the feature is too large — note the scope cut and escalate."

Add `(P0|P1|P2)` to each AC line.

---

## Design

**Thinking like:** a senior product designer who shipped consumer apps at scale.

### What works
- Discovery-first ordering is correct and enforced.
- Three directions with CEO choice is excellent — forces genuine divergence, not aesthetic variation.
- Dual-surface (mobile + desktop) and dual-theme mandates are exactly right for consumer apps.
- Design system section with persistence is strong — prevents drift across features.
- SEO/AEO section is unusually good for a design prompt.

### Gaps

**Direct contradiction in the skill.** `## What NOT to do` says "No animations spec, no dark mode." But the main skill body has an entire `## Dual-theme MANDATE` section. The `What NOT to do` section was written earlier and never updated. Dark mode is now mandatory; that line must be removed or it will confuse the LLM.

**"Hold under any of the three directions" weakens the spec.** The instruction to design flows and screens that work for all three directions means nothing is actually committed. The designer ends up producing the intersection of three ideas, which is usually the least interesting version of any of them. Better: design flows for direction A (the one you think is strongest), clearly note what changes for B and C, and let the CEO pick knowing the full cost of each change.

**Error states are designed, but error recovery is not.** Every screen has loading/success/error/empty — good. But "error" in most specs is just a red message. The best designers specify: when this error occurs, what can the user DO next? What's the recovery action? "Try again," "contact support," "go back and fix X" — these are design decisions, not engineering ones. Add to the flows mandate: "For every error state, specify the recovery action the user can take."

**The three direction mockups need a stronger divergence instruction.** Without explicit guidance, designers tend to produce three layouts that are subtly different (card vs list, blue vs green). The mockup prompt should say: "Each direction mockup must use a visually distinct layout pattern — different navigation structure, different information hierarchy, different visual density. Cosmetic color variations are not directions."

**No loading/skeleton specification.** "Skeletons over spinners" is a principle but skeleton layout is a real design artifact — which elements appear, in what structure, for how long. Skeleton structure should be in Screens & Components: "Loading state: show skeleton of [header + 3 card placeholders]."

**Analytics instrumentation is invisible.** The best designers at data-driven companies think about what events to track. This is especially important because the PM defined a success metric — Design should identify which interactions prove/disprove it. Even a simple "## Instrumentation" section: "Track: [event name] on [user action] to verify AC-N."

**`_build_components` says don't import shadcn, but the design skill explicitly lists shadcn as the component library.** The kit components are supposed to be self-contained (no shadcn dependency) because they ship before shadcn is scaffolded. But the design spec says to use `Button, Input, Dialog...` from shadcn — those are design-time references, not implementation imports. This distinction is never explained in the skill and creates real confusion. The skill should say: "In the design spec, reference shadcn component names as design vocabulary (Button, Dialog, etc.). In the actual kit code, implement them with plain Tailwind — do not import from shadcn."

**Content strategy is missing.** For consumer apps, copy is a first-class design artifact but the current output is just "microcopy" (individual strings). The best designers also specify information hierarchy: what goes above the fold, what's progressive disclosure, what's in an empty state vs. a populated state with real data. Add to `## Content & Microcopy`: "Information hierarchy: [what appears first, what's secondary, what's deferred]."

### Fixes
- Delete "No dark mode" from `## What NOT to do`.
- Change "Design the rest of this spec to hold under any of the three directions" → "Write this spec for direction A (your recommended choice). Note under each direction what specifically changes — so the CEO understands the real cost of B or C."
- Add to Flows: "For each error state, name the recovery action."
- Add to mockup prompt: "Each direction must use a structurally distinct layout (different navigation pattern, information density, or visual hierarchy). Color-only variation is not a direction."
- Add `## Instrumentation` section to the spec output.
- Clarify shadcn-as-vocabulary vs shadcn-as-import in the kit build section.

---

## Architect

**Thinking like:** a staff engineer who has designed 20+ production APIs.

### What works
- "Boring technology wins" is the right default stance.
- Security defaults section is concrete and non-negotiable.
- Data model rules (UUID, timestamps, explicit FKs, indexes) are production-ready.
- The stack confirmation handoff is well-designed.

### Gaps

**The skill says "The Engineer does not see the PRD or Design spec." This is wrong.** The engineer now receives the design spec and mockup via `_read_design()`. The skill's output contract section needs updating — engineers no longer rely solely on the tech spec. This matters because it changes what the architect must include. The architect used to be the only relay for design decisions; now Design's spec reaches the engineer directly. The architect should focus on what's missing from Design's spec: data model completeness, auth, edge cases.

**Max caps send the wrong signal.** "Max 10 endpoints" and "Max 15 files" are practical constraints for prompt size, but they teach the wrong lesson. An architect who has 12 necessary endpoints writes 10 and omits 2. Better: no numerical caps in the skill; instead say "only what this feature needs — if you find yourself listing more than 10 endpoints, the feature is too large and should be flagged." The actual token cap on context is the real constraint.

**No migration plan.** Every feature that touches the data model needs a migration strategy: is it additive? Does it require backfill? Can it run zero-downtime? The tech spec has no `## Migration Plan` section. Alembic is mentioned in the engineer skill but never in the architect's output. An architect who doesn't specify migration semantics is leaving a landmine for the engineer.

**No async/background job section.** Many features require async work: sending emails, processing uploads, refreshing data, scheduling jobs. The current spec format has no place for this. A feature spec that silently omits a required Celery task or background job will produce working tests (sync) and broken production behavior.

**Request/response shapes are prose tables, not schemas.** "Request body / Response body" as a table column is too ambiguous. A senior architect produces something closer to OpenAPI — actual field names, types, required/optional. The current format lets the engineer interpret "creates a new item" in any way they like. Add a field-level schema block for each endpoint's request and response.

**No third-party integration section.** If the feature needs Stripe, SendGrid, S3, Twilio, etc., where does this go in the current spec? Nowhere. Add `## External Dependencies` to the output format: "any third-party API this feature calls, with the endpoint and authentication method."

**Forward tracing from Design to API is missing.** The critic checks tech spec against PRD. But a world-class architect also checks: does every interactive element in the Design spec have a backing endpoint? The architect should explicitly map "Design screen X → endpoint Y" to ensure nothing in the mockup is unbacked.

### Fixes
- Update output contract: "The Engineer reads both this spec AND the Design spec — focus on data models, auth, edge cases, and anything Design doesn't specify."
- Replace "Max 10 endpoints / Max 15 files" with: "List only what's needed. If you exceed 10 endpoints, note that the feature may be too large."
- Add `## Migration Plan` section: additive vs destructive, zero-downtime feasibility, backfill requirements.
- Add `## Async Operations` section: background jobs, queues, scheduled tasks (or "None").
- Add `## External Dependencies` section.
- Expand endpoint format to include field-level request/response schema, not just "Request body / Response body."

---

## Engineer

**Thinking like:** a senior engineer who has shipped production FastAPI + Next.js apps.

### What works
- The same-origin relative paths / Next.js rewrite pattern is exactly right and a live-caught lesson.
- Kit wiring rules (wire, don't modify, don't duplicate) are clear.
- Rate limiting env-gate pattern prevents e2e flakiness.
- Toolchain-detecting runner with layer-specific containers is well thought out.
- `error_log[-6000:]` tail-slicing (vs head-slicing) is correct.

### Gaps

**Kit rule contradiction.** `_read_kit` in the engineer's context says "NEVER modify, rewrite, or re-emit any kit file." But `_build_components` (which the design agent uses) says "On a feature that EXTENDS an existing kit, READ the existing components first and EDIT them in place (add props/fields)." So Design edits kit files in extend mode, but the engineer is forbidden to. The engineer doesn't know this distinction. The engineer-facing rule should say: "Kit files are owned by Design. If a kit component needs new props for this feature, that's a design change — flag it via `===NEEDS_INPUT===`, don't modify the file yourself."

**The error log prompt doesn't tell the engineer what to expect.** The tail is injected but the engineer isn't told "pytest deprecation warnings appear at the top; the actual assertion failures are at the bottom — fix those, not the warnings." This is captured in the CLAUDE.md gotcha but not in the engineer's actual prompt. Add a one-line note: "The tail of the error log contains the real failures — ignore deprecation/warning noise above them."

**Missing: N+1 query prevention.** The engineer skill mentions SQLAlchemy ORM and DB patterns but has no guidance on eager loading, `joinedload`, or when to use `select_in_load`. N+1 is the most common performance problem in ORM-based backends and it's silent until scale.

**Shared types between backend and frontend are invisible.** The backend defines Pydantic schemas; the frontend defines TypeScript interfaces — but there's no instruction to keep them consistent. A world-class engineer generates or verifies shared types. At minimum, add: "Frontend TypeScript interfaces in `types/` must mirror the backend's Pydantic response schemas for the same resources. If they diverge, it's a bug."

**The React Query + api/ client pattern is described but not composed.** The skill says "server state via React Query" and shows an `api/feature.ts` function, but never shows how they connect: `useQuery({ queryKey: ['items'], queryFn: getItems })`. Without this, engineers use React Query with inline fetch calls, defeating the api/ abstraction. Add a complete usage example.

**"No TODOs / No placeholders" is a rule but no guidance on what to do instead.** When the engineer hits a genuinely unclear requirement (no design for a state, missing data field), the rule says don't write a TODO — but what should they do? They should use `===NEEDS_INPUT===`. The engineer skill doesn't mention this fallback.

**Incremental dependency order is not specified.** For a complex feature (DB → models → routes → services → tests → frontend → config), the order matters. An engineer who implements frontend first against nonexistent APIs wastes a fix loop. Add: "Implement in dependency order: DB migrations → models → services → routes → tests → frontend components → wiring → config."

### Fixes
- Reconcile kit rule: "Kit files are design-owned. If a kit component needs new props, flag via `===NEEDS_INPUT===`; don't modify it."
- Add to error_log context: "Real failures are at the tail; warnings at the top are noise."
- Add `## Query Discipline` subsection: "Use `joinedload` / `selectinload` for relations needed in the same response. Never issue N queries in a loop."
- Add: "TypeScript interfaces in `types/` must match backend Pydantic response schemas exactly. Divergence = bug."
- Add full React Query + api/ usage example to Frontend Patterns.
- Add implementation order guideline.

---

## QA

**Thinking like:** a senior QA lead who writes specs that find real bugs.

### What works
- Code review on the pass path (not just rubber-stamping test passage) is excellent.
- The tail-sliced error log for diagnosis is correct.
- Kit selector block — using resolved testids as the only selector source — is the right constraint.
- Isolation mandate (`autouse=True` fixture with API cleanup) is non-negotiable and correct.
- The e2e lint step that drops bad specs before they reach integration is smart.

### Gaps

**The Go/No-Go verdict is buried prose.** The sign-off report ends with "Go / No-go recommendation" in natural language. But the pipeline needs a machine-readable signal to route correctly. Currently `_emit_nogo_feedback` scans for "NO-GO" as a string in the report. This works, but the format should make it unambiguous: require `===VERDICT: GO===` or `===VERDICT: NO-GO===` as a standalone line. Prose at the end of a 250-word report is easy to miss or misparse.

**No severity levels on findings.** A QA report that lists "missing error handling on line 42" and "SQL injection risk in search input" at the same level isn't useful for triage. Add severity: `CRITICAL` (blocks go-live), `MAJOR` (blocks PR), `MINOR` (note for next iteration). The gate logic can act on CRITICAL findings independently.

**No regression check.** The QA prompt gives the QA the current feature's PRD and code — but nothing from the project ledger or existing test suite. A world-class QA lead always asks: "does this break what was there before?" The pass-path prompt should include: "Check the existing test suite results. If any previously-passing tests are now failing, that's a regression — name it as a CRITICAL finding regardless of whether the new feature's tests pass."

**The isolation approach is fragile for complex data graphs.** The mandate is "list entities via `page.request`, delete each." This works for flat data but fails when entities have dependencies (deleting a user before their posts causes FK violations, or the test must delete in reverse dependency order). A better instruction: "Prefer a dedicated `DELETE /api/test/reset` endpoint (engineer must implement one in test mode) for full isolation. Fall back to per-entity cleanup only for simple flat data."

**E2e scope constraint is lines-based, not coverage-based.** "AT MOST 10 tests / ~350 lines" is a practical token cap, but it teaches the wrong mental model. QA should think: "one journey test per distinct user flow, plus the critical unhappy path." Replace with: "Write one test per distinct end-to-end user journey. Multiple ACs can be covered in one journey. The unhappy path counts as a separate journey. If you find yourself writing more than 8 journey tests, the e2e surface is too large for one spec file — split by feature area."

**Performance is never mentioned.** Even a floor: "flag if any API call in the code takes an obviously unbounded query (no LIMIT, no pagination on a collection endpoint)." QA is reading the code — they're the right place to catch this.

### Fixes
- Add `===VERDICT: GO|NO-GO===` as a required line in the sign-off format. The current prose "Go / No-go" becomes the explanation; the verdict line is the machine signal.
- Add `CRITICAL / MAJOR / MINOR` severity tags to findings.
- Add to pass-path prompt: "Review the integration test results — any previously-passing test that now fails is a CRITICAL regression."
- Change isolation guidance: "Use a `DELETE /api/test/reset` endpoint (test-mode only, implemented by the engineer) for full isolation. Per-entity cleanup is fragile for relational data."
- Replace lines/count cap with journey-count framing.
- Add: "Flag collection endpoints with no LIMIT clause or pagination."

---

## Test Author

**Thinking like:** Kent Beck — every test is a specification, not a checkpoint.

### What works
- Independent oracle framing ("marking your own homework") is exactly right.
- Non-empty oracle guard (≥1 `def test_`) prevents silent empty suites.
- `# covers: AC-N` annotation on every test is the right traceability mechanism.
- Clarification protocol for ambiguous ACs ("wrong oracle is worse than missing one") is correct.

### Gaps

**No test data builder pattern.** The skill says "use fixture functions for DB session and test client" but says nothing about test data factories. Without factories, engineers hand-write `{"title": "test", "user_id": "abc123"}` dicts in every test, leading to brittleness when models change. The skill should mandate: "Write a `tests/factories.py` with one builder function per model. Tests call `make_user()`, `make_item()` etc. — never inline raw dicts."

**"Keep each file under 200 lines" is the wrong constraint.** This leads to arbitrary splits. The right constraint is semantic: "one file per route group or feature area (e.g. `test_auth.py`, `test_items.py`, `test_search.py`). Keep files focused on one concern. Split when a file covers multiple unrelated behaviors."

**Frontend vitest guidance is thin.** "render + key interactions + states" is three words. A world-class test author for frontend would specify: test that the component renders without errors, test key user interactions (click, type, submit), test loading state (mock the API call with a pending promise), test error state (mock the API call with a rejection), test empty state. Add a concrete vitest example to the skill.

**No guidance on testing async state.** Many frontend components have loading → data → error state transitions. The skill doesn't mention `waitFor`, `findBy*` vs `getBy*`, or how to mock React Query. These are the most common vitest/testing-library failure modes.

**The self-check round is mentioned in system logic but not in the test author's own prompt.** The system has a "retry once if coverage gaps found" mechanism, but the test author's prompt doesn't tell it to self-check before emitting. Add: "Before emitting your final output, verify: does every AC-N from the list above have at least one test with `# covers: AC-N`? If not, add the missing test or emit `===NEEDS_INPUT===` with the AC that can't be tested as written."

**No guidance on test isolation at the DB level.** The skill says "use a transactional/throwaway test database." But how? SQLite override? A per-test transaction rollback? For FastAPI + SQLAlchemy, the standard pattern is `begin_once` / `rollback_per_test` via a session fixture override. This should be specified, not implied.

### Fixes
- Add `tests/factories.py` mandate with builder function pattern.
- Replace "under 200 lines" with "one file per route group or feature area."
- Add concrete vitest test example (render + interaction + loading + error + empty).
- Add: "Before emitting, self-check AC coverage. Missing coverage → add a test or flag via `===NEEDS_INPUT===`."
- Add SQLAlchemy transactional test isolation pattern to the skill.

---

## DevOps

**Thinking like:** a platform engineer who has operated GCP at scale.

### What works
- Pinned slim/alpine images — no floating `:latest`.
- OIDC auth (not JSON key secrets) is correct and secure.
- Healthcheck on DB so API waits correctly.
- `docker-compose.it-override.yml` pattern for test isolation is well thought out.
- No credentials in files — ever.

### Gaps

**No Alembic migration step in the deploy workflow.** The CI/CD pipeline is: test → build → push → deploy. But deploying a new container with schema changes against an un-migrated database breaks the app. The deploy step must include running `alembic upgrade head` before the new container starts taking traffic. This is the most common deployment failure pattern in SQLAlchemy apps and it's completely absent from the skill.

**`GET /health` is specified but not defined.** The skill says "publishes 8000:8000 and exposes `GET /health` returning 200" but doesn't say what that endpoint should check. A health endpoint that always returns 200 (even when the DB is down) is worse than none — it lies to load balancers. The health endpoint should check DB connectivity: `SELECT 1`. Add: "`GET /health` must verify DB connectivity and return 503 if the DB is unreachable."

**No security scanning in CI.** The pipeline has no Trivy, Snyk, or `pip audit` / `npm audit` step. A world-class CI pipeline catches known CVEs before they reach production. Add a "security scan" step after build: `trivy image --exit-code 1 --severity HIGH,CRITICAL $IMAGE_TAG`.

**Scale to zero cold start is never flagged.** Min instances: 0 is cost-efficient but Cloud Run cold starts add ~1-3s to the first request after idle. For a consumer app this is a visible UX problem. The DevOps skill should note: "min-instances: 0 means cold starts on idle. If the product profile indicates an always-on consumer experience, set min-instances: 1 and note the cost."

**No rollback automation.** The README template mentions "rollback command" but it's manual. A world-class CI pipeline has automatic rollback: if the health check fails after deploy, revert to the previous revision. Cloud Run supports this natively: `--rollout-strategy=gradual` + health-based cutover. Add this as a CI step.

**Secrets Manager vs GitHub Secrets is unclear.** The skill says "DB_USER, DB_PASSWORD always from Secret Manager" (GCP Secrets Manager) but the workflow injects them as `${{ secrets.DB_PASSWORD }}` (GitHub Actions secrets). These are two different systems. Specify: "Inject `DATABASE_URL` as a Cloud Run mounted secret from GCP Secret Manager at deploy time; use GitHub Actions secrets only for CI credentials (GCP_PROJECT_ID, WIF provider)."

### Fixes
- Add migration step to CI: after container push, before traffic swap: `gcloud run jobs execute migrate --region $REGION`.
- Add to health endpoint spec: "Must query `SELECT 1` and return 503 if DB is unreachable."
- Add security scan step: `trivy image` after build.
- Add min-instances cold start caveat to the skill.
- Add automatic rollback guidance using Cloud Run's native health-check cutover.
- Clarify GCP Secret Manager (runtime secrets) vs GitHub Actions secrets (CI credentials).

---

## Triage

**Thinking like:** a delivery lead who has triaged 500+ feature requests.

### What works
- "When in doubt, choose feature" is a safe conservative default.
- Four categories (feature / bugfix / refactor / chore) cover the common cases.
- Single-word output is clean and parseable.

### Gaps

**Single word with no rationale means misclassifications are silent.** If triage mis-classifies "add dark mode to the existing app" as a chore (no PRD, no design, goes straight to engineer), no one knows why it got the quick lane. The word comes back, it routes, and debugging a wrong path is hard. Add: "After the single classification word, on a new line, add a one-sentence rationale: `REASON: <why this type>`." This costs almost nothing and makes misclassifications debuggable.

**No "feature-lite" category.** Some requests are genuinely in between: adding a sort order to an existing list, a new filter on a search page, a minor field addition. These need a brief spec (to prevent scope creep) but not a full PRD + design + architecture cycle. The current system makes them either "feature" (full pipeline, over-engineered) or "bugfix/chore" (no spec, under-governed). A "feature-lite" classification → abbreviated PM brief + straight to architect/engineer would save pipeline cycles.

**No risk signal.** "Add Stripe payment integration" and "add a tooltip" are both "feature" but have wildly different blast radii. The triage output gives no signal about risk level, which means the PM and architect have no head start on where to be careful. Even: `RISK: low|medium|high` as a second output line would be useful.

**The brief fed to triage might be short enough to misclassify.** The user message is `f"Classify this change request:\n\n{brief}\n\nOne word:"` — but `brief` comes from `state.get("prd_path")` or `state.get("feature_request")`. A one-line feature request is very little signal. Consider adding: "If the request is too ambiguous to classify confidently, default to `feature` and note the ambiguity in REASON."

### Fixes
- Change output format: classification word on line 1, `REASON: <one sentence>` on line 2.
- Add `RISK: low|medium|high` on line 3.
- Consider a `feature-lite` type with a defined abbreviated path.

---

## Surveyor

**Thinking like:** a senior engineer doing code archaeology before a risky feature branch.

### What works
- Grounding every claim in the actual repo map (not invented files) is the right discipline.
- Extend-mode guard (greenfield no-op) is well-handled.
- The 5 brief sections (Stack, Plugs In, Reuse, Risks, Questions) are the right structure.

### Gaps

**Keyword extraction is fragile.** `_relevant_excerpts` greps for keywords extracted from the PRD. But codebases use different naming than PRDs — a PRD says "user profile" and the codebase calls it `member_record`. Keyword grep misses this. The surveyor should always read the README/CLAUDE.md/main entry points first (they map the vocabulary), then use keywords from those to find relevant files. Add: "Before keyword search, read the root README.md, any CLAUDE.md, and the main entry files (main.py, index.ts, app.py) — these map the codebase's vocabulary."

**Only 6 excerpts at 1500 chars is thin.** A complex codebase might have the relevant model, router, service, schema, migration, and test all as separate files — that's already 6. The excerpt budget should be higher (10-12 at 2000 chars) or adaptive: "read as many files as needed to produce a confident integration brief."

**No check for currently failing tests.** Before adding a feature, a world-class engineer checks the health of the existing test suite. If 3 tests are already failing before we touch anything, the QA gate will be harder to interpret. Add: "If a test runner is detectable, note the current test status (passing/failing count). If tests are already failing, surface this as a RISK."

**No dependency audit.** The surveyor reads file structure but doesn't look at `package.json` or `requirements.txt`. A dependency audit catches: deprecated packages that the new feature can't use, version conflicts, security vulnerabilities. Add: "Read `requirements.txt` / `package.json` and note: any package that is pinned to an old major version that would conflict with the feature's needs."

**The brief serves two audiences (architect + engineer) with different needs.** The architect needs: data model landscape, auth patterns, migration state. The engineer needs: file naming conventions, import paths, test patterns. One brief serves both but the sections aren't labeled by audience. Consider adding audience tags: "For Architect: …" and "For Engineer: …" within the relevant sections.

### Fixes
- Add step 0: "Read README.md, CLAUDE.md, and main entry files to learn the codebase vocabulary before searching."
- Increase excerpt budget to 10-12 files at 2000 chars.
- Add: "Note current test status if detectable."
- Add: "Read `requirements.txt` / `package.json` and flag dependency conflicts or outdated pins."

---

## Critic

**Thinking like:** a principal engineer doing a technical design review before a sprint.

### What works
- "Pass is a real bar" — not rubber stamping.
- Specific gap format ("AC#3 has no endpoint" not "needs more detail") is excellent.
- JSON-only output is clean for routing.
- Retry-then-escalate loop is well-bounded.

### Gaps

**Only backward-checks (tech spec → PRD). Doesn't forward-check (can this be built/tested unambiguously?).** A world-class technical reviewer also asks: "Could the test author write correct tests from this spec?" and "Could an engineer implement this without ambiguity?" Currently the critic only verifies coverage — it doesn't verify completeness of contracts. Add a forward-check criterion: "Is each endpoint specified with enough detail (method, path, request fields, response fields, status codes) that an engineer and test author can implement and test it without asking questions?"

**No severity on gaps.** A gap like "missing `updated_at` on the model" and "no auth on the payment endpoint" are not the same class of problem. The current output is a flat numbered list. Add severity to each gap: `[CRITICAL] / [MAJOR] / [MINOR]`. This lets the routing logic — or the CEO reading escalations — immediately see what matters.

**The critic doesn't verify the File Structure section.** The tech spec has a `## File Structure` section but the critic's checklist doesn't include it. A missing file in the File Structure means the engineer won't create it, which typically causes an import error. Add: "File Structure: are all files referenced in API endpoints and data models present in the File Structure list? Are the paths consistent with the stack's conventions?"

**The critic doesn't check Design → API coverage.** It checks PRD → tech spec. But there's a second traceability gap: every interactive UI element in the Design spec should have a backing endpoint. If the Design spec has a "Mark as complete" button but no PATCH endpoint for completion status, the engineer will either invent something or raise a blocker. Add: "Design-to-API: verify that each interactive element in the Design spec (forms, buttons that mutate state) has a corresponding write endpoint in the tech spec."

**The critic sees `review_notes` on retries but the skill doesn't acknowledge this.** On a retry, the critic is re-running with its previous gaps in the context. The skill should say: "On a second pass (review_notes present), verify that the architect addressed each gap from the previous review. Don't re-report gaps that were genuinely fixed. Surface any new gaps introduced by the revision."

### Fixes
- Add forward-check criterion: "Completeness of contracts — could a test author write unambiguous tests from this spec?"
- Add `[CRITICAL] / [MAJOR] / [MINOR]` severity to each gap.
- Add File Structure verification to the checklist.
- Add Design-to-API coverage check.
- Add retry-awareness: "On second pass, verify prior gaps were addressed. Don't re-report fixed items."

---

## Design QA

**Thinking like:** a design lead who has shipped 50+ consumer features and seen every drift pattern.

### What works
- "What the user sees must BE the design" is the right bar.
- Single-shot pragmatism (skip if no mockup/screenshot) avoids blocking on tooling.
- "Never block on tooling problems" is a correct safety valve.
- The ALIGNED/MISALIGNED signal is clean and machine-parseable.

### Gaps

**Single vision call is the riskiest point in the pipeline.** One call comparing a full-page screenshot to a full mockup is holistic and prone to false positives and false negatives. The comparison is too coarse — a small misaligned component gets diluted by everything that is aligned. A structured rubric would help: the call should evaluate specific dimensions (copy match, structural match, component presence) and report on each, not just issue a single verdict. Even asking the model to produce a checklist before issuing the verdict would improve reliability.

**The design mandates dual-surface and dual-theme — but Design QA compares one screenshot.** The integration stage captures `tests/app_screenshot.png` while the stack is up — but at what viewport and in which theme? If the screenshot is 1280px desktop in light mode, it never verifies the 375px mobile layout or the dark mode implementation, both of which are first-class design mandates. Design QA should require screenshots at all four combinations (375px light, 375px dark, 1280px light, 1280px dark) and compare each against the corresponding mockup frame. Currently none of this is specified or enforced.

**ALIGNED/MISALIGNED is binary when the real world is gradient.** A build might be 80% aligned with one wrong button label and a missing empty state. "MISALIGNED → engineer" triggers a full fix loop. A severity-aware output would be better: `CRITICAL` misalignments (wrong structure, wrong copy, missing screen) → engineer; `MINOR` misalignments (spacing off, icon slightly wrong) → note for next iteration, proceed to gate.

**"Last set of eyes before the human gate" but the prompt doesn't tell the model what the human will see.** The human at the PR gate sees the QA report, the screenshot, and the mockup. The Design QA prompt should be aware of this audience and write its findings as a handoff brief: "Here is what the human reviewer needs to check manually that automation cannot verify (hover states, transition behavior, keyboard navigation)."

**Design QA has no access to the design spec's microcopy contract.** The vision call compares visual screenshots. But the most common drift is copy: a button says "Create Task" in the mockup and "Add Task" in the app. A pixel comparison won't catch this if the visual structure is similar. Design QA should receive the `REQUIRED MICROCOPY` section from the kit manifest and verify each string is present in the screenshot's text (or the served HTML). This is deterministic and should run before the vision call.

### Fixes
- Replace single-verdict vision call with a structured rubric: "Evaluate each dimension (structural layout, copy, components, states) separately before issuing a verdict."
- Mandate screenshots at multiple viewports/themes: "Capture at 375px and 1280px, in light and dark mode. Compare each combination against the corresponding mockup frame."
- Add severity to findings: CRITICAL (blocks) vs MINOR (notes for next iteration).
- Add audience framing: "Include a section of what the human reviewer must manually verify."
- Add pre-vision microcopy check against the MANIFEST's `REQUIRED MICROCOPY` block.

---

## Cross-Cutting Issues

These aren't specific to one agent but affect the whole pipeline:

**The `qa_log` escalation path is invisible in agent skills.** Every producing agent can emit `===NEEDS_INPUT===` to escalate to the CEO. This is explained in CLAUDE.md but not in any agent's skill. An agent that doesn't know it has this escape hatch will make things up rather than block. Every agent skill should include a one-paragraph "When blocked" section describing the clarification protocol.

**Feedback events are captured but the retro is fast/haiku-tier.** `learnings.run_retro` uses a fast/haiku call to distill lessons. A retro over a pipeline that runs expensive strong/reason calls for generation should use a strong call for the distillation — otherwise the lessons are too generic to be actionable. Consider: "If the run included a gate rejection or a NO-GO, use the strong tier for retro."

**No agent skill references the `project_ledger`.** The ledger is injected into PM and Design context, but no skill tells either agent how to use it. Both skills should have a "Project continuity" section: "The ledger lists features already built. Don't re-specify what's there. For enhancements to existing features, read the ledger entry and specify only the delta."

**The design spec's `## Design System` section is extracted and persisted — but no agent is told this explicitly.** The fact that this section becomes law for future features is critical information for the Design agent. It should be called out prominently: "The `## Design System` section you write WILL BE PERSISTED and fed to every future Design run. Write it as if it's the design constitution for this product, not a spec section."
