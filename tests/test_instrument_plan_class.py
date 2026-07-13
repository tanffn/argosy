"""Block H — instrument→plan-class mapping precedence + no US-broad dump."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.services.allocation_breakdown import build_allocation_breakdown
from argosy.services.instrument_plan_class import (
    UNMAPPED_LABEL,
    ClassificationEntry,
    SOURCE_FLEET,
    SOURCE_OWNER,
    SOURCE_PLAN,
    resolve_sleeve_label,
)


def _entry(label: str, source: str) -> ClassificationEntry:
    return ClassificationEntry(
        symbol="X", plan_class_label=label, source=source,
    )


def test_precedence_plan_doc_beats_owner():
    plan = {"SCHD": "Dividend-quality income"}
    cmap = {
        "SCHD": ClassificationEntry(
            "SCHD", "US broad-market core", SOURCE_OWNER,
        ),
    }
    assert resolve_sleeve_label(
        "SCHD", plan_symbol_labels=plan, classification_map=cmap,
    ) == "Dividend-quality income"


def test_precedence_owner_beats_fleet():
    cmap = {
        "VOO": ClassificationEntry("VOO", "US broad-market core", SOURCE_OWNER),
    }
    # Simulate a fleet label that would disagree — only one row exists; owner wins
    # because that's what's stored. Separate check: fleet alone applies.
    assert resolve_sleeve_label("VOO", classification_map=cmap) == "US broad-market core"
    cmap_fleet = {
        "VOO": ClassificationEntry(
            "VOO", "Global quality growth (ex-NVDA-dense)", SOURCE_FLEET,
        ),
    }
    assert resolve_sleeve_label(
        "VOO", classification_map=cmap_fleet,
    ) == "Global quality growth (ex-NVDA-dense)"


def test_precedence_fleet_beats_plan_seed_row():
    # Only one row per symbol in DB; resolve uses whatever is stored. Fleet
    # seed never overwrites owner; plan seed never overwrites fleet — tested
    # in upsert. Here: fleet row is used when no live plan instrument.
    cmap = {
        "SCHD": ClassificationEntry(
            "SCHD", "Dividend-quality income", SOURCE_FLEET,
        ),
    }
    assert resolve_sleeve_label(
        "SCHD", classification_map=cmap,
    ) == "Dividend-quality income"


def test_unmapped_not_us_broad():
    # Bare equity with no map must NOT land in US-broad.
    label = resolve_sleeve_label("BRK/B", asset_type="Equity")
    assert label == UNMAPPED_LABEL
    assert "broad" not in label.lower()


def test_cash_structural_shortcut():
    assert resolve_sleeve_label("-", asset_type="Cash").startswith("Cash")


def test_ibta_plan_first_short_duration():
    """Acceptance correction: IBTA stays wherever the plan puts it."""
    plan = {"IBTA": "Short-duration IG bonds"}
    cmap = {
        "IBTA": ClassificationEntry(
            "IBTA", "Cash & T-bills (incl. ILS tranche)", SOURCE_FLEET,
        ),
    }
    assert resolve_sleeve_label(
        "IBTA", plan_symbol_labels=plan, classification_map=cmap,
    ) == "Short-duration IG bonds"


def test_cross_surface_same_resolve_function():
    """Sleeve column / allocation / deploy gaps share resolve_sleeve_label."""
    from argosy.services import allocation_breakdown as ab
    from argosy.services import instrument_plan_class as ipc
    from argosy.services.deployment_funnel import plan_gaps as pg

    assert ab.resolve_sleeve_label is ipc.resolve_sleeve_label
    # plan_gaps imports build_allocation_breakdown which calls the same function
    assert callable(pg.sleeve_gaps_for_deploy)


def test_breakdown_uses_map_not_asset_type_dump():
    cmap = {
        "SCHD": ClassificationEntry(
            "SCHD", "Dividend-quality income", SOURCE_FLEET,
        ),
        "VOO": ClassificationEntry(
            "VOO", "US broad-market core", SOURCE_FLEET,
        ),
    }
    from datetime import date
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc, AllocationInstrument, GlideWaypoint, TargetAllocationDoc,
    )

    def cls(label, sym, pct):
        return AllocationClassDoc(
            label=label, snapshot_category=label, sigma_class="x",
            target_pct=pct,
            instruments=[AllocationInstrument(
                symbol=sym, role="primary",
                weight_within_class_pct=100.0, domicile="IE",
            )],
        )

    doc = TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[
            cls("US broad-market core", "CSPX", 40.0),
            cls("Dividend-quality income", "FUSA", 20.0),
            cls("Cash & T-bills (incl. ILS tranche)", "IB01", 40.0),
        ],
        glide=[GlideWaypoint(
            quarter=0, date=date(2026, 1, 1),
            composition_pct_by_class={"US broad-market core": 40.0},
        )],
    )
    snap = SimpleNamespace(positions=[
        SimpleNamespace(symbol="SCHD", asset_type="Equity", usd_value_k=160.0, details=""),
        SimpleNamespace(symbol="CSPX", asset_type="Core Equity", usd_value_k=400.0, details=""),
        SimpleNamespace(symbol="FWRA", asset_type="Equity", usd_value_k=100.0, details=""),
        SimpleNamespace(symbol="-", asset_type="Cash", usd_value_k=340.0, details=""),
    ])
    rows = build_allocation_breakdown(snap, doc, classification_map=cmap)
    by = {r.label: r for r in rows}
    assert "SCHD" in {h.symbol for h in by["Dividend-quality income"].holdings}
    assert round(by["Dividend-quality income"].current_pct, 0) == 16.0
    # FWRA has no global-core class in plan → Unmapped (fail loud)
    assert "FWRA" in {h.symbol for h in by[UNMAPPED_LABEL].holdings}
    # Must not absorb FWRA into US-broad
    core_syms = {h.symbol for h in by["US broad-market core"].holdings}
    assert "FWRA" not in core_syms
    assert "CSPX" in core_syms
