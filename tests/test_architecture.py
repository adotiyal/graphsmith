"""
SET 1 — Architecture tests.

Verifies the framework itself works: the graph compiles and is wired correctly,
every routing function sends the right way, the Q&A control flow (caps, escalation,
force-proceed) terminates, the gate/ceo_qa nodes behave, and the parsers/tools are
correct. No agent LLM behavior here — that's Set 2.
"""

import json

import pytest

from conftest import base_state, seed


# ── Graph wiring ──────────────────────────────────────────────────────────────

def test_graph_compiles_with_all_nodes():
    from graph.graph import build_graph
    g = build_graph(":memory:")
    nodes = set(g.get_graph().nodes.keys())
    for n in ["ceo", "pm", "prd_gate", "design", "architect", "critic_architect",
              "test_author", "engineer", "qa", "pr_gate", "ship", "devops", "ceo_qa"]:
        assert n in nodes, f"missing node {n}"


def test_interrupt_nodes_configured():
    # The blocking gates + clarification node must be interrupt_before nodes.
    from graph.graph import build_graph
    g = build_graph(":memory:")
    for n in ["ceo", "ceo_qa", "prd_gate", "pr_gate"]:
        assert n in g.interrupt_before_nodes, f"{n} should be an interrupt point"


# ── Routing functions ─────────────────────────────────────────────────────────

def test_pm_routing():
    from graph.graph import pm_routing
    assert pm_routing(base_state()) == "prd_gate"
    assert pm_routing(base_state(ceo_qa_pending="q", ceo_qa_from="pm")) == "ceo_qa"


def test_triage_routing():
    from graph.graph import triage_routing
    assert triage_routing(base_state(change_type="feature")) == "pm"
    assert triage_routing(base_state(change_type=None)) == "pm"        # safe default
    for t in ("bugfix", "refactor", "chore"):
        assert triage_routing(base_state(change_type=t)) == "engineer"


def test_ship_routing_skips_devops_in_quick_lane():
    from graph.graph import ship_routing
    assert ship_routing(base_state(change_type="feature")) == "devops"
    assert ship_routing(base_state(change_type="chore")) == "end"


def test_qa_routing_giveup_is_lane_aware():
    from graph.graph import qa_routing, MAX_FIX_ATTEMPTS
    fail = dict(tests_passed=False, fix_attempts=MAX_FIX_ATTEMPTS)
    assert qa_routing(base_state(change_type="feature", **fail)) == "devops"
    assert qa_routing(base_state(change_type="bugfix", **fail)) == "pr_gate"


def test_prd_gate_routing():
    from graph.graph import prd_gate_routing
    assert prd_gate_routing(base_state(prd_approved=True)) == "surveyor"   # survey before design
    assert prd_gate_routing(base_state(prd_approved=False)) == "pm"


def test_surveyor_routing():
    from graph.graph import _needs_ceo_qa
    route = _needs_ceo_qa("surveyor", "design")
    assert route(base_state()) == "design"
    assert route(base_state(ceo_qa_pending="q", ceo_qa_from="surveyor")) == "ceo_qa"


def test_critic_routing_pass_retry_escalate():
    from graph.graph import critic_architect_routing
    assert critic_architect_routing(base_state(review_action="pass")) == "test_author"
    assert critic_architect_routing(base_state(review_action="retry")) == "architect"
    assert critic_architect_routing(base_state(review_action="escalate")) == "ceo_qa"


def test_critic_design_routing():
    from graph.graph import critic_design_routing
    assert critic_design_routing(base_state(review_action="pass")) == "architect"
    assert critic_design_routing(base_state(review_action="retry")) == "design"
    assert critic_design_routing(base_state(review_action="escalate")) == "ceo_qa"


def test_qa_routing():
    from graph.graph import qa_routing, MAX_FIX_ATTEMPTS
    # tests green → integration (prove the app actually RUNS) before the human gate
    assert qa_routing(base_state(tests_passed=True)) == "integration"
    assert qa_routing(base_state(tests_passed=False, fix_attempts=1)) == "engineer"
    assert qa_routing(base_state(tests_passed=False, fix_attempts=MAX_FIX_ATTEMPTS)) == "devops"
    assert qa_routing(base_state(tests_passed=True, ceo_qa_pending="q", ceo_qa_from="qa")) == "ceo_qa"


def test_integration_routing():
    from graph.graph import integration_routing
    from agents.integration import MAX_INTEGRATION_ATTEMPTS
    # green stack → design QA (does it LOOK right?) before the human gate
    assert integration_routing(base_state(integration_passed=True)) == "design_qa"
    # failure with attempts left → loop the engineer
    assert integration_routing(base_state(integration_passed=False,
                                          integration_attempts=1)) == "engineer"
    # cap hit → proceed to the gate anyway (CEO sees the red report; always completes)
    assert integration_routing(base_state(integration_passed=False,
                                          integration_attempts=MAX_INTEGRATION_ATTEMPTS)) == "pr_gate"


def test_design_qa_routing():
    from graph.graph import design_qa_routing
    from agents.design_qa import MAX_DESIGN_QA_ATTEMPTS
    assert design_qa_routing(base_state(design_qa_passed=True)) == "pr_gate"
    # single-shot (cost control): one misaligned verdict → one engineer fix round
    assert design_qa_routing(base_state(design_qa_passed=False,
                                        design_qa_attempts=0)) == "engineer"
    assert design_qa_routing(base_state(design_qa_passed=False,
                                        design_qa_attempts=MAX_DESIGN_QA_ATTEMPTS)) == "pr_gate"


def test_pr_gate_routing():
    from graph.graph import pr_gate_routing
    assert pr_gate_routing(base_state(pr_approved=True)) == "ship"
    assert pr_gate_routing(base_state(pr_approved=False)) == "engineer"


def test_devops_can_escalate():
    from graph.graph import devops_routing
    assert devops_routing(base_state()) == "end"
    assert devops_routing(base_state(ceo_qa_pending="q", ceo_qa_from="devops")) == "ceo_qa"


def test_ceo_qa_return_routing_includes_all_askers():
    from graph.graph import ceo_qa_return_routing
    for who in ["pm", "surveyor", "design", "architect", "test_author", "engineer", "qa", "devops"]:
        assert ceo_qa_return_routing(base_state(ceo_qa_from=who)) == who
    # critic escalation proceeds forward, not back
    assert ceo_qa_return_routing(base_state(ceo_qa_from="design_critic")) == "architect"
    assert ceo_qa_return_routing(base_state(ceo_qa_from="architect_critic")) == "test_author"


# ── Q&A control flow (real run_with_qa, stub task_fns) ────────────────────────

def test_run_with_qa_no_questions_passes_through(llm):
    from tools.qa_utils import run_with_qa
    out = run_with_qa(base_state(), "pm", lambda s, q, r, allow_clarify=True: {"done": 1},
                      consultable_agents=["ceo"])
    assert out["done"] == 1


def test_run_with_qa_escalates_ceo(llm):
    from tools.qa_utils import run_with_qa
    tf = lambda s, q, r, allow_clarify=True: {"_clarify": {"ceo": "scope?"}}
    out = run_with_qa(base_state(), "pm", tf, consultable_agents=["ceo"])
    assert out["ceo_qa_pending"] == "scope?"
    assert out["ceo_qa_from"] == "pm"


def test_run_with_qa_peer_consult_then_proceed(llm):
    from tools.qa_utils import run_with_qa
    llm.default = "peer says: use email+password"
    calls = {"n": 0}
    def tf(s, q, r, allow_clarify=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"_clarify": {"design": "what fields?"}}
        return {"done": True}
    out = run_with_qa(base_state(), "architect", tf, consultable_agents=["ceo", "design"])
    assert out["done"] is True
    assert out["agent_qa_counts"]["architect"] == 1  # one peer consult happened


def test_run_with_qa_agent_cap_then_escalates(llm):
    from tools.qa_utils import run_with_qa, MAX_AGENT_INTERACTIONS
    llm.default = "peer answer"
    # Always asks a peer → exhausts the 3-interaction cap → escalates to CEO.
    tf = lambda s, q, r, allow_clarify=True: {"_clarify": {"design": "again?"}}
    out = run_with_qa(base_state(), "architect", tf, consultable_agents=["ceo", "design"])
    assert out["ceo_qa_pending"]                       # not silently dropped
    assert out["agent_qa_counts"]["architect"] == MAX_AGENT_INTERACTIONS


def test_run_with_qa_force_proceeds_when_rounds_exhausted(llm):
    from tools.qa_utils import run_with_qa, MAX_QA_ROUNDS
    # CEO question but rounds already exhausted → must force output (allow_clarify=False),
    # never loop forever.
    seen = {"forced": False}
    def tf(s, q, r, allow_clarify=True):
        if not allow_clarify:
            seen["forced"] = True
            return {"done": "forced"}
        return {"_clarify": {"ceo": "still?"}}
    out = run_with_qa(base_state(qa_rounds={"pm": MAX_QA_ROUNDS}), "pm", tf,
                      consultable_agents=["ceo"])
    assert seen["forced"] and out["done"] == "forced"


# ── Parsers ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("## Feature\nplain artifact", None),
    ('===NEEDS_INPUT===\n{"ceo": "x?", "pm": null}\n===END===', {"ceo": "x?"}),
    ('===NEEDS_INPUT===\n{"design": "fields?"}\n===END===', {"design": "fields?"}),
    ('===NEEDS_INPUT===\n{"ceo": null, "pm": "none"}\n===END===', None),
    ('===NEEDS_INPUT===\n{bad json}\n===END===', None),
])
def test_parse_needs_input(raw, expected):
    from tools.qa_utils import _parse_needs_input
    assert _parse_needs_input(raw) == expected


# The critic verdict is now a VALIDATED structured decision (call_structured); this unit
# covers the critic-specific gaps-normalization on the already-validated dict. (Malformed-
# reply → fail-open and the JSON extraction itself are covered in test_structured.py.)
@pytest.mark.parametrize("data,verdict,has_gaps", [
    ({"verdict": "pass", "gaps": None}, "pass", False),
    ({"verdict": "fail", "gaps": "1. missing rate limit"}, "fail", True),
    ({"verdict": "fail", "gaps": "none"}, "fail", False),     # literal "none" → no gaps
    ({"verdict": None, "gaps": None}, "pass", False),          # absent → fail-open to pass
])
def test_critic_verdict_and_gaps(data, verdict, has_gaps):
    from agents.critic import _verdict_and_gaps
    v, gaps = _verdict_and_gaps(data)
    assert v == verdict
    assert (gaps is not None) == has_gaps


# ── Gate + ceo_qa node logic ──────────────────────────────────────────────────

def test_prd_gate_approve_and_reject():
    from agents import prd_gate
    a = prd_gate.run(base_state(approval_decision="approve"))
    assert a["prd_approved"] is True and a["approval_pending"] is None
    r = prd_gate.run(base_state(approval_decision="reject", approval_feedback="add SSO"))
    assert r["prd_approved"] is False and r["review_notes"] == "add SSO"


def test_gates_default_deny_without_explicit_decision():
    # A gate reached with NO decision means no human approved — must NOT approve.
    # (A live run's driver auto-resumed past the interrupt; default-approve then
    # shipped red code with zero sign-off.)
    from agents import pr_gate, prd_gate
    out = pr_gate.run(base_state(approval_decision=None))
    assert out["pr_approved"] is False
    out2 = prd_gate.run(base_state(approval_decision=None))
    assert out2["prd_approved"] is False


def test_qa_fail_at_cap_quick_lane_marks_pr_approval_pending(llm, ws):
    # Quick lane at the retry cap routes to pr_gate — QA must set approval_pending so
    # drivers PAUSE for the human instead of sailing through the interrupt.
    from agents import qa
    from graph.graph import MAX_FIX_ATTEMPTS
    llm.default = "ROOT CAUSE: x\nFIX: y"
    out = qa.run(base_state(tests_passed=False, change_type="bugfix",
                            fix_attempts=MAX_FIX_ATTEMPTS, error_log="boom"))
    assert out["approval_pending"] == "pr"
    # feature lane at cap goes to devops (no gate) — must NOT set it
    out2 = qa.run(base_state(tests_passed=False, change_type="feature",
                             fix_attempts=MAX_FIX_ATTEMPTS, error_log="boom"))
    assert "approval_pending" not in out2


def test_overseer_flags_unapproved_ship():
    from evals.overseer import check_invariants
    # ship ran (pr_url set) but nobody approved → HIGH finding
    bad = base_state(pr_url="https://github.com/x/pr/1", pr_approved=False, tests_passed=False)
    findings = {f["check"]: f for f in check_invariants(bad)}
    assert findings["no_silent_red_ship"]["ok"] is False
    # explicit CEO approval of an imperfect diff is allowed (red + approved)
    ok = base_state(pr_url="https://github.com/x/pr/1", pr_approved=True, tests_passed=False)
    findings2 = {f["check"]: f for f in check_invariants(ok)}
    assert findings2["no_silent_red_ship"]["ok"] is True


def test_pr_gate_reject_loops_back_to_engineer_state():
    from agents import pr_gate
    r = pr_gate.run(base_state(approval_decision="reject", approval_feedback="fix error handling"))
    assert r["pr_approved"] is False
    assert r["tests_passed"] is False          # forces a re-test cycle
    assert "fix error handling" in r["error_log"]


def test_ceo_qa_records_answer_into_log():
    from agents import ceo_qa
    st = base_state(ceo_qa_from="architect",
                    ceo_qa_answer="multi-tenant",
                    qa_log=[{"from": "architect", "to": "ceo", "question": "tenancy?"}])
    out = ceo_qa.run(st)
    assert out["ceo_qa_pending"] is None
    entry = [e for e in out["qa_log"] if e["to"] == "ceo"][0]
    assert entry["answer"] == "multi-tenant"


# ── file_io + registry tools ──────────────────────────────────────────────────

def test_read_artifact_truncation(ws):
    from tools import file_io
    p = ws / "big.txt"
    p.write_text("x" * (file_io.MAX_READ_CHARS + 50), encoding="utf-8")
    out = file_io.read_artifact(str(p))
    assert "[... truncated ...]" in out
    assert len(out) <= file_io.MAX_READ_CHARS + len("\n\n[... truncated ...]")


def test_get_artifact_for_engineer_reads_code_files_when_path_is_dir(ws):
    # Managed/extend mode: code_path is the repo DIRECTORY, not a file. The consult
    # helper must read code_files instead of choking on the directory (IsADirectoryError).
    from tools.qa_utils import _get_artifact_for_agent
    repo = ws / "proj"
    repo.mkdir()
    f = repo / "app" / "main.py"
    f.parent.mkdir(parents=True)
    f.write_text("APP_MARKER = 1\n", encoding="utf-8")
    state = {"code_path": str(repo), "code_files": [str(f)]}
    ctx = _get_artifact_for_agent(state, "engineer")
    assert "APP_MARKER = 1" in ctx and "main.py" in ctx


def test_get_artifact_for_engineer_reads_file_path_directly(ws):
    # Greenfield: code_path may be a single file — still read it directly.
    from tools.qa_utils import _get_artifact_for_agent
    p = ws / "code.py"
    p.write_text("X = 2\n", encoding="utf-8")
    ctx = _get_artifact_for_agent({"code_path": str(p)}, "engineer")
    assert "X = 2" in ctx


# ── Toolchain detection (right test tool for the right stack) ─────────────────

def test_detect_toolchains_flat_python(tmp_path):
    from tools.registry import detect_toolchains
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): assert True\n")
    layers = detect_toolchains(str(tmp_path))
    assert layers == [{"kind": "python", "dir": "", "runner": "pytest"}]


def test_detect_toolchains_split_fullstack(tmp_path):
    # The default stack: FastAPI backend + Next.js frontend in sibling dirs.
    from tools.registry import detect_toolchains
    be = tmp_path / "backend"; be.mkdir()
    (be / "pyproject.toml").write_text("[project]\nname='x'\n")
    (be / "tests").mkdir(); (be / "tests" / "test_api.py").write_text("def test_a(): assert 1\n")
    fe = tmp_path / "frontend"; fe.mkdir()
    (fe / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^2.0.0"}}))
    layers = detect_toolchains(str(tmp_path))
    kinds = {l["kind"]: l for l in layers}
    assert kinds["python"]["dir"] == "backend" and kinds["python"]["runner"] == "pytest"
    assert kinds["node"]["dir"] == "frontend" and kinds["node"]["runner"] == "vitest"


def test_detect_toolchains_picks_jest_and_empty(tmp_path):
    from tools.registry import detect_toolchains
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"jest": "^29"}}))
    layers = detect_toolchains(str(tmp_path))
    assert layers == [{"kind": "node", "dir": "", "runner": "jest"}]
    # python markers without any test files are not a testable layer
    (tmp_path / "requirements.txt").write_text("flask\n")
    layers2 = detect_toolchains(str(tmp_path))
    assert all(l["kind"] != "python" for l in layers2)


def test_run_project_tests_aggregates_layers(tmp_path, monkeypatch):
    import tools.registry as reg
    be = tmp_path / "backend"; be.mkdir()
    (be / "requirements.txt").write_text("x\n"); (be / "tests").mkdir()
    (be / "tests" / "test_a.py").write_text("def test_a(): assert 1\n")
    fe = tmp_path / "frontend"; fe.mkdir()
    (fe / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^2"}}))
    monkeypatch.setattr(reg, "run_tests_in_docker",
                        lambda d, timeout=300, test_path="tests/", workdir="": (True, f"py:{workdir}"))
    monkeypatch.setattr(reg, "_run_node_layer",
                        lambda d, wd, runner, timeout: (False, f"node-fail:{wd}"))
    ok, report = reg.run_project_tests(str(tmp_path))
    assert ok is False                       # one layer failed → overall fail
    assert "py:backend" in report and "node-fail:frontend" in report


def test_compose_integration_requires_compose_for_managed(tmp_path):
    from tools.registry import run_compose_integration, has_compose_file
    assert not has_compose_file(str(tmp_path))
    ok, msg = run_compose_integration(str(tmp_path), require_compose=True)
    assert not ok and "docker-compose.yml" in msg
    ok, msg = run_compose_integration(str(tmp_path), require_compose=False)
    assert ok and "skipped" in msg            # external repo: graceful skip


def test_detect_toolchains_ignores_e2e_dir(tmp_path):
    # e2e/ belongs to the integration stage; the playwright runner once dropped a
    # package.json there and e2e (sorting before "frontend") stole the node layer,
    # silently skipping vitest entirely.
    from tools.registry import detect_toolchains
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text('{"devDependencies":{"vitest":"^2"}}')
    (tmp_path / "e2e").mkdir()
    (tmp_path / "e2e" / "package.json").write_text('{}')
    layers = detect_toolchains(str(tmp_path))
    node = [l for l in layers if l["kind"] == "node"]
    assert len(node) == 1 and node[0]["dir"] == "frontend"


def test_all_skills_load_untruncated():
    # I2 (live finding): skills/engineer.md crossed MAX_SKILL_CHARS and its newest
    # mandates (SEO floor, theme wiring) were silently cut from EVERY engineer call.
    # Every skill must reach its agent in full — truncation is never acceptable here.
    from pathlib import Path
    from tools.file_io import MAX_SKILL_CHARS, load_skill
    skills_dir = Path(__file__).parent.parent / "skills"
    skill_files = sorted(skills_dir.glob("*.md"))
    assert skill_files, "no skills found — wrong directory?"
    for f in skill_files:
        full = f.read_text(encoding="utf-8").strip()
        loaded = load_skill(f.stem)
        assert loaded == full, (
            f"skills/{f.name} is TRUNCATED on load ({len(full)} chars > cap "
            f"{MAX_SKILL_CHARS}) — its tail rules never reach the agent. "
            f"Trim the skill or raise MAX_SKILL_CHARS.")


def test_model_tiers_thinking_vs_generation():
    # CEO/CTO allocation (2026-06-27): TWO models, split by workload.
    # Opus 4.8 = thinking/decision/analysis (fast + reason); Sonnet 5 = coding (strong).
    from tools.llm import MODELS
    assert MODELS["reason"] == "claude-opus-4-8"   # deep thinking + oracle (architect/critic/test_author/design-spec/vision)
    assert MODELS["strong"] == "claude-sonnet-5"   # hands-on coding (engineer/design-kit/qa-e2e/devops)
    assert MODELS["fast"] == "claude-opus-4-8"     # lighter decision/analysis (ceo/pm/triage/qa-review/consult/retro)
    # exactly two distinct models across all tiers
    assert set(MODELS.values()) == {"claude-opus-4-8", "claude-sonnet-5"}


def test_llm_backend_dispatch(monkeypatch):
    # Default backend is claude-cli (subscription, $0 marginal — CEO decision);
    # vision goes through the CLI too (image paths via its Read tool);
    # LLM_BACKEND=api opts back into the metered API.
    from tools import llm
    calls = {}
    def fake_cli(s, u, m, timeout=900, images=None, web_search=False):
        calls["cli"] = (s, u, m, images)
        return ("cli-out", 1, 2)
    def fake_api(s, u, t, images=None, web_search=False):
        calls["api"] = images
        return ("api-out", 3, 4)
    monkeypatch.setattr(llm, "_cli_call", fake_cli)
    monkeypatch.setattr(llm, "_api_call", fake_api)
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    assert llm.call_llm("sys", "hello", tier="reason") == "cli-out"   # DEFAULT = cli
    assert calls["cli"][2] == "claude-opus-4-8"            # model id passed through
    imgs = [("app", "/tmp/x.png")]
    assert llm.call_llm("sys", "see", tier="strong", images=imgs) == "cli-out"
    assert calls["cli"][3] == imgs                        # vision via CLI too
    monkeypatch.setenv("LLM_BACKEND", "api")
    assert llm.call_llm("sys", "hello") == "api-out"      # explicit opt-out


def test_cli_call_slimming_and_quality_guards(monkeypatch, tmp_path):
    # The CLI backend must not degrade generation vs the api backend:
    # neutral cwd (no CLAUDE.md contamination), no MCP servers, anti-brevity/
    # anti-tool-use guard appended, output ceiling >= the api tier caps.
    from tools import llm
    import os
    import subprocess
    seen = {}
    def fake_run(cmd, capture_output, text, timeout, cwd, env, input=None):
        seen["cmd"], seen["cwd"], seen["env"], seen["prompt"] = cmd, cwd, env, input
        class R:
            returncode = 0
            stdout = '{"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 2}}'
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(llm, "_find_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(llm, "_CLI_WORKDIR", str(tmp_path))

    text, _, _ = llm._cli_call("AGENT-SYS", "do work", "claude-opus-4-8")
    cmd = seen["cmd"]
    assert text == "ok"
    assert seen["cwd"] == str(tmp_path) != os.getcwd()      # neutral cwd
    assert "--strict-mcp-config" in cmd                     # no MCP tool defs
    sys_arg = cmd[cmd.index("--append-system-prompt") + 1]
    assert sys_arg.startswith("AGENT-SYS") and "COMPLETE, full-length" in sys_arg
    assert "Tools are DISABLED" in sys_arg                  # text calls: no tools
    assert cmd[cmd.index("--max-turns") + 1] == "4"         # stray tool attempt self-recovers
    assert int(seen["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]) >= max(llm.MAX_TOKENS.values())

    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"png"), b.write_bytes(b"png")
    llm._cli_call("AGENT-SYS", "compare", "claude-opus-4-8",
                  images=[("app", str(a)), ("mock", str(b))])
    cmd = seen["cmd"]
    sys_arg = cmd[cmd.index("--append-system-prompt") + 1]
    assert cmd[cmd.index("--allowed-tools") + 1] == "Read"  # vision keeps Read only
    assert "no tool other than Read" in sys_arg
    assert "Tools are DISABLED" not in sys_arg
    prompt = seen["prompt"]
    assert str(tmp_path) in prompt and str(a) not in prompt  # images copied INTO cwd
    assert cmd[-1] == "-p"                                   # prompt rides stdin, not argv


def test_cli_call_retries_once_on_max_turns(monkeypatch, tmp_path):
    # Live failure: a fast-tier consult IGNORED the no-tools guard, burned the turn
    # budget on tool calls, and error_max_turns hard-crashed the QA node mid-graph.
    # The call must retry ONCE with a doubled budget + explicit retry guard.
    from tools import llm
    import subprocess
    attempts = []

    def fake_run(cmd, capture_output, text, timeout, cwd, env, input=None):
        attempts.append(cmd)
        class R:
            returncode = 1 if len(attempts) == 1 else 0
            stdout = ('{"is_error": true, "subtype": "error_max_turns"}'
                      if len(attempts) == 1 else
                      '{"result": "recovered", "usage": {"input_tokens": 1, "output_tokens": 2}}')
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(llm, "_find_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(llm, "_CLI_WORKDIR", str(tmp_path))
    text, _, _ = llm._cli_call("SYS", "hello", "claude-opus-4-8")
    assert text == "recovered"
    assert len(attempts) == 2
    second = attempts[1]
    assert second[second.index("--max-turns") + 1] == "8"           # doubled budget
    assert "FAILED because you invoked tools" in second[second.index("--append-system-prompt") + 1]

    # any other failure still raises immediately (no infinite retry)
    attempts.clear()
    def fake_fail(cmd, capture_output, text, timeout, cwd, env, input=None):
        attempts.append(cmd)
        class R:
            returncode = 1
            stdout = '{"is_error": true, "subtype": "error_during_execution"}'
            stderr = ""
        return R()
    monkeypatch.setattr(subprocess, "run", fake_fail)
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="claude-cli call failed"):
        llm._cli_call("SYS", "hello", "claude-opus-4-8")
    assert len(attempts) == 1


def test_cli_binary_discovery_error(monkeypatch):
    from tools import llm
    monkeypatch.setattr(llm, "_CLI_BIN", None)
    monkeypatch.setenv("CLAUDE_CLI_BIN", "/nonexistent/claude")
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr("glob.glob", lambda _: [])
    monkeypatch.setattr("os.path.isfile", lambda _: False)
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="no `claude` binary"):
        llm._find_claude_bin()


def test_strip_ansi_removes_color_codes():
    from tools.registry import _strip_ansi
    colored = "\x1b[31mFAIL\x1b[39m tests/Task.test.tsx \x1b[33mname\x1b[39m=\x1b[32m\"title\"\x1b[39m"
    assert _strip_ansi(colored) == 'FAIL tests/Task.test.tsx name="title"'


def test_run_e2e_skips_without_specs(tmp_path):
    from tools.registry import _run_e2e
    ok, msg = _run_e2e(str(tmp_path))
    assert ok and "skipped" in msg


def test_run_project_tests_falls_back_to_pytest(tmp_path, monkeypatch):
    import tools.registry as reg
    monkeypatch.setattr(reg, "run_tests_in_docker",
                        lambda d, timeout=300, test_path="tests/", workdir="": (True, "legacy pytest"))
    ok, report = reg.run_project_tests(str(tmp_path))  # nothing detected
    assert ok and report == "legacy pytest"


def test_validate_components_and_api_spec():
    from tools.registry import validate_components, validate_api_spec
    # NOTE: validate_components is a brittle heuristic — any capitalized word that
    # isn't a known component is flagged (e.g. "Use", "The"). See AGENT_AUDIT.md.
    ok, _ = validate_components("Button Input Card Dialog Table")
    assert ok
    flagged, _ = validate_components("Use a CustomWidget here")  # unknown component
    assert not flagged
    bad, _ = validate_api_spec("GET /getUser returns the user")  # verb-in-path
    assert not bad
