"""Platform fixes distilled from the TrailTribe phase-2 run.

Gap 2 — the integration stage relaxes per-IP rate limiting for the shared-IP e2e suite
via an IT-ONLY compose override, without weakening the shipped compose (the friend-request
send 429'd mid-suite from the single runner IP and flaked the run, ≥4th occurrence).

Gap 1 (migration) — check_interface_additive must not false-flag a prior BARE base testid
that the suffix-renderer fix (resolve_kit_testids) replaced with state-suffixed children;
otherwise the next run reports a phantom interface regression.
"""
import subprocess
import types
from pathlib import Path

from tools import registry


def _mk_project(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services:\n  api: {}\n", encoding="utf-8")
    return str(tmp_path)


def test_write_and_remove_it_override(tmp_path):
    proj = _mk_project(tmp_path)
    f = tmp_path / registry.IT_OVERRIDE_FILE
    assert not f.exists()
    registry._write_it_override(proj)
    assert f.exists()
    text = f.read_text()
    assert "api:" in text and 'RATE_LIMIT_ENABLED: "0"' in text
    registry._remove_it_override(proj)
    assert not f.exists()
    registry._remove_it_override(proj)   # idempotent — never raises when already gone


def test_compose_includes_override_only_when_present(tmp_path, monkeypatch):
    proj = _mk_project(tmp_path)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # absent → plain `docker compose -p … up`, no -f files
    registry._compose(proj, "up", "-d")
    assert "-f" not in captured["cmd"]

    # present → base + override merged via -f, BEFORE the subcommand
    registry._write_it_override(proj)
    registry._compose(proj, "up", "-d")
    cmd = captured["cmd"]
    assert cmd[:4] == ["docker", "compose", "-p", registry.COMPOSE_PROJECT]
    assert "docker-compose.yml" in cmd and registry.IT_OVERRIDE_FILE in cmd
    assert cmd.index(registry.IT_OVERRIDE_FILE) < cmd.index("up")


def test_integration_writes_override_before_up_and_removes_after(tmp_path, monkeypatch):
    proj = _mk_project(tmp_path)
    monkeypatch.setattr(registry, "_foreign_port_holders", lambda: "")
    seen = {}

    def fake_compose(project_dir, *args, timeout=120):
        if args[:1] == ("up",):
            seen["override_live_during_up"] = (
                Path(project_dir) / registry.IT_OVERRIDE_FILE).exists()
            return 1, "build boom"   # short-circuit before health/e2e (no real docker)
        return 0, ""

    monkeypatch.setattr(registry, "_compose", fake_compose)

    passed, _report = registry.run_compose_integration(proj)
    assert passed is False                          # our stub failed the build
    assert seen.get("override_live_during_up") is True   # relaxed limit was active for `up`
    assert not (Path(proj) / registry.IT_OVERRIDE_FILE).exists()   # never left behind


def test_additive_keeps_base_covered_by_suffixed_children():
    prior = ("## TESTIDS\n- profile-relationship\n- profile-relationship-error\n\n"
             "## REQUIRED MICROCOPY\n")
    cur_ids = {"profile-relationship-add-friend", "profile-relationship-requested",
               "profile-relationship-error"}
    ok, msg, _merged = registry.check_interface_additive(prior, cur_ids, set(), set())
    assert ok, msg   # bare base is COVERED by its suffixed children → not a regression


def test_additive_still_flags_a_truly_dropped_testid():
    prior = "## TESTIDS\n- profile-badge\n\n## REQUIRED MICROCOPY\n"
    ok, msg, _merged = registry.check_interface_additive(prior, {"something-else"}, set(), set())
    assert not ok and "profile-badge" in msg   # nothing covers it → a real drop
