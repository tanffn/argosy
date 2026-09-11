from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

import argosy.orchestrator.loops.watchlist as watchlist_mod
from argosy.agents.base import ModelCall
from argosy.agents.watchlist import WatchlistAgent
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import (
    ActionProposal,
    AgentReport,
    PortfolioSnapshotRow,
    Proposal,
    ScanState,
    User,
)


class _OpenCostGuard:
    async def should_pause_non_routine(self, **kwargs) -> bool:
        return False


@pytest.mark.asyncio
async def test_default_gather_connects_book_discovery_and_verdicts(engine) -> None:
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            PortfolioSnapshotRow(
                user_id="ariel",
                snapshot_date=date.today(),
                imported_at=now,
                source_path="test",
                positions_json=json.dumps(
                    [
                        {"symbol": "NVDA", "asset_type": "Equity"},
                        {"symbol": "-", "asset_type": "Cash"},
                    ]
                ),
            )
        )
        session.add_all(
            [
                Proposal(
                    user_id="ariel",
                    ticker="GLUE",
                    action="buy",
                    size_shares_or_currency=26_000,
                    size_units="currency",
                    tier="T2",
                    status="awaiting_human",
                    source="decision_funnel",
                    shadow=0,
                ),
                Proposal(
                    user_id="ariel",
                    ticker="NVDA",
                    action="sell",
                    size_shares_or_currency=519,
                    size_units="shares",
                    tier="T2",
                    status="awaiting_human",
                    source="verdict_trigger_sweep",
                    shadow=0,
                ),
                ScanState(
                    user_id="ariel",
                    ticker="IONQ",
                    status="active",
                    last_score=80,
                    last_seen_at=now,
                ),
                ActionProposal(
                    user_id="ariel",
                    summary="Watch APP",
                    rationale_md="Research ingest watch disposition.",
                    suggested_payload=json.dumps({"ticker": "APP", "watch_kind": "candidate"}),
                    severity="info",
                    surfaced_at=now,
                    expires_at=now + timedelta(days=30),
                    status="open",
                    kind="set_watchlist",
                    dedup_key="ingest_watch|youtube|video|APP",
                    execution_state="proposed",
                ),
            ]
        )
        await session.commit()

    inputs = await watchlist_mod._default_gather_inputs("ariel")
    assert inputs.positions_tickers == ["NVDA"]
    assert inputs.plan_candidates == ["APP", "GLUE", "IONQ"]
    assert inputs.plan_reduce_list == ["NVDA"]
    assert inputs.snapshot_label.startswith("portfolio_snapshot:")


@pytest.mark.real_seam
@pytest.mark.asyncio
async def test_watchlist_real_agent_dispatch_persists_report(monkeypatch, engine) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    monkeypatch.setattr(watchlist_mod, "get_cost_guard", lambda *a, **k: _OpenCostGuard())
    payload = {
        "current_tickers": [
            {"ticker": "NVDA", "kind": "reduce", "note": "Current holding."},
            {"ticker": "GLUE", "kind": "candidate", "note": "Under review."},
        ],
        "added_today": ["GLUE"],
        "removed_today": [],
        "candidates_under_review": ["GLUE"],
        "summary": "Tracked holdings and current candidate.",
        "confidence": "HIGH",
        "cited_sources": ["snapshot:test"],
    }

    async def fake_call(self, *, system, user, **kwargs):
        assert "GLUE" in user and "SNAPSHOT LABEL: snapshot:test" in user
        return ModelCall(text=json.dumps(payload), tokens_in=10, tokens_out=10, model="test-model")

    monkeypatch.setattr(WatchlistAgent, "_call_model", fake_call)
    inputs = watchlist_mod.WatchlistInputs(
        user_id="ariel",
        positions_tickers=["NVDA"],
        prior_watchlist=[{"ticker": "NVDA", "kind": "position", "note": ""}],
        plan_candidates=["GLUE"],
        plan_reduce_list=["NVDA"],
        snapshot_label="snapshot:test",
    )
    loop = watchlist_mod.WatchlistLoop(
        schedule=LoopSchedule(cron="30 8 * * *"),
        user_id="ariel",
        gather_inputs=lambda user_id: inputs,
    )
    result = await loop.tick(now=lambda: datetime(2026, 8, 26, tzinfo=UTC))

    async with db_mod.get_session() as session:
        roles = (await session.execute(select(AgentReport.agent_role))).scalars().all()
    assert result == {"status": "ok", "count": 2, "added": 1, "removed": 0}
    assert roles == ["watchlist"]
