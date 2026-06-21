"""
Interface Contract (additive-freeze): the kit's testids + required microcopy may only
GROW across phases — never drop a guarantee that existing e2e specs depend on. This is
the deterministic catch for the phase-3 regression class (a design rework renamed
profile testids and dropped the discover-card bio, silently breaking the whole e2e
suite at the most expensive stage).
"""

from pathlib import Path

from tools import registry, product


def _kit(tmp_path, files: dict, manifest: str = "") -> Path:
    d = tmp_path / "kit"
    d.mkdir()
    for name, content in files.items():
        (d / name).write_text(content)
    if manifest:
        (d / "MANIFEST.md").write_text(manifest)
    return d


def test_extract_kit_interface_testids_prefixes_microcopy(tmp_path):
    d = _kit(tmp_path, {
        "Card.tsx": '<div data-testid="profile-name"/><li data-testid={`task-card-${id}`}/>',
        "Form.tsx": '<button data-testid="save-btn">Save</button>',
    }, manifest='## REQUIRED MICROCOPY\n- "Save"\n- "Log an adventure"\n')
    ids, prefixes, micro = registry.extract_kit_interface(d, (d / "MANIFEST.md").read_text())
    assert ids == {"profile-name", "save-btn"}
    assert prefixes == {"task-card-"}
    assert micro == {"Save", "Log an adventure"}


def test_additive_grow_is_ok(tmp_path):
    prior = registry._render_interface_contract({"a", "b"}, {"row-"}, {"Hi"})
    ok, msg, merged = registry.check_interface_additive(
        prior, {"a", "b", "c"}, {"row-"}, {"Hi", "Bye"})   # only added
    assert ok
    pids, ppx, pmc = registry.parse_interface_contract(merged)
    assert pids == {"a", "b", "c"} and pmc == {"Hi", "Bye"}


def test_dropped_testid_is_regression(tmp_path):
    # the phase-3 class: a rework drops "profile-display-name" + the card bio microcopy
    prior = registry._render_interface_contract(
        {"profile-display-name", "profile-bio", "discover-card"}, set(),
        {"Trail bio shows here"})
    ok, msg, merged = registry.check_interface_additive(
        prior, {"discover-card"}, set(), set())   # dropped 2 testids + 1 microcopy
    assert not ok
    assert "profile-display-name" in msg and "profile-bio" in msg
    assert "Trail bio shows here" in msg
    # the merged contract still records everything (the floor never shrinks)
    pids, _, pmc = registry.parse_interface_contract(merged)
    assert {"profile-display-name", "profile-bio"} <= pids


def test_first_run_no_prior_is_ok(tmp_path):
    ok, msg, merged = registry.check_interface_additive("", {"a"}, set(), {"X"})
    assert ok and "a" in merged


def test_design_enforces_additive_and_persists(ws, monkeypatch, tmp_path):
    # A design run that DROPS a prior-phase testid must trigger a restore re-emit,
    # then persist the additive union.
    from agents import design
    prod = tmp_path / "product"
    monkeypatch.setattr(product, "PROFILE_ROOT", prod, raising=False)
    monkeypatch.setattr("tools.product.PROFILE_ROOT", prod, raising=False)
    product.save_interface_contract(registry._render_interface_contract(
        {"profile-bio", "profile-name"}, set(), {"Welcome"}))

    kit = ws / "proj" / "frontend" / "src" / "components" / "kit"
    kit.mkdir(parents=True)
    (kit / "MANIFEST.md").write_text('## REQUIRED MICROCOPY\n- "Welcome"\n')
    calls = {"n": 0}

    def reemit(extra=""):
        calls["n"] += 1
        # first emission DROPPED profile-bio; the restore round re-adds it
        if "RESTORE" in extra.upper() or calls["n"] >= 2:
            (kit / "Profile.tsx").write_text(
                '<h1 data-testid="profile-name"/><p data-testid="profile-bio"/>')
        else:
            (kit / "Profile.tsx").write_text('<h1 data-testid="profile-name"/>')

    reemit()   # initial emission (drops profile-bio)
    from tests.conftest import base_state
    design._enforce_interface_additive(base_state(), reemit)
    assert calls["n"] == 2                                  # one restore round happened
    ids, _, _ = registry.extract_kit_interface(kit, (kit / "MANIFEST.md").read_text())
    assert "profile-bio" in ids                             # restored
    # persisted contract still guarantees both
    pids, _, _ = registry.parse_interface_contract(product.load_interface_contract())
    assert {"profile-bio", "profile-name"} <= pids
