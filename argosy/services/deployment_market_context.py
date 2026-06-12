"""Deployment market context — freshness + market-context dataclasses + staleness helpers.

Provides the value-object layer for the P2 deployment advisor market awareness:
- ``DataFreshness`` — per-feed age + staleness flag
- ``NvdaVerification`` — NVDA price/shares/market_cap consistency record
- ``DeploymentMarketContext`` — assembles snapshot + freshness tuple + NVDA verify
- ``DEPLOY_FRESHNESS_MAX_AGE`` — per-feed TTL config constants
- ``is_stale`` — boundary-correct staleness predicate
- ``nvda_consistency`` — hard consistency check per pinned doctrine (None = missing data)

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


# ---------------------------------------------------------------------------
# Task 2: config constants + staleness helpers
# ---------------------------------------------------------------------------

# Per-feed max-age TTLs in seconds (named config constants — no magic numbers).
DEPLOY_FRESHNESS_MAX_AGE: dict[str, int] = {
    "quotes": 900,       # 15 minutes
    "macro": 86_400,     # 24 hours
    "fx": 86_400,        # 24 hours
    "news": 172_800,     # 48 hours
}


def is_stale(age_seconds: float, max_age_seconds: float) -> bool:
    """Return True when ``age_seconds`` strictly exceeds ``max_age_seconds``.

    Boundary: exactly at the TTL is NOT stale (``age_seconds == max_age_seconds``
    returns False), consistent with the trust-data-feed doctrine of not flagging
    data as stale unless there is a demonstrable reason.
    """
    return age_seconds > max_age_seconds


def nvda_consistency(
    price: float,
    shares: float | None,
    market_cap: float | None,
) -> bool | None:
    """Check NVDA price / shares / market_cap internal consistency.

    Returns:
        True  — ``abs(market_cap/shares - price) / price <= 0.10``
        False — drift > 10%
        None  — shares or market_cap is missing or <= 0 (never silently True)

    The 10% threshold is the pinned consistency rule from the P2 spec
    (§Pinned technical definitions, item 1 / item 4).
    """
    if not shares or not market_cap or shares <= 0 or market_cap <= 0:
        return None
    implied_price = market_cap / shares
    drift = abs(implied_price - price) / price
    return drift <= 0.10
