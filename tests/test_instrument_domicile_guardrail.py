"""Systemic guardrail (S18): the canonical instrument layer must be validated
against US-situs estate exposure — the check that was missing when the frozen
engine emitted US-domiciled VOO/SCHD for a non-US-person.
"""
from __future__ import annotations

from datetime import date

from argosy.services.allocation_plan import build_target_allocation
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    GlideWaypoint,
    TargetAllocationDoc,
    validate_instrument_domicile,
)


def _doc(instruments: list[AllocationInstrument]) -> TargetAllocationDoc:
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0, fi_pct=21.3,
        provenance="test",
        classes=[AllocationClassDoc(
            label="C", snapshot_category="Core Equity", sigma_class="us_equity",
            target_pct=100.0, instruments=instruments,
        )],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 6, 11), composition_pct_by_class={"C": 100.0})],
    )


def test_engine_instruments_are_domicile_stamped_ucits_except_nvda():
    alloc = build_target_allocation()
    for c in alloc.classes:
        for i in c.instruments:
            assert i.domicile is not None, f"{c.label}/{i.symbol} has no domicile"
            if i.symbol == "NVDA":
                assert i.domicile == "US" and i.is_us_situs
            else:
                assert i.domicile == "IE", f"{i.symbol} should be Irish UCITS"
                assert not i.is_us_situs


def test_us_domiciled_non_nvda_is_red():
    doc = _doc([AllocationInstrument(
        symbol="VOO", role="primary", weight_within_class_pct=100.0, domicile="US",
    )])
    v = validate_instrument_domicile(doc)
    assert len(v) == 1 and v[0].severity == "RED" and v[0].symbol == "VOO"


def test_nvda_us_situs_is_sanctioned():
    doc = _doc([AllocationInstrument(
        symbol="NVDA", role="primary", weight_within_class_pct=100.0, domicile="US",
    )])
    assert validate_instrument_domicile(doc) == []


def test_unstamped_domicile_is_yellow_never_silently_ok():
    doc = _doc([AllocationInstrument(
        symbol="MYSTERY", role="primary", weight_within_class_pct=100.0,
    )])
    v = validate_instrument_domicile(doc)
    assert len(v) == 1 and v[0].severity == "YELLOW"


def test_ucits_doc_is_clean():
    doc = _doc([AllocationInstrument(
        symbol="CSPX", role="primary", weight_within_class_pct=100.0, domicile="IE",
    )])
    assert validate_instrument_domicile(doc) == []


def test_us_person_skips_the_estate_check():
    doc = _doc([AllocationInstrument(
        symbol="VOO", role="primary", weight_within_class_pct=100.0, domicile="US",
    )])
    assert validate_instrument_domicile(doc, non_us_person=False) == []
