"""Tests for the high-potential (satellite) sleeve sizing (S18)."""
from __future__ import annotations

import pytest

from argosy.services.high_potential_sleeve import (
    SleeveCandidate,
    build_high_potential_sleeve,
    sleeve_vehicle_split,
)


def test_conviction_weighted_sizing_sums_to_budget():
    allocs = build_high_potential_sleeve(12_500.0)
    assert allocs, "expected the seed sleeve to produce allocations"
    assert sum(a.amount_usd for a in allocs) == pytest.approx(12_500.0, abs=0.5)
    assert sum(a.pct_of_sleeve for a in allocs) == pytest.approx(100.0, abs=0.1)


def test_higher_conviction_gets_more_dollars():
    cands = (
        SleeveCandidate("HI", "high", "single_name", "HIGH", "t", True),
        SleeveCandidate("LO", "low", "single_name", "LOW", "t", True),
    )
    allocs = build_high_potential_sleeve(4_000.0, cands)
    by = {a.candidate.ticker: a.amount_usd for a in allocs}
    # HIGH=3, LOW=1 → 3000 / 1000
    assert by["HI"] == pytest.approx(3_000.0)
    assert by["LO"] == pytest.approx(1_000.0)


def test_blend_has_both_ucits_core_and_single_name_carveout():
    allocs = build_high_potential_sleeve(12_500.0)
    split = sleeve_vehicle_split(allocs)
    assert "ucits_thematic" in split and "single_name" in split
    # UCITS core should be the larger share (the "core" of the blend).
    assert split["ucits_thematic"] >= split["single_name"]
    # Every single-name carve-out candidate is flagged US-situs (estate-tax).
    for a in allocs:
        if a.candidate.vehicle == "single_name":
            assert a.candidate.us_situs is True
        else:
            assert a.candidate.us_situs is False


def test_non_positive_budget_returns_empty():
    assert build_high_potential_sleeve(0.0) == []
    assert build_high_potential_sleeve(-5.0) == []


def test_seed_candidates_carry_a_thesis_and_source():
    allocs = build_high_potential_sleeve(12_500.0)
    for a in allocs:
        assert a.candidate.thesis.strip()
        assert a.candidate.source == "advisor_seed"
