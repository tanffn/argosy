"""Tests for the canonical instrument-level TargetAllocationDoc (roadmap T1.x).

The doc is the single structured object every surface reads: instrument-level
(tickers per class), canonical (engine-authored), and time-varying (a quarterly
glide). T1.1 covers the schema + JSON round-trip; later tasks add the builder.
"""

from __future__ import annotations

from datetime import date

from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    GlideWaypoint,
    TargetAllocationDoc,
)


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
