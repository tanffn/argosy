"""build_gate_inputs + plan_to_candidates + run_preflight_for_plan glue."""
from types import SimpleNamespace

from argosy.services.deployment_funnel.contracts import CandidateStatus
from argosy.services.deployment_funnel.from_plan import (
    build_gate_inputs,
    plan_to_candidates,
    run_preflight_for_plan,
)


def _doc():
    return SimpleNamespace(
        nvda_cap_pct=13.0,
        classes=[
            SimpleNamespace(
                label="US broad-market core", target_pct=31.5,
                instruments=[SimpleNamespace(symbol="CSPX")]),
            SimpleNamespace(
                label="Cash & T-bills (incl. ILS tranche)", target_pct=6.14,
                instruments=[SimpleNamespace(symbol="IB01")]),
            SimpleNamespace(
                label="Strategic single-stock (NVDA)", target_pct=12.0,
                instruments=[SimpleNamespace(symbol="NVDA")]),
        ],
    )


def _plan():
    line = lambda sym, amt: SimpleNamespace(symbol=sym, amount_usd=amt, rationale="x")
    tier = lambda name, lines: SimpleNamespace(name=name, lines=lines)
    return SimpleNamespace(tiers=[
        tier("core", [line("CSPX", 22910.0), line("IB01", 23616.0)]),
    ])


class _Provider:
    def quote(self, s): return 100.0
    def history_high(self, s): return 100.0
    def zscore(self, s): return 0.5


def test_build_gate_inputs_from_doc_and_holdings():
    # Book: NVDA $2.296M + CSPX $157k + SGOV $127k + cash $145k.
    holdings = {"NVDA": 2_296_000.0, "CSPX": 156_820.0, "SGOV": 127_040.0}
    gi = build_gate_inputs(doc=_doc(), holdings_usd=holdings, cash_usd=145_000.0)
    assert gi.book_usd == round(2_296_000 + 156_820 + 127_040 + 145_000, 2)
    # Effective NVDA = full NVDA + 7% of CSPX.
    assert gi.current_effective_nvda_usd == round(2_296_000 + 0.07 * 156_820, 2)
    # Reserve target 6.14% of book vs $127k SGOV + $145k cash => funded (0).
    assert gi.reserve_shortfall_usd == 0.0
    assert "Cash & T-bills (incl. ILS tranche)" in gi.plan_classes


def test_plan_to_candidates_flattens_tiers():
    cands = plan_to_candidates(_plan())
    assert [c.legs[0].symbol for c in cands] == ["CSPX", "IB01"]
    assert cands[0].total_notional_usd == 22910.0


def test_run_preflight_for_plan_flags_lookthrough_and_reserve(monkeypatch):
    # Force the provider to our stub (no network) via snapshot prices.
    holdings = {"NVDA": 2_296_000.0, "CSPX": 156_820.0, "SGOV": 127_040.0}
    res = run_preflight_for_plan(
        _plan(), doc=_doc(), holdings_usd=holdings, cash_usd=145_000.0,
        deployable_usd=95_000.0,
        snapshot_prices={"CSPX": 100.0, "IB01": 100.0},
    )
    by = {e.symbol: e.status for e in res.enriched}
    # NVDA already ~57% >> 13% cap => any CSPX look-through NVDA is vetoed.
    assert by["CSPX"] is CandidateStatus.VETO
    # Reserve funded => IB01 vetoed.
    assert by["IB01"] is CandidateStatus.VETO
