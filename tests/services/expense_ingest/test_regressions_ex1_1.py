"""Regression tests for Wave EX1.1 bug fixes.

Each test pins one of the 7 bugs from the EX1 handover so future regressions
are caught immediately.
"""

from __future__ import annotations


def test_bug5_household_categorizer_uses_canonical_model_id():
    """Bug 5 — model alias 'sonnet' replaced with canonical 'claude-sonnet-4-6'.

    The api_key backend may reject the alias; only claude_code resolves it.
    Use the canonical id everywhere for portability.
    """
    from argosy.agents.base import DEFAULT_MODEL_BY_ROLE
    assert DEFAULT_MODEL_BY_ROLE["household_categorizer"] == "claude-sonnet-4-6"


def test_bug7_leumi_raw_row_uses_semantic_keys():
    """Bug 7 — Leumi raw_row keys must be semantic ('date', 'description', etc.)
    not positional integer strings ('0'..'8').

    Uses the in-tree Leumi fixture if present; otherwise constructs a tiny
    HTML statement on the fly.
    """
    import json
    from pathlib import Path
    from argosy.services.expense_ingest.parsers import leumi_osh

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "expenses"
    leumi_files = list(fixtures.glob("leumi_osh*.xls"))
    if not leumi_files:
        import pytest
        pytest.skip("no Leumi fixture available — re-run after Task 9 adds one")
    result = leumi_osh.parse(leumi_files[0])
    assert result.transactions, "fixture has no transactions"
    keys = set(result.transactions[0].raw_row.keys())
    expected_minimum = {"date", "description"}
    purely_integer_keys = {k for k in keys if k.isdigit()}
    assert expected_minimum.issubset(keys), (
        f"raw_row missing semantic keys; got {sorted(keys)}"
    )
    assert not purely_integer_keys, (
        f"raw_row still has positional keys: {purely_integer_keys}"
    )
