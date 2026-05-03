"""MacroAnalystAgent tests. Mock the Anthropic client."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.macro_analyst import MacroAnalystAgent, MacroReport


class _MockMacroAgent(MacroAnalystAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
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
        "cited_sources": ["fred:VIXCLS", "fred:DGS10", "boi:USD_NIS"],
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
