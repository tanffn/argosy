"""MacroAnalystAgent tests. Mock the Anthropic client."""

from __future__ import annotations

import json
from typing import Any

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.macro_analyst import MacroAnalystAgent, MacroReport


class _MockMacroAgent(MacroAnalystAgent):
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
        **_extra: Any,
    ) -> ModelCall:
        self.last_sources = sources
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=180,
            tokens_out=220,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_macro_regime_call() -> None:
    canned = {
        "regime": "risk_off",
        "drivers": ["VIX > 25", "10Y > 4.5%", "USD strength"],
        "key_metrics": {"vix": 27.0, "fred_10y": 4.55, "usd_nis": 3.7},
        "summary": "Risk-off conditions. Vol up, yields up.",
        "confidence": "MEDIUM",
        "cited_sources": [
            "macro/FRED/VIXCLS",
            "macro/FRED/DGS10",
            "macro/BOI/USD_NIS",
        ],
    }
    agent = _MockMacroAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        macro_snapshot={"vix": 27.0, "fred_10y": 4.55, "usd_nis": 3.7}
    )
    out = report.output
    assert isinstance(out, MacroReport)
    assert out.regime == "risk_off"
    assert "VIX > 25" in out.drivers
    assert out.key_metrics["fred_10y"] == 4.55
    assert out.cited_sources


def test_build_prompt_returns_sources_tuple() -> None:
    """Wave A: build_prompt returns (system, user, sources) and the
    per-indicator readings are attached as document sources rather than
    inlined as numeric values in the user prompt."""
    agent = MacroAnalystAgent(user_id="ariel")
    bp = agent.build_prompt(
        macro_snapshot={"vix": 27.0, "fred_10y": 4.55, "usd_nis": 3.7},
    )
    assert len(bp) == 3
    system, user, sources = bp
    assert isinstance(system, str) and system
    assert isinstance(user, str) and user

    # Each snapshot key becomes a document source. Sorted alphabetically
    # (matches the build_prompt implementation).
    assert sources == [
        ("macro/FRED/DGS10", "fred_10y: 4.55"),
        ("macro/BOI/USD_NIS", "usd_nis: 3.7"),
        ("macro/FRED/VIXCLS", "vix: 27.0"),
    ]

    # User prompt references sources by source_id but does NOT inline the
    # numeric values (those live in the document blocks now).
    assert "macro/FRED/VIXCLS" in user
    assert "macro/FRED/DGS10" in user
    assert "macro/BOI/USD_NIS" in user
    assert "27.0" not in user
    assert "4.55" not in user
    assert "3.7" not in user


def test_build_prompt_empty_snapshot_returns_empty_sources() -> None:
    """An empty snapshot yields no sources and an explicit 'empty' marker
    in the user prompt."""
    agent = MacroAnalystAgent(user_id="ariel")
    system, user, sources = agent.build_prompt(macro_snapshot={})
    assert sources == []
    assert "snapshot empty" in user


def test_build_prompt_unknown_key_falls_through() -> None:
    """Snapshot keys not in the canonical source map still get a document
    block, using a `macro/UNKNOWN/<key>` source_id."""
    agent = MacroAnalystAgent(user_id="ariel")
    _, _, sources = agent.build_prompt(macro_snapshot={"new_metric": 1.23})
    assert sources == [("macro/UNKNOWN/new_metric", "new_metric: 1.23")]


@pytest.mark.asyncio
async def test_run_forwards_sources_to_call_model() -> None:
    """BaseAgent.run threads the sources tuple through to _call_model."""
    canned = {
        "regime": "neutral",
        "drivers": ["VIX flat"],
        "key_metrics": {"vix": 18.0},
        "summary": "Neutral regime.",
        "confidence": "MEDIUM",
        "cited_sources": ["macro/FRED/VIXCLS"],
    }
    agent = _MockMacroAgent(user_id="ariel", canned_output=canned)
    await agent.run(macro_snapshot={"vix": 18.0})
    assert agent.last_sources is not None
    assert len(agent.last_sources) == 1
    assert agent.last_sources[0][0] == "macro/FRED/VIXCLS"
