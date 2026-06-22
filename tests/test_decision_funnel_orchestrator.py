"""Integration test for the decision-funnel orchestrator (no live LLM)."""
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from argosy.services.decision_funnel.orchestrator import run_funnel
from argosy.services.decision_funnel.triage import TriageOutcome
from argosy.services.decision_funnel.deep_decision import DeepDecisionOutcome
from argosy.state.models import (
    Base,
    DecisionSnapshot,
    FunnelRun,
    FunnelStageRow,
    PortfolioSnapshotRow,
    Proposal,
    User,
)

NOW = datetime(2026, 6, 22, 18, 30, tzinfo=timezone.utc)


@pytest.fixture
def sf():
    # StaticPool so every Session() shares ONE in-memory DB.
    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.add(
        PortfolioSnapshotRow(
            user_id="ariel",
            snapshot_date=date(2026, 6, 22),
            imported_at=NOW,
            positions_json=json.dumps([
                {"symbol": "NVDA", "asset_type": "Individual Stocks", "usd_value_k": 600},
                {"symbol": "CSPX", "asset_type": "Core Equity", "usd_value_k": 400},
            ]),
        )
    )
    s.commit()
    s.close()
    return SF


def _triage_go(candidate, **kwargs):
    return TriageOutcome(
        subject=candidate.subject, warrants_decision=True, urgency="HIGH",
        rationale="material", model="claude-sonnet-4-6", prompt_hash="h",
        tokens_in=100, tokens_out=20, cost_usd=0.01,
    )


@pytest.mark.asyncio
async def test_shadow_calibration_skips_stage3(sf):
    """Default: stage3 disabled -> Stage 0-2 run + traced, no deep decision."""
    settings = SimpleNamespace(decision_funnel_shadow=True, decision_funnel_stage3=False)

    async def _deep(**kwargs):  # should NOT be called
        raise AssertionError("stage3 must not run when disabled")

    out = await run_funnel(
        "ariel", now=NOW, session_factory=sf, triage_fn=_triage_go,
        deep_decision_fn=_deep, settings=settings,
    )
    assert out["stage1_routed"] >= 1  # NVDA breaches the fallback cap
    assert out["stage3_skipped"] >= 1
    assert out["stage3_proposed"] == 0

    s = sf()
    run = s.execute(sa.select(FunnelRun)).scalars().one()
    assert run.status == "ok"
    assert run.shadow == 1
    rows = s.execute(sa.select(FunnelStageRow).where(FunnelStageRow.run_id == run.id)).scalars().all()
    stages = {r.stage for r in rows}
    assert {"stage0", "stage1", "stage2", "stage3"} <= stages
    # No immutable snapshot or proposal in shadow-skip mode.
    assert s.execute(sa.select(sa.func.count()).select_from(DecisionSnapshot)).scalar_one() == 0
    s.close()


@pytest.mark.asyncio
async def test_stage3_enabled_records_snapshot_and_stamps_proposal(sf):
    settings = SimpleNamespace(decision_funnel_shadow=True, decision_funnel_stage3=True)

    async def _deep(*, user_id, ticker, funnel_meta=None, **kwargs):
        # Create a proposal row applying funnel_meta the way flow._persist_proposal
        # now does (shadow set ATOMICALLY at birth).
        s = sf()
        fm = funnel_meta or {}
        p = Proposal(
            user_id=user_id, ticker=ticker, action="sell", tier="T2",
            status="awaiting_human", source=fm.get("source", "manual"),
            shadow=int(fm.get("shadow", 0)), expires_at=fm.get("expires_at"),
            funnel_run_id=fm.get("funnel_run_id"),
        )
        s.add(p)
        s.commit()
        pid = p.id
        s.close()
        return DeepDecisionOutcome(
            ticker=ticker, status="approved", decision_run_id=None,
            proposal_id=pid, action="sell",
        )

    out = await run_funnel(
        "ariel", now=NOW, session_factory=sf, triage_fn=_triage_go,
        deep_decision_fn=_deep, settings=settings,
    )
    assert out["stage3_proposed"] >= 1
    assert out["surfaced"] == 0  # shadow mode -> recorded, not surfaced

    s = sf()
    snap = s.execute(sa.select(DecisionSnapshot)).scalars().first()
    assert snap is not None
    assert snap.ticker == "NVDA"
    assert json.loads(snap.decision_json)["action"] == "sell"
    assert snap.policy_version.startswith("pol-")
    # Proposal stamped with funnel lifecycle columns.
    p = s.execute(sa.select(Proposal)).scalars().first()
    assert p.source == "decision_funnel"
    assert p.shadow == 1
    assert p.funnel_run_id == snap.run_id
    assert p.expires_at is not None
    # Surface row is 'hidden' under shadow.
    surf = s.execute(
        sa.select(FunnelStageRow).where(FunnelStageRow.stage == "surface")
    ).scalars().all()
    assert surf and all(r.decision == "hidden" for r in surf)
    s.close()


@pytest.mark.asyncio
async def test_stage3_surfaces_when_not_shadow(sf):
    settings = SimpleNamespace(decision_funnel_shadow=False, decision_funnel_stage3=True)

    async def _deep(*, user_id, ticker, funnel_meta=None, **kwargs):
        s = sf()
        fm = funnel_meta or {}
        p = Proposal(
            user_id=user_id, ticker=ticker, action="sell", tier="T2",
            status="awaiting_human", source=fm.get("source", "manual"),
            shadow=int(fm.get("shadow", 0)), expires_at=fm.get("expires_at"),
            funnel_run_id=fm.get("funnel_run_id"),
        )
        s.add(p)
        s.commit()
        pid = p.id
        s.close()
        return DeepDecisionOutcome(ticker=ticker, status="approved", proposal_id=pid, action="sell")

    out = await run_funnel(
        "ariel", now=NOW, session_factory=sf, triage_fn=_triage_go,
        deep_decision_fn=_deep, settings=settings,
    )
    assert out["surfaced"] >= 1
    s = sf()
    p = s.execute(sa.select(Proposal)).scalars().first()
    assert p.shadow == 0  # non-shadow + north-star aligned -> client-visible
    s.close()
