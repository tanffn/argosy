from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus,
    HistoryFeatures,
)
from argosy.services.deployment_funnel.gates import GateInputs, classify_candidate


def _cand(symbol, usd):
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


def _hf(stale=False):
    return HistoryFeatures(
        last_price=100.0, ath=100.0, pct_below_ath=0.0,
        zscore_vs_window=0.5, drawdown_pct=0.0, stale=stale,
    )


_GI = GateInputs(
    current_effective_nvda_usd=2_296_000.0, book_usd=4_060_000.0,
    nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
    plan_classes=frozenset({"US broad-market core", "Cash & T-bills"}),
    class_of={
        "CSPX": "US broad-market core", "IB01": "Cash & T-bills",
        "SGLD": "gold",
    },
)


def test_low_nvda_index_buy_DILUTES_and_is_approved():
    # CSPX is ~7% NVDA vs a 56.6% book — buying it LOWERS the NVDA share, so it
    # must be APPROVED even though the book is over the cap (the old logic wrongly
    # vetoed it for adding any absolute NVDA, stranding the cash).
    st, reason, _ = classify_candidate(
        _cand("CSPX", 22000.0), "CSPX", _hf(), "neutral", _GI
    )
    assert st is CandidateStatus.APPROVE
    assert "DILUTES" in reason


def test_buy_that_RAISES_nvda_share_is_vetoed():
    # A direct NVDA buy (100% NVDA >> 56.6% book) raises the share → veto.
    st, reason, _ = classify_candidate(
        _cand("NVDA", 22000.0), "NVDA", _hf(), None, _GI
    )
    assert st is CandidateStatus.VETO
    assert "RAISES" in reason


def test_tbill_when_reserve_funded_is_vetoed():
    st, reason, _ = classify_candidate(
        _cand("IB01", 3000.0), "IB01", _hf(), None, _GI
    )
    assert st is CandidateStatus.VETO
    assert "reserve" in reason.lower()


def test_missing_plan_class_requires_plan_change():
    st, reason, _ = classify_candidate(
        _cand("SGLD", 45000.0), "SGLD", _hf(), None, _GI
    )
    assert st is CandidateStatus.REQUIRES_PLAN_CHANGE
    assert "plan" in reason.lower()


def test_stale_quote_does_NOT_gate_a_price_independent_verdict():
    # A missing price must NOT force defer: the plan-gap/reserve/cap verdict
    # needs no price. SGLD has no plan class here -> requires_plan_change,
    # regardless of the stale quote (not a defer artifact).
    st, reason, _ = classify_candidate(
        _cand("SGLD", 45000.0), "SGLD", _hf(stale=True), None, _GI
    )
    assert st is CandidateStatus.REQUIRES_PLAN_CHANGE


def test_stale_quote_still_approves_a_clean_line():
    # A plan-member line with no objection approves even with a stale quote
    # (price isn't needed to buy toward a plan target within caps).
    gi = GateInputs(
        current_effective_nvda_usd=0.0, book_usd=1_000_000.0, nvda_cap_pct=13.0,
        reserve_shortfall_usd=0.0, plan_classes=frozenset({"International developed (ex-US)"}),
        class_of={"EXUS": "International developed (ex-US)"},
    )
    st, _, _ = classify_candidate(
        _cand("EXUS", 5000.0), "EXUS", _hf(stale=True), None, gi
    )
    assert st is CandidateStatus.APPROVE


def test_ath_alone_does_not_veto():
    gi = GateInputs(
        current_effective_nvda_usd=2_296_000.0, book_usd=4_060_000.0,
        nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
        plan_classes=frozenset({"gold"}), class_of={"SGLD": "gold"},
    )
    at_ath = HistoryFeatures(
        last_price=372.0, ath=372.0, pct_below_ath=0.0,
        zscore_vs_window=2.5, drawdown_pct=0.0,
    )
    st, _, _ = classify_candidate(_cand("SGLD", 45000.0), "SGLD", at_ath, None, gi)
    assert st is CandidateStatus.APPROVE
