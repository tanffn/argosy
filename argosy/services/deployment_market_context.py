"""Deployment market context — freshness + market-context dataclasses + staleness helpers.

Provides the value-object layer for the P2 deployment advisor market awareness:
- ``DataFreshness`` — per-feed age + staleness flag
- ``NvdaVerification`` — NVDA price/shares/market_cap consistency record
- ``DeploymentMarketContext`` — assembles snapshot + freshness tuple + NVDA verify

See docs/superpowers/plans/2026-06-12-deployment-advisor-p2.md §Pinned technical definitions.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DataFreshness:
    """Per-field freshness record surfaced in the market context."""

    field: str
    fetched_at: str           # ISO-8601 timestamp string (UTC)
    age_seconds: float
    source: str
    is_stale: bool


@dataclass(frozen=True)
class NvdaVerification:
    """NVDA price / share-count / market-cap consistency record.

    ``consistent`` is ``True`` when ``abs(market_cap/shares - price)/price <= 0.10``,
    ``False`` when drift > 10%, or ``None`` when shares or market_cap is
    missing / non-positive (never silently consistent).
    """

    price: float
    shares: float | None
    market_cap: float | None
    consistent: bool | None   # None = data missing; never silently True
    note: str


@dataclass(frozen=True)
class DeploymentMarketContext:
    """Assembled market context snapshot for the deployment advisor."""

    snapshot: dict[str, float]
    freshness: tuple[DataFreshness, ...]
    nvda: NvdaVerification | None
    overall_age_label: str

    @property
    def is_any_stale(self) -> bool:
        """True if any freshness entry is stale, or if NVDA data is present but not consistent."""
        if any(f.is_stale for f in self.freshness):
            return True
        if self.nvda is not None and not self.nvda.consistent:
            return True
        return False
