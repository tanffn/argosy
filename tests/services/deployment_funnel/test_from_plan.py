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


def test_unmapped_held_fund_surfaces_baseline_undercount_note():
    # A held symbol with no look-through entry (codex): the current-NVDA
    # baseline may be under-counted, so the note must flag it.
    holdings = {"NVDA": 2_296_000.0, "ZZZFUND": 60_000.0}
    res = run_preflight_for_plan(
        _plan(), doc=_doc(), holdings_usd=holdings, cash_usd=145_000.0,
        deployable_usd=95_000.0, snapshot_prices={"CSPX": 100.0, "IB01": 100.0})
    assert any("UNDER-counted" in n and "ZZZFUND" in n for n in res.notes)


def test_rerank_plan_drops_vetoed_resizes_capped():
    """Non-shadow re-rank: vetoed lines gone, capped resized, remainder grows."""
    from types import SimpleNamespace
    from argosy.services.deployment_funnel.from_plan import rerank_plan
    from argosy.services.deployment_funnel.sizer import SizedLine, SizedPlan

    est = SimpleNamespace(status="estate_safe", domicile="IE", note="")
    line = lambda sym, amt: SimpleNamespace(
        symbol=sym, amount_usd=amt, estate=est, type="ETF")
    # replace() needs a real frozen dataclass; use the actual DeploymentLine.
    from argosy.services.deployment_advisor import DeploymentLine, DeploymentTier, DeploymentPlan
    from argosy.services.target_allocation_doc import validate_instrument_domicile  # noqa: F401
    from datetime import date

    def dl(sym, amt):
        return DeploymentLine(
            symbol=sym, type="ETF", amount_usd=amt, timing="now", is_new=True,
            tier="core", horizon="10yr+",
            estate=SimpleNamespace(status="estate_safe", domicile="IE", note=""),
            cap_note="", net_of_tax_caveat="", rationale="", cites=(),
        )
    plan = DeploymentPlan(
        deploy_amount_usd=100000.0, as_of=date(2026, 7, 1),
        tiers=(DeploymentTier(name="core", cap_pct=70.0,
                              lines=(dl("CSPX", 22910.0), dl("EXUS", 2846.0))),),
        us_situs_exposed_usd=0.0, us_situs_sanctioned_usd=0.0,
        undeployed_remainder_usd=74244.0, market_context_age=None, caveats=(),
    )
    # Sizer kept only EXUS (CSPX vetoed -> not in sized lines).
    sized = SizedPlan(
        lines=(SizedLine(symbol="EXUS", final_usd=2846.0, status="approve_candidate", reason=""),),
        deployed_usd=2846.0, undeployed_remainder_usd=97154.0, reserve_parked_usd=0.0)
    out = rerank_plan(plan, sized)
    syms = [l.symbol for t in out.tiers for l in t.lines]
    assert syms == ["EXUS"]                      # CSPX dropped
    assert out.deployed_total_usd == 2846.0
    assert out.undeployed_remainder_usd == round(100000.0 - 2846.0, 2)
    assert any("held back" in c for c in out.caveats)
