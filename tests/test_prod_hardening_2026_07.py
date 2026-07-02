"""
Production-hardening pass (2026-07) — regression tests for the code items surfaced by a
live product build:

  C1  node test runner: convention-first (run the project's own `test` script) + an
      environment-not-code hint on infra-noise output.
  C3  engineer/QA design-input caps raised — the components MANIFEST (a wiring CONTRACT)
      is read effectively untruncated.
  C4  compose-integration logs captured PER SERVICE (app first, db last + hard-capped) +
      a healthcheck hint when containers run but probes never pass.

All pure-function / prompt-construction tests — no Docker, no key.
"""

from pathlib import Path

from tools import registry


# ── C1: node runner — convention over invocation ────────────────────────────────────────
def _pkg(tmp_path, data: dict) -> Path:
    import json
    p = tmp_path / "package.json"
    p.write_text(json.dumps(data))
    return p


def test_node_runner_prefers_own_test_script_over_vitest(tmp_path):
    # A project with vitest in deps AND its own `test` script → run `npm test`, not raw
    # vitest (the project scopes its own unit run, excluding DB-dependent integration specs).
    pkg = _pkg(tmp_path, {"devDependencies": {"vitest": "^1.0.0"},
                          "scripts": {"test": "vitest run --exclude '**/*.integration.*'"}})
    assert registry._node_runner(pkg) == "npm-test"


def test_node_runner_falls_back_to_vitest_without_test_script(tmp_path):
    pkg = _pkg(tmp_path, {"devDependencies": {"vitest": "^1.0.0"}, "scripts": {"build": "vite"}})
    assert registry._node_runner(pkg) == "vitest"


def test_node_runner_falls_back_to_jest_without_test_script(tmp_path):
    pkg = _pkg(tmp_path, {"devDependencies": {"jest": "^29"}})
    assert registry._node_runner(pkg) == "jest"


def test_node_runner_blank_test_script_is_not_a_convention(tmp_path):
    # an empty/whitespace `test` value is not a real script — keep detecting the tool.
    pkg = _pkg(tmp_path, {"devDependencies": {"vitest": "^1"}, "scripts": {"test": "   "}})
    assert registry._node_runner(pkg) == "vitest"


def test_node_env_hint_injected_on_infra_noise():
    for noise in ("Cannot find module '@rollup/rollup-linux-x64-gnu'",
                  "Error: Cannot find module 'lightningcss.linux-x64-musl.node' (native)",
                  "connect ECONNREFUSED 127.0.0.1:5432",
                  "P1001: Can't reach database server"):
        out = registry._node_env_hint(f"some log\n{noise}\nmore log")
        assert "TEST-ENVIRONMENT failure" in out
        assert "integration suite" in out


def test_node_env_hint_absent_on_real_code_failure():
    out = registry._node_env_hint("FAIL src/foo.test.ts\n  expected 3 to equal 4")
    assert "TEST-ENVIRONMENT" not in out
    assert out == "FAIL src/foo.test.ts\n  expected 3 to equal 4"


# ── C3: manifest read is a CONTRACT — not truncated below its new floor ──────────────────
def test_engineer_reads_manifest_untruncated(tmp_path, monkeypatch):
    from agents import engineer
    manifest = tmp_path / "MANIFEST.md"
    body = "# manifest\n" + ("wiring line with required microcopy\n" * 1000)  # ~34k chars
    manifest.write_text(body)
    assert len(body) > 24000  # bigger than the old 6000 cap; exercises the floor
    block = engineer._read_kit(
        {"design_component_files": ["frontend/src/components/kit/X.tsx"],
         "components_manifest_path": str(manifest)})
    # the full contract up to the safety ceiling is present (old 6000 cap would have cut it)
    assert "required microcopy" in block
    assert block.count("wiring line with required microcopy") > 500


def test_engineer_caps_meet_floors():
    from agents import engineer
    assert engineer.MANIFEST_CAP >= 24000
    assert engineer.DESIGN_SPEC_CAP >= 16000
    assert engineer.DESIGN_MOCKUP_CAP >= 16000


def test_qa_manifest_cap_at_least_24000():
    from agents import qa
    assert qa.MANIFEST_CAP >= 24000


# ── C4: per-service integration logs + healthcheck hint ──────────────────────────────────
def test_service_logs_put_app_first_db_last_and_capped():
    per_service = {
        "db": "db init line\n" * 400,      # noisy — must not evict the app
        "api": "traceback: KeyError 'user_id' at handler.py:42",
        "frontend": "ready on :3000",
    }
    out = registry._assemble_service_logs(per_service, total_budget=4000, db_cap=15)
    # app services appear before the db block
    assert out.index("--- api") < out.index("--- db")
    assert out.index("--- frontend") < out.index("--- db")
    # the real app error survives
    assert "KeyError 'user_id'" in out
    # the db is hard-capped to db_cap lines
    db_section = out.split("--- db")[1]
    assert db_section.count("db init line") <= 15


def test_service_logs_empty_is_safe():
    assert registry._assemble_service_logs({}) == "(no service logs captured)"


def test_db_service_detection():
    assert registry._is_db_service("db")
    assert registry._is_db_service("postgres")
    assert registry._is_db_service("app-database")
    assert registry._is_db_service("redis")
    assert not registry._is_db_service("api")
    assert not registry._is_db_service("frontend")


def test_healthcheck_hint_when_running_but_probe_failing():
    msg = "services not healthy after 120s: api=running/starting; db=running/healthy"
    hint = registry._healthcheck_hint(msg)
    assert "127.0.0.1" in hint and "start_period" in hint


def test_healthcheck_hint_absent_when_container_not_running():
    msg = "services not healthy after 120s: api=exited/"
    assert registry._healthcheck_hint(msg) == ""


def test_healthcheck_hint_absent_on_no_containers():
    assert registry._healthcheck_hint("services not healthy after 120s: no containers found") == ""
