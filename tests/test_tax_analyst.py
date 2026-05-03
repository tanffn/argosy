"""TaxAnalystAgent tests."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.errors import AgentRunError
from argosy.agents.tax_analyst import (
    TaxAnalystAgent,
    TaxReport,
    TLHCandidate,
)


class _MockTaxAgent(TaxAnalystAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=300,
            tokens_out=400,
            model=self.model,
        )


_DOMAIN_KB = {
    "domain_knowledge/tax/israel/capital_gains.md": "Israeli capital gains tax: 25% on real gains.",
    "domain_knowledge/treaties/us_israel.md": "US-Israel treaty: 25% withholding on dividends, 25% credit.",
}


@pytest.mark.asyncio
async def test_tax_report_shape_with_citations() -> None:
    canned = {
        "tlh_candidates": [
            {
                "ticker": "TSLA",
                "lot_id": "lot-42",
                "quantity": 50.0,
                "cost_basis_usd": 30000.0,
                "current_price_usd": 200.0,
                "unrealized_loss_usd": 20000.0,
                "wash_sale_risk": False,
                "note": "Eligible for harvest.",
                "cited_sources": ["domain_knowledge/tax/israel/capital_gains.md"],
            }
        ],
        "dividend_projections": [
            {
                "ticker": "VTI",
                "annual_dividend_usd": 1500.0,
                "estimated_withholding_usd": 375.0,
                "estimated_residual_tax_usd": 0.0,
                "cited_sources": ["domain_knowledge/treaties/us_israel.md"],
            }
        ],
        "rsu_vest_estimates": [],
        "year_end_hints": ["Harvest TSLA loss before 31 Dec."],
        "summary": "TLH candidate; W-8BEN looks correct.",
        "confidence": "MEDIUM",
        "cited_sources": [
            "domain_knowledge/tax/israel/capital_gains.md",
            "domain_knowledge/treaties/us_israel.md",
        ],
    }
    agent = _MockTaxAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        lots_summary="TSLA lot-42: 50 shares @ $600 cost basis",
        dividends_summary="VTI: $1500 annual",
        rsu_schedule_summary="(no upcoming vests)",
        domain_kb_files=_DOMAIN_KB,
    )
    out = report.output
    assert isinstance(out, TaxReport)
    assert len(out.tlh_candidates) == 1
    assert isinstance(out.tlh_candidates[0], TLHCandidate)
    assert out.cited_sources


@pytest.mark.asyncio
async def test_tax_report_citation_gate_rejects_empty() -> None:
    """Output without any cited_sources fails the citation gate."""
    canned_empty = {
        "tlh_candidates": [],
        "dividend_projections": [],
        "rsu_vest_estimates": [],
        "year_end_hints": [],
        "summary": "Nothing actionable.",
        "confidence": "LOW",
        "cited_sources": [],
    }
    agent = _MockTaxAgent(user_id="ariel", canned_output=canned_empty)
    with pytest.raises(AgentRunError):
        await agent.run(
            lots_summary="(no lots)",
            dividends_summary="(no dividends)",
            rsu_schedule_summary="(none)",
            domain_kb_files=_DOMAIN_KB,
        )
