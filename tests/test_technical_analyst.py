"""TechnicalAnalystAgent tests."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.technical_analyst import (
    TechnicalAnalystAgent,
    TechnicalReport,
    TickerTechnicals,
)


class _MockTechnicalAgent(TechnicalAnalystAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=120,
            tokens_out=180,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_technical_report_shape() -> None:
    canned = {
        "per_ticker": {
            "NVDA": {
                "ticker": "NVDA",
                "indicators": {"rsi_14": 28.0, "macd": 0.5, "macd_signal": 0.2},
                "signal": "entry",
                "rationale": "Oversold + bullish MACD cross.",
                "cited_sources": ["yfinance:NVDA:1d"],
            }
        },
        "summary": "Mostly entry signals.",
        "confidence": "MEDIUM",
        "cited_sources": ["yfinance:NVDA:1d"],
    }
    agent = _MockTechnicalAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        tickers=["NVDA"],
        indicators_payload={
            "NVDA": {
                "rsi_14": 28.0,
                "macd": 0.5,
                "macd_signal": 0.2,
                "source": "yfinance:NVDA:1d",
            }
        },
    )
    out = report.output
    assert isinstance(out, TechnicalReport)
    assert isinstance(out.per_ticker["NVDA"], TickerTechnicals)
    assert out.per_ticker["NVDA"].signal == "entry"
    assert out.cited_sources


@pytest.mark.asyncio
async def test_technical_signal_values() -> None:
    """Signals must be one of entry|hold|exit per ticker."""
    canned = {
        "per_ticker": {
            "AAPL": {
                "ticker": "AAPL",
                "indicators": {"rsi_14": 75.0},
                "signal": "exit",
                "rationale": "Overbought.",
                "cited_sources": ["yfinance:AAPL:1d"],
            }
        },
        "summary": "AAPL overbought.",
        "confidence": "MEDIUM",
        "cited_sources": ["yfinance:AAPL:1d"],
    }
    agent = _MockTechnicalAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        tickers=["AAPL"],
        indicators_payload={"AAPL": {"rsi_14": 75.0, "source": "yfinance:AAPL:1d"}},
    )
    assert report.output.per_ticker["AAPL"].signal in ("entry", "hold", "exit")
