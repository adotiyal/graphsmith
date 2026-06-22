# Smoke-Test Runbook

A step-by-step guide to validating the pipeline on your machine. The automated tests run
with everything mocked (no key/Docker). The **live** run needs Docker; the LLM backend
defaults to the **claude CLI** (subscription-billed, no API key) — set `LLM_BACKEND=api`
for the metered API. The GitHub CLI is optional (only to open a real PR).

> Why a runbook: the dev environment these were built in has no Docker or `gh`, so the
> live end-to-end run must happen on your machine.

---

## 0. Prerequisites

| Need | Check | Notes |
|---|---|---|
| Python 3.11+ | `python3 --version` | |
| LLM backend | `claude --version` | **default**: claude CLI ($0, no key). Or `LLM_BACKEND=api` + `ANTHROPIC_API_KEY` |
| Docker running | `docker ps` | test + integration stages run in pinned slim/alpine images |
| GitHub CLI | `gh auth status` | OPTIONAL — only to open a real PR (Ship no-ops without it) |

---

## 1. Set up

```bash
cd graphsmith
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # runtime deps + pytest
# LLM defaults to the claude CLI (no key needed). For the metered API instead:
#   export LLM_BACKEND=api ANTHROPIC_API_KEY=sk-ant-...
```

## 2. Run the automated tests first (no key/Docker needed)

```bash
pytest tests/ -q
# expect: 401 passed, 3 skipped   (the 3 skipped are the live evals)
```

If this is red, **stop** — the architecture is broken; don't waste API spend on a live run.

## 3. (Optional) Live agent-quality eval — real LLM, no Docker

```bash
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY pytest tests/test_live_eval.py -q -s
# Runs PM / Architect / Test Author for real and checks their output has substance.
# Cheap (~a few cents). A good canary before the full run.
```

## 4. Full live pipeline

```bash
# Pre-flight
docker ps          # must be running
gh auth status     # only if you want the PR opened
(cd workspace && git init 2>/dev/null; git remote -v)   # PR needs a remote

python main.py
```

**First run only:** you'll be asked for a one-time **product profile** (category, target
users, brand/tone, goals). Type a few lines, end with a blank line — it's saved to
`product/profile.md` and reused for every future feature (Design + PM read it).

Then type a small, well-scoped feature when prompted, e.g.:

```
> A URL shortener API: POST a long URL and get a short code; GET the code redirects (302); unknown code returns 404.
```

### What to expect (in order)

0. **Triage** classifies your request. A net-new **feature** runs the full pipeline below;
   a **bugfix/refactor/chore** takes the **quick lane** — straight to Engineer → QA → PR
   gate → Ship (no PRD/design/architecture/TDD, no DevOps). Try a feature first, then e.g.
   "fix the 404 on the about page" to see the short path.
1. `[ceo] ✓ done` → `[triage] ✓ done` … `[pm] ✓ done`
2. **PRD approval gate** — the PRD prints and pauses:
   ```
   CEO APPROVAL REQUIRED: PRD
   ...
   Approve? [y]es / [n]o:
   ```
   - `y` → continues to Design. `n` → type feedback; it loops back to PM and regenerates.
3. **Design** (does discovery from your product profile; may ask you a brand/user question;
   writes `design/design_spec.md` **and `design/mockup.html`** — open the mockup in a
   browser to *see* the design) → **Design Critic** (may loop the design back to fix gaps)
   → **Architect** → it pauses to confirm the **tech stack** with you (as CTO):
   ```
   QUESTION FROM ARCHITECT  (you answer as CEO/CTO)
   TECH STACK DECISION (CTO call): proposed default is FastAPI (backend) + Next.js
   (frontend) + Postgres (database). Confirm or specify a different one ...
   CEO/CTO>
   ```
   Type `confirm` (use the default) or name a different stack — it's used downstream.
   Then → **Critic** (may silently send the spec back up to 2× to fix gaps; if it still
   can't, it asks **you** a question).
4. **Test Author** writes `tests/`, then **Engineer** writes `src/` and runs the tests in
   Docker (retries up to 3×). First Docker run is slow (pip install).
5. **QA** writes the sign-off, then the **PR approval gate**:
   ```
   CEO APPROVAL REQUIRED: PR
   ```
   - `y` → the **ship** node opens the PR (needs `gh`). `n` → feedback → back to Engineer.
6. **DevOps** writes `deploy/`. Pipeline ends:
   `[+] Pipeline complete. Workspace: workspace/<project-id>/`

Any agent may also pause with `QUESTION FROM <AGENT>` — type an answer and it resumes.

### Inspect the output

```bash
tree workspace/<project-id>/     # prd/ design/ tests/ src/ deploy/
```

## 5. Resume after a crash

```bash
python main.py --resume <project-id>     # continues from the last checkpoint
```

---

## Known constraints to expect (see AGENT_AUDIT.md for the full list)

- **Stack is CTO-confirmed**, defaulting to FastAPI + Next.js + Postgres. The Architect
  pauses for you to confirm or change it. For the smoke test, a backend API feature with
  the default stack is the smoothest path.
- **Extend mode (`--repo <path>`)** now writes back: the Surveyor maps the repo, and the
  test-author + engineer write code/tests **into your repo** at real paths, run its own
  test suite, and won't clobber existing tests. It's new — **point it at a disposable
  clone/branch first** and review the diff (`git diff`) before trusting it. For a clean
  first smoke test, prefer greenfield (no `--repo`); meta-artifacts always stay in
  `workspace/<id>/`.
- **First Docker test run is slow** — it pip-installs into `python:3.11-slim` each time.
- **PR step needs `gh` + a git remote.** Without them the pipeline still completes; the PR
  step just reports a failure string and DevOps still runs.
- **Design now also outputs a visual mockup** (`design/mockup.html`, Tailwind via CDN) —
  open it in a browser to review the screens/states.

## If something fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `TypeError: Invalid checkpointer` | stale `graph.py` | already fixed — pull latest |
| `Docker not found` | Docker not running | start Docker Desktop |
| Engineer loops to 3 fails then DevOps | tests never passed | inspect `workspace/<id>/tests` + the printed error |
| PR step fails | `gh` not authed / no remote | `gh auth login`; add a remote to `workspace/` |
| Hangs at a prompt | it's waiting on you | answer the `Approve?` / `CEO>` prompt |
