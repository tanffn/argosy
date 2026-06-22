"""Tests for the funnel trace recorder + proposal expiry + view builders (P0)."""
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services import funnel_trace as ft
from argosy.services import funnel_view as fv
from argosy.services.proposal_expiry import default_expiry, expire_stale_proposals
from argosy.state.models import (
    Base,
    DecisionSnapshot,
    FunnelStageRow,
    Proposal,
    ProposalHistory,
    User,
)

NOW = datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc)
DAY = "2026-06-22"


@pytest.fixture
def session():
    eng = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _portfolio():
    return {"nvda_pct": 62.5, "cash_usd": 100000, "fx_usd_nis": 3.7}


def _market():
    return {"vix": 18.2, "spx_pct": -0.4, "as_of": "2026-06-22T17:00:00Z"}


# ---- run lifecycle ----


def test_open_run_is_idempotent(session):
    r1 = ft.open_run(
        session, user_id="ariel", day=DAY, trigger="scheduler", shadow=True,
        policy_version="pol-1", ips_version="ips-abc", plan_version_id=62,
        started_at=NOW,
    )
    r2 = ft.open_run(
        session, user_id="ariel", day=DAY, trigger="scheduler", shadow=True,
        policy_version="pol-1", ips_version="ips-abc", plan_version_id=62,
        started_at=NOW,
    )
    assert r1.id == r2.id  # same (user, day, trigger) -> same run
    assert r1.shadow == 1


def test_close_run_records_totals_and_macro(session):
    run = ft.open_run(
        session, user_id="ariel", day=DAY, shadow=True, policy_version="p",
        ips_version="i", plan_version_id=1, started_at=NOW,
    )
    ft.close_run(
        session, run_id=run.id, status="ok",
        totals={"stage1_in": 50, "stage3_out": 1},
        macro_read={"summary": "semis -3%, risk-off"},
        finished_at=NOW + timedelta(minutes=4),
    )
    session.refresh(run)
    assert run.status == "ok"
    assert fv.run_summary(run)["totals"]["stage3_out"] == 1


# ---- per-name stage audit ----


def test_record_stage_row_appends(session):
    run = ft.open_run(
        session, user_id="ariel", day=DAY, shadow=True, policy_version="p",
        ips_version="i", plan_version_id=1, started_at=NOW,
    )
    ft.record_stage_row(
        session, run_id=run.id, stage="stage1", subject="NVDA",
        subject_type="holding", decision="routed", reason="thesis-break hard trigger",
        signal_or_rule="thesis_break",
    )
    ft.record_stage_row(
        session, run_id=run.id, stage="stage1", subject="VWCE",
        subject_type="holding", decision="dropped", reason="no material signal",
        signal_or_rule="no_match",
    )
    rows = session.execute(
        sa.select(FunnelStageRow).where(FunnelStageRow.run_id == run.id)
    ).scalars().all()
    assert len(rows) == 2
    assert {r.decision for r in rows} == {"routed", "dropped"}


# ---- immutable snapshot dedup ----


def test_record_snapshot_dedup_identical(session):
    run = ft.open_run(
        session, user_id="ariel", day=DAY, shadow=True, policy_version="p",
        ips_version="i", plan_version_id=1, started_at=NOW,
    )
    common = dict(
        run_id=run.id, user_id="ariel", ticker="NVDA", day=DAY,
        decision={"action": "sell", "shares": 100, "why": "trim to cap"},
        portfolio_snapshot=_portfolio(), market_snapshot=_market(),
        policy_version="pol-1", policy={"nvda_cap": 13.0},
        model_name="claude-opus-4-8", prompt_template_hash="abc123",
    )
    s1 = ft.record_snapshot(session, **common)
    s2 = ft.record_snapshot(session, **common)
    assert s1.id == s2.id  # identical inputs dedupe
    count = session.execute(
        sa.select(sa.func.count()).select_from(DecisionSnapshot)
    ).scalar_one()
    assert count == 1


def test_record_snapshot_new_row_when_input_changes(session):
    run = ft.open_run(
        session, user_id="ariel", day=DAY, shadow=True, policy_version="p",
        ips_version="i", plan_version_id=1, started_at=NOW,
    )
    base = dict(
        run_id=run.id, user_id="ariel", ticker="NVDA", day=DAY,
        decision={"action": "sell", "shares": 100},
        portfolio_snapshot=_portfolio(), market_snapshot=_market(),
        policy_version="pol-1", policy={"nvda_cap": 13.0},
        model_name="claude-opus-4-8", prompt_template_hash="abc123",
    )
    s1 = ft.record_snapshot(session, **base)
    # Market moved -> different fingerprint -> a NEW immutable row, not a loss.
    changed = {**base, "market_snapshot": {**_market(), "vix": 30.0}}
    s2 = ft.record_snapshot(session, **changed)
    assert s1.id != s2.id
    count = session.execute(
        sa.select(sa.func.count()).select_from(DecisionSnapshot)
    ).scalar_one()
    assert count == 2


# ---- proposal expiry ----


def _mk_proposal(session, *, status, expires_at):
    p = Proposal(
        user_id="ariel", ticker="NVDA", action="sell", tier="T2",
        status=status, source="decision_funnel", shadow=0, expires_at=expires_at,
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def test_expire_stale_proposals(session):
    stale = _mk_proposal(session, status="awaiting_human", expires_at=NOW - timedelta(hours=1))
    fresh = _mk_proposal(session, status="awaiting_human", expires_at=NOW + timedelta(hours=10))
    # Attach an immutable snapshot to the stale proposal.
    run = ft.open_run(
        session, user_id="ariel", day=DAY, shadow=True, policy_version="p",
        ips_version="i", plan_version_id=1, started_at=NOW,
    )
    snap = ft.record_snapshot(
        session, run_id=run.id, user_id="ariel", ticker="NVDA", day=DAY,
        decision={"action": "sell"}, portfolio_snapshot=_portfolio(),
        market_snapshot=_market(), policy_version="pol-1", policy={},
        model_name="m", prompt_template_hash="h", proposal_id=stale.id,
    )

    expired = expire_stale_proposals(session, user_id="ariel", now=NOW)
    assert expired == [stale.id]
    session.refresh(stale)
    session.refresh(fresh)
    session.refresh(snap)
    assert stale.status == "expired"
    assert fresh.status == "awaiting_human"
    assert snap.human_action_state == "expired"
    # History row recorded.
    hist = session.execute(
        sa.select(ProposalHistory).where(ProposalHistory.proposal_id == stale.id)
    ).scalars().all()
    assert any(h.status == "expired" and h.transitioned_by == "proposal_expiry" for h in hist)


def test_default_expiry_ttl():
    exp = default_expiry(NOW, ttl_hours=72)
    assert exp == NOW + timedelta(hours=72)


# ---- client narrative ----


def test_build_client_narrative(session):
    run = ft.open_run(
        session, user_id="ariel", day=DAY, shadow=True, policy_version="p",
        ips_version="i", plan_version_id=1, started_at=NOW,
    )
    ft.close_run(
        session, run_id=run.id, status="ok",
        macro_read={"summary": "semis -3%, risk-off"}, finished_at=NOW,
    )
    ft.record_stage_row(session, run_id=run.id, stage="stage1", subject="NVDA",
                        subject_type="holding", decision="routed", reason="x")
    ft.record_stage_row(session, run_id=run.id, stage="stage3", subject="NVDA",
                        subject_type="holding", decision="proposed", reason="trim",
                        inputs={"action": "trim"}, proposal_id=5)
    ft.record_stage_row(session, run_id=run.id, stage="stage1", subject="VWCE",
                        subject_type="holding", decision="dropped", reason="no signal")
    session.refresh(run)
    rows = session.execute(
        sa.select(FunnelStageRow).where(FunnelStageRow.run_id == run.id)
        .order_by(FunnelStageRow.id)
    ).scalars().all()
    narrative = fv.build_client_narrative(run, list(rows))
    assert "Scanned the market" in narrative["headline"]
    assert "NVDA" in narrative["headline"]
    assert narrative["counts"]["proposed"] == 1
    assert narrative["counts"]["routed"] == 1
