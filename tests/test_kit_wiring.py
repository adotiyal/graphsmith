"""
I3 verification suite — kit-wiring ENFORCEMENT (protection ≠ usage).

A live run's engineer ignored the design kit and built parallel components; the
microcopy gate caught 17 missing strings only at integration. The check is
deterministic, post-write, and fails the engineer round with the exact rule.
"""

from pathlib import Path

from tests.conftest import base_state, seed
from tools.registry import check_kit_wiring

KIT = ["frontend/src/components/kit/TodoPage.tsx",
       "frontend/src/components/kit/TaskCard.tsx"]


def _mk_project(tmp_path, wired=True, dupe=False):
    root = tmp_path / "proj"
    kitdir = root / "frontend/src/components/kit"
    kitdir.mkdir(parents=True)
    (kitdir / "TodoPage.tsx").write_text("export const TodoPage = () => <div/>;")
    (kitdir / "TaskCard.tsx").write_text("export const TaskCard = () => <li/>;")
    container = root / "frontend/src/components/TaskPage.tsx"
    if wired:
        container.write_text('import { TodoPage } from "./kit/TodoPage";\n'
                             "export const TaskPage = () => <TodoPage/>;")
    else:
        container.write_text("export const TaskPage = () => <div>own ui</div>;")
    if dupe:
        (root / "frontend/src/components/TaskCard.tsx").write_text(
            "export const TaskCard = () => <li>parallel</li>;")
    # noise that must be ignored
    nm = root / "frontend/node_modules/lib"
    nm.mkdir(parents=True)
    (nm / "TodoPage.tsx").write_text("not ours")
    (root / "e2e").mkdir()
    (root / "e2e" / "x.spec.ts").write_text("no kit import here")
    return root


def test_no_kit_means_nothing_to_enforce(tmp_path):
    ok, _ = check_kit_wiring(str(tmp_path), [])
    assert ok


def test_wired_project_passes(tmp_path):
    root = _mk_project(tmp_path, wired=True)
    ok, msg = check_kit_wiring(str(root), KIT)
    assert ok, msg


def test_barrel_import_counts_as_wired(tmp_path):
    # The engineer may import via the kit's index.ts barrel: `from "@/components/kit"`.
    # The deep-only regex wrongly failed this for 2 live rounds.
    root = _mk_project(tmp_path, wired=False)
    (root / "frontend/src/components/TaskPage.tsx").write_text(
        'import { TodoPage } from "@/components/kit";\nexport const X = () => <TodoPage/>;')
    ok, msg = check_kit_wiring(str(root), KIT)
    assert ok, msg


def test_unwired_project_fails_with_the_rule(tmp_path):
    root = _mk_project(tmp_path, wired=False)
    ok, msg = check_kit_wiring(str(root), KIT)
    assert not ok
    assert "NOT WIRED" in msg and "./kit/" in msg     # exact, actionable rule


def test_parallel_component_fails_by_name(tmp_path):
    root = _mk_project(tmp_path, wired=True, dupe=True)
    ok, msg = check_kit_wiring(str(root), KIT)
    assert not ok
    assert "PARALLEL COMPONENT" in msg and "TaskCard.tsx" in msg


def test_node_modules_and_e2e_ignored(tmp_path):
    # the node_modules copy of TodoPage.tsx must not count as a dupe,
    # and e2e files must not count as unwired sources
    root = _mk_project(tmp_path, wired=True)
    ok, msg = check_kit_wiring(str(root), KIT)
    assert ok, msg


def test_engineer_fails_round_on_unwired_kit(llm, ws, no_docker, tmp_path, monkeypatch):
    from agents import engineer
    spec = seed(ws, "proj", "design", "tech_spec.md", "build it")
    # greenfield project root with a kit + an UNWIRED container
    root = ws / "proj"
    kitdir = root / "frontend/src/components/kit"
    kitdir.mkdir(parents=True)
    (kitdir / "TodoPage.tsx").write_text("export const TodoPage = () => <div/>;")
    llm.default = ("===FILE: frontend/src/components/TaskPage.tsx===\n"
                   "export const TaskPage = () => <div>own ui, no kit</div>;\n===END===")
    state = base_state(design_path=spec,
                       design_component_files=["frontend/src/components/kit/TodoPage.tsx"])
    out = engineer.run(state)
    assert out["tests_passed"] is False
    assert "KIT WIRING FAILURE" in out["error_log"] and "NOT WIRED" in out["error_log"]

    # the fix round wires the kit → check passes, tests run
    llm.default = ("===FILE: frontend/src/components/TaskPage.tsx===\n"
                   'import { TodoPage } from "./kit/TodoPage";\n'
                   "export const TaskPage = () => <TodoPage/>;\n===END===")
    out2 = engineer.run({**state, **out})
    assert out2["tests_passed"] is True
