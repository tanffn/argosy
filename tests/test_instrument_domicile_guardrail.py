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


def test_engine_instruments_are_domicile_stamped_non_us_except_nvda():
    """Every engine instrument is domicile-stamped and NON-US-situs except the one
    sanctioned NVDA holding. Most non-NVDA primaries are Irish UCITS, but the
    Alternatives sleeve's bitcoin ETP (IB1T) is Swiss-domiciled — also non-US-situs
    — so the guardrail is "non-US", not "necessarily IE". The estate doctrine
    (only NVDA may be US-situs) is unchanged."""
    alloc = build_target_allocation()
    for c in alloc.classes:
        for i in c.instruments:
            assert i.domicile is not None, f"{c.label}/{i.symbol} has no domicile"
            if i.symbol == "NVDA":
                assert i.domicile == "US" and i.is_us_situs
            else:
                assert i.domicile != "US", f"{i.symbol} must be non-US-domiciled"
                assert not i.is_us_situs
                # The equity/FI sleeves stay Irish UCITS; the alternatives sleeve
                # may be Irish (gold ETC) or Swiss (bitcoin ETP IB1T).
                assert i.domicile in {"IE", "CH"}, (
                    f"{i.symbol} domicile {i.domicile} unexpected for a canonical sleeve"
                )


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


def test_alternatives_gold_and_btc_are_clean_non_us():
    """The Alternatives sleeve's instruments are non-US-situs: Irish physical gold
    (IGLN) and the Swiss bitcoin ETP (IB1T). Neither is flagged RED/YELLOW, so the
    sleeve introduces no new US-situs estate exposure (only NVDA is sanctioned)."""
    doc = _doc([
        AllocationInstrument(symbol="IGLN", role="primary",
                             weight_within_class_pct=80.0, domicile="IE"),
        AllocationInstrument(symbol="IB1T", role="primary",
                             weight_within_class_pct=20.0, domicile="CH"),
    ])
    assert validate_instrument_domicile(doc) == []


def test_full_engine_doc_has_no_domicile_violations():
    """End-to-end: the built canonical allocation (now incl. Alternatives) is
    estate-clean — the domicile validator returns no RED/YELLOW flags."""
    from datetime import date as _date

    from argosy.services.target_allocation_doc import build_target_allocation_doc

    comp = {
        "Strategic single-stock (NVDA)": 60.0,
        "US broad-market core": 25.0,
        "Cash & T-bills (incl. ILS tranche)": 15.0,
    }
    doc = build_target_allocation_doc(today=_date(2026, 6, 12), today_composition=comp)
    assert validate_instrument_domicile(doc) == []


def test_us_person_skips_the_estate_check():
    doc = _doc([AllocationInstrument(
        symbol="VOO", role="primary", weight_within_class_pct=100.0, domicile="US",
    )])
    assert validate_instrument_domicile(doc, non_us_person=False) == []
