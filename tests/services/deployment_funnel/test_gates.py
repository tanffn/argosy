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


# _GI's book is 2.296M / 4.06M ≈ 56.6% NVDA — far over the 13% plan cap. So the
# engine does NOT invent an approve/veto for ANY NVDA-bearing buy while over the
# cap; it ROUTES the judgment to the fleet (NEEDS_FLEET_REVIEW). Only zero-NVDA
# diversifiers deploy freely, and the pure-arithmetic reconcile applies only when
# the book has headroom under the cap (tested separately below).

def test_nvda_bearing_index_over_cap_routes_to_fleet():
    # CSPX carries ~7% NVDA look-through. With the book already over the plan cap,
    # whether to add ANY more NVDA-correlated exposure is a risk-officer judgment,
    # not a hand-coded rule -> route to the fleet.
    st, reason, cap = classify_candidate(
        _cand("CSPX", 22000.0), "CSPX", _hf(), "neutral", _GI
    )
    assert st is CandidateStatus.NEEDS_FLEET_REVIEW
    assert cap is None
    assert "plan cap" in reason and "fleet" in reason


def test_direct_nvda_over_cap_routes_to_fleet():
    # Direct NVDA (100% NVDA) while the book is over the cap — the engine no longer
    # vetoes by hand; the fleet decides (sell to deconcentrate? reserve? add?).
    st, _, _ = classify_candidate(
        _cand("NVDA", 22000.0), "NVDA", _hf(), None, _GI
    )
    assert st is CandidateStatus.NEEDS_FLEET_REVIEW


def test_r1gr_over_cap_routes_to_fleet():
    # R1GR ~14% NVDA; book over cap -> routed to the fleet (was a hand-coded veto).
    st, _, _ = classify_candidate(
        _cand("R1GR", 13000.0), "R1GR", _hf(), None, _GI
    )
    assert st is CandidateStatus.NEEDS_FLEET_REVIEW


# ---------------------------------------------------------------------------
# Pure-arithmetic reconcile: ONLY when the book has headroom under the plan cap.
# Derives entirely from the plan's own cap number — no investment judgment.
# ---------------------------------------------------------------------------

# Book 200k NVDA / 2.0M = 10% < 13% cap -> headroom exists.
_GI_HEADROOM = GateInputs(
    current_effective_nvda_usd=200_000.0, book_usd=2_000_000.0,
    nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
    plan_classes=frozenset({"US broad-market core"}),
    class_of={"CSPX": "US broad-market core"},
)


def test_under_cap_small_nvda_buy_approved():
    # A small CSPX buy keeps the resulting book under the 13% cap -> APPROVE.
    st, reason, cap = classify_candidate(
        _cand("CSPX", 10_000.0), "CSPX", _hf(), None, _GI_HEADROOM
    )
    assert st is CandidateStatus.APPROVE
    assert cap is None
    assert "13% NVDA plan cap" in reason


def test_under_cap_large_nvda_buy_capped_to_the_plan_cap():
    # A very large CSPX buy would push the book past 13% -> CAP_AT_PCT to the slice
    # that lands the resulting book exactly at the plan cap. cap*B - C = 60k of
    # NVDA headroom; at ~7% instrument weight that's ~857k of CSPX notional, so a
    # 2M buy is capped to ~43%. Assert it is a partial cap, not a hand-coded veto.
    st, reason, cap = classify_candidate(
        _cand("CSPX", 2_000_000.0), "CSPX", _hf(), None, _GI_HEADROOM
    )
    assert st is CandidateStatus.CAP_AT_PCT
    assert cap is not None and 0.0 < cap < 100.0
    assert "plan cap" in reason


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
