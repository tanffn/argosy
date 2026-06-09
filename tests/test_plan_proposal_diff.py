"""T4.1 — plan→proposal diff: target_allocation_doc vs current holdings.

The canonical plan's instrument-level targets, diffed against actual holdings,
yield per-ticker keep/trim/add deltas (the substrate the action-proposer turns
into 'buy X of VOO / trim Y of NVDA'). Pure money-math; hand-verified below.
"""
from __future__ import annotations

import pytest

from argosy.services.plan_proposal_diff import ProposalDelta, diff_plan_vs_holdings
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    TargetAllocationDoc,
)


def _doc(classes: list[AllocationClassDoc]) -> TargetAllocationDoc:
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0, fi_pct=21.3,
        provenance="test", classes=classes, glide=[],
    )


def _cls(label, sigma_class, pct, instruments) -> AllocationClassDoc:
    return AllocationClassDoc(
        label=label, snapshot_category=label, sigma_class=sigma_class,
        target_pct=pct, instruments=instruments,
    )


def _instr(symbol, w) -> AllocationInstrument:
    return AllocationInstrument(symbol=symbol, role="primary", weight_within_class_pct=w)


# NVDA 12% of book, core equity (VOO) 88% of book.
DOC = _doc([
    _cls("Strategic single-stock (NVDA)", "concentrated_equity", 12.0, [_instr("NVDA", 100.0)]),
    _cls("US broad-market core", "us_equity", 88.0, [_instr("VOO", 100.0)]),
])


def test_overweight_nvda_trims_underweight_voo_adds():
    # Book $2,500k: NVDA $2,400k (96%), VOO $100k (4%).
    holdings = {"NVDA": 2_400_000.0, "VOO": 100_000.0}
    deltas = {d.symbol: d for d in diff_plan_vs_holdings(DOC, holdings)}

    # NVDA target = 12% * 2.5M = 300k; current 2.4M → trim ~2.1M.
    assert deltas["NVDA"].action == "trim"
    assert deltas["NVDA"].target_value_usd == pytest.approx(300_000.0)
    assert deltas["NVDA"].delta_value_usd == pytest.approx(-2_100_000.0)
    # VOO target = 88% * 2.5M = 2.2M; current 100k → add ~2.1M.
    assert deltas["VOO"].action == "add"
    assert deltas["VOO"].target_value_usd == pytest.approx(2_200_000.0)
    assert deltas["VOO"].delta_value_usd == pytest.approx(2_100_000.0)


def test_aligned_holding_is_keep_within_band():
    # Exactly on target (12% / 88% of a $1M book) → keep, no trade.
    holdings = {"NVDA": 120_000.0, "VOO": 880_000.0}
    deltas = {d.symbol: d for d in diff_plan_vs_holdings(DOC, holdings)}
    assert deltas["NVDA"].action == "keep"
    assert deltas["VOO"].action == "keep"


def test_held_symbol_absent_from_plan_trims_to_zero():
    # SOFI is held but not in the plan → full exit (target 0).
    holdings = {"NVDA": 300_000.0, "VOO": 2_200_000.0, "SOFI": 50_000.0}
    deltas = {d.symbol: d for d in diff_plan_vs_holdings(DOC, holdings)}
    assert deltas["SOFI"].action == "trim"
    assert deltas["SOFI"].target_value_usd == pytest.approx(0.0)
    assert deltas["SOFI"].delta_value_usd == pytest.approx(-50_000.0)


def test_deltas_are_self_consistent_and_typed():
    holdings = {"NVDA": 2_400_000.0, "VOO": 100_000.0}
    deltas = diff_plan_vs_holdings(DOC, holdings)
    assert all(isinstance(d, ProposalDelta) for d in deltas)
    # Every delta = target - current; trims net against adds (closed book).
    for d in deltas:
        assert d.delta_value_usd == pytest.approx(d.target_value_usd - d.current_value_usd)
    assert sum(d.delta_value_usd for d in deltas) == pytest.approx(0.0, abs=1.0)


def test_empty_book_returns_empty():
    assert diff_plan_vs_holdings(DOC, {}) == []
