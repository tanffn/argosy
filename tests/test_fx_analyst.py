"""FXAnalystAgent tests."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.fx_analyst import FXAnalystAgent, FXReport, PairLevels


class _MockFXAgent(FXAnalystAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=80,
            tokens_out=120,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_fx_report_shape() -> None:
    canned = {
        "pairs": [
            {
                "pair": "USD/NIS",
                "spot": 3.65,
                "trend_30d": "weakening",
                "pct_change_30d": -1.2,
                "pct_change_90d": 0.4,
                "cited_sources": ["fred:DEXISUS"],
            }
        ],
        "position_sizing_notes": [
            "USD/NIS weakening; favor smaller USD purchases this month."
        ],
        "hedging_recommendations": [],
        "summary": "Mild USD weakness vs NIS.",
        "confidence": "MEDIUM",
        "cited_sources": ["fred:DEXISUS"],
    }
    agent = _MockFXAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        fx_payload={
            "USD/NIS": {
                "spot": 3.65,
                "pct_change_30d": -1.2,
                "pct_change_90d": 0.4,
                "source": "fred:DEXISUS",
            }
        },
    )
    out = report.output
    assert isinstance(out, FXReport)
    assert isinstance(out.pairs[0], PairLevels)
    assert out.pairs[0].trend_30d == "weakening"
    assert out.cited_sources


@pytest.mark.asyncio
async def test_fx_empty_payload_handled() -> None:
    """Empty payload must produce a usable prompt; agent returns LOW confidence."""
    agent = _MockFXAgent(
        user_id="ariel",
        canned_output={
            "pairs": [],
            "position_sizing_notes": [],
            "hedging_recommendations": [],
            "summary": "No FX data.",
            "confidence": "LOW",
            "cited_sources": ["fred:none"],
        },
    )
    sys, usr = agent.build_prompt(fx_payload={})
    assert "no FX data supplied" in usr
    assert "FXReport" in sys
