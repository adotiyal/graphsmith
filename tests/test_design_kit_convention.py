"""
C5 — Design follows the EXISTING kit's implementation convention instead of regenerating.

When the kit dir already has components that COMPOSE an installed component library, the
kit-emission prompt must carry a FOLLOW-THE-CONVENTION instruction + 2-3 representative
files, so new/extended components wrap the same library. On a greenfield/empty kit (or one
with no library convention), the prompt keeps the SELF-CONTAINED greenfield default.

Generic: follow-the-existing-convention beats regenerate (a hardcoded self-contained
mandate once hand-rolled ~1800 unstyled lines against a kit that wrapped a library).
"""

from pathlib import Path

from agents import design


def _kit_file(root: Path, name: str, body: str) -> None:
    kit = root / "frontend" / "src" / "components" / "kit"
    kit.mkdir(parents=True, exist_ok=True)
    (kit / name).write_text(body)


def test_convention_block_carries_follow_instruction_and_sample(tmp_path):
    state = {"target_repo": str(tmp_path)}
    _kit_file(tmp_path, "Button.tsx",
              'import { Button as UiButton } from "some-ui-lib";\n'
              "export const Button = (p) => <UiButton {...p}/>;")
    block = design._kit_convention_block(state)
    assert "FOLLOW THE EXISTING KIT" in block
    assert "some-ui-lib" in block          # the representative file is embedded
    assert "compose that same library" in block or "wrap the same library" in block


def test_convention_block_empty_on_greenfield_kit(tmp_path):
    state = {"target_repo": str(tmp_path)}       # no kit dir at all
    assert design._kit_convention_block(state) == ""


def test_convention_block_empty_when_kit_is_self_contained(tmp_path):
    # existing kit imports ONLY react (+ relative kit files) — no library convention,
    # so the greenfield self-contained default should stand (empty block).
    state = {"target_repo": str(tmp_path)}
    _kit_file(tmp_path, "Card.tsx",
              'import React from "react";\nimport { X } from "./icons";\n'
              "export const Card = () => <div/>;")
    assert design._kit_convention_block(state) == ""


def test_convention_block_empty_when_kit_uses_only_path_aliases(tmp_path):
    # A self-contained kit that imports a first-party path alias ("@/lib/utils", "~/utils")
    # is NOT composing an installed library — the alias must not be misclassified as one, or
    # the prompt would instruct new components to "wrap the same library" (an alias, not a lib).
    state = {"target_repo": str(tmp_path)}
    _kit_file(tmp_path, "Badge.tsx",
              'import React from "react";\n'
              'import { cn } from "@/lib/utils";\n'
              'import { fmt } from "~/utils";\n'
              "export const Badge = () => <span/>;")
    assert design._kit_convention_block(state) == ""


def test_build_components_prompt_includes_convention_when_kit_wraps_library(tmp_path, monkeypatch):
    state = {"target_repo": str(tmp_path), "project_id": "proj"}
    _kit_file(tmp_path, "Input.tsx",
              'import { TextField } from "@some/design-lib";\n'
              "export const Input = (p) => <TextField {...p}/>;")
    captured = {}

    def _fake_generate_in_domain(system, user_msg, root, allowed_prefixes=None, tier=None):
        captured["prompt"] = user_msg
        return {"violations": []}

    monkeypatch.setattr(design.codegen, "generate_in_domain", _fake_generate_in_domain)
    monkeypatch.setattr(design, "_enforce_interface_additive", lambda s, r: None)
    monkeypatch.setattr(design, "_enforce_testid_uniqueness", lambda s, r: None)
    monkeypatch.setattr(design, "_collect_kit", lambda s: ([], None))
    monkeypatch.setattr(design, "_use_tools", lambda: True)

    design._build_components("sys", "spec", "<html>mock</html>", state)
    assert "FOLLOW THE EXISTING KIT" in captured["prompt"]
    assert "@some/design-lib" in captured["prompt"]


def test_build_components_prompt_keeps_self_contained_on_greenfield(tmp_path, monkeypatch):
    state = {"target_repo": str(tmp_path), "project_id": "proj"}   # no existing kit
    captured = {}

    def _fake_generate_in_domain(system, user_msg, root, allowed_prefixes=None, tier=None):
        captured["prompt"] = user_msg
        return {"violations": []}

    monkeypatch.setattr(design.codegen, "generate_in_domain", _fake_generate_in_domain)
    monkeypatch.setattr(design, "_enforce_interface_additive", lambda s, r: None)
    monkeypatch.setattr(design, "_enforce_testid_uniqueness", lambda s, r: None)
    monkeypatch.setattr(design, "_collect_kit", lambda s: ([], None))
    monkeypatch.setattr(design, "_use_tools", lambda: True)

    design._build_components("sys", "spec", "<html>mock</html>", state)
    assert "SELF-CONTAINED" in captured["prompt"]
    assert "FOLLOW THE EXISTING KIT" not in captured["prompt"]
