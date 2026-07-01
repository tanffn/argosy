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


def test_disabled_flag_is_respected(monkeypatch):
    from argosy.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_FUNNEL_ENABLED", "0")
    assert get_settings().deployment_funnel_enabled is False
    get_settings.cache_clear()
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_FUNNEL_ENABLED", "1")
    assert get_settings().deployment_funnel_enabled is True
    get_settings.cache_clear()
