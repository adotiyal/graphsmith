"""
tools/product.py  (Design upgrade — persistent product profile)
---------------------------------------------------------------
The product profile is the company's standing context: product category, target users /
customer base, key use cases, brand & tone, and business goals. It is set ONCE by the
CEO/CTO and PERSISTS ACROSS features (like the tech-stack decision and learnings) — so
the Design and PM agents reason from real product context instead of re-deriving it each
feature. Per-feature gaps are still asked via the normal CEO/CTO escalation.

Stored at product/profile.md (gitignored by default — it's company context; un-ignore to
share across the team). Tests redirect PROFILE_ROOT to a temp dir.
"""

from pathlib import Path

PROFILE_ROOT = Path(__file__).parent.parent / "product"
PROFILE_PATH = PROFILE_ROOT / "profile.md"
MAX_PROFILE_CHARS = 4000


def _cap(text: str, cap: int, label: str) -> str:
    """Return text capped to `cap` chars, but NEVER silently — a head-slice that drops
    the tail of authoritative content is the exact failure class this codebase keeps
    hitting (skills, design spec, error logs). If we must cap, say so loudly so the
    operator raises the cap or trims the file, rather than losing load-bearing content
    invisibly. The keep-the-head slice is retained (callers expect a prefix), but the
    cap-hit is logged, not swallowed."""
    if len(text) > cap:
        print(f"[product] WARNING: {label} is {len(text)} chars > cap {cap} — truncated; "
              f"the TAIL is being dropped. Trim the file or raise the cap.")
        return text[:cap]
    return text


def has_profile() -> bool:
    return PROFILE_PATH.exists() and bool(PROFILE_PATH.read_text(encoding="utf-8").strip())


def load_profile() -> str:
    if not PROFILE_PATH.exists():
        return ""
    text = PROFILE_PATH.read_text(encoding="utf-8").strip()
    return _cap(text, MAX_PROFILE_CHARS, "profile.md")


def save_profile(text: str) -> str:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text((text or "").strip(), encoding="utf-8")
    return str(PROFILE_PATH)


# --- Tech stack (persisted across features for a greenfield product) ---
# The stack is finalized by the CEO/CTO once and reused for every later feature, so the
# architect doesn't re-ask it on every run. (Extend mode detects the stack from the repo
# instead and does NOT use this — the target repo is the source of truth.)

# --- Design system (persisted across features — the product's visual/UX memory) ---
# Fonts, color/spacing tokens, component inventory, UX patterns and microcopy voice.
# Written by the Design agent on the first UI feature and READ + EXTENDED on every
# later one, so successive features feel like the same product (no design disconnect).

MAX_DESIGN_SYSTEM_CHARS = 6000


def _design_system_path():
    return PROFILE_ROOT / "design_system.md"


def _design_system_pinned_path():
    return PROFILE_ROOT / "design_system.pinned.md"


def load_design_system() -> str:
    """The product's design-system memory, in TWO tiers:

    - PINNED (`design_system.pinned.md`, optional, HUMAN-authored): standing mandates the
      agent may NEVER rewrite (e.g. "consume the installed design system"). The design
      agent's memory-compaction once dropped exactly these human rules.
    - MANAGED (`design_system.md`): the agent-maintained memory (tokens, inventory, voice),
      rewritten each UI feature via save_design_system().

    Returns pinned FIRST, then managed (both capped-with-warning). Absent pinned = exact
    prior behavior (managed alone)."""
    pinned = _design_system_pinned_path()
    pinned_text = pinned.read_text(encoding="utf-8").strip() if pinned.exists() else ""
    managed = _design_system_path()
    managed_text = managed.read_text(encoding="utf-8").strip() if managed.exists() else ""
    # Cap the tiers SEPARATELY — capping the concatenation let a large pinned tier silently
    # evict the TAIL of the managed memory (the newest additions), observed live.
    if pinned_text:
        pinned_text = _cap(pinned_text, MAX_DESIGN_SYSTEM_CHARS, "design_system.pinned.md")
    if managed_text:
        managed_text = _cap(managed_text, MAX_DESIGN_SYSTEM_CHARS, "design_system.md")
    if pinned_text and managed_text:
        return pinned_text + "\n\n" + managed_text
    return pinned_text or managed_text


def save_design_system(text: str) -> str:
    """Write ONLY the managed tier — the pinned tier is human-owned and never touched here.

    Agents see load_design_system()'s pinned+managed CONCATENATION and re-emit it wholesale,
    so a save would silently duplicate the pinned block INTO the managed file (observed live:
    the next load then doubled the pinned rules and blew the cap). Strip any embedded copy of
    the current pinned text before writing."""
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    cleaned = (text or "").strip()
    pinned = _design_system_pinned_path()
    if pinned.exists():
        pinned_text = pinned.read_text(encoding="utf-8").strip()
        if pinned_text and pinned_text in cleaned:
            cleaned = cleaned.replace(pinned_text, "", 1).strip()
    _design_system_path().write_text(cleaned, encoding="utf-8")
    return str(_design_system_path())


def _interface_contract_path():
    return PROFILE_ROOT / "interface_contract.md"


def load_interface_contract() -> str:
    """The persisted Interface Contract — the cumulative testids + required microcopy the
    product's component kit GUARANTEES across phases (additive-only). Empty on first run."""
    p = _interface_contract_path()
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def save_interface_contract(text: str) -> str:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    _interface_contract_path().write_text((text or "").strip(), encoding="utf-8")
    return str(_interface_contract_path())


def _stack_path():
    return PROFILE_ROOT / "stack.md"


def load_stack() -> str:
    p = _stack_path()
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def save_stack(text: str) -> str:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    _stack_path().write_text((text or "").strip(), encoding="utf-8")
    return str(_stack_path())


# --- Deploy target (persisted across features, like the stack) ---
# The CEO/CTO's deploy-target decision (e.g. "local compose only, dry-run manifests" vs a
# cloud target). DevOps kept re-asking it every run across products (I5, 4th+ recurrence);
# once persisted, DevOps reuses it and never re-escalates. Free-text (stack-agnostic).

def _deploy_target_path():
    return PROFILE_ROOT / "deploy_target.md"


def load_deploy_target() -> str:
    p = _deploy_target_path()
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def save_deploy_target(text: str) -> str:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    _deploy_target_path().write_text((text or "").strip(), encoding="utf-8")
    return str(_deploy_target_path())
