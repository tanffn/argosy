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


def test_loader_default_floor_routes_medium_and_high_buys(sf):
    # Default floor is MEDIUM (proposal 68, Ariel-approved 2026-07-10): the
    # HIGH-only default routed ZERO names in 440 lifetime scans. MEDIUM+HIGH
    # BUYs route; non-BUYs still drop.
    s = sf()
    _seed_pick(s, "ASML", "HIGH", "BUY")
    _seed_pick(s, "MELI", "MED", "BUY")
    _seed_pick(s, "SHOP", "HIGH", "WATCH")  # not a BUY
    s.commit()
    cands = load_discovery_candidates(s, user_id="ariel", held_tickers=set())
    s.close()
    assert {c.subject for c in cands} == {"ASML", "MELI"}
    c = next(c for c in cands if c.subject == "ASML")
    assert c.subject_type == "discovery"
    assert c.primary_signal == "discovery_pick"
    assert c.extra["conviction"] == "HIGH"
    assert c.extra["verdict"] == "BUY"
    assert "10-K" in c.extra["grader_cites"]


def test_explicit_high_floor_routes_only_high(sf):
    # The floor stays IPS/policy-owned — an explicit HIGH floor restores the
    # strict behavior.
    from dataclasses import replace

    from argosy.services.decision_funnel.policy import DEFAULT_POLICY

    s = sf()
    _seed_pick(s, "ASML", "HIGH", "BUY")
    _seed_pick(s, "MELI", "MED", "BUY")
    s.commit()
    policy = replace(DEFAULT_POLICY, discovery_conviction_floor="HIGH")
    cands = load_discovery_candidates(
        s, user_id="ariel", held_tickers=set(), policy=policy
    )
    s.close()
    assert [c.subject for c in cands] == ["ASML"]


def test_lowering_floor_to_medium_routes_med_and_high(sf):
    # The conviction floor is a real FLOOR (>=), IPS/policy-owned: lowering it to
    # MEDIUM routes MEDIUM + HIGH BUYs (the grader's MED picks are no longer
    # silently vetoed), while LOW and non-BUYs still drop.
    from dataclasses import replace

    from argosy.services.decision_funnel.policy import DEFAULT_POLICY

    s = sf()
    _seed_pick(s, "ASML", "HIGH", "BUY")
    _seed_pick(s, "MELI", "MED", "BUY")
    _seed_pick(s, "TEVA", "LOW", "BUY")     # below a MEDIUM floor -> drop
    _seed_pick(s, "SHOP", "HIGH", "WATCH")  # not a BUY -> drop
    s.commit()
    policy = replace(DEFAULT_POLICY, discovery_conviction_floor="MEDIUM")
    cands = load_discovery_candidates(
        s, user_id="ariel", held_tickers=set(), policy=policy
    )
    s.close()
    assert sorted(c.subject for c in cands) == ["ASML", "MELI"]


def test_loader_skips_held_names(sf):
    s = sf()
    _seed_pick(s, "NVDA", "HIGH", "BUY")  # already held
    s.commit()
    cands = load_discovery_candidates(s, user_id="ariel", held_tickers={"NVDA"})
    s.close()
    assert cands == []


def _seed_signal_pick(s, ticker: str, *, mixed_radar: bool) -> None:
    families = (
        "SIGNAL_STREAM:gov_contracts,GROWTH"
        if mixed_radar
        else "SIGNAL_STREAM:gov_contracts"
    )
    s.add(
        ScanState(
            user_id="ariel",
            ticker=ticker,
            status="active",
            radar_fingerprint=f"s=90|f={families}|l=high",
            fleet_json=_pick_to_json(
                FleetPick(
                    ticker=ticker,
                    conviction="HIGH",
                    thesis_md="t",
                    verdict="BUY",
                    cites=("10-K",),
                )
            ),
            nomination_evidence_json=json.dumps(
                {
                    "stream": "gov_contracts",
                    "dedup_key": f"award:{ticker}",
                    "evidence": {"award_url": f"https://example.test/{ticker}"},
                }
            ),
        )
    )


def _killed_scorecard() -> dict:
    return {
        "source": "signal_stream:gov_contracts",
        "scored_outcomes": 100,
        "win_rate": 0.40,
        "avg_pnl_pct": -0.02,
        "observation_days": 200,
        "calibration": "calibrated",
        "horizons": {
            "30d": {
                "scored_outcomes": 100,
                "win_rate": 0.45,
                "avg_pnl_pct": -0.01,
            },
            "180d": {
                "scored_outcomes": 50,
                "win_rate": 0.40,
                "avg_pnl_pct": -0.03,
                "always_long_same_tickers_win_rate": 0.40,
            },
        },
        "funnel_context_enabled": False,
        "kill_reason": (
            "180d stream win rate 40.0% does not beat always-long "
            "same-tickers benchmark 40.0% (n=50)"
        ),
    }


def test_loader_suppresses_candidate_sourced_only_from_killed_signal(sf, monkeypatch):
    import argosy.services.decision_funnel.discovery_candidates as dc

    monkeypatch.setattr(
        dc,
        "signal_source_scorecard",
        lambda session, user_id, stream: _killed_scorecard(),
    )
    s = sf()
    _seed_signal_pick(s, "KILL", mixed_radar=False)
    s.commit()

    candidates = dc.load_discovery_candidates(
        s, user_id="ariel", held_tickers=set()
    )

    assert candidates == []
    assert s.query(ScanState).filter_by(ticker="KILL").one().status == "active"
    s.close()


def test_loader_keeps_mixed_candidate_but_omits_killed_signal_context(sf, monkeypatch):
    import argosy.services.decision_funnel.discovery_candidates as dc

    monkeypatch.setattr(
        dc,
        "signal_source_scorecard",
        lambda session, user_id, stream: _killed_scorecard(),
    )
    s = sf()
    _seed_signal_pick(s, "MIXED", mixed_radar=True)
    s.commit()

    candidates = dc.load_discovery_candidates(
        s, user_id="ariel", held_tickers=set()
    )

    assert [candidate.subject for candidate in candidates] == ["MIXED"]
    extra = candidates[0].extra
    assert "signal_stream" not in extra
    assert "signal_nomination" not in extra
    assert "signal_scorecard" not in extra
    assert "grader_cites" not in extra
    assert "10-K" not in json.dumps(extra)
    assert extra["conviction"] == "HIGH"
    s.close()


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
    # Funding gate (step 8 v0) recorded a 'funding' row for the approved BUY,
    # honestly labelled nominal-snapshot cash (no fabricated settlement).
    fund = s.execute(
        sa.select(FunnelStageRow).where(
            FunnelStageRow.stage == "funding", FunnelStageRow.subject == "ASML"
        )
    ).scalars().all()
    assert fund, "expected a funding row for the discovery BUY"
    assert "nominal_snapshot" in (fund[0].inputs_json or "")
    s.close()
