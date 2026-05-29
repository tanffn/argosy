"""Plan-target-gap detector for unallocated cash.

The continuous version of the windfall flow ([[feedback_unallocated_cash_reframe]]):
the routine paycheck-into-bank case below the $25K windfall threshold still
needs allocation. This detector fires whenever the latest portfolio
snapshot's cash position exceeds the plan-target cash by a configurable
ratio (default 1.5x -- so $5K over a $10K target triggers, $14K over
$10K doesn't).

Self-tuning: there's no hard-coded dollar threshold. The trigger is
relative to the user's plan target for cash, which is parsed from the
TSV's "Current allocation:" block (`AllocationRow`). The user's chosen
threshold was "Plan-target gap" via AskUserQuestion on 2026-05-29.

Reuses ``windfall_allocator._allocate_long_term`` directly so the
proposals carry the same shape + asset-class targeting logic as the
windfall flow. The Accept/Defer surface piggybacks on the existing
``windfall_actions`` table -- a "cash overage" decision and a "windfall
RSU sale allocation" decision have the same shape (horizon, asset_class,
instrument, amount_usd, rationale, closes_delta_usd) so a separate
table would be churn for no information gain.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.ingest.tsv import AllocationRow, PortfolioSnapshot
from argosy.services.portfolio_snapshot_store import (
    get_latest_snapshot_row,
    row_to_snapshot,
)
from argosy.services.retirement.windfall_allocator import (
    AllocationProposal,
    _allocate_long_term,
)
from argosy.services.retirement.windfall_detector import AllocationLine


# Default ratio: current cash > target cash * 1.5 -> fire.
# 1.5x picks up routine paycheck residue without going off on every $1K of
# noise. Tune via the function parameter; not exposed as an env var to
# keep the trigger contract explicit at the call site.
DEFAULT_OVERAGE_RATIO = 1.5

# Snapshots older than this don't fire the detector. Codex zigzag (b)#I1
# (2026-05-29): acting on a stale snapshot misleads the user about how
# much unallocated cash they actually have (likely already allocated).
DEFAULT_STALENESS_DAYS = 45


@dataclass
class UnallocatedCashEvent:
    """One detector firing.

    Distinct from WindfallEvent: this isn't a *delta* (no cross-month
    comparison); it's a current-state observation. The "amount to
    allocate" is the excess over plan-target, not a cash-delta.
    """
    detected_at: datetime
    snapshot_date: str | None
    current_cash_k_usd: float
    target_cash_k_usd: float
    overage_ratio: float
    excess_usd: float
    proposals: list[AllocationProposal]
    allocation_table: list[AllocationLine]
    headline: str

    def to_dict(self) -> dict:
        return {
            "detected_at": self.detected_at.isoformat(),
            "snapshot_date": self.snapshot_date,
            "current_cash_k_usd": round(self.current_cash_k_usd, 2),
            "target_cash_k_usd": round(self.target_cash_k_usd, 2),
            "overage_ratio": round(self.overage_ratio, 3),
            "excess_usd": round(self.excess_usd, 2),
            "headline": self.headline,
            "proposals": [p.to_dict() for p in self.proposals],
            "allocation_delta_table": [
                {
                    "asset_class": l.asset_class,
                    "current_pct": l.current_pct,
                    "current_k_usd": l.current_k_usd,
                    "target_pct": l.target_pct,
                    "target_k_usd": l.target_k_usd,
                    "delta_k_usd": l.delta_k_usd,
                }
                for l in self.allocation_table
            ],
        }


def detect_unallocated_cash_overage(
    db: Session,
    *,
    user_id: str,
    overage_ratio: float = DEFAULT_OVERAGE_RATIO,
    staleness_days: int = DEFAULT_STALENESS_DAYS,
    today: date | None = None,
) -> UnallocatedCashEvent | None:
    """Return an event if current cash exceeds plan-target cash by the ratio.

    Reads the latest portfolio_snapshot from the DB. Returns None when:
      * No snapshot exists for the user.
      * Snapshot is older than ``staleness_days`` (codex zigzag (b)#I1).
      * No "Current allocation" block in the snapshot.
      * No cash row (or cash row has no target).
      * Current cash <= target cash * overage_ratio.
      * Excess is non-positive (defensive; shouldn't happen if the
        ratio gate fires but the math is on the wire so guard anyway).
    """
    row = get_latest_snapshot_row(db, user_id=user_id)
    if row is None:
        return None
    snapshot = row_to_snapshot(row)
    # Staleness guard: snapshots older than the threshold don't fire.
    # ``today`` is injectable for testing; production callers omit it.
    if snapshot.snapshot_date is not None:
        if today is None:
            today = datetime.now(timezone.utc).date()
        if (today - snapshot.snapshot_date).days > staleness_days:
            return None
    return _detect_from_snapshot(snapshot, overage_ratio=overage_ratio)


def _detect_from_snapshot(
    snapshot: PortfolioSnapshot,
    *,
    overage_ratio: float = DEFAULT_OVERAGE_RATIO,
) -> UnallocatedCashEvent | None:
    """Pure detector logic -- broken out from the DB-fetching wrapper so
    tests can exercise the math without seeding the DB."""
    if not snapshot.allocations:
        return None
    cash_row = _find_cash_row(snapshot.allocations)
    if cash_row is None or cash_row.target_k is None or cash_row.target_k <= 0:
        return None
    current_k = cash_row.usd_value_k or 0.0
    target_k = cash_row.target_k
    if target_k <= 0:
        return None
    ratio = current_k / target_k
    if ratio < overage_ratio:
        return None
    excess_usd = (current_k - target_k) * 1000.0
    if excess_usd <= 0:
        return None

    # Convert AllocationRow -> AllocationLine for the allocator.
    allocation_table = [
        _row_to_line(r, snapshot)
        for r in snapshot.allocations
        if r.target_pct is not None
    ]

    # Allocate 100% of the excess to long-term proposals (no medium/short
    # placeholders -- the unallocated-cash flow is about concrete buy
    # suggestions, not horizon split).
    proposals, _remaining = _allocate_long_term(
        excess_usd, allocation_table,
        long_term_budget_fraction=1.0,
    )

    headline = (
        f"Cash sits {ratio:.1f}x your plan target "
        f"({current_k:.0f}K vs {target_k:.0f}K). Proposed allocation closes "
        f"the largest plan-target gaps."
    )

    return UnallocatedCashEvent(
        detected_at=datetime.now(timezone.utc),
        snapshot_date=(
            snapshot.snapshot_date.isoformat() if snapshot.snapshot_date else None
        ),
        current_cash_k_usd=current_k,
        target_cash_k_usd=target_k,
        overage_ratio=ratio,
        excess_usd=excess_usd,
        proposals=proposals,
        allocation_table=allocation_table,
        headline=headline,
    )


def _find_cash_row(allocations: list[AllocationRow]) -> AllocationRow | None:
    """Find the cash row in the allocation block.

    The TSV's category column for cash is typically literally "Cash"; we
    also accept "cash & equivalents" / similar variants by substring."""
    for r in allocations:
        cat = (r.category or "").lower()
        if "cash" in cat and "equiv" not in cat:
            return r
        if cat == "cash":
            return r
    # Fallback to substring match.
    for r in allocations:
        if "cash" in (r.category or "").lower():
            return r
    return None


def _row_to_line(row: AllocationRow, snapshot: PortfolioSnapshot) -> AllocationLine:
    """Convert AllocationRow (TSV parser) -> AllocationLine (allocator input).

    Field name differences:
      AllocationRow.pct          <-> AllocationLine.current_pct
      AllocationRow.usd_value_k  <-> AllocationLine.current_k_usd
      AllocationRow.target_pct   <-> AllocationLine.target_pct
      AllocationRow.target_k     <-> AllocationLine.target_k_usd
      AllocationRow.delta_k      <-> AllocationLine.delta_k_usd

    The TSV parser computes delta_k = target_k - current_k when present;
    fall back to that when delta_k is missing.
    """
    current_k = row.usd_value_k or 0.0
    target_k = row.target_k or 0.0
    delta_k = row.delta_k if row.delta_k is not None else (target_k - current_k)
    return AllocationLine(
        asset_class=row.category,
        current_pct=row.pct or 0.0,
        current_k_usd=current_k,
        target_pct=row.target_pct or 0.0,
        target_k_usd=target_k,
        delta_k_usd=delta_k,
    )


__all__ = [
    "DEFAULT_OVERAGE_RATIO",
    "UnallocatedCashEvent",
    "detect_unallocated_cash_overage",
]
