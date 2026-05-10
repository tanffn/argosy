"""Tests for argosy.services.expense_dashboard aggregation helpers."""
from argosy.services import expense_dashboard


def test_module_importable():
    assert hasattr(expense_dashboard, "__name__")
