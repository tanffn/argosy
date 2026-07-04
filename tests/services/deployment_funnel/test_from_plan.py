"""build_gate_inputs + plan_to_candidates + run_preflight_for_plan glue."""
from types import SimpleNamespace

from argosy.services.deployment_funnel.contracts import CandidateStatus
from argosy.services.deployment_funnel.from_plan import (
    SnapshotOrLiveProvider,
    build_gate_inputs,
    plan_to_candidates,
    run_preflight_for_plan,
)


class _StubQuote:
    def __init__(self, price):
        self.price = price


def test_live_quote_retries_ucits_exchange_suffix(monkeypatch):
    """Regression: Irish UCITS ETFs (FUSA, R1GR, SPMV, ...) are NOT quoted under
    their bare symbol on Yahoo — they need an exchange suffix like '.L'. The
    provider must try bare first (US listings), then the UCITS suffixes, instead
    of giving up on the bare miss (which zeroed/deferred every UCITS buy)."""
    calls: list[str] = []

    class _FakeAdapter:
        async def get_quote(self, ticker):
            calls.append(ticker)
            return _StubQuote(42.0) if ticker == "FUSA.L" else _StubQuote(None)

    # _live_quote imports YFinanceAdapter from the adapter module at call time.
    monkeypatch.setattr(
        "argosy.adapters.data.yfinance_adapter.YFinanceAdapter",
        lambda *a, **k: _FakeAdapter(),
    )
    prov = SnapshotOrLiveProvider()  # empty snapshot -> forces the live path
    price = prov.quote("FUSA")
    assert price == 42.0
    assert calls[0] == "FUSA", "must try the bare symbol first (US listings)"
    assert "FUSA.L" in calls, "must retry the LSE (.L) suffix for a UCITS miss"


def test_live_quote_prefers_bare_us_symbol(monkeypatch):
    """A US symbol resolves on its bare listing and must NOT be over-queried on
    foreign suffixes once found."""
    calls: list[str] = []

    class _FakeAdapter:
        async def get_quote(self, ticker):
            calls.append(ticker)
            return _StubQuote(308.0) if ticker == "AAPL" else _StubQuote(None)

    monkeypatch.setattr(
        "argosy.adapters.data.yfinance_adapter.YFinanceAdapter",
        lambda *a, **k: _FakeAdapter(),
    )
    prov = SnapshotOrLiveProvider()
    assert prov.quote("AAPL") == 308.0
    assert calls == ["AAPL"], "bare hit must short-circuit before suffix retries"


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
    # CSPX ~7% NVDA <= the 13% single-name cap -> plan-compliant diversified buy,
    # approved even though the book is ~57% NVDA (fix that by SELLING NVDA, not by
    # refusing diversified equity).
    assert by["CSPX"] is CandidateStatus.APPROVE
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


def test_redirect_overflow_into_diversifiers():
    # T-bills held for reserve-overfund must be REDIRECTED into the plan's zero-NVDA
    # diversifier ETF (EXUS) — deployed into a plan holding, not left as idle cash,
    # and no gold / plan change invented.
    from datetime import date

    from argosy.services.contracts import AllocationCandidate, AllocationLeg
    from argosy.services.deployment_advisor import (
        DeploymentLine, DeploymentPlan, DeploymentTier, EstateTag,
    )
    from argosy.services.deployment_funnel.contracts import (
        CandidateFlag, EnrichedCandidate, HistoryFeatures, PreflightResult,
    )
    from argosy.services.deployment_funnel.from_plan import (
        redirect_overflow_to_diversifiers,
    )

    doc = SimpleNamespace(nvda_cap_pct=13.0, classes=[
        SimpleNamespace(label="Cash & T-bills", target_pct=6.0,
                        instruments=[SimpleNamespace(symbol="IB01")]),
        SimpleNamespace(label="International developed (ex-US)", target_pct=11.0,
                        instruments=[SimpleNamespace(symbol="EXUS")]),
    ])
    est = EstateTag(domicile="IE", status="estate_safe", note="")
    dl = lambda sym, amt: DeploymentLine(
        symbol=sym, type="ETF", amount_usd=amt, timing="now", is_new=True,
        tier="core", horizon="10yr+", estate=est, cap_note="",
        net_of_tax_caveat="", rationale="x")
    plan = DeploymentPlan(
        deploy_amount_usd=30000.0, as_of=date(2026, 7, 1),
        tiers=(DeploymentTier(name="core", cap_pct=0.0, lines=(dl("IB01", 30000.0),)),),
        us_situs_exposed_usd=0.0, us_situs_sanctioned_usd=0.0,
        undeployed_remainder_usd=0.0, market_context_age=None, caveats=())
    hf = HistoryFeatures(last_price=100.0, ath=100.0, pct_below_ath=0.0,
                         zscore_vs_window=0.0, drawdown_pct=0.0)
    cand = AllocationCandidate(kind="BUY", legs=(AllocationLeg(
        side="BUY", symbol="IB01", account_id="leumi", currency="USD",
        notional_usd=30000.0, funding_source="cash"),), horizon="now")
    res = PreflightResult(deployable_usd=30000.0, kept_total_usd=0.0, enriched=(
        EnrichedCandidate(candidate=cand, symbol="IB01", effective_nvda_usd=0.0,
            news_sentiment=None, history=hf,
            status=__import__("argosy.services.deployment_funnel.contracts",
                              fromlist=["CandidateStatus"]).CandidateStatus.NEEDS_FLEET_REVIEW,
            reason="reserve overfund", cap_pct=None,
            flags=(CandidateFlag(kind="reserve_overfund", materiality="medium",
                                 fact="reserve funded"),)),
    ), plan_gaps=(), notes=())
    plan2, note = redirect_overflow_to_diversifiers(plan, res, doc)
    assert note and "EXUS" in note
    syms = {l.symbol.upper(): l.amount_usd for t in plan2.tiers for l in t.lines}
    assert "IB01" not in syms          # the reserve-overfund T-bill line dropped
    assert syms.get("EXUS", 0) >= 29000.0   # ~$30k redirected into EXUS

    # Exposure-aware: with a held estate-safe substitute (FWRA) covering the ex-US
    # sleeve, the overflow tops up FWRA instead of opening a new EXUS position.
    plan3, note3 = redirect_overflow_to_diversifiers(
        plan, res, doc, holdings={"FWRA": 200_000.0})
    syms3 = {l.symbol.upper(): l.amount_usd for t in plan3.tiers for l in t.lines}
    assert syms3.get("FWRA", 0) >= 29000.0  # redirected into the HELD substitute
    assert "EXUS" not in syms3              # no new ex-US position opened
    # The synthesized redirect line reflects the ACTUAL held position (not new, held
    # value populated) — dumping the full engine detail must not mislabel it.
    fwra_line = next(l for t in plan3.tiers for l in t.lines if l.symbol.upper() == "FWRA")
    assert fwra_line.is_new is False
    assert fwra_line.held_value_usd == 200_000.0

    # Conservation: the redirected overflow is placed EXACTLY (no cents created or
    # lost) even when the split across sleeves does not divide evenly. The dropped
    # $30k reserve line reappears in full across the diversifier lines.
    doc3 = SimpleNamespace(nvda_cap_pct=13.0, classes=[
        SimpleNamespace(label="Cash & T-bills", target_pct=6.0,
                        instruments=[SimpleNamespace(symbol="IB01")]),
        SimpleNamespace(label="International developed (ex-US)", target_pct=11.0,
                        instruments=[SimpleNamespace(symbol="EXUS")]),
        SimpleNamespace(label="Emerging-markets equity", target_pct=11.0,
                        instruments=[SimpleNamespace(symbol="EIMI")]),
        SimpleNamespace(label="Real assets (REIT/TIPS)", target_pct=11.0,
                        instruments=[SimpleNamespace(symbol="DPYA")]),
    ])
    plan4, _ = redirect_overflow_to_diversifiers(plan, res, doc3)
    added = sum(l.amount_usd for t in plan4.tiers for l in t.lines
                if l.symbol.upper() in {"EXUS", "EIMI", "DPYA"})
    assert abs(added - 30000.0) < 0.005   # exact to the cent
