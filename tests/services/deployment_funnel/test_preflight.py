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
    assert by["CSPX"] in (CandidateStatus.VETO, CandidateStatus.CAP_AT_PCT)
    assert by["IB01"] is CandidateStatus.VETO           # reserve funded
    assert by["SGLD"] is CandidateStatus.REQUIRES_PLAN_CHANGE
    assert any(g.asset_class == "gold" for g in res.plan_gaps)
    # Dollar conservation: kept (approved/capped) never exceeds deployable.
    assert res.kept_total_usd <= res.deployable_usd


def test_aggregate_headroom_is_consumed_across_candidates():
    # codex H1: two sub-headroom NVDA buys must NOT both approve past the cap.
    # Cap 13% of $1,000,000 = $130,000; current $129,000 -> $1,000 headroom.
    # Two NVDA buys of $600 each add $1,200 > headroom; the second must be
    # capped/vetoed once the first consumes most of the headroom.
    gi = GateInputs(
        current_effective_nvda_usd=129_000.0, book_usd=1_000_000.0,
        nvda_cap_pct=13.0, reserve_shortfall_usd=0.0,
        plan_classes=frozenset({"Strategic single-stock (NVDA)"}),
        class_of={"NVDA": "Strategic single-stock (NVDA)"},
    )
    cands = [_c("NVDA", 600.0), _c("NVDA", 600.0)]
    res = run_preflight(
        cands, symbol_of=lambda c: c.legs[0].symbol, gate_inputs=gi,
        provider=_Provider(), signals_by_symbol={}, deployable_usd=1200.0)
    kept_nvda = sum(
        e.effective_nvda_usd * (e.cap_pct / 100.0 if e.cap_pct is not None else 1.0)
        for e in res.enriched if e.status.value in ("approve_candidate", "cap_at_pct")
    )
    # Total kept NVDA must stay within the $1,000 headroom (floor rounding).
    assert 129_000.0 + kept_nvda <= 130_000.0 + 0.01


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
