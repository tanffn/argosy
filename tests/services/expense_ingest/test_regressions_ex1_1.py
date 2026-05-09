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
