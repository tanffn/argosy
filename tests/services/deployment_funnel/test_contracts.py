from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus,
    EnrichedCandidate,
    HistoryFeatures,
    PlanGap,
    PreflightResult,
)


def _cand(symbol="CSPX", usd=22000.0):
    return AllocationCandidate(
        kind="BUY",
        legs=(
            AllocationLeg(
                side="BUY", symbol=symbol, account_id="leumi",
                currency="USD", notional_usd=usd, funding_source="cash",
            ),
        ),
        horizon="now",
    )


def test_enriched_candidate_and_result_round_trip():
    hf = HistoryFeatures(
        last_price=368.0, ath=372.0, pct_below_ath=1.08,
        zscore_vs_window=1.9, drawdown_pct=1.08,
    )
    ec = EnrichedCandidate(
        candidate=_cand(), symbol="CSPX", effective_nvda_usd=1540.0,
        news_sentiment="neutral", history=hf,
        status=CandidateStatus.APPROVE, reason="fills US core",
    )
    assert ec.symbol == "CSPX"
    assert ec.status is CandidateStatus.APPROVE
    gap = PlanGap(
        asset_class="gold", current_target_pct=0.0, proposed_target_pct=None,
        reason_refs=("0% vs typical 3-5%",), blocked_amount_usd=45000.0,
    )
    res = PreflightResult(
        deployable_usd=95000.0, enriched=(ec,), plan_gaps=(gap,),
        kept_total_usd=22000.0,
    )
    assert res.plan_gaps[0].asset_class == "gold"
    assert res.kept_total_usd <= res.deployable_usd
