"""Deployment market context — freshness + market-context dataclasses + staleness helpers.

Provides the value-object layer for the P2 deployment advisor market awareness:
- ``DataFreshness`` — per-feed age + staleness flag
- ``NvdaVerification`` — NVDA price/shares/market_cap consistency record
- ``DeploymentMarketContext`` — assembles snapshot + freshness tuple + NVDA verify
- ``DEPLOY_FRESHNESS_MAX_AGE`` — per-feed TTL config constants
- ``is_stale`` — boundary-correct staleness predicate
- ``nvda_consistency`` — hard consistency check per pinned doctrine (None = missing data)
- ``verify_nvda`` — fetch live NVDA quote+fundamentals and return NvdaVerification
- ``assemble_deployment_market_context`` — live + cached-fallback assembler (Task 5)

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


# ---------------------------------------------------------------------------
# Task 5: assemble_deployment_market_context — live + cached-fallback assembler
# ---------------------------------------------------------------------------

# Default user_id for the single-user deployment (matches the seeded user).
_DEFAULT_USER_ID = "ariel"

# AgentReport roles that carry macro / FX context.
_CACHE_ROLES = ("macro", "fx")


def _human_age(age_seconds: float) -> str:
    """Format an age in seconds as a human-readable string (e.g. '3h ago')."""
    if age_seconds < 60:
        return f"{int(age_seconds)}s ago"
    if age_seconds < 3600:
        return f"{int(age_seconds / 60)}m ago"
    if age_seconds < 86_400:
        return f"{int(age_seconds / 3600)}h ago"
    return f"{int(age_seconds / 86_400)}d ago"


def _query_latest_agent_report(
    session: Any,
    user_id: str,
    role: str,
) -> Any | None:
    """Query the latest AgentReport row for the given user + role.

    Works with a synchronous SQLAlchemy Session (the only context this
    assembler is called from — the route is sync via a sync Depends(get_db)
    session).  Never raises; returns None on any error or if no rows found.
    """
    from argosy.logging import get_logger

    _log = get_logger("argosy.services.deployment_market_context")

    try:
        from sqlalchemy import desc, select

        from argosy.state.models import AgentReport as AgentReportRow

        row = session.execute(
            select(AgentReportRow)
            .where(AgentReportRow.user_id == user_id)
            .where(AgentReportRow.agent_role == role)
            .order_by(desc(AgentReportRow.created_at))
            .limit(1)
        ).scalars().first()
        return row
    except Exception as exc:
        _log.warning(
            "deployment_market_context.agent_report_query_failed",
            role=role,
            error=str(exc)[:200],
        )
        return None


def _freshness_from_cache(
    field: str,
    role: str,
    age_seconds: float,
) -> DataFreshness:
    """Build a DataFreshness from a cached AgentReport age."""
    from datetime import datetime, timezone

    fetched_at = (
        datetime.now(timezone.utc).isoformat()
    )
    stale = is_stale(age_seconds, DEPLOY_FRESHNESS_MAX_AGE.get(role, DEPLOY_FRESHNESS_MAX_AGE["macro"]))
    return DataFreshness(
        field=field,
        fetched_at=fetched_at,
        age_seconds=age_seconds,
        source=f"agent_reports:{role}",
        is_stale=stale,
    )


def _build_cached_context(session: Any, user_id: str) -> "DeploymentMarketContext":
    """Build a DeploymentMarketContext from the latest cached AgentReport rows.

    Age is always computed from ``created_at`` and surfaced in ``overall_age_label``.
    Snapshot values default to 0.0 when the cached report JSON cannot be parsed
    (the spec requirement is age-surfacing, not perfect numeric extraction).
    Never raises.
    """
    import json
    from datetime import datetime, timezone

    from argosy.logging import get_logger

    _log = get_logger("argosy.services.deployment_market_context")

    now = datetime.now(timezone.utc)
    snapshot: dict[str, float] = {}
    freshness_list: list[DataFreshness] = []

    # Field-to-role mapping: which AgentReport role supplies which snapshot key.
    role_fields: dict[str, list[str]] = {
        "macro": ["sp500", "vix", "oil_wti", "boi_rate", "cpi_yoy"],
        "fx": ["usd_nis"],
    }

    max_age_seen: float = 0.0

    for role in _CACHE_ROLES:
        fields = role_fields.get(role, [])
        row = _query_latest_agent_report(session, user_id, role)

        if row is None:
            # No cached row — mark all fields for this role as stale/missing.
            age = float(DEPLOY_FRESHNESS_MAX_AGE.get(role, DEPLOY_FRESHNESS_MAX_AGE["macro"]) + 1)
            for field in fields:
                snapshot[field] = 0.0
                freshness_list.append(
                    DataFreshness(
                        field=field,
                        fetched_at=now.isoformat(),
                        age_seconds=age,
                        source=f"agent_reports:{role}:no_row",
                        is_stale=True,
                    )
                )
            max_age_seen = max(max_age_seen, age)
            continue

        # Compute real age from created_at.
        try:
            created_at = row.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_seconds = (now - created_at).total_seconds()
        except Exception:
            age_seconds = float(DEPLOY_FRESHNESS_MAX_AGE["macro"] + 1)

        max_age_seen = max(max_age_seen, age_seconds)

        # Attempt to parse numeric values from response_text JSON.
        parsed: dict[str, float] = {}
        try:
            data = json.loads(row.response_text or "{}")
            # The agent report may carry the snapshot at various nesting levels.
            # Try top-level keys first, then look inside common wrappers.
            candidate_dicts: list[dict] = [data]
            for wrapper_key in ("snapshot", "macro", "market", "context", "data"):
                if isinstance(data.get(wrapper_key), dict):
                    candidate_dicts.append(data[wrapper_key])
            for d in candidate_dicts:
                for field in fields:
                    if field in d:
                        try:
                            parsed[field] = float(d[field])
                        except (TypeError, ValueError):
                            pass
        except Exception as exc:
            _log.debug(
                "deployment_market_context.cached_parse_failed",
                role=role,
                error=str(exc)[:100],
            )

        for field in fields:
            snapshot[field] = parsed.get(field, 0.0)
            freshness_list.append(_freshness_from_cache(field, role, age_seconds))

    # Ensure all six canonical keys are present.
    for key in ("sp500", "vix", "oil_wti", "usd_nis", "boi_rate", "cpi_yoy"):
        if key not in snapshot:
            snapshot[key] = 0.0
            role = "fx" if key == "usd_nis" else "macro"
            freshness_list.append(
                DataFreshness(
                    field=key,
                    fetched_at=now.isoformat(),
                    age_seconds=float(DEPLOY_FRESHNESS_MAX_AGE[role] + 1),
                    source=f"agent_reports:{role}:missing",
                    is_stale=True,
                )
            )

    if max_age_seen <= 0:
        age_label = "cached (unknown age)"
    else:
        age_label = f"cached ({_human_age(max_age_seen)})"

    return DeploymentMarketContext(
        snapshot=snapshot,
        freshness=tuple(freshness_list),
        nvda=None,
        overall_age_label=age_label,
    )


def assemble_deployment_market_context(
    session: Any,
    *,
    allow_live: bool = True,
    user_id: str = _DEFAULT_USER_ID,
) -> "DeploymentMarketContext":
    """Assemble a :class:`DeploymentMarketContext` for the deployment advisor.

    Live path (``allow_live=True``):
        1. Call ``market_snapshot(session)`` to get the six macro/FX fields.
        2. Call ``verify_nvda(session)`` to verify NVDA price/shares.
        3. Build the context from the live results; ``age_seconds ≈ 0``
           (freshly fetched), ``overall_age_label = "live"``.

    Cached fallback (``allow_live=False`` OR any live exception):
        - Query the latest ``AgentReport`` row for roles ``"macro"`` and ``"fx"``.
        - Compute real age from ``created_at`` → stamp each ``DataFreshness``.
        - Surface the age in ``overall_age_label`` (e.g. ``"cached (3h ago)"``).
        - ``nvda`` is set to ``None`` (no live fetch; cached path cannot verify).

    Contract:
        - **Never returns a blank context.** When no cached rows exist, all
          snapshot values are 0.0 and all freshness entries are ``is_stale=True``,
          but the context object is always returned.
        - **Age is always surfaced.** Stale data is never silently passed through.
        - Never raises.

    Args:
        session: A synchronous SQLAlchemy ``Session`` (used for AgentReport
            queries on the fallback path). Passed through to the live helpers
            for API consistency; not used directly on the live path.
        allow_live: When ``False``, skip the live fetch and go straight to
            the cached fallback.
        user_id: The user whose AgentReport rows to query. Defaults to
            ``"ariel"`` (the single-tenant deployment user).

    Returns:
        A :class:`DeploymentMarketContext` instance.
    """
    import argosy.services.market_snapshot as _ms_module

    from argosy.logging import get_logger

    _log = get_logger("argosy.services.deployment_market_context")

    if allow_live:
        try:
            snap = _ms_module.market_snapshot(session)
            nvda = verify_nvda(session)

            snapshot: dict[str, float] = {}
            freshness_list: list[DataFreshness] = []
            for key, (value, df) in snap.items():
                snapshot[key] = value
                freshness_list.append(df)

            return DeploymentMarketContext(
                snapshot=snapshot,
                freshness=tuple(freshness_list),
                nvda=nvda,
                overall_age_label="live",
            )
        except Exception as exc:
            _log.warning(
                "deployment_market_context.live_fetch_failed_falling_back",
                error=str(exc)[:200],
            )
            # Fall through to cached path.

    return _build_cached_context(session, user_id)
