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
