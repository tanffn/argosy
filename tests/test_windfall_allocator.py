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
