"""
Design-system COMPOSITION layer (design-fidelity hardening) — pinned tests.

A live build hand-rolled 47/102 kit files, re-implementing 31 components the product's
installed component library already exported (right tokens, wrong composition) and nothing
detected it. This layer is the deterministic ground truth:

- registry.detect_component_library  — find the installed UI library generically (no
  hardcoded package name): a `dependencies` entry whose node_modules package ships a `.d.ts`
  barrel with >=8 PascalCase value exports.
- registry.check_ds_composition      — THE GATE, TWO-TIER (the real drift RENAMED its forks —
  AdventureActivityCard re-implemented ActivityCard — so exact-only matching was trivially
  evaded): a non-importing kit file fails on an EXACT export match OR a trailing
  PascalCase-segment SUFFIX match ≥6 chars; generic short names (Card/Icon/Input/Stars)
  sit below the cutoff by design and never gate. A file that imports the library is a
  wrapper (always ok).
- registry.design_parity_report      — ADVISORY parity metric folded into code_quality:
  usage counts library imports + JSX tags in kit/library-wired files (kit barrels re-export
  library components, so import-mentions alone undercount), plus a fuzzier "possible
  re-implementations" containment tail (≥5 chars, whole PascalCase segment run) for a
  human/vision pass — advisory only, never the gate.
- design._enforce_ds_composition     — re-emit-once-then-advisory kit gate (mirrors
  _enforce_testid_uniqueness); engineer folds the parity report into code_quality.

Stack-AGNOSTIC: every test builds its own fake library — no real package name is hardcoded.
Byte-identical behavior when no component library is installed (all checks no-op).
"""

import json
from pathlib import Path

from conftest import base_state, seed
from tools.registry import (
    detect_component_library, check_ds_composition, design_parity_report,
    _dts_component_exports, _pascal_suffix_match, _pascal_contains,
)

_DEFAULT_EXPORTS = ["Button", "Card", "Modal", "Avatar", "Badge", "Input",
                    "Select", "Tooltip", "Sheet", "Dialog", "ActivityCard", "Gallery"]


def _scaffold_lib(frontend: Path, pkg="@acme/ui", exports=None, dts_extra="",
                  dep_extra=None, types="index.d.ts") -> Path:
    """Write a fake frontend package.json declaring `pkg` + an installed node_modules
    package that ships a `.d.ts` barrel exporting `exports`. Returns the lib dir."""
    exports = _DEFAULT_EXPORTS if exports is None else exports
    frontend.mkdir(parents=True, exist_ok=True)
    deps = {pkg: "^1.0.0", "react": "^18.0.0"}
    if dep_extra:
        deps.update(dep_extra)
    (frontend / "package.json").write_text(
        json.dumps({"dependencies": deps, "devDependencies": {"vitest": "^1"}}))
    lib = frontend / "node_modules" / Path(*pkg.split("/"))
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "package.json").write_text(json.dumps({"name": pkg, "types": types}))
    barrel = "export { " + ", ".join(exports) + " };\n" + dts_extra
    (lib / types).parent.mkdir(parents=True, exist_ok=True)
    (lib / types).write_text(barrel)
    return lib


# ── _dts_component_exports (pure parser) ─────────────────────────────────────

def test_dts_exports_keeps_pascalcase_drops_types_hooks_lowercase():
    dts = """
    export { Button, Card, type ButtonProps, Modal as Sheet };
    export declare function Avatar(props: any): JSX.Element;
    export declare const ThemeProvider: React.FC<any>;
    export declare function useToast(): void;
    declare const cn: (...a: any[]) => string;
    export const BUTTON_VARIANTS = {};
    """
    names = _dts_component_exports(dts)
    assert {"Button", "Card", "Sheet", "Avatar", "ThemeProvider"} <= names   # kept
    assert "ButtonProps" not in names        # `type ` dropped
    assert "useToast" not in names           # hook dropped (lowercase)
    assert "cn" not in names                 # lowercase dropped
    assert "BUTTON_VARIANTS" not in names    # constant (underscore) dropped
    assert "Modal" not in names              # re-exported AS Sheet → Sheet, not Modal


# ── detect_component_library ─────────────────────────────────────────────────

def test_detect_finds_installed_library(tmp_path):
    _scaffold_lib(tmp_path / "frontend")
    det = detect_component_library(str(tmp_path))
    assert det is not None
    assert det["name"] == "@acme/ui"
    assert {"Button", "Card", "Gallery"} <= det["exports"]


def test_detect_reads_types_field_over_default(tmp_path):
    _scaffold_lib(tmp_path / "frontend", types="dist/types/index.d.ts")
    det = detect_component_library(str(tmp_path))
    assert det and det["name"] == "@acme/ui"


def test_detect_none_when_no_package_json(tmp_path):
    assert detect_component_library(str(tmp_path)) is None


def test_detect_none_when_no_node_modules(tmp_path):
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "package.json").write_text(json.dumps({"dependencies": {"@acme/ui": "^1"}}))
    assert detect_component_library(str(tmp_path)) is None   # never installed → None


def test_detect_none_when_barrel_has_too_few_components(tmp_path):
    _scaffold_lib(tmp_path / "frontend", exports=["Button", "Card", "Modal", "Avatar", "Badge"])
    assert detect_component_library(str(tmp_path)) is None   # <8 exports


def test_detect_never_returns_react_even_with_a_big_barrel(tmp_path):
    # react ships a huge type barrel but is a framework, never "the component library".
    _scaffold_lib(tmp_path / "frontend", pkg="react",
                  exports=["Component", "Fragment", "Suspense", "StrictMode", "Profiler",
                           "PureComponent", "Children", "Provider", "Consumer", "Context"])
    assert detect_component_library(str(tmp_path)) is None


def test_detect_skips_first_party_path_alias(tmp_path):
    lib = _scaffold_lib(tmp_path / "frontend", pkg="@/ui")   # a path-alias, not a package
    assert lib.exists()
    assert detect_component_library(str(tmp_path)) is None


def test_detect_prefers_ui_named_dep_and_most_exports(tmp_path):
    fe = tmp_path / "frontend"
    # a generic dep with MANY exports, and a /ui/-named dep — the name match wins.
    _scaffold_lib(fe, pkg="widgets",
                  exports=[f"Widget{i}" for i in range(20)])
    lib2 = fe / "node_modules" / "@shop" / "ui"
    lib2.mkdir(parents=True)
    (lib2 / "package.json").write_text(json.dumps({"types": "index.d.ts"}))
    (lib2 / "index.d.ts").write_text("export { " + ", ".join(_DEFAULT_EXPORTS) + " };")
    data = json.loads((fe / "package.json").read_text())
    data["dependencies"]["@shop/ui"] = "^1"
    (fe / "package.json").write_text(json.dumps(data))
    det = detect_component_library(str(tmp_path))
    assert det and det["name"] == "@shop/ui"   # /ui/ name match beats raw export count


def test_detect_never_raises_on_garbage_package_json(tmp_path):
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "package.json").write_text("{ not valid json ]")
    assert detect_component_library(str(tmp_path)) is None


# ── check_ds_composition (THE GATE) ──────────────────────────────────────────

def _kit(tmp_path, name, body) -> str:
    kit = tmp_path / "frontend" / "src" / "components" / "kit"
    kit.mkdir(parents=True, exist_ok=True)
    (kit / name).write_text(body)
    return f"frontend/src/components/kit/{name}"


def test_gate_flags_handrolled_reimplementation(tmp_path):
    _scaffold_lib(tmp_path / "frontend")
    k = _kit(tmp_path, "ActivityCard.tsx",
             "export function ActivityCard(){ return <div/>; }")
    ok, msg = check_ds_composition(str(tmp_path), [k])
    assert not ok
    assert "ActivityCard" in msg and "re-implements" in msg
    assert 'import { ActivityCard } from "@acme/ui"' in msg   # actionable compose guidance


def test_gate_passes_a_wrapper_that_imports_the_library(tmp_path):
    _scaffold_lib(tmp_path / "frontend")
    # basename collides with the export Button, but it IMPORTS the library → wrapper, ok.
    k = _kit(tmp_path, "Button.tsx",
             'import { Button as B } from "@acme/ui";\n'
             'export function Button(p){ return <B {...p} data-testid="cta"/>; }')
    ok, msg = check_ds_composition(str(tmp_path), [k])
    assert ok, msg


def test_gate_flags_exported_const_collision(tmp_path):
    _scaffold_lib(tmp_path / "frontend")
    # file name does NOT collide; the EXPORTED const name (Badge) does.
    k = _kit(tmp_path, "StatusPill.tsx",
             "export const Badge = () => <span/>;")
    ok, msg = check_ds_composition(str(tmp_path), [k])
    assert not ok and "Badge" in msg


def test_gate_lookalike_subpackage_import_does_not_exempt(tmp_path):
    _scaffold_lib(tmp_path / "frontend")
    # imports @acme/ui-icons (a DIFFERENT package) — must NOT count as importing @acme/ui.
    k = _kit(tmp_path, "Modal.tsx",
             'import { X } from "@acme/ui-icons";\n'
             'export function Modal(){ return <div/>; }')
    ok, msg = check_ds_composition(str(tmp_path), [k])
    assert not ok and "Modal" in msg


def test_gate_subpath_import_of_the_library_exempts(tmp_path):
    _scaffold_lib(tmp_path / "frontend")
    k = _kit(tmp_path, "Modal.tsx",
             'import { Modal as M } from "@acme/ui/modal";\n'
             'export function Modal(p){ return <M {...p}/>; }')
    ok, msg = check_ds_composition(str(tmp_path), [k])
    assert ok, msg


def test_gate_skips_when_no_library(tmp_path):
    k = _kit(tmp_path, "Button.tsx", "export function Button(){ return <div/>; }")
    ok, msg = check_ds_composition(str(tmp_path), [k])
    assert ok and "no component library" in msg


def test_gate_skips_on_empty_kit(tmp_path):
    _scaffold_lib(tmp_path / "frontend")
    ok, msg = check_ds_composition(str(tmp_path), [])
    assert ok and "no kit files" in msg


# ── renamed-fork tier (pure matchers + gate) ─────────────────────────────────
# The real drift renamed its forks: AdventureActivityCard re-implemented ActivityCard,
# ActivityGallery re-implemented Gallery — exact matching caught none of them.

def test_pascal_suffix_match_whole_trailing_segment_run():
    exports = {"ActivityCard", "Gallery", "Card"}
    assert _pascal_suffix_match("AdventureActivityCard", exports) == "ActivityCard"
    assert _pascal_suffix_match("ActivityGallery", exports) == "Gallery"
    assert _pascal_suffix_match("MyCardigan", exports) is None    # Card: <6 AND mid-segment
    assert _pascal_suffix_match("ActivityCard", exports) is None  # exact = the exact tier's job


def test_pascal_suffix_match_requires_lowercase_boundary():
    # char before the suffix must be lowercase (a real PascalCase segment boundary) —
    # an acronym/uppercase run before the match never fires.
    assert _pascal_suffix_match("XYSlider", {"Slider"}) is None
    assert _pascal_suffix_match("KeySlider", {"Slider"}) == "Slider"


def test_pascal_contains_whole_segment_run_only():
    assert _pascal_contains("RatingInput", {"Input"}) == "Input"    # trailing segment
    assert _pascal_contains("InputRating", {"Input"}) == "Input"    # leading segment
    assert _pascal_contains("RatingStars", {"Stars"}) == "Stars"
    assert _pascal_contains("Starship", {"Stars"}) is None          # segment continues lowercase
    assert _pascal_contains("ProfileCard", {"Card"}) is None        # Card = 4 < 5 cutoff


def test_gate_flags_renamed_fork_via_suffix(tmp_path):
    _scaffold_lib(tmp_path / "frontend")   # exports include ActivityCard (12 ≥ 6)
    k = _kit(tmp_path, "AdventureActivityCard.tsx",
             "export function AdventureActivityCard(){ return <div/>; }")
    ok, msg = check_ds_composition(str(tmp_path), [k])
    assert not ok
    assert "renamed re-implementation" in msg and "ActivityCard" in msg
    assert "rename it so it doesn't shadow the library's" in msg   # the two-way guidance


def test_gate_short_suffix_never_fires_but_advisory_lists_ge5(tmp_path):
    # Card(4)/Icon(4)/Stars(5) are all below the ≥6 gate cutoff BY DESIGN — generic short
    # names must not fire the gate; the ≥5 tail (Stars) goes to the advisory possible-list.
    exports = ["Card", "Icon", "Stars", "Modal", "Avatar", "Badge", "Tooltip", "Dialog"]
    _scaffold_lib(tmp_path / "frontend", exports=exports)
    k1 = _kit(tmp_path, "ProfileCard.tsx", "export function ProfileCard(){ return <div/>; }")
    k2 = _kit(tmp_path, "RatingStars.tsx", "export function RatingStars(){ return <div/>; }")
    ok, msg = check_ds_composition(str(tmp_path), [k1, k2])
    assert ok, msg
    rep = design_parity_report(str(tmp_path))
    assert "possible re-implementations:" in rep
    tail = rep.split("possible re-implementations:")[1]
    assert "RatingStars→Stars" in tail                # ≥5 whole-segment containment
    assert "ProfileCard" not in tail                  # Card = 4 < 5, never listed


def test_gate_suffix_respects_pascal_boundary(tmp_path):
    # exports include an ≥6 name (Slider); XYSlider's match is preceded by an UPPERCASE
    # char (no lowercase→uppercase boundary) → never fires.
    exports = ["Slider", "Modal", "Avatar", "Badge", "Tooltip", "Dialog", "Drawer", "Banner"]
    _scaffold_lib(tmp_path / "frontend", exports=exports)
    k = _kit(tmp_path, "XYSlider.tsx", "export function XYSlider(){ return <div/>; }")
    ok, msg = check_ds_composition(str(tmp_path), [k])
    assert ok, msg


def test_gate_importing_file_with_suffix_collision_passes(tmp_path):
    _scaffold_lib(tmp_path / "frontend")
    k = _kit(tmp_path, "AdventureActivityCard.tsx",
             'import { ActivityCard } from "@acme/ui";\n'
             "export function AdventureActivityCard(p){ return <ActivityCard {...p}/>; }")
    ok, msg = check_ds_composition(str(tmp_path), [k])
    assert ok, msg                                    # wrapper exemption covers both tiers


# ── design_parity_report (advisory) ──────────────────────────────────────────

def test_parity_reports_counts_and_colliding_names(tmp_path):
    _scaffold_lib(tmp_path / "frontend")
    _kit(tmp_path, "ActivityCard.tsx", "export function ActivityCard(){ return <div/>; }")
    _kit(tmp_path, "Gallery.tsx",
         'import { Gallery as G } from "@acme/ui";\nexport function Gallery(){ return <G/>; }')
    # a page that actually uses two library exports
    app = tmp_path / "frontend" / "src" / "App.tsx"
    app.write_text('import { Button, Card } from "@acme/ui";\nexport default function App(){}')
    rep = design_parity_report(str(tmp_path))
    assert rep.startswith("parity: @acme/ui")
    assert "kit 1 compose / 1 hand-rolled" in rep
    assert "hand-rolled colliding with exports: ActivityCard" in rep
    assert "/12 exported components used" in rep   # 12 exports in the barrel


def test_parity_empty_when_no_library(tmp_path):
    _kit(tmp_path, "Button.tsx", "export function Button(){ return <div/>; }")
    assert design_parity_report(str(tmp_path)) == ""


def test_parity_counts_jsx_tag_usage_in_kit_wired_files(tmp_path):
    # The kit barrel re-exports library components, so a page importing the kit and
    # rendering <Gallery> never import-mentions the library — an import-only count
    # undercounts (real repo: 17/56 by imports vs 23/56 with JSX in wired files).
    _scaffold_lib(tmp_path / "frontend")
    _kit(tmp_path, "ActivityCard.tsx",
         'import { ActivityCard as C } from "@acme/ui";\n'
         "export function ActivityCard(p){ return <C {...p}/>; }")
    pages = tmp_path / "frontend" / "src" / "pages"
    pages.mkdir(parents=True)
    (pages / "Dash.tsx").write_text(
        'import { ActivityCard } from "../components/kit";\n'
        "export const Dash = () => <div><ActivityCard/><Gallery/></div>;\n")
    rep = design_parity_report(str(tmp_path))
    # ActivityCard via the kit wrapper's lib import + Gallery via a JSX tag in a
    # kit-wired page → 2 used (Gallery has NO library import-mention anywhere).
    assert "2/12 exported components used" in rep


def test_parity_jsx_in_unwired_file_does_not_count(tmp_path):
    # A JSX tag in a file that imports NEITHER the kit nor the library is a LOCAL
    # component (usually the fork itself — the real repo's <Stepper> was an inline
    # hand-rolled function) — counting it would credit the library for its own forks.
    _scaffold_lib(tmp_path / "frontend")
    pages = tmp_path / "frontend" / "src" / "pages"
    pages.mkdir(parents=True)
    (pages / "Standalone.tsx").write_text(
        "function Gallery(){ return <div/>; }\n"
        "export const Standalone = () => <Gallery/>;\n")
    rep = design_parity_report(str(tmp_path))
    assert "0/12 exported components used" in rep


# ── design agent wiring (_enforce_ds_composition) ────────────────────────────

_HANDROLLED = (
    "===FILE: frontend/src/components/kit/ActivityCard.tsx===\n"
    "export function ActivityCard({ title }){ return <div data-testid=\"activity-card\">{title}</div>; }\n"
    "===END===\n"
    "===FILE: frontend/src/components/kit/MANIFEST.md===\n"
    "# Design Component Kit — wiring manifest\n## Components\n- ActivityCard\n"
    "===END===\n"
)
_WRAPPER = (
    "===FILE: frontend/src/components/kit/ActivityCard.tsx===\n"
    'import { ActivityCard as UiCard } from "@acme/ui";\n'
    "export function ActivityCard(props){ return <div data-testid=\"activity-card\">"
    "<UiCard {...props}/></div>; }\n"
    "===END===\n"
)


def _build_state_with_lib(ws):
    """A greenfield state whose code root already has the installed component library."""
    state = base_state("proj")
    frontend = ws / "proj" / "frontend"
    _scaffold_lib(frontend)          # @acme/ui exports ActivityCard among others
    return state


def test_design_reemits_once_and_resolves_composition(llm, ws):
    from agents import design
    state = _build_state_with_lib(ws)
    llm.queue = [_HANDROLLED, _WRAPPER]     # first collides, re-emit composes → resolved
    design._build_components("SYS", "SPEC", "<html/>", state)
    assert len(llm.calls) == 2              # exactly one re-emit round
    kit = ws / "proj" / "frontend" / "src" / "components" / "kit"
    final = (kit / "ActivityCard.tsx").read_text()
    assert '@acme/ui' in final              # the composed wrapper won
    manifest = (kit / "MANIFEST.md").read_text()
    assert "DS-COMPOSITION ADVISORY" not in manifest   # resolved → no advisory


def test_design_downgrades_to_advisory_when_unresolved(llm, ws):
    from agents import design
    state = _build_state_with_lib(ws)
    llm.queue = [_HANDROLLED, _HANDROLLED]  # re-emit STILL hand-rolls → advisory, no block
    # returns normally (pipeline continues) despite the unresolved violation
    files, manifest_path = design._build_components("SYS", "SPEC", "<html/>", state)
    assert len(llm.calls) == 2              # one re-emit, then advisory (no infinite loop)
    manifest = (ws / "proj" / "frontend" / "src" / "components" / "kit" / "MANIFEST.md").read_text()
    assert "DS-COMPOSITION ADVISORY" in manifest
    assert "re-implements" in manifest


# ── engineer fold-in (parity: label in code_quality) ─────────────────────────

def test_engineer_folds_parity_into_code_quality(llm, ws, no_docker):
    from agents import engineer
    tech = seed(ws, "proj", "design", "tech_spec.md", "## API\nPOST /run")
    seed(ws, "proj", "tests", "test_run.py", "def test_run():\n    assert True")
    # the project ships an installed component library under its frontend
    _scaffold_lib(ws / "proj" / "frontend")
    (ws / "proj" / "frontend" / "src" / "components" / "kit").mkdir(parents=True, exist_ok=True)
    llm.default = "===FILE: src/main.py===\nprint('hi')\n===END==="
    out = engineer.run(base_state(design_path=tech))
    assert any(s.startswith("parity:") for s in out["code_quality"])


def test_engineer_no_parity_when_no_library(llm, ws, no_docker):
    from agents import engineer
    tech = seed(ws, "proj", "design", "tech_spec.md", "## API\nPOST /run")
    seed(ws, "proj", "tests", "test_run.py", "def test_run():\n    assert True")
    llm.default = "===FILE: src/main.py===\nprint('hi')\n===END==="
    out = engineer.run(base_state(design_path=tech))
    assert not any(s.startswith("parity:") for s in out["code_quality"])   # byte-identical
