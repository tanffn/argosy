from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus,
    EnrichedCandidate,
    HistoryFeatures,
)
from argosy.services.deployment_funnel.sizer import size_deployment


def _hf():
    return HistoryFeatures(
        last_price=100.0, ath=100.0, pct_below_ath=0.0,
        zscore_vs_window=0.0, drawdown_pct=0.0,
    )


def _ec(symbol, usd, status, cap_pct=None):
    cand = AllocationCandidate(
        kind="BUY",
        legs=(
            AllocationLeg(
                side="BUY", symbol=symbol, account_id="leumi",
                currency="USD", notional_usd=usd, funding_source="cash",
            ),
        ),
        horizon="now",
    )
    return EnrichedCandidate(
        candidate=cand, symbol=symbol, effective_nvda_usd=0.0,
        news_sentiment=None, history=_hf(), status=status, reason="",
        cap_pct=cap_pct,
    )


def test_approve_full_cap_fractional_veto_zero():
    enriched = [
        _ec("SGLD", 45000.0, CandidateStatus.APPROVE),
        _ec("CSPX", 20000.0, CandidateStatus.CAP_AT_PCT, cap_pct=25.0),
        _ec("IB01", 10000.0, CandidateStatus.VETO),
        _ec("XYZ", 8000.0, CandidateStatus.REQUIRES_PLAN_CHANGE),
    ]
    plan = size_deployment(enriched, deployable_usd=95000.0)
    by = {l.symbol: l.final_usd for l in plan.lines}
    assert by["SGLD"] == 45000.0
    assert by["CSPX"] == 5000.0            # 25% of 20k
    assert "IB01" not in by and "XYZ" not in by
    assert plan.deployed_usd == 50000.0
    assert plan.undeployed_remainder_usd == 45000.0


def test_never_exceeds_deployable_waterfill():
    enriched = [
        _ec("A", 80000.0, CandidateStatus.APPROVE),
        _ec("B", 80000.0, CandidateStatus.APPROVE),
    ]
    plan = size_deployment(enriched, deployable_usd=100000.0)
    assert plan.deployed_usd <= 100000.0 + 0.01
    assert sum(l.final_usd for l in plan.lines) <= 100000.0 + 0.01


def test_min_ticket_drops_dust():
    enriched = [
        _ec("A", 50000.0, CandidateStatus.APPROVE),
        _ec("DUST", 100.0, CandidateStatus.APPROVE),   # below $500 min
    ]
    plan = size_deployment(enriched, deployable_usd=95000.0)
    syms = {l.symbol for l in plan.lines}
    assert "A" in syms and "DUST" not in syms


def test_move_to_reserve_parks_not_buys():
    enriched = [
        _ec("SGLD", 45000.0, CandidateStatus.APPROVE),
        _ec("CSPX", 20000.0, CandidateStatus.MOVE_TO_RESERVE),
    ]
    plan = size_deployment(enriched, deployable_usd=95000.0)
    syms = {l.symbol for l in plan.lines}
    assert "CSPX" not in syms
    assert plan.reserve_parked_usd == 20000.0
