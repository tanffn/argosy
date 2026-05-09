"""Unit tests for HouseholdCategorizerAgent — uses a mock LLM, no live calls."""

from datetime import date
from unittest.mock import patch

import pytest

from argosy.agents.household_categorizer_types import (
    CategorizeRow, CategorizeRequest, CategorizeResult, CategorizeResponse,
)


def _row(tx_id: int, merchant: str, amount: float = 100.0,
         direction: str = "debit", issuer: str = "isracard",
         hint: str | None = None) -> CategorizeRow:
    return CategorizeRow(
        tx_id=tx_id, merchant_normalized=merchant.lower(),
        merchant_raw=merchant, amount_nis=amount, direction=direction,
        occurred_on=date(2026, 4, 8),
        issuer_kind="card", issuer_name=issuer, issuer_category_he=hint,
    )


def test_categorize_row_construction():
    r = _row(1, "NETFLIX.COM")
    assert r.tx_id == 1
    assert r.amount_nis == 100.0


def test_categorize_request_round_trip():
    req = CategorizeRequest(
        transactions=[_row(1, "NETFLIX.COM"), _row(2, "WOLT")],
        taxonomy=["dining_out.takeout", "subscriptions.streaming"],
    )
    assert len(req.transactions) == 2


def test_categorize_response_parses_results():
    resp = CategorizeResponse(
        results=[
            CategorizeResult(tx_id=1, category_slug="subscriptions.streaming",
                             confidence=0.95, rationale="Netflix is streaming"),
            CategorizeResult(tx_id=2, category_slug="uncategorized",
                             confidence=0.40, rationale="ambiguous"),
        ],
        model="sonnet", tokens_in=100, tokens_out=50, cost_usd=0.001,
    )
    assert resp.results[0].confidence == 0.95


def test_categorize_result_validation_on_confidence_range():
    with pytest.raises(Exception):
        CategorizeResult(tx_id=1, category_slug="x", confidence=1.5,
                         rationale="x")
    with pytest.raises(Exception):
        CategorizeResult(tx_id=1, category_slug="x", confidence=-0.1,
                         rationale="x")


@patch("argosy.agents.household_categorizer.HouseholdCategorizerAgent._invoke_llm")
def test_agent_returns_uncategorized_below_threshold(mock_llm):
    """Even when the LLM picks a slug, confidence < 0.85 -> uncategorized."""
    from argosy.agents.household_categorizer import HouseholdCategorizerAgent
    mock_llm.return_value = CategorizeResponse(
        results=[CategorizeResult(tx_id=1, category_slug="dining_out.restaurants",
                                  confidence=0.50, rationale="weak signal")],
        model="sonnet", tokens_in=10, tokens_out=5, cost_usd=0.0001,
    )
    agent = HouseholdCategorizerAgent(user_id="ariel")
    out = agent.categorize_batch([_row(1, "Vendor X")], taxonomy=["dining_out.restaurants"])
    assert out[0].category_slug == "uncategorized"
    assert out[0].confidence == 0.50


@patch("argosy.agents.household_categorizer.HouseholdCategorizerAgent._invoke_llm")
def test_agent_passes_through_high_confidence(mock_llm):
    from argosy.agents.household_categorizer import HouseholdCategorizerAgent
    mock_llm.return_value = CategorizeResponse(
        results=[CategorizeResult(tx_id=1, category_slug="subscriptions.streaming",
                                  confidence=0.95, rationale="Netflix")],
        model="sonnet", tokens_in=10, tokens_out=5, cost_usd=0.0001,
    )
    agent = HouseholdCategorizerAgent(user_id="ariel")
    out = agent.categorize_batch([_row(1, "NETFLIX.COM")],
                                  taxonomy=["subscriptions.streaming"])
    assert out[0].category_slug == "subscriptions.streaming"
    assert out[0].confidence == 0.95
