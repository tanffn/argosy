"""ConcentrationAnalystAgent tests. Mock the Anthropic client."""

from __future__ import annotations

import json

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.concentration_analyst import (
    Breach,
    ConcentrationAnalystAgent,
    ConcentrationReport,
)


class _MockConcentrationAgent(ConcentrationAnalystAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=120,
            tokens_out=140,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_concentration_report_shape() -> None:
    canned = {
        "breaches": [
            {
                "category": "NVDA",
                "actual_pct": 68.0,
                "cap_pct": 25.0,
                "severity": "breach",
                "note": "NVDA single-position cap exceeded by 43pp.",
            }
        ],
        "deltas_vs_target": {"NVDA": 53.0, "Growth": -5.0},
        "nvda_pace": {
            "shares_sold_ytd": 2000,
            "target_shares_ytd": 4000,
            "delta_shares": -2000,
            "on_track": False,
        },
        "summary": "NVDA way over cap; pace behind plan.",
        "confidence": "HIGH",
        "cited_sources": ["TSV 26-May", "Jacobs_Wealth_Plan v2.0"],
    }
    agent = _MockConcentrationAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        positions_summary="NVDA 11,471 shares × $200 ≈ $2.296M (~68% of liquid).",
        plan_targets={"NVDA": 15.0, "Growth": 20.0},
        nvda_shares_sold_ytd=2000,
        nvda_target_shares_ytd=4000,
    )
    out = report.output
    assert isinstance(out, ConcentrationReport)
    assert len(out.breaches) == 1
    assert isinstance(out.breaches[0], Breach)
    assert out.nvda_pace.on_track is False
    assert out.deltas_vs_target["NVDA"] == 53.0
    assert out.cited_sources
