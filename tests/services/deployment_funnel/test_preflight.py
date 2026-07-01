from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import CandidateStatus
from argosy.services.deployment_funnel.gates import GateInputs
from argosy.services.deployment_funnel.preflight import run_preflight


class _Provider:
    def quote(self, s):
        return 100.0

    def history_high(self, s):
        return 100.0

    def zscore(self, s):
        return 0.5


def _c(symbol, usd):
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


def test_preflight_catches_the_three_failures():
    # The real failure book: 56.6% NVDA, reserve funded, gold not a plan class.
    gi = GateInputs(
        current_effective_nvda_usd=2_296_000.0, book_usd=4_060_000.0,
        nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
        plan_classes=frozenset({"US broad-market core", "Cash & T-bills"}),
        class_of={
            "CSPX": "US broad-market core", "IB01": "Cash & T-bills",
            "SGLD": "gold",
        },
    )
    cands = [_c("CSPX", 22910.0), _c("IB01", 23616.0), _c("SGLD", 45000.0)]
    res = run_preflight(
        cands, symbol_of=lambda c: c.legs[0].symbol, gate_inputs=gi,
        provider=_Provider(), signals_by_symbol={}, deployable_usd=95000.0,
    )
    by = {e.symbol: e.status for e in res.enriched}
    # CSPX (~7% NVDA) DILUTES the 56.6% book → approved (the corrected logic).
    assert by["CSPX"] is CandidateStatus.APPROVE
    assert by["IB01"] is CandidateStatus.VETO           # reserve funded
    assert by["SGLD"] is CandidateStatus.REQUIRES_PLAN_CHANGE  # gold not in plan
    # Dollar conservation: kept (approved/capped) never exceeds deployable.
    assert res.kept_total_usd <= res.deployable_usd


def test_aggregate_cap_enforced_across_batch():
    # codex H1, %-logic: NVDA buys must not push the book NVDA% over the cap
    # across the batch. Just under the cap (12.9% of $1M, cap 13%); the first
    # small NVDA buy stays within, the second (evaluated against the grown book
    # + grown NVDA) crosses the cap and is vetoed.
    gi = GateInputs(
        current_effective_nvda_usd=129_000.0, book_usd=1_000_000.0,
        nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
        plan_classes=frozenset({"Strategic single-stock (NVDA)"}),
        class_of={"NVDA": "Strategic single-stock (NVDA)"},
    )
    cands = [_c("NVDA", 600.0), _c("NVDA", 2000.0)]
    res = run_preflight(
        cands, symbol_of=lambda c: c.legs[0].symbol, gate_inputs=gi,
        provider=_Provider(), signals_by_symbol={}, deployable_usd=2600.0)
    # Fixed-book model (cash reallocated within the book): final book NVDA% is
    # (starting NVDA + kept NVDA) / FIXED book, and must not exceed the cap.
    kept_nvda = 129_000.0
    for e in res.enriched:
        if e.status.value in ("approve_candidate", "cap_at_pct"):
            frac = e.cap_pct / 100.0 if e.cap_pct is not None else 1.0
            kept_nvda += e.effective_nvda_usd * frac
    assert kept_nvda / 1_000_000.0 <= 0.13 + 1e-6


def test_unmapped_symbol_is_flagged_not_silently_trusted():
    # codex H2: a symbol with no look-through entry -> concentration unverified.
    gi = GateInputs(
        current_effective_nvda_usd=0.0, book_usd=1_000_000.0, nvda_cap_pct=13.0,
        reserve_shortfall_usd=0.0, plan_classes=frozenset({"x"}),
        class_of={},  # no class -> not a plan gap; symbol also not in map
    )
    res = run_preflight(
        [_c("WTAI", 1364.0)], symbol_of=lambda c: c.legs[0].symbol,
        gate_inputs=gi, provider=_Provider(), signals_by_symbol={},
        deployable_usd=1364.0)
    assert any("UNVERIFIED" in n and "WTAI" in n for n in res.notes)


def test_oversized_tbill_capped_to_reserve_shortfall():
    # codex M4: $10k T-bill buy but reserve only needs $1k -> capped ~10%.
    gi = GateInputs(
        current_effective_nvda_usd=0.0, book_usd=1_000_000.0, nvda_cap_pct=13.0,
        reserve_shortfall_usd=1000.0,
        plan_classes=frozenset({"Cash & T-bills"}),
        class_of={"IB01": "Cash & T-bills"},
    )
    res = run_preflight(
        [_c("IB01", 10000.0)], symbol_of=lambda c: c.legs[0].symbol,
        gate_inputs=gi, provider=_Provider(), signals_by_symbol={},
        deployable_usd=10000.0)
    e = res.enriched[0]
    assert e.status is CandidateStatus.CAP_AT_PCT
    assert e.cap_pct is not None and e.cap_pct <= 10.0  # floored, never over
    assert res.kept_total_usd <= 1000.0 + 0.01


def test_disabled_flag_is_respected(monkeypatch):
    from argosy.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_FUNNEL_ENABLED", "0")
    assert get_settings().deployment_funnel_enabled is False
    get_settings.cache_clear()
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_FUNNEL_ENABLED", "1")
    assert get_settings().deployment_funnel_enabled is True
    get_settings.cache_clear()
