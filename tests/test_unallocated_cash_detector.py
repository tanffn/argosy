"""Tests for the self-tuning unallocated-cash detector.

Closes the user-facing "I have $X unallocated cash, what should I do
with it?" flow ([[feedback_unallocated_cash_reframe]]). Threshold is
relative to plan-target cash (default 1.5x), not a hard-coded dollar
amount.
"""
from __future__ import annotations

from datetime import date

import pytest

from argosy.ingest.tsv import AllocationRow, PortfolioSnapshot
from argosy.services.unallocated_cash_detector import (
    DEFAULT_OVERAGE_RATIO,
    _detect_from_snapshot,
    _find_cash_row,
)


def _snap(
    *,
    cash_current_k: float,
    cash_target_k: float,
    growth_current_k: float = 100,
    growth_target_k: float = 200,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        source_path="test://",
        snapshot_date=date(2026, 5, 29),
        fx_usd_nis=3.7,
        allocations=[
            AllocationRow(
                category="Cash",
                pct=cash_current_k / (cash_current_k + growth_current_k) * 100,
                usd_value_k=cash_current_k,
                target_pct=cash_target_k / (cash_target_k + growth_target_k) * 100,
                target_k=cash_target_k,
                delta_k=cash_target_k - cash_current_k,
            ),
            AllocationRow(
                category="Growth",
                pct=growth_current_k / (cash_current_k + growth_current_k) * 100,
                usd_value_k=growth_current_k,
                target_pct=growth_target_k / (cash_target_k + growth_target_k) * 100,
                target_k=growth_target_k,
                delta_k=growth_target_k - growth_current_k,
            ),
        ],
    )


class TestThresholdGating:
    def test_below_threshold_returns_none(self):
        """Current cash within 1.5x of target -> no event."""
        snap = _snap(cash_current_k=10, cash_target_k=10)
        assert _detect_from_snapshot(snap) is None

    def test_just_below_threshold_returns_none(self):
        snap = _snap(cash_current_k=14, cash_target_k=10)  # 1.4x
        assert _detect_from_snapshot(snap) is None

    def test_above_threshold_fires(self):
        """Current cash > target * 1.5 -> event fires."""
        snap = _snap(cash_current_k=20, cash_target_k=10)  # 2.0x
        event = _detect_from_snapshot(snap)
        assert event is not None
        assert event.current_cash_k_usd == 20.0
        assert event.target_cash_k_usd == 10.0
        assert event.overage_ratio == 2.0
        assert event.excess_usd == 10_000.0  # ($20K - $10K) = $10K excess

    def test_custom_overage_ratio(self):
        """Tighter ratio = fires sooner."""
        snap = _snap(cash_current_k=11, cash_target_k=10)  # 1.1x
        assert _detect_from_snapshot(snap, overage_ratio=1.5) is None
        e = _detect_from_snapshot(snap, overage_ratio=1.05)
        assert e is not None
        assert e.excess_usd == pytest.approx(1000.0)


class TestProposalsShape:
    def test_proposals_target_under_target_classes(self):
        """The excess flows to growth (under-target) when growth has room."""
        snap = _snap(
            cash_current_k=50, cash_target_k=10,
            growth_current_k=100, growth_target_k=200,
        )
        event = _detect_from_snapshot(snap)
        assert event is not None
        assert event.excess_usd == 40_000
        # At least one proposal in Growth (under target by $100K).
        growth_proposals = [p for p in event.proposals if p.asset_class == "Growth"]
        assert len(growth_proposals) > 0
        total_proposed = sum(p.amount_usd for p in event.proposals)
        # 100% of excess goes to long-term (no medium/short split for
        # the unallocated-cash flow).
        assert total_proposed == pytest.approx(40_000, rel=1e-3)

    def test_headline_describes_ratio(self):
        snap = _snap(cash_current_k=30, cash_target_k=10)
        event = _detect_from_snapshot(snap)
        assert event is not None
        assert "3.0x" in event.headline


class TestMissingData:
    def test_no_allocations_returns_none(self):
        snap = PortfolioSnapshot(source_path="x", allocations=[])
        assert _detect_from_snapshot(snap) is None

    def test_no_cash_row_returns_none(self):
        snap = PortfolioSnapshot(
            source_path="x",
            allocations=[
                AllocationRow(category="Growth", target_k=100, usd_value_k=50),
            ],
        )
        assert _detect_from_snapshot(snap) is None

    def test_zero_target_cash_returns_none(self):
        """Defensive: zero or null target cash isn't actionable."""
        snap = PortfolioSnapshot(
            source_path="x",
            allocations=[
                AllocationRow(category="Cash", target_k=0, usd_value_k=50),
                AllocationRow(category="Growth", target_k=100, usd_value_k=50, target_pct=50.0),
            ],
        )
        assert _detect_from_snapshot(snap) is None


class TestCashRowDetection:
    def test_literal_cash(self):
        rows = [AllocationRow(category="Cash", target_k=10)]
        assert _find_cash_row(rows) is not None

    def test_cash_substring(self):
        rows = [AllocationRow(category="Cash & ST bonds", target_k=10)]
        assert _find_cash_row(rows) is not None

    def test_no_match(self):
        rows = [AllocationRow(category="Growth", target_k=10)]
        assert _find_cash_row(rows) is None
