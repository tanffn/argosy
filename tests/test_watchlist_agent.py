"""WatchlistAgent tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.watchlist import (
    WatchlistAgent,
    WatchlistEntry,
    WatchlistReport,
)


class _MockWatchlistAgent(WatchlistAgent):
    def __init__(self, *, user_id: str, canned_output: dict) -> None:
        super().__init__(user_id=user_id)
        self._canned = canned_output

    async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
        return ModelCall(
            text=json.dumps(self._canned),
            tokens_in=80,
            tokens_out=140,
            model=self.model,
        )


@pytest.mark.asyncio
async def test_watchlist_report_shape() -> None:
    canned = {
        "current_tickers": [
            {"ticker": "NVDA", "kind": "reduce", "note": "Plan flags reduce."},
            {"ticker": "MSFT", "kind": "position", "note": ""},
            {"ticker": "GOOG", "kind": "candidate", "note": "Plan candidate."},
        ],
        "added_today": ["GOOG"],
        "removed_today": [],
        "candidates_under_review": ["GOOG"],
        "summary": "Added GOOG as a candidate; NVDA still in reduce.",
        "confidence": "HIGH",
        "cited_sources": ["tsv:may2026", "plan:v2.0"],
    }
    agent = _MockWatchlistAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        positions_tickers=["NVDA", "MSFT"],
        prior_watchlist=[
            {"ticker": "NVDA", "kind": "reduce", "note": ""},
            {"ticker": "MSFT", "kind": "position", "note": ""},
        ],
        plan_candidates=["GOOG"],
        plan_reduce_list=["NVDA"],
        snapshot_label="tsv:may2026",
    )
    out = report.output
    assert isinstance(out, WatchlistReport)
    assert len(out.current_tickers) == 3
    assert isinstance(out.current_tickers[0], WatchlistEntry)
    assert "GOOG" in out.added_today
    assert "NVDA" in [e.ticker for e in out.current_tickers if e.kind == "reduce"]
    assert out.cited_sources


@pytest.mark.asyncio
async def test_watchlist_no_prior_creates_all_as_added() -> None:
    """When prior_watchlist is empty, every current ticker is added_today."""
    canned = {
        "current_tickers": [
            {"ticker": "MSFT", "kind": "position", "note": ""}
        ],
        "added_today": ["MSFT"],
        "removed_today": [],
        "candidates_under_review": [],
        "summary": "First snapshot.",
        "confidence": "HIGH",
        "cited_sources": ["tsv:initial"],
    }
    agent = _MockWatchlistAgent(user_id="ariel", canned_output=canned)
    report = await agent.run(
        positions_tickers=["MSFT"],
        prior_watchlist=[],
        snapshot_label="tsv:initial",
    )
    assert "MSFT" in report.output.added_today
