"""
Design upgrade — persistent product profile (tools/product.py).
"""

from tools import product


def test_profile_save_load_has(product_root):
    assert product.has_profile() is False
    assert product.load_profile() == ""
    product.save_profile("Category: social app\nUsers: gen-z creators\nTone: playful")
    assert product.has_profile() is True
    assert "social app" in product.load_profile()


def test_blank_profile_is_not_present(product_root):
    product.save_profile("   \n  ")
    assert product.has_profile() is False
    assert product.load_profile() == ""


def test_stack_save_load(product_root):
    assert product.load_stack() == ""
    product.save_stack("FastAPI + Next.js + Postgres")
    assert "FastAPI" in product.load_stack()


# ── C6: pinned design-system tier — human-authored mandates the agent can't rewrite ──────
def test_design_system_absent_pinned_is_exact_prior_behavior(product_root):
    assert product.load_design_system() == ""
    product.save_design_system("## Tokens\nprimary: blue")
    assert "primary: blue" in product.load_design_system()


def test_pinned_survives_a_managed_save_and_loads_first(product_root):
    product_root.mkdir(parents=True, exist_ok=True)
    (product_root / "design_system.pinned.md").write_text(
        "## STANDING RULE\nConsume the installed design system — never hand-roll UI.")
    product.save_design_system("## Tokens\nspacing: 8px")
    loaded = product.load_design_system()
    # pinned comes FIRST, then managed; both present
    assert loaded.index("STANDING RULE") < loaded.index("Tokens")
    assert "Consume the installed design system" in loaded and "spacing: 8px" in loaded
    # save writes ONLY the managed file — the pinned file is untouched
    assert (product_root / "design_system.pinned.md").read_text().count("STANDING RULE") == 1
    product.save_design_system("## Tokens\nspacing: 16px")   # rewrite managed again
    assert "STANDING RULE" in product.load_design_system()   # pinned still there


def test_pinned_only_no_managed(product_root):
    product_root.mkdir(parents=True, exist_ok=True)
    (product_root / "design_system.pinned.md").write_text("## RULE\nkeep it")
    assert product.load_design_system() == "## RULE\nkeep it"


# ── C7: deploy-target persistence (I5) ───────────────────────────────────────────────────
def test_deploy_target_absent_is_empty(product_root):
    assert product.load_deploy_target() == ""


def test_deploy_target_save_load(product_root):
    product.save_deploy_target("local compose only, dry-run manifests")
    assert "dry-run" in product.load_deploy_target()
