# tests/test_apply_merchant_category_service.py
"""Tests for argosy.services.merchant_service — the apply-category primitive
shared by PATCH /transactions, PATCH /merchants, and the bulk endpoint.
"""
from argosy.services import merchant_service


def test_module_importable():
    assert hasattr(merchant_service, "__name__")
