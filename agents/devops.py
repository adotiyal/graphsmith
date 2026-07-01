"""
DevOps Agent
------------
Inserted between QA and END with zero changes to any other agent.
This is the proof that the framework is extensible.

Reads:  state["code_path"]   → knows where the code lives
        state["tests_passed"] → decides whether to do a real deploy or dry-run
Writes: state["deploy_path"]  → path to generated IaC files
        state["deployed"]     → bool
        state["deploy_url"]   → URL if deployed, None otherwise

DESIGN DECISION: DevOps generates IaC files (Dockerfile, docker-compose,
GitHub Actions workflow). It does NOT execute the deploy directly.
- Executing deploys requires cloud credentials on the host machine.
- Generating the files is safe, reviewable, and still fully automated.
- You (CEO) can trigger the actual deploy with one command.
- This is the right KISS boundary for v1. Automated execution = v2.

DESIGN DECISION: if tests_passed is False, DevOps writes a dry-run manifest
and sets deployed=False. Pipeline doesn't halt — you get the IaC anyway,
and the failure is visible in the deploy report.

Q&A (escalation principle): DevOps is a decision-making agent too — it may need to
know the deploy target, GCP project/region, custom domain, or secret names. Like
every other agent it can escalate to the CEO via run_with_qa rather than guessing.
"""

from graph.state import ProjectState
from tools.file_io import load_prompt, load_skill, read_artifact, write_artifact
from tools.qa_utils import run_with_qa, work_call, format_qa_context

CONSULT = ["ceo"]


def run(state: ProjectState) -> dict:
    return run_with_qa(state, "devops", _do_work, consultable_agents=CONSULT)


def _do_work(state: dict, qa_log: list, rounds: dict, allow_clarify: bool = True) -> dict:
    identity = load_prompt("devops")
    skill = load_skill("devops")
    from tools.learnings import augment_system
    system = augment_system(f"{identity}\n\n{skill}" if skill else identity, "devops")

    # Read only what devops needs — the tech spec (has stack info) and
    # whether tests passed (determines deploy vs dry-run)
    tech_spec = read_artifact(state["design_path"]) if state.get("design_path") else ""
    tests_ok = state.get("tests_passed", False)
    project_id = state["project_id"]
    qa_ctx = format_qa_context(qa_log, "devops")
    stack = state.get("tech_stack") or "FastAPI backend + Next.js frontend + Postgres"

    deploy_mode = "PRODUCTION DEPLOY" if tests_ok else "DRY RUN (tests did not pass)"

    user_msg = f"""
Project: {project_id}
Deploy mode: {deploy_mode}

CEO/CTO-confirmed stack: {stack}

Tech spec summary (for additional detail):
{tech_spec}

{qa_ctx}

Generate deployment configuration files using the ===FILE:===END=== format.

Required files:
1. Dockerfile          — production-ready, multi-stage if applicable
2. docker-compose.yml  — local dev + CI usage
3. .github/workflows/deploy.yml — GitHub Actions: test → build → push → deploy
4. deploy/README.md    — how to run the deploy, what env vars are needed

Rules:
- Match the confirmed stack above.
- Default target: Google Cloud Run (backend) + Firebase/Vercel Hosting (frontend);
  adapt if the stack differs.
- Use environment variables for all secrets — never hardcode
- GitHub Actions uses OIDC auth to GCP — no stored service account keys
- If deploy mode is DRY RUN: generate files but add a comment at the top
  of deploy.yml: # DRY RUN — tests did not pass, review before deploying
- Keep each file under 100 lines
"""

    questions, raw_output = work_call(system, user_msg, "strong", CONSULT, allow_clarify)
    if questions:
        return {"_clarify": questions}

    # Parse and write deploy files
    deploy_path = _write_deploy_files(raw_output, project_id)

    deploy_url = f"https://console.cloud.google.com/run?project={project_id}" if tests_ok else None

    return {
        "current_node": "devops",
        "deploy_path": deploy_path,
        "deployed": False,   # v1 boundary: generates IaC, does not execute the deploy
        "deploy_url": deploy_url,
        "qa_log": qa_log,
        "qa_rounds": rounds,
        "ceo_qa_from": None,
    }


def _write_deploy_files(raw: str, project_id: str) -> str:
    """
    Reuses the same ===FILE:===END=== parser pattern as engineer.
    DESIGN DECISION: same output format across all code-generating agents.
    No new parsing logic needed.
    """
    import re
    from pathlib import Path
    from tools.file_io import WORKSPACE_ROOT

    pattern = r"===FILE: (.+?)===\n(.*?)===END==="
    matches = re.findall(pattern, raw, re.DOTALL)

    if not matches:
        write_artifact(project_id, "deploy", "deploy_raw.txt", raw)
        return str(WORKSPACE_ROOT / project_id / "deploy")

    for rel_path, content in matches:
        rel_path = rel_path.strip()
        parts = Path(rel_path).parts
        # Everything goes under deploy/ in workspace
        subdir = "deploy"
        filename = rel_path  # preserve nested paths like .github/workflows/deploy.yml
        write_artifact(project_id, subdir, filename, content.strip())

    return str(WORKSPACE_ROOT / project_id / "deploy")
