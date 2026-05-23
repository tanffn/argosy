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
        self._last_sources: list[tuple[str, str]] | None = None

    async def _call_model(
        self,
        *,
        system: str,
        user: str,
        sources: list[tuple[str, str]] | None = None,
        **_extra: object,
    ) -> ModelCall:
        # Capture the sources kwarg so tests can assert BaseAgent.run
        # forwards the 3-tuple's third element into the model call.
        self._last_sources = sources
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
    # BaseAgent.run unpacks the build_prompt 3-tuple and forwards sources
    # into _call_model so the Citations API path receives document blocks.
    assert agent._last_sources is not None
    source_ids = [sid for sid, _ in agent._last_sources]
    assert "portfolio/holdings" in source_ids
    assert "plan/targets" in source_ids


def test_build_prompt_returns_sources_tuple() -> None:
    """build_prompt returns (system, user, sources) with holdings + plan extracted."""
    agent = ConcentrationAnalystAgent(user_id="ariel")
    positions_summary = (
        "NVDA 11,471 shares × $200 ≈ $2.296M (~68% of liquid)."
    )
    plan_targets = {"NVDA": 15.0, "Growth": 20.0}

    result = agent.build_prompt(
        positions_summary=positions_summary,
        plan_targets=plan_targets,
        nvda_shares_sold_ytd=2000,
        nvda_target_shares_ytd=4000,
    )
    assert len(result) == 3
    system, user, sources = result

    # User prompt references source_ids but NO longer inlines the bodies.
    assert "portfolio/holdings" in user
    assert "plan/targets" in user
    assert "11,471 shares" not in user
    assert "target 15.0%" not in user
    # NVDA pace scalars remain inline.
    assert "shares_sold_ytd: 2000" in user
    assert "target_shares_ytd: 4000" in user

    # Sources carry the bodies in the documented order.
    source_ids = [sid for sid, _ in sources]
    assert source_ids == ["portfolio/holdings", "plan/targets"]
    bodies = dict(sources)
    assert "11,471 shares" in bodies["portfolio/holdings"]
    assert "NVDA: target 15.0%" in bodies["plan/targets"]
    assert "Growth: target 20.0%" in bodies["plan/targets"]

    # System prompt mentions the document source_ids.
    assert "portfolio/holdings" in system
    assert "plan/targets" in system


def test_build_prompt_empty_inputs_returns_no_sources() -> None:
    """No positions summary + no plan targets → sources == []."""
    agent = ConcentrationAnalystAgent(user_id="ariel")
    system, user, sources = agent.build_prompt(
        positions_summary="",
        plan_targets={},
    )
    assert sources == []
    assert "no positions summary supplied" in user
    assert "no plan targets supplied" in user
    # Schema still embedded in the system prompt.
    assert "ConcentrationReport" in system
