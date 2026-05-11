"""Smoke tests for Pydantic types backing the merchant endpoints."""
from argosy.api.routes.expenses import (
    MerchantOut, MerchantsListResponse,
    MerchantPatchRequest, MerchantPatchResponse,
    BulkCategoryRequest, BulkCategoryItemResult, BulkCategoryResponse,
)


def test_merchant_out_fields():
    m = MerchantOut(
        merchant_normalized="X", category_slug="food.groceries",
        category_label="Groceries", parent_slug="food",
        parent_label="Food", confidence=0.92, source="llm",
        is_cached=True, tx_count=3, total_nis=100.0, total_usd=0.0,
        last_seen="2026-05-08",
    )
    assert m.tx_count == 3


def test_merchant_patch_request_accepts_either_shape():
    a = MerchantPatchRequest(user_id="ariel", category_slug="food.groceries")
    b = MerchantPatchRequest(user_id="ariel", confirm=True)
    assert a.confirm is False
    assert b.confirm is True
    assert b.category_slug is None


def test_bulk_category_request_requires_one_of():
    import pytest as _pt
    from pydantic import ValidationError
    with _pt.raises(ValidationError):
        BulkCategoryRequest(user_id="ariel", merchant_normalizeds=["X"])
