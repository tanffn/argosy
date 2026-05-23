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

    async def _call_model(
        self, *, system: str, user: str, **_extra: object,
    ) -> ModelCall:
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
    sys, usr, sources = agent.build_prompt(fx_payload={})
    assert "no FX data supplied" in usr
    assert "FXReport" in sys
    assert sources == []


@pytest.mark.asyncio
async def test_fx_build_prompt_returns_sources_tuple() -> None:
    """build_prompt returns (system, user, sources) with one document block
    per currency pair, source_ids keyed by `fx/rates/<pair>`, and the user
    prompt references those source_ids instead of inlining the payload."""
    agent = _MockFXAgent(
        user_id="ariel",
        canned_output={
            "pairs": [],
            "position_sizing_notes": [],
            "hedging_recommendations": [],
            "summary": "",
            "confidence": "LOW",
            "cited_sources": ["fred:none"],
        },
    )
    payload = {
        "USD/NIS": {
            "spot": 3.65,
            "pct_change_30d": -1.2,
            "pct_change_90d": 0.4,
            "source": "fred:DEXISUS",
        },
        "USD/EUR": {
            "spot": 0.92,
            "pct_change_30d": 0.3,
            "pct_change_90d": -1.1,
            "source": "fred:DEXUSEU",
        },
    }
    system, user, sources = agent.build_prompt(fx_payload=payload)

    # Source IDs use the documented `fx/rates/<pair>` shape, one per pair.
    source_ids = [sid for sid, _ in sources]
    assert set(source_ids) == {"fx/rates/USD/NIS", "fx/rates/USD/EUR"}
    assert len(sources) == 2

    # Source body carries the full per-pair payload so it can be cited.
    bodies = {sid: body for sid, body in sources}
    assert "spot: 3.65" in bodies["fx/rates/USD/NIS"]
    assert "pct_change_30d: -1.2" in bodies["fx/rates/USD/NIS"]
    assert "source: fred:DEXISUS" in bodies["fx/rates/USD/NIS"]
    assert "source: fred:DEXUSEU" in bodies["fx/rates/USD/EUR"]

    # User prompt references source_ids rather than inlining bodies.
    assert "`fx/rates/USD/NIS`" in user
    assert "`fx/rates/USD/EUR`" in user
    assert "3.65" not in user
    assert "fred:DEXISUS" not in user

    # System prompt advertises the document-block convention.
    assert "fx/rates/" in system
