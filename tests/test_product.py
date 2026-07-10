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


# ── P6-dogfood hardening: truncation caps must fit real specs/design-memory ──────────────
def test_design_system_over_old_6000_cap_loads_untruncated(product_root):
    # Gap 3: the design-system memory cap was raised 6000→16000 because a mature product's
    # memory (the madclub run's was 8.4KB) had its TAIL (newest mandates) dropped at 6000.
    body = "## Tokens\n" + ("token: x\n" * 900) + "\nTAIL_MANDATE: compose the installed DS"
    assert 6000 < len(body) < 16000
    product.save_design_system(body)
    loaded = product.load_design_system()
    assert "TAIL_MANDATE" in loaded            # would have been tail-dropped under the old 6000 cap
    assert product.MAX_DESIGN_SYSTEM_CHARS >= 16000


def test_design_spec_read_untruncated_for_a_large_feature_spec(tmp_path):
    # Gap 2: a 30KB feature spec was head-sliced (engineer 16000 / architect default 24000) so
    # the tail — carrying the console-nav wiring — was lost. Both now read it near-untruncated.
    from agents.engineer import DESIGN_SPEC_CAP
    from tools.file_io import read_artifact
    spec = tmp_path / "design_spec.md"
    body = "# Spec\n" + ("a design detail line\n" * 1500) + "\nCONSOLE_NAV_WIRING at the tail"
    assert len(body) > 24000
    spec.write_text(body, encoding="utf-8")
    got = read_artifact(str(spec), DESIGN_SPEC_CAP)
    assert "CONSOLE_NAV_WIRING at the tail" in got   # dropped under the old 16000/24000 caps
    assert DESIGN_SPEC_CAP >= 30000


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


def test_save_strips_reemitted_pinned_block(product_root):
    # Agents see load()'s pinned+managed concatenation and re-emit it wholesale on save —
    # observed live: the pinned block got duplicated INTO the managed file. save must strip it.
    product_root.mkdir(parents=True, exist_ok=True)
    (product_root / "design_system.pinned.md").write_text(
        "## STANDING RULE\nConsume the installed design system.")
    product.save_design_system(
        "## STANDING RULE\nConsume the installed design system.\n\n## Tokens\nspacing: 8px")
    managed = (product_root / "design_system.md").read_text()
    assert "STANDING RULE" not in managed          # pinned copy stripped from managed
    assert "spacing: 8px" in managed               # agent content kept
    loaded = product.load_design_system()
    assert loaded.count("STANDING RULE") == 1      # pinned appears exactly once


def test_save_strips_reformatted_pinned_echo_via_sentinel(product_root):
    # A live agent echoed the pinned block REFORMATTED (re-wrapped lines), defeating
    # exact-substring stripping. The load output carries a sentinel line between the tiers;
    # save keeps only what follows it — robust to any rewrapping of the pinned prose.
    product_root.mkdir(parents=True, exist_ok=True)
    (product_root / "design_system.pinned.md").write_text(
        "## STANDING RULE\nConsume the installed\ndesign system.")
    loaded = product.load_design_system()  # pinned only (no managed yet) — no sentinel
    assert product.PINNED_END_SENTINEL not in loaded
    product.save_design_system("## Tokens\nspacing: 8px")
    loaded = product.load_design_system()
    assert product.PINNED_END_SENTINEL in loaded   # both tiers -> sentinel between them
    # agent echoes the WHOLE loaded doc back, with the pinned prose REWRAPPED
    echo = loaded.replace("Consume the installed\ndesign system.",
                          "Consume the installed design system.") + "\n- new addition"
    product.save_design_system(echo)
    managed = (product_root / "design_system.md").read_text()
    assert "STANDING RULE" not in managed          # reformatted pinned still stripped
    assert "new addition" in managed and "spacing: 8px" in managed
    assert product.load_design_system().count("STANDING RULE") == 1


def test_pinned_and_managed_capped_separately_no_tail_eviction(product_root):
    # Capping the CONCATENATION let a large pinned tier evict the managed TAIL (the newest
    # additions) — observed live. Each tier is capped on its own instead.
    product_root.mkdir(parents=True, exist_ok=True)
    (product_root / "design_system.pinned.md").write_text("P" * 4000)
    product.save_design_system(("M" * 5000) + "\nNEWEST-ADDITION-MARKER")
    loaded = product.load_design_system()
    assert "NEWEST-ADDITION-MARKER" in loaded      # tail survives even though total > 6000
