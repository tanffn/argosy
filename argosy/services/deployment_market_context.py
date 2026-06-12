"""Deployment market context — freshness + market-context dataclasses + staleness helpers.

Provides the value-object layer for the P2 deployment advisor market awareness:
- ``DataFreshness`` — per-feed age + staleness flag
- ``NvdaVerification`` — NVDA price/shares/market_cap consistency record
- ``DeploymentMarketContext`` — assembles snapshot + freshness tuple + NVDA verify
- ``DEPLOY_FRESHNESS_MAX_AGE`` — per-feed TTL config constants
- ``is_stale`` — boundary-correct staleness predicate
- ``nvda_consistency`` — hard consistency check per pinned doctrine (None = missing data)
- ``verify_nvda`` — fetch live NVDA quote+fundamentals and return NvdaVerification

See docs/superpowers/plans/2026-06-12-deployment-advisor-p2.md §Pinned technical definitions.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from argosy.adapters.data.yfinance_adapter import YFinanceAdapter


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


# ---------------------------------------------------------------------------
# Task 4: verify_nvda — live NVDA price/shares/market_cap verification
# ---------------------------------------------------------------------------


def verify_nvda(session: Any) -> NvdaVerification:
    """Fetch NVDA price + shares outstanding + market cap and verify consistency.

    Uses ``YFinanceAdapter.get_quote_with_fundamentals`` (async, bridged via
    ``asyncio.run`` mirroring the ``inputs.py`` pattern).

    Returns a ``NvdaVerification`` in all cases — never raises:
    - If the fetch succeeds: ``consistent`` is the result of ``nvda_consistency``.
    - If the fetch fails: price=0.0, shares=None, market_cap=None, consistent=None,
      note describes the error.

    The ``session`` parameter is accepted for API consistency and future use
    (e.g. looking up cached price from the DB when the live fetch fails).
    """
    from argosy.logging import get_logger

    _log = get_logger("argosy.services.deployment_market_context")

    try:
        adapter = YFinanceAdapter()
        data: dict[str, Any] = asyncio.run(
            adapter.get_quote_with_fundamentals("NVDA")
        )
        price_raw = data.get("price")
        shares_raw = data.get("shares")
        mc_raw = data.get("market_cap")

        price: float = float(price_raw) if price_raw is not None else 0.0
        shares: float | None = float(shares_raw) if shares_raw is not None else None
        market_cap: float | None = float(mc_raw) if mc_raw is not None else None

        consistent = nvda_consistency(price, shares, market_cap)
        if consistent is True:
            drift_note = "consistent: market_cap/shares within 10% of price"
        elif consistent is False:
            implied = (market_cap / shares) if (shares and market_cap) else None
            drift_pct = (
                abs(implied - price) / price * 100.0
                if implied is not None and price > 0
                else None
            )
            drift_note = (
                f"INCONSISTENT: implied price ${implied:.2f} "
                f"vs live ${price:.2f} "
                f"({drift_pct:.1f}% drift)" if drift_pct is not None else "INCONSISTENT: drift > 10%"
            )
        else:
            missing = []
            if shares is None:
                missing.append("shares")
            if market_cap is None:
                missing.append("market_cap")
            drift_note = f"data missing: {', '.join(missing) or 'unknown'} — cannot verify"

        return NvdaVerification(
            price=price,
            shares=shares,
            market_cap=market_cap,
            consistent=consistent,
            note=drift_note,
        )

    except Exception as exc:
        _log.warning(
            "deployment_market_context.verify_nvda_failed",
            error=str(exc)[:200],
        )
        return NvdaVerification(
            price=0.0,
            shares=None,
            market_cap=None,
            consistent=None,
            note=f"fetch failed: {exc!s:.120}",
        )
