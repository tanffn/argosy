"""TechnicalAnalystAgent tests."""

from __future__ import annotations

import json
from typing import Any

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
        self.last_sources: list[tuple[str, str]] | None = None

    async def _call_model(
        self,
        *,
        system: str,
        user: str,
        sources: list[tuple[str, str]] | None = None,
        **kwargs: Any,
    ) -> ModelCall:
        self.last_sources = sources
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


def test_build_prompt_returns_sources_tuple() -> None:
    """Wave A: build_prompt returns (system, user, sources) and the
    per-ticker indicator blocks are attached as document sources rather
    than inlined in the user prompt."""
    agent = TechnicalAnalystAgent(user_id="ariel")
    bp = agent.build_prompt(
        tickers=["NVDA", "AAPL"],
        indicators_payload={
            "NVDA": {
                "rsi_14": 28.0,
                "macd": 0.5,
                "macd_signal": 0.2,
                "source": "yfinance:NVDA:1d",
            },
            "AAPL": {"rsi_14": 75.0, "source": "yfinance:AAPL:1d"},
        },
    )
    assert len(bp) == 3
    system, user, sources = bp
    assert isinstance(system, str) and system
    assert isinstance(user, str) and user

    # Each ticker with a payload becomes a document source.
    assert sources == [
        (
            "indicators/NVDA",
            "## NVDA\n  - rsi_14: 28.0\n  - macd: 0.5\n  - macd_signal: 0.2"
            "\n  - source: yfinance:NVDA:1d",
        ),
        (
            "indicators/AAPL",
            "## AAPL\n  - rsi_14: 75.0\n  - source: yfinance:AAPL:1d",
        ),
    ]

    # User prompt references sources by source_id but does NOT inline the
    # indicator bodies (those live in the document blocks now).
    assert "indicators/NVDA" in user
    assert "indicators/AAPL" in user
    assert "rsi_14: 28.0" not in user
    assert "macd_signal: 0.2" not in user


def test_build_prompt_no_indicators_returns_empty_sources() -> None:
    """When no ticker has indicators, sources is empty and the user
    prompt mentions the missing tickers explicitly."""
    agent = TechnicalAnalystAgent(user_id="ariel")
    system, user, sources = agent.build_prompt(
        tickers=["NVDA"],
        indicators_payload={},
    )
    assert sources == []
    assert "NVDA" in user
    assert "no pre-computed indicator documents attached" in user


@pytest.mark.asyncio
async def test_run_forwards_sources_to_call_model() -> None:
    """BaseAgent.run threads the sources tuple through to _call_model."""
    canned = {
        "per_ticker": {
            "NVDA": {
                "ticker": "NVDA",
                "indicators": {"rsi_14": 28.0},
                "signal": "entry",
                "rationale": "Oversold.",
                "cited_sources": ["indicators/NVDA", "yfinance:NVDA:1d"],
            }
        },
        "summary": "NVDA entry.",
        "confidence": "MEDIUM",
        "cited_sources": ["indicators/NVDA"],
    }
    agent = _MockTechnicalAgent(user_id="ariel", canned_output=canned)
    await agent.run(
        tickers=["NVDA"],
        indicators_payload={"NVDA": {"rsi_14": 28.0, "source": "yfinance:NVDA:1d"}},
    )
    assert agent.last_sources is not None
    assert len(agent.last_sources) == 1
    assert agent.last_sources[0][0] == "indicators/NVDA"
