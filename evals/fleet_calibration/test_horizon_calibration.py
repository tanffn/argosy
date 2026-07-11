"""Stubbed tests for horizon-band calibration scoring — no live LLM."""
from __future__ import annotations

from horizon_calibration import (
    ClockBand,
    months_between,
    parse_clock_band,
    score_horizon_calibration,
    score_row,
)
from datetime import date


def test_parse_band_years():
    text = (
        "THE CLOCK: clean-sheet CPU launch in ~12-18 months. "
        "Honest re-rating horizon: 2-3 years"
    )
    band = parse_clock_band(text)
    assert isinstance(band, ClockBand)
    assert band.low_months == 24
    assert band.high_months == 36


def test_parse_band_months():
    band = parse_clock_band("Honest re-rating horizon: 3-6 months out.")
    assert isinstance(band, ClockBand)
    assert (band.low_months, band.high_months) == (3, 6)


def test_parse_unestimable():
    text = (
        "THE CLOCK: next print is unknowable; re-rating horizon is "
        "genuinely unestimable in the constructive direction."
    )
    assert parse_clock_band(text) == "unestimable"


def test_months_between():
    assert months_between(date(2016, 2, 29), date(2016, 5, 24)) == 3


def test_score_inside_outside():
    band = ClockBand(24, 36, "2-3 years")
    assert score_horizon_calibration(
        band, actual_months=30, resolution_present=True,
    ) == "inside"
    assert score_horizon_calibration(
        band, actual_months=6, resolution_present=True,
    ) == "outside"


def test_score_synthetic_not_applicable():
    assert score_horizon_calibration(
        ClockBand(24, 48, "2-4 years"),
        actual_months=None,
        resolution_present=False,
    ) == "not_applicable"


def test_score_row_amd_stub():
    """Persisted-replay shape from block2b amd_2016_f1 — stubbed, no burn."""
    rationale = (
        "THE CLOCK: clean-sheet CPU launch in ~12-18 months. "
        "Honest re-rating horizon: 2-3 years"
    )
    packet = {
        "freeze_date": "2016-02-29",
        "resolution": {
            "horizon_label": "6y (2022-02-01)",
            "clock_calibration": {"rerating_date": "2016-05-24"},
            "benchmark_return_pct": 5357.0,
        },
    }
    out = score_row(
        rationale=rationale,
        freeze_date=packet["freeze_date"],
        packet=packet,
    )
    assert out["stated"]["low_months"] == 24
    assert out["actual_months"] == 3
    assert out["score"] == "outside"  # 3 mo vs 24-36 band


def test_score_row_omk_synthetic_stub():
    rationale = (
        "THE CLOCK: constructive re-rating is unestimable given the burn."
    )
    packet = {
        "freeze_date": "2024-01-15",
        "resolution": None,
        "synthetic": True,
    }
    out = score_row(
        rationale=rationale,
        freeze_date=packet["freeze_date"],
        packet=packet,
    )
    assert out["stated"] == "unestimable"
    assert out["score"] == "unestimable_stated"
