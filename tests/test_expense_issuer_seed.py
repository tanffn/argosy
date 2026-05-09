"""Tests for the Hebrew ענף → slug mapping (Max card pre-categorization)."""

import pytest

from argosy.services.expense_ingest.issuer_seed import (
    map_issuer_category, IssuerSeedResult,
)


@pytest.mark.parametrize("anaf,slug,confidence", [
    ("מסעדות",            "dining_out.restaurants",        0.90),
    ("תיירות",            "travel.vacation_other",          0.85),
    ("רפואה ובריאות",     "healthcare.medical_other",       0.85),
    ("ריהוט ובית",        "housing.home_maintenance",       0.80),
    ("דלק ותחנות דלק",    "transportation.fuel",            0.95),
    ("לבוש והנעלה",       "discretionary.shopping_clothing", 0.90),
])
def test_unambiguous_anaf_maps_directly(anaf, slug, confidence):
    result = map_issuer_category(anaf)
    assert result.slug == slug
    assert result.confidence == confidence
    assert result.defer_to_llm is False


@pytest.mark.parametrize("anaf", [
    "ביטוח ופיננסים",
    "תקשורת ומחשבים",
    "מקצועות חופשיים",
])
def test_ambiguous_anaf_defers_to_llm(anaf):
    result = map_issuer_category(anaf)
    assert result.slug is None
    assert result.defer_to_llm is True
    assert result.hint == anaf


def test_unknown_anaf_defers_with_hint():
    result = map_issuer_category("בלה בלה ענף לא ידוע")
    assert result.defer_to_llm is True
    assert result.hint == "בלה בלה ענף לא ידוע"
    assert result.slug is None


def test_none_input_returns_no_seed():
    result = map_issuer_category(None)
    assert result.slug is None
    assert result.defer_to_llm is False
    assert result.hint is None
