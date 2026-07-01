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


# _GI's book is 2.296M / 4.06M ≈ 56.6% NVDA — far over the 13% plan cap. The gate
# reconciles a buy against the plan's SINGLE-NAME cap density: an instrument no
# denser than the cap (CSPX ~7%, FUSA ~6%) is a plan-compliant diversified buy and
# APPROVES even while the book is over the cap (deconcentration is a SELL decision,
# not a reason to strand cash). Only an instrument DENSER than the cap (R1GR ~14%,
# direct NVDA 100%), added while the book is over the cap, is a genuine judgment
# that ROUTES to the fleet.

def test_below_cap_density_index_approved_even_when_book_over_cap():
    # CSPX ~7% NVDA <= the 13% cap: buying it can't worsen single-name density past
    # the cap, and it's what the plan targets -> APPROVE (do NOT strand the cash).
    st, reason, cap = classify_candidate(
        _cand("CSPX", 22000.0), "CSPX", _hf(), "neutral", _GI
    )
    assert st is CandidateStatus.APPROVE
    assert cap is None
    assert "within the 13% single-name cap" in reason


def test_direct_nvda_over_cap_routes_to_fleet():
    # Direct NVDA (100% >> 13% cap) while the book is over the cap — DENSER than the
    # cap; the engine doesn't veto by hand, the fleet decides (sell? reserve? add?).
    st, _, _ = classify_candidate(
        _cand("NVDA", 22000.0), "NVDA", _hf(), None, _GI
    )
    assert st is CandidateStatus.NEEDS_FLEET_REVIEW


def test_r1gr_denser_than_cap_over_cap_routes_to_fleet():
    # R1GR ~14% NVDA > 13% cap, book over cap -> a >cap-dense add is routed to the
    # fleet (the live fleet run VETO'd it).
    st, _, _ = classify_candidate(
        _cand("R1GR", 13000.0), "R1GR", _hf(), None, _GI
    )
    assert st is CandidateStatus.NEEDS_FLEET_REVIEW


# ---------------------------------------------------------------------------
# Pure-arithmetic reconcile: a >cap-DENSE instrument while the book still has
# headroom under the cap. Derives entirely from the plan's own cap number.
# ---------------------------------------------------------------------------

# Book 200k NVDA / 2.0M = 10% < 13% cap -> headroom exists.
_GI_HEADROOM = GateInputs(
    current_effective_nvda_usd=200_000.0, book_usd=2_000_000.0,
    nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
    plan_classes=frozenset({"US broad-market core", "US growth tilt (ex-NVDA)"}),
    class_of={"CSPX": "US broad-market core", "R1GR": "US growth tilt (ex-NVDA)"},
)


def test_below_cap_density_buy_approved_with_headroom():
    # CSPX ~7% <= 13% cap -> plan-compliant APPROVE regardless of book headroom.
    st, reason, cap = classify_candidate(
        _cand("CSPX", 10_000.0), "CSPX", _hf(), None, _GI_HEADROOM
    )
    assert st is CandidateStatus.APPROVE
    assert cap is None
    assert "single-name cap" in reason


def test_dense_instrument_capped_to_the_plan_cap_with_headroom():
    # R1GR ~14% NVDA is DENSER than the 13% cap. With the book under the cap, a
    # large R1GR buy is CAP_AT_PCT to the slice that lands the resulting book at the
    # cap: headroom cap*B - C = 0.13*2M - 200k = 60k NVDA; at 14% density that is
    # ~428.6k of R1GR notional, so a 2M buy caps to ~21%. A partial cap, not a veto.
    st, reason, cap = classify_candidate(
        _cand("R1GR", 2_000_000.0), "R1GR", _hf(), None, _GI_HEADROOM
    )
    assert st is CandidateStatus.CAP_AT_PCT
    assert cap is not None and 20.0 < cap < 22.0
    assert "cap" in reason


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
