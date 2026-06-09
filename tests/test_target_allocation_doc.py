"""Tests for the canonical instrument-level TargetAllocationDoc (roadmap T1.x).

The doc is the single structured object every surface reads: instrument-level
(tickers per class), canonical (engine-authored), and time-varying (a quarterly
glide). T1.1 covers the schema + JSON round-trip; later tasks add the builder.
"""

from __future__ import annotations

from datetime import date

import pytest

from argosy.services.target_allocation_doc import (
    OTHER_SINGLES_LABEL,
    AllocationClassDoc,
    AllocationInstrument,
    GlideWaypoint,
    TargetAllocationDoc,
    build_target_allocation_doc,
    derive_full_book_today_composition,
    load_plan_target_allocation,
)

# A full liquid book incl. NVDA (sums to ~100) — the settled basis is the FULL
# tradeable book, NVDA ~60%. Passed explicitly so the builder stays pure; the
# snapshot-derivation of this composition is a wiring concern (T1.5/T1.6),
# verified separately because the basis is money-math-sensitive.
_TODAY_FULL_BOOK = {
    "Strategic single-stock (NVDA)": 60.47,
    "US growth tilt (ex-NVDA)": 11.04,
    "US broad-market core": 10.53,
    "Dividend-quality income": 7.01,
    "Cash & T-bills (incl. ILS tranche)": 4.95,
    "Short-duration IG bonds": 3.29,
    "Real assets (REIT/TIPS)": 1.82,
    "International developed (ex-US)": 0.90,
}


def test_doc_is_instrument_level_and_roundtrips() -> None:
    doc = TargetAllocationDoc(
        anchor_sigma=0.18,
        blended_sigma=0.18,
        nvda_cap_pct=13.0,
        fi_pct=21.3,
        provenance="panel",
        classes=[
            AllocationClassDoc(
                label="Strategic single-stock (NVDA)",
                snapshot_category="Individual Stocks",
                sigma_class="concentrated_equity",
                target_pct=12.0,
                instruments=[
                    AllocationInstrument(
                        symbol="NVDA", role="primary", weight_within_class_pct=100.0
                    )
                ],
            ),
        ],
        glide=[
            GlideWaypoint(
                quarter=1,
                date=date(2026, 9, 1),
                composition_pct_by_class={"Strategic single-stock (NVDA)": 12.0},
            )
        ],
    )

    again = TargetAllocationDoc.model_validate_json(doc.model_dump_json())

    assert again.classes[0].instruments[0].symbol == "NVDA"
    assert again.glide[0].composition_pct_by_class["Strategic single-stock (NVDA)"] == 12.0


def test_doc_carries_schema_defaults() -> None:
    """schema_version + basis default without being supplied (provenance contract)."""
    doc = TargetAllocationDoc(
        anchor_sigma=0.18,
        blended_sigma=0.18,
        nvda_cap_pct=13.0,
        fi_pct=21.3,
        provenance="panel",
        classes=[],
        glide=[],
    )
    assert doc.schema_version == 1
    assert doc.basis == "full tradeable book"


def test_builds_instrument_level_doc_with_quarterly_glide() -> None:
    doc = build_target_allocation_doc(
        today=date(2026, 6, 9),
        today_composition=_TODAY_FULL_BOOK,
        quarters=8,
    )

    # instrument-level: every class names its tickers
    assert all(c.instruments for c in doc.classes)
    nvda = next(c for c in doc.classes if c.snapshot_category == "Individual Stocks")
    assert [i.symbol for i in nvda.instruments] == ["NVDA"]

    # the 13% hard cap is carried, not re-derived
    assert doc.nvda_cap_pct == pytest.approx(13.0)

    # the glide is q0 (today anchor) + 8 quarterly waypoints, each a coherent 100%
    assert len(doc.glide) == 9
    assert doc.glide[0].quarter == 0
    for wp in doc.glide:
        assert sum(wp.composition_pct_by_class.values()) == pytest.approx(100.0, abs=0.1)

    nvda_label = "Strategic single-stock (NVDA)"
    # q0 reflects TODAY exactly (the chart's left anchor == /portfolio's reality)
    assert doc.glide[0].composition_pct_by_class[nvda_label] == pytest.approx(
        _TODAY_FULL_BOOK[nvda_label], abs=0.01
    )

    # the final waypoint lands exactly on the end-state class targets
    final = doc.glide[-1].composition_pct_by_class
    for c in doc.classes:
        assert final[c.label] == pytest.approx(c.target_pct, abs=0.1)

    # NVDA deconcentrates: bigger today than at the target
    assert (
        doc.glide[0].composition_pct_by_class[nvda_label]
        > doc.glide[-1].composition_pct_by_class[nvda_label]
    )


class _PV:
    """Minimal PlanVersion stand-in carrying just the column the reader reads."""

    def __init__(self, target_allocation_json: str | None) -> None:
        self.target_allocation_json = target_allocation_json


def test_load_plan_target_allocation_parses_when_set() -> None:
    doc = build_target_allocation_doc(
        today=date(2026, 6, 9), today_composition=_TODAY_FULL_BOOK
    )
    loaded = load_plan_target_allocation(_PV(doc.model_dump_json()))
    assert loaded is not None
    assert loaded.classes[0].instruments  # instrument-level survives the round-trip


def test_load_plan_target_allocation_returns_none_and_never_raises() -> None:
    # empty / missing column -> None
    assert load_plan_target_allocation(_PV(None)) is None
    assert load_plan_target_allocation(_PV("")) is None
    # malformed JSON -> None (never raises)
    assert load_plan_target_allocation(_PV("{not valid json")) is None
    # object with no such attribute at all -> None
    assert load_plan_target_allocation(object()) is None


# The live snapshot (portfolio_snapshots.id=1) ex-NVDA categories, normalized
# keys as _categories_from_snapshot returns them. NVDA is NOT here — it is a
# separate positions row; its weight comes from the concentration report.
_SNAPSHOT_EX_NVDA = {
    "alternative": 0.0,
    "cash": 12.94,
    "core equity": 26.25,
    "defensive": 11.06,
    "dividend": 18.28,
    "growth": 10.9,
    "individual stocks": 18.21,
    "international": 2.36,
}


def test_derive_full_book_composition_matches_codex_verified() -> None:
    """Reconciles to the codex danger-full-access verdict against the live DB:
    NVDA 64.86% (concentration report, NOT the snapshot 'Individual Stocks'),
    ex-NVDA categories scaled by (100-64.86)/100, Defensive split low-vol/bonds
    by target ratio, other-singles modeled as a distinct redeploy band."""
    comp = derive_full_book_today_composition(
        nvda_tradeable_pct=64.86,
        ex_nvda_categories=_SNAPSHOT_EX_NVDA,
        low_vol_target=5.56,
        bonds_target=6.39,
    )
    assert sum(comp.values()) == pytest.approx(100.0, abs=0.01)
    assert comp["Strategic single-stock (NVDA)"] == pytest.approx(64.86)
    assert comp["US broad-market core"] == pytest.approx(9.2242, abs=0.001)
    assert comp["Dividend-quality income"] == pytest.approx(6.4236, abs=0.001)
    assert comp["International developed (ex-US)"] == pytest.approx(0.8293, abs=0.001)
    # growth ALONE (10.9 x 0.3514), NOT folded with the other singles
    assert comp["US growth tilt (ex-NVDA)"] == pytest.approx(3.8303, abs=0.001)
    assert comp["US low-volatility equity"] == pytest.approx(1.8083, abs=0.001)
    assert comp["Short-duration IG bonds"] == pytest.approx(2.0782, abs=0.001)
    assert comp["Cash & T-bills (incl. ILS tranche)"] == pytest.approx(4.5471, abs=0.001)
    assert comp["Real assets (REIT/TIPS)"] == pytest.approx(0.0, abs=0.001)
    # the non-NVDA singles are an honest, distinct redeploy band (-> glides to 0)
    assert comp[OTHER_SINGLES_LABEL] == pytest.approx(6.399, abs=0.001)


def test_derived_composition_drives_a_two_sided_glide() -> None:
    """The derived composition + the engine target produce a glide where BOTH
    NVDA and the legacy singles deconcentrate toward the target (redeploy)."""
    comp = derive_full_book_today_composition(
        nvda_tradeable_pct=64.86,
        ex_nvda_categories=_SNAPSHOT_EX_NVDA,
        low_vol_target=5.56,
        bonds_target=6.39,
    )
    doc = build_target_allocation_doc(
        today=date(2026, 6, 9), today_composition=comp, quarters=8
    )
    nvda = "Strategic single-stock (NVDA)"
    # q0 anchors on today's real NVDA weight (64.86%); it then glides toward the
    # 12 target; the legacy singles band glides to ~0; every quarter sums to 100.
    assert doc.glide[0].composition_pct_by_class[nvda] == pytest.approx(64.86, abs=0.01)
    assert doc.glide[0].composition_pct_by_class[nvda] > doc.glide[-1].composition_pct_by_class[nvda]
    assert doc.glide[-1].composition_pct_by_class.get(OTHER_SINGLES_LABEL, 0.0) == pytest.approx(0.0, abs=0.01)
    for wp in doc.glide:
        assert sum(wp.composition_pct_by_class.values()) == pytest.approx(100.0, abs=0.1)
