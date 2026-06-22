"""Tests for discovery-driven candidates feeding the decision funnel (step 7).

Covers the loader (only HIGH-conviction BUY picks route, held names skipped,
dedupe, telemetry in ``extra``), the north-star classification of a discovery
BUY, and an end-to-end orchestrator pass where a seeded discovery pick reaches
Stage 3 and proposes a new-name BUY (shadow).
"""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from argosy.services.contracts import FleetPick
from argosy.services.decision_funnel.deep_decision import DeepDecisionOutcome
from argosy.services.decision_funnel.discovery_candidates import (
    load_discovery_candidates,
)
from argosy.services.decision_funnel.north_star import assess_alignment
from argosy.services.decision_funnel.orchestrator import run_funnel
from argosy.services.decision_funnel.triage import TriageOutcome
from argosy.services.high_potential_funnel import _pick_to_json
from argosy.state.models import (
    Base,
    DecisionSnapshot,
    FunnelStageRow,
    PortfolioSnapshotRow,
    Proposal,
    ScanState,
    User,
)

NOW = datetime(2026, 6, 22, 18, 30, tzinfo=UTC)


def _seed_pick(s, ticker, conviction, verdict, cites=("10-K",)):
    s.add(
        ScanState(
            user_id="ariel",
            ticker=ticker,
            status="active",
            fleet_json=_pick_to_json(
                FleetPick(
                    ticker=ticker, conviction=conviction, thesis_md="t",
                    verdict=verdict, cites=list(cites),
                )
            ),
        )
    )


@pytest.fixture
def sf():
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


# --- loader ----------------------------------------------------------------


def test_loader_routes_only_high_conviction_buys(sf):
    s = sf()
    _seed_pick(s, "ASML", "HIGH", "BUY")
    _seed_pick(s, "MELI", "MED", "BUY")    # conviction too low
    _seed_pick(s, "SHOP", "HIGH", "WATCH")  # not a BUY
    s.commit()
    cands = load_discovery_candidates(s, user_id="ariel", held_tickers=set())
    s.close()
    assert [c.subject for c in cands] == ["ASML"]
    c = cands[0]
    assert c.subject_type == "discovery"
    assert c.primary_signal == "discovery_pick"
    assert c.extra["conviction"] == "HIGH"
    assert c.extra["verdict"] == "BUY"
    assert "10-K" in c.extra["grader_cites"]


def test_loader_skips_held_names(sf):
    s = sf()
    _seed_pick(s, "NVDA", "HIGH", "BUY")  # already held
    s.commit()
    cands = load_discovery_candidates(s, user_id="ariel", held_tickers={"NVDA"})
    s.close()
    assert cands == []


# --- north star ------------------------------------------------------------


def test_discovery_buy_is_opportunity_aligned():
    v = assess_alignment(triggers=["discovery_pick"], action="buy", proposed=True)
    assert v.aligned is True
    assert "discovery_pick" in v.justification or "event-driven" in v.justification


# --- orchestrator integration ----------------------------------------------


def _triage_go(candidate, **kwargs):
    return TriageOutcome(
        subject=candidate.subject, warrants_decision=True, urgency="HIGH",
        rationale="material", model="claude-sonnet-4-6", prompt_hash="h",
        tokens_in=10, tokens_out=2, cost_usd=0.0,
    )


@pytest.mark.asyncio
async def test_discovery_pick_reaches_stage3_and_proposes_buy(sf):
    s = sf()
    _seed_pick(s, "ASML", "HIGH", "BUY")
    s.commit()
    s.close()
    settings = SimpleNamespace(decision_funnel_shadow=True, decision_funnel_stage3=True)

    async def _deep(*, user_id, ticker, funnel_meta=None, **kwargs):
        s2 = sf()
        fm = funnel_meta or {}
        p = Proposal(
            user_id=user_id, ticker=ticker, action="buy", tier="T2",
            status="awaiting_human", source=fm.get("source", "manual"),
            shadow=int(fm.get("shadow", 0)), expires_at=fm.get("expires_at"),
            funnel_run_id=fm.get("funnel_run_id"),
        )
        s2.add(p)
        s2.commit()
        pid = p.id
        s2.close()
        return DeepDecisionOutcome(
            ticker=ticker, status="approved", proposal_id=pid, action="buy",
        )

    out = await run_funnel(
        "ariel", now=NOW, session_factory=sf, triage_fn=_triage_go,
        deep_decision_fn=_deep, settings=settings,
    )
    assert out["stage3_proposed"] >= 1

    s = sf()
    # The discovery candidate was traced as a Stage-1 'discovery' row carrying
    # its conviction (telemetry: radar → proposal).
    s1 = s.execute(
        sa.select(FunnelStageRow).where(
            FunnelStageRow.stage == "stage1",
            FunnelStageRow.subject_type == "discovery",
        )
    ).scalars().all()
    assert s1 and s1[0].subject == "ASML"
    assert "HIGH" in (s1[0].inputs_json or "")
    # And it produced an immutable snapshot + a BUY proposal for the new name.
    snap = s.execute(
        sa.select(DecisionSnapshot).where(DecisionSnapshot.ticker == "ASML")
    ).scalars().first()
    assert snap is not None
    p = s.execute(
        sa.select(Proposal).where(Proposal.ticker == "ASML")
    ).scalars().first()
    assert p is not None and p.action == "buy" and p.source == "decision_funnel"
    s.close()
