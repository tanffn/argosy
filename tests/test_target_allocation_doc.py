"""Tests for the canonical instrument-level TargetAllocationDoc (roadmap T1.x).

The doc is the single structured object every surface reads: instrument-level
(tickers per class), canonical (engine-authored), and time-varying (a quarterly
glide). T1.1 covers the schema + JSON round-trip; later tasks add the builder.
"""

from __future__ import annotations

from datetime import date

import pytest

from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    GlideWaypoint,
    TargetAllocationDoc,
    build_target_allocation_doc,
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

    # the glide is 8 quarterly waypoints, each a coherent 100%-composition
    assert len(doc.glide) == 8
    for wp in doc.glide:
        assert sum(wp.composition_pct_by_class.values()) == pytest.approx(100.0, abs=0.1)

    # the final waypoint lands exactly on the end-state class targets
    final = doc.glide[-1].composition_pct_by_class
    for c in doc.classes:
        assert final[c.label] == pytest.approx(c.target_pct, abs=0.1)

    # NVDA deconcentrates: it is a bigger slice at q1 than at the target
    nvda_label = "Strategic single-stock (NVDA)"
    assert (
        doc.glide[0].composition_pct_by_class[nvda_label]
        > doc.glide[-1].composition_pct_by_class[nvda_label]
    )
