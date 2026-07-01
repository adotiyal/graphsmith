"""
DESIGN DECISION: State is a flat TypedDict, not Pydantic.
- LangGraph checkpoints serialize this to JSON on every node transition.
- We store file PATHS, never file CONTENT.
- All artifact content lives on disk under workspace/.

EXTENSIBILITY RULE:
- Adding a new agent = add its output path here + a deployed flag if needed.
- Never add content/blobs. Only paths, flags, and short strings.
- Optional[str] for paths so the graph can start without them set.
"""

from typing import TypedDict, Optional


class ProjectState(TypedDict):
    # --- Identity ---
    project_id: str
    feature_request: str

    # --- Artifact paths ---
    # Each agent owns one path. New agent = one new path field here.
    prd_path:      Optional[str]   # CEO + PM
    design_path:   Optional[str]   # Design + Architect
    code_path:     Optional[str]   # Engineer
    deploy_path:   Optional[str]   # DevOps  ← new

    # --- Status flags ---
    # Each agent that has a pass/fail outcome owns one flag.
    tests_passed:  bool
    deployed:      bool            # DevOps  ← new
    pr_url:        Optional[str]
    deploy_url:    Optional[str]   # DevOps  ← new

    # --- Loop control (engineer/qa retry loop) ---
    fix_attempts:  int
    error_log:     Optional[str]

    # --- Routing ---
    current_node:  str
    change_type:   Optional[str]   # triage: "feature" (full pipeline) | "bugfix" | "refactor" | "chore" (quick lane)

    # --- Bidirectional Q&A ---
    # qa_log accumulates all questions and answers across the pipeline run.
    # Every agent reads it for context; agents append to it when asking/answering.
    qa_log:            list           # [{from, to, question, answer, round}]
    qa_rounds:         dict           # {agent_name: rounds_used} — CEO Q&A rounds, caps at MAX_QA_ROUNDS
    agent_qa_counts:   dict           # {agent_name: int} — total agent-to-agent consult() calls, caps at MAX_AGENT_INTERACTIONS (10)
    ceo_qa_pending:    Optional[str]  # formatted questions for CEO (triggers interrupt)
    ceo_qa_from:       Optional[str]  # which agent is waiting for CEO's answer
    ceo_qa_answer:     Optional[str]  # CEO's answer, injected by main.py via update_state

    # --- Independent tests (Phase 1.1, TDD) ---
    # Test agent writes tests from PRD acceptance criteria BEFORE the engineer.
    # Engineer must make these pass and must NOT modify them. This is the oracle.
    test_path:         Optional[str]  # path to authoritative tests dir

    # --- Critic review loop (Phase 1.2) ---
    review_attempts:   dict           # {stage: count} — bounds critic retries (MAX_REVIEW_ATTEMPTS)
    review_notes:      Optional[str]  # critic gaps / CEO feedback for the regenerating agent; cleared on consume
    review_action:     Optional[str]  # "pass" | "retry" | "escalate" — routing signal set by critic

    # --- CEO approval gates (Phase 1.3) ---
    prd_approved:      bool           # CEO signed off on the PRD
    pr_approved:       bool           # CEO signed off before the PR is opened
    approval_pending:  Optional[str]  # "prd" | "pr" | None — triggers the approval interrupt
    approval_decision: Optional[str]  # "approve" | "reject" — injected by main.py
    approval_feedback: Optional[str]  # CEO's rejection feedback — injected by main.py

    # --- Tech stack (finalized by the human CEO/CTO during architecture) ---
    # The architect proposes a default stack but must get the CEO/CTO to confirm or
    # change it before committing the tech spec. The human CTO owns this decision.
    tech_stack:           Optional[str]  # the confirmed stack string
    tech_stack_confirmed: bool           # True once the CEO/CTO has finalized it

    # --- Codebase awareness (Phase 2.1, "extend mode") ---
    # When target_repo is set, the pipeline extends an existing repository instead of
    # building greenfield. The Surveyor maps it; downstream agents read that context.
    target_repo:    Optional[str]  # absolute path to the repo to extend (None = greenfield)
    repo_map_path:  Optional[str]  # path to the generated repo-map artifact
    detected_stack: Optional[str]  # stack detected from the existing repo (extend mode)

    # --- Project continuity (single persistent product across runs) ---
    managed_project: bool          # True = the platform's own persistent project (workspace/project)
    project_ledger:  Optional[str]  # summary of features already built — fed to planning agents
    test_files:     list           # relpaths the Test Author wrote — engineer must not touch them

    # --- Execution hardening (Phase 2.3) ---
    security_warnings: list         # static-scan findings on generated code, surfaced to CEO/CTO
    code_quality:      list         # advisory lint/type/complexity + frontend-tooling findings (PR gate)
    code_files:        list         # paths the engineer wrote — QA reads these to review the code (#5)

    # --- Product profile (standing product context; set once by CEO/CTO) ---
    product_profile:   Optional[str]  # category, users, use cases, brand/tone, goals — feeds Design + PM
    # Code-verifiable product invariants (unique/check constraints, computed-not-stored
    # columns, enums, route+auth surface) STATICALLY extracted from the backend each run
    # (registry.extract_product_invariants). Injected into the generation agents that
    # otherwise have NO standing product context — architect/test_author/engineer/qa.
    product_invariants: Optional[str]
    design_mockup_path: Optional[str]  # path to the Design agent's HTML/Tailwind mockup (visual review)
    design_spec_path:  Optional[str]  # stable pointer to design_spec.md (design_path is overwritten by architect)

    # --- Run-and-verify (Phase 4.2 / 4.3) ---
    integration_passed:   bool  # the composed stack came up + smoke + e2e all green
    integration_attempts: int   # bounded loop back to engineer (MAX_INTEGRATION_ATTEMPTS)
    e2e_files:            list  # Playwright specs QA wrote (e2e/*.spec.ts) — engineer must not touch them
    integration_failed_stage: str   # which integration stage failed (compose/health/smoke/.../e2e)
    design_options:       list  # the 3 design directions [{id,title,rationale}] shown to the human
    design_choice:        str   # which direction the CEO/CTO picked (A/B/C)
    e2e_revision_pending: bool  # I4(e): e2e failed on a healthy app → QA revises the specs once
    e2e_revised:          bool  # the bounded spec-revision round is spent

    # --- Design QA (vision verification of the running UI vs the design) ---
    app_screenshot_path:  Optional[str]  # live-app PNG captured by integration while the stack was up
    design_qa_passed:     bool           # vision verdict: the app matches the design
    design_qa_attempts:   int            # bounded loop back to engineer (MAX_DESIGN_QA_ATTEMPTS)

    # --- Design-owned component kit (alignment by construction) ---
    design_component_files:  list           # presentational components design wrote — engineer must not touch
    components_manifest_path: Optional[str] # wiring manifest + REQUIRED MICROCOPY (deterministic conformance)
