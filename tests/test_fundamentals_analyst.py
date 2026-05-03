"""FundamentalsAnalystAgent tests."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.fundamentals_analyst import (
    FundamentalsAnalystAgent,
    FundamentalsReport,
    TickerFundamentals,
)


class _MockFundamentalsAgent(FundamentalsAnalystAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=200,
            tokens_out=300,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_fundamentals_report_shape() -> None:
    canned = {
        "per_ticker": {
            "NVDA": {
                "ticker": "NVDA",
                "pe_ratio": 60.0,
                "peg_ratio": 1.2,
                "ev_ebitda": 45.0,
                "revenue_growth_yoy": 0.45,
                "earnings_growth_yoy": 0.50,
                "debt_to_equity": 0.4,
                "balance_sheet_quality": "strong",
                "fair_value_estimate_usd": 220.0,
                "confidence": "MEDIUM",
                "notes": "AI demand premium",
                "cited_sources": ["sec:NVDA:10K", "yfinance:NVDA"],
            }
        },
        "summary": "NVDA: high multiple but justified by AI growth.",
        "confidence": "MEDIUM",
        "cited_sources": ["sec:NVDA:10K"],
    }
    agent = _MockFundamentalsAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        tickers=["NVDA"],
        fundamentals_payload={
            "NVDA": {
                "pe_ratio": 60.0,
                "peg_ratio": 1.2,
                "ev_ebitda": 45.0,
                "revenue_growth_yoy": 0.45,
                "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?CIK=NVDA",
            }
        },
    )
    out = report.output
    assert isinstance(out, FundamentalsReport)
    assert "NVDA" in out.per_ticker
    assert isinstance(out.per_ticker["NVDA"], TickerFundamentals)
    assert out.per_ticker["NVDA"].balance_sheet_quality == "strong"
    assert out.cited_sources, "citation gate"


@pytest.mark.asyncio
async def test_fundamentals_payload_omitted_ticker() -> None:
    """A ticker without payload still produces an entry with `(no fundamentals payload...)`."""
    canned = {
        "per_ticker": {},
        "summary": "(no data)",
        "confidence": "LOW",
        "cited_sources": ["domain_knowledge/_meta/sources.md"],
    }
    agent = _MockFundamentalsAgent(user_id="ariel", canned_output=canned)
    sys, usr = agent.build_prompt(
        tickers=["NVDA", "TSLA"],
        fundamentals_payload={"NVDA": {"pe_ratio": 60.0}},
    )
    assert "NVDA" in usr and "TSLA" in usr
    assert "no fundamentals payload" in usr
    assert "FundamentalsReport" in sys
