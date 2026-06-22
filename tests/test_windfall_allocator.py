"""Plan-bound windfall allocation: targets come from the canonical doc, not the TSV."""
from __future__ import annotations

from datetime import date


def test_windfall_targets_come_from_canonical_doc_not_tsv():
    """The long-term allocation closes gaps against the canonical plan's
    glide-aware class targets, not the TSV-typed targets."""
    import argosy.services.retirement.windfall_allocator as wa
    from argosy.services.target_allocation_doc import (
        TargetAllocationDoc, AllocationClassDoc, AllocationInstrument, GlideWaypoint,
    )
    doc = TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[AllocationClassDoc(label="Core", snapshot_category="Core",
                 sigma_class="us_equity", target_pct=100.0,
                 instruments=[AllocationInstrument(symbol="CSPX", role="primary",
                 weight_within_class_pct=100.0, domicile="IE")])],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 1, 1),
               composition_pct_by_class={"Core": 100.0})],
    )
    # event with $50k cash, empty book
    plan = wa.propose_allocations_from_plan(doc, holdings={}, cash_usd=50_000.0,
                                            as_of=date(2026, 6, 1))
    # all the long-term cash goes to the canonical instrument CSPX
    longs = [c for c in plan if c.kind == "BUY"]
    assert longs and all(l.symbol == "CSPX" for c in longs for l in c.legs)
    # buy-only and capped at cash
    assert all(l.side == "BUY" for c in plan for l in c.legs)
    assert round(sum(l.notional_usd for c in plan for l in c.legs), 2) <= 50_000.0


def _multiclass_doc():
    """A 3-class canonical doc so the buy list has >1 instrument — a single-
    instrument doc would pass the SET-equality invariant trivially."""
    from datetime import date as _date

    from argosy.services.target_allocation_doc import (
        AllocationClassDoc, AllocationInstrument, GlideWaypoint, TargetAllocationDoc,
    )
    def _cls(label, sym, pct, dom="IE"):
        return AllocationClassDoc(
            label=label, snapshot_category=label, sigma_class="us_equity",
            target_pct=pct,
            instruments=[AllocationInstrument(
                symbol=sym, role="primary", weight_within_class_pct=100.0,
                domicile=dom)])
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[_cls("Core", "CSPX", 50.0), _cls("Growth", "CNDX", 30.0),
                 _cls("Defensive", "IB01", 20.0)],
        glide=[GlideWaypoint(quarter=0, date=_date(2026, 1, 1),
               composition_pct_by_class={"Core": 50.0, "Growth": 30.0,
                                         "Defensive": 20.0})],
    )


def test_long_term_buy_set_equals_deploy_cash_core():
    """CONSISTENCY INVARIANT (the whole point of this fix): for the same
    (doc, holdings, cash), the LONG-term instrument SET proposed by the
    windfall / unallocated-cash path == the core/plan-bound instrument set
    from /deploy-cash. No hardcoded CNDX-style divergence."""
    import argosy.services.retirement.windfall_allocator as wa
    from argosy.services.deployment_advisor import assemble_deployment_plan

    doc = _multiclass_doc()
    holdings = {"CSPX": 100_000.0}  # under-weight Growth + Defensive
    cash = 80_000.0
    as_of = date(2026, 6, 1)

    # The canonical engine + the proposal converter (the windfall/unallocated path).
    longs, _rem = wa._allocate_long_term_from_plan(cash, doc, holdings, as_of=as_of)
    wa_symbols = {p.instrument for p in longs}

    # /deploy-cash core tier (sleeve OFF so core == full deploy, directly comparable).
    plan = assemble_deployment_plan(
        doc=doc, holdings=holdings, deploy_amount_usd=cash, as_of=as_of,
        use_high_potential=False)
    core = next(t for t in plan.tiers if t.name == "core")
    deploy_symbols = {l.symbol for l in core.lines}

    assert wa_symbols, "expected a non-empty canonical buy list"
    assert wa_symbols == deploy_symbols, (
        f"windfall/unallocated buy set {wa_symbols} diverges from deploy-cash "
        f"core set {deploy_symbols} — the engines disagree")
    # And the dollars match instrument-for-instrument (same engine, same math).
    wa_by_sym = {p.instrument: round(p.amount_usd, 2) for p in longs}
    deploy_by_sym = {l.symbol: round(l.amount_usd, 2) for l in core.lines}
    assert wa_by_sym == deploy_by_sym
