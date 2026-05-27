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
        self.last_sources: list[tuple[str, str]] | None = None

    async def _call_model(
        self,
        *,
        system: str,
        user: str,
        sources: list[tuple[str, str]] | None = None,
        image_attachments: list = None,
    ) -> ModelCall:
        self.last_sources = sources
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
    # Wave A: build_prompt should expose the per-ticker payload as a
    # document source so the Citations API can attribute claims.
    assert agent.last_sources is not None
    assert any(sid == "fundamentals/NVDA" for sid, _ in agent.last_sources)
    nvda_source = next(c for sid, c in agent.last_sources if sid == "fundamentals/NVDA")
    assert "pe_ratio: 60.0" in nvda_source
    assert "source_url:" in nvda_source


@pytest.mark.asyncio
async def test_fundamentals_payload_omitted_ticker() -> None:
    """A ticker without payload is called out in the user prompt and contributes no source."""
    canned = {
        "per_ticker": {},
        "summary": "(no data)",
        "confidence": "LOW",
        "cited_sources": ["domain_knowledge/_meta/sources.md"],
    }
    agent = _MockFundamentalsAgent(user_id="ariel", canned_output=canned)
    sys_prompt, usr, sources = agent.build_prompt(
        tickers=["NVDA", "TSLA"],
        fundamentals_payload={"NVDA": {"pe_ratio": 60.0}},
    )
    assert "NVDA" in usr and "TSLA" in usr
    assert "No fundamentals payload was attached for: TSLA" in usr
    assert "FundamentalsReport" in sys_prompt
    # Only NVDA contributes a source; TSLA's payload is absent.
    source_ids = [sid for sid, _ in sources]
    assert source_ids == ["fundamentals/NVDA"]


@pytest.mark.asyncio
async def test_unsupported_citation_flagged_in_report() -> None:
    """W7 — invented source_ids must be flagged on agent_report.hallucinated_sources.

    The model is stubbed to return a per-ticker citation that does NOT
    appear in the build_prompt's sources list (``fundamentals/NVDA``).
    The agent should:
      * Not strip the offending id from the output (verify it's still in
        the response_text's per-ticker cited_sources).
      * Surface it on ``AgentReport.hallucinated_sources``.
    """
    canned = {
        "per_ticker": {
            "NVDA": {
                "ticker": "NVDA",
                "pe_ratio": 60.0,
                "balance_sheet_quality": "strong",
                "fair_value_estimate_usd": 220.0,
                "confidence": "MEDIUM",
                "notes": "AI demand premium",
                # `fundamentals/NVDA` is supplied as a source — legitimate.
                # `robotaxi/FSD/Optimus` is invented — should be flagged.
                "cited_sources": ["fundamentals/NVDA", "robotaxi/FSD/Optimus"],
            }
        },
        "summary": "NVDA: high multiple but justified by AI growth.",
        "confidence": "MEDIUM",
        "cited_sources": ["fundamentals/NVDA"],
    }
    agent = _MockFundamentalsAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        tickers=["NVDA"],
        fundamentals_payload={
            "NVDA": {
                "pe_ratio": 60.0,
                "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?CIK=NVDA",
            }
        },
    )

    assert report.hallucinated_sources == ["robotaxi/FSD/Optimus"], (
        "invented source_id should be flagged, not silently stripped"
    )
    # The invented id is NOT stripped from the output — flag, don't strip.
    cited = report.output.per_ticker["NVDA"].cited_sources
    assert "robotaxi/FSD/Optimus" in cited, (
        "offending citation should remain in the output for downstream review"
    )
    assert "fundamentals/NVDA" in cited, "legitimate citation should pass through"


@pytest.mark.asyncio
async def test_legitimate_citations_no_hallucination_flag() -> None:
    """W7 negative test — citations that match supplied source_ids verbatim
    must not be flagged."""
    canned = {
        "per_ticker": {
            "NVDA": {
                "ticker": "NVDA",
                "pe_ratio": 60.0,
                "balance_sheet_quality": "strong",
                "fair_value_estimate_usd": 220.0,
                "confidence": "MEDIUM",
                "cited_sources": ["fundamentals/NVDA"],
            }
        },
        "summary": "NVDA strong.",
        "confidence": "MEDIUM",
        "cited_sources": ["fundamentals/NVDA"],
    }
    agent = _MockFundamentalsAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        tickers=["NVDA"],
        fundamentals_payload={"NVDA": {"pe_ratio": 60.0}},
    )
    assert report.hallucinated_sources == []


@pytest.mark.asyncio
async def test_fundamentals_build_prompt_empty_payload() -> None:
    """When no tickers have payload, sources is empty and missing list covers all."""
    canned = {
        "per_ticker": {},
        "summary": "(no data)",
        "confidence": "LOW",
        "cited_sources": ["domain_knowledge/_meta/sources.md"],
    }
    agent = _MockFundamentalsAgent(user_id="ariel", canned_output=canned)
    _sys, usr, sources = agent.build_prompt(
        tickers=["NVDA", "TSLA"],
        fundamentals_payload={},
    )
    assert sources == []
    assert "Attached fundamentals sources: (none)" in usr
    assert "NVDA" in usr and "TSLA" in usr
