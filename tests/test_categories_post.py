"""POST /api/expenses/categories — add sub-category under existing parent."""
from __future__ import annotations

import pytest


@pytest.fixture()
def client_with_categories(expense_client):
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults,
        seed_user_categories,
    )
    SessionLocal = expense_client.app.state.session_factory
    with SessionLocal() as s:
        seed_system_defaults(s)
        s.flush()
        seed_user_categories(s, "ariel")
        s.commit()
    return expense_client


def test_create_subcategory_happy(client_with_categories):
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance_other",
              "slug": "health", "label_en": "Health Insurance",
              "label_he": "ביטוח בריאות"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "insurance_other.health"
    assert body["label_en"] == "Health Insurance"
    assert body["parent_slug"] == "insurance_other"


def test_create_subcategory_duplicate_returns_409(client_with_categories):
    client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance_other",
              "slug": "health", "label_en": "Health Insurance"},
    )
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance_other",
              "slug": "health", "label_en": "Health Insurance"},
    )
    assert resp.status_code == 409, resp.text


def test_create_subcategory_parent_not_top_level_returns_422(client_with_categories):
    # First add insurance_other.health.
    client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance_other",
              "slug": "health", "label_en": "Health Insurance"},
    )
    # Now try to add insurance_other.health.dental — parent is not top-level.
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance_other.health",
              "slug": "dental", "label_en": "Dental"},
    )
    assert resp.status_code == 422, resp.text


def test_create_subcategory_slug_with_dot_returns_422(client_with_categories):
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "insurance_other",
              "slug": "health.life", "label_en": "X"},
    )
    assert resp.status_code == 422, resp.text


def test_create_subcategory_unknown_parent_returns_404(client_with_categories):
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "no-such",
              "slug": "x", "label_en": "X"},
    )
    assert resp.status_code == 404, resp.text


def test_create_subcategory_inherits_parent_flags(client_with_categories):
    # 'transfers' is is_excluded_from_spend=True per default taxonomy.
    resp = client_with_categories.post(
        "/api/expenses/categories",
        json={"user_id": "ariel", "parent_slug": "transfers",
              "slug": "internal_bit", "label_en": "BIT (internal)"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_excluded_from_spend"] is True
