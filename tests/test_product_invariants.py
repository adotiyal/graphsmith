"""Knowledge-base wiring (Steps 1-2): the static product-invariants extractor, the
injection helper, and the now-non-silent profile/design_system cap. These are the
deterministic units behind giving architect/test_author/engineer/qa standing,
code-verifiable product context."""
import os

from tools import registry, qa_utils, product


def _write(d, name, content):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)
    return p


def test_extract_product_invariants_from_models(tmp_path):
    root = str(tmp_path)
    models = os.path.join(root, "backend", "app", "models")
    _write(models, "event.py", (
        "from sqlalchemy import CheckConstraint\n"
        "class Event(Base):\n"
        '    __table_args__ = (CheckConstraint("capacity >= 0", name="ck_events_capacity_non_negative"),)\n'
        "    capacity: Mapped[int] = mapped_column(Integer, nullable=False)\n"
    ))
    _write(models, "registration.py", (
        '"""A row exists iff the user holds a spot. spots_remaining is never stored here."""\n'
        "class Registration(Base):\n"
        '    __table_args__ = (UniqueConstraint("user_id", "event_id", name="uq_reg"),)\n'
    ))
    _write(models, "company.py", (
        "import enum\n"
        "class CompanyStatus(str, enum.Enum):\n"
        '    pending = "pending"\n'
        '    approved = "approved"\n'
        "class Company(Base):\n"
        "    email: Mapped[str] = mapped_column(\n"
        "        String(255), nullable=False, unique=True, index=True\n"
        "    )\n"
    ))
    out = registry.extract_product_invariants(root)
    assert "CHECK(capacity >= 0)" in out
    assert "UNIQUE(user_id, event_id)" in out
    assert "UNIQUE(email)" in out                       # multi-line column unique
    assert "never stored" in out.lower()                # computed-not-stored note
    assert "enum CompanyStatus: pending, approved" in out


def test_extract_returns_empty_when_no_models(tmp_path):
    # Extend-mode / greenfield safety: never crash, just yield no context.
    assert registry.extract_product_invariants(str(tmp_path)) == ""
    assert registry.extract_product_invariants(None) == ""
    assert registry.extract_product_invariants("/no/such/dir/xyz123") == ""


def test_extractor_caps_output(tmp_path):
    models = os.path.join(str(tmp_path), "app", "models")
    fields = "\n".join(
        f"    f{i}: Mapped[str] = mapped_column(String, unique=True)" for i in range(400)
    )
    _write(models, "big.py", f"class Big(Base):\n{fields}\n")
    out = registry.extract_product_invariants(str(tmp_path), cap=500)
    assert len(out) <= 600
    assert "truncated" in out


def test_invariants_block_present_and_absent():
    assert qa_utils.product_invariants_block({}) == ""
    assert qa_utils.product_invariants_block({"product_invariants": None}) == ""
    assert qa_utils.product_invariants_block({"product_invariants": "   "}) == ""
    blk = qa_utils.product_invariants_block({"product_invariants": "- **Event**: CHECK(capacity >= 0)"})
    assert "OVERRIDE" in blk
    assert "CHECK(capacity >= 0)" in blk


def test_profile_cap_is_not_silent(capsys, tmp_path, monkeypatch):
    # A canonical file over its cap WARNS instead of silently dropping its tail.
    monkeypatch.setattr(product, "PROFILE_PATH", tmp_path / "profile.md")
    monkeypatch.setattr(product, "MAX_PROFILE_CHARS", 50)
    (tmp_path / "profile.md").write_text("x" * 200)
    out = product.load_profile()
    assert len(out) == 50
    assert "truncated" in capsys.readouterr().out.lower()


def test_real_madclub_backend_if_present():
    # Smoke against the actual managed project when it exists (skipped in clean checkouts).
    # Phase-ROBUST: the project is rebuilt phase-by-phase, so assert only invariants that
    # exist from phase 1 onward (Adventure's unique name) — never a later-phase-only
    # constraint like UNIQUE(user_id, event_id), which isn't present until registrations land.
    if not os.path.isdir("workspace/project/backend/app/models"):
        return
    out = registry.extract_product_invariants("workspace/project")
    assert "Model invariants" in out
    assert "UNIQUE(name)" in out
