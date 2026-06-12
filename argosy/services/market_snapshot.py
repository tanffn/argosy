"""Market snapshot helper — Task 3 of Deployment Advisor P2.

Returns a dict mapping canonical field names to (value, DataFreshness) pairs.
All six fields are always present; missing/failed series are represented with
a 0.0 value and ``is_stale=True`` so callers never crash on missing data.

Keys:
  - ``sp500``    — S&P 500 index level (FRED SP500)
  - ``vix``      — CBOE VIX (FRED VIXCLS)
  - ``oil_wti``  — WTI crude oil USD/bbl (FRED DCOILWTICO)
  - ``usd_nis``  — USD/NIS spot rate (BoI API → FRED fallback)
  - ``boi_rate`` — Bank of Israel policy rate % (FRED IRSTCI01ILM156N)
  - ``cpi_yoy``  — US CPI YoY % change (FRED CPIAUCSL, computed from level index)

Invocation pattern mirrors ``argosy.orchestrator.flows.plan_synthesis.inputs``
(synchronous function, uses ``asyncio.run`` to bridge async adapters).
The ``session`` parameter is accepted for API consistency but is not used
in the current implementation (all data comes via the FRED/BoI adapters).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from argosy.adapters.data.boi_adapter import BoiAdapter
from argosy.adapters.data.fred_adapter import FredAdapter
from argosy.logging import get_logger
from argosy.services.deployment_market_context import (
    DEPLOY_FRESHNESS_MAX_AGE,
    DataFreshness,
    is_stale,
)

_log = get_logger("argosy.services.market_snapshot")

# ---------------------------------------------------------------------------
# FRED series IDs
# ---------------------------------------------------------------------------
_SERIES_VIX = "VIXCLS"
_SERIES_OIL_WTI = "DCOILWTICO"
_SERIES_SP500 = "SP500"
_SERIES_CPI = "CPIAUCSL"
_SERIES_BOI_RATE = "IRSTCI01ILM156N"  # Bank of Israel overnight rate, monthly

# Max age for macro series (24 h).
_MACRO_MAX_AGE = DEPLOY_FRESHNESS_MAX_AGE["macro"]
_FX_MAX_AGE = DEPLOY_FRESHNESS_MAX_AGE["fx"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _freshness_now(field: str, source: str) -> DataFreshness:
    """Return a DataFreshness stamped 'right now' (age ≈ 0, not stale)."""
    return DataFreshness(
        field=field,
        fetched_at=_utcnow().isoformat(),
        age_seconds=0.0,
        source=source,
        is_stale=False,
    )


def _freshness_missing(field: str, reason: str) -> DataFreshness:
    """Return a DataFreshness for a missing series (is_stale=True)."""
    # Age is arbitrarily large so callers that check age see it as stale.
    return DataFreshness(
        field=field,
        fetched_at=_utcnow().isoformat(),
        age_seconds=float(_MACRO_MAX_AGE + 1),
        source=f"MISSING:{reason}",
        is_stale=True,
    )


def _latest_value(rows: list[dict[str, Any]]) -> float | None:
    """Return the most recent non-None float value from a FRED series result."""
    for row in reversed(rows or []):
        v = row.get("value") if isinstance(row, dict) else None
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _compute_cpi_yoy(rows: list[dict[str, Any]]) -> tuple[float, bool]:
    """Compute CPI year-over-year % change from a FRED index level series.

    Picks the latest observation as the current value, then finds the
    observation closest to 12 months prior as the year-ago anchor.

    Returns:
        (yoy_pct, ok) where ok=True when the calculation was possible.
        Returns (0.0, False) when there is insufficient history.
    """
    if not rows:
        return 0.0, False

    # Gather (date_str, value) pairs — filter out None values.
    dated: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = row.get("date")
        v = row.get("value")
        if d is None or v is None:
            continue
        try:
            dated.append((str(d), float(v)))
        except (TypeError, ValueError):
            continue

    if len(dated) < 2:
        return 0.0, False

    # Sort ascending by date string (ISO format sorts lexicographically).
    dated.sort(key=lambda x: x[0])

    latest_date, latest_val = dated[-1]

    # Find the observation closest to 12 months before the latest date.
    # Compare as strings: derive year-1 version for a crude 12-month target.
    try:
        from datetime import date as _date
        latest_dt = _date.fromisoformat(latest_date[:10])
    except ValueError:
        return 0.0, False

    target_year_ago = _date(
        latest_dt.year - 1,
        latest_dt.month,
        latest_dt.day if latest_dt.day <= 28 else 28,
    )

    best_row: tuple[str, float] | None = None
    best_delta = float("inf")
    for d_str, v in dated[:-1]:  # exclude the latest
        try:
            dt = _date.fromisoformat(d_str[:10])
        except ValueError:
            continue
        delta = abs((dt - target_year_ago).days)
        if delta < best_delta:
            best_delta = delta
            best_row = (d_str, v)

    if best_row is None or best_delta > 60:  # >2 months off — too imprecise
        return 0.0, False

    year_ago_val = best_row[1]
    if year_ago_val == 0.0:
        return 0.0, False

    yoy = (latest_val / year_ago_val - 1.0) * 100.0
    return yoy, True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _compute_sp_vs_trend(rows: list[dict[str, Any]], window: int = 200) -> tuple[float, bool]:
    """S&P % deviation from its trailing ``window``-observation simple mean.

    `(latest - mean(last window obs)) / mean * 100`. The FRED SP500 series is a
    daily (business-day) index level, so ~200 observations ≈ the 200-day trend.
    Returns (pct, ok); ok=False when there is too little history to be meaningful.
    """
    vals: list[float] = []
    for row in rows or []:
        v = row.get("value") if isinstance(row, dict) else None
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(vals) < max(50, window // 4):
        return 0.0, False
    latest = vals[-1]
    trailing = vals[-window:]
    ma = sum(trailing) / len(trailing)
    if ma <= 0:
        return 0.0, False
    return (latest - ma) / ma * 100.0, True


def market_snapshot(
    session: Any,
) -> dict[str, tuple[float, DataFreshness]]:
    """Return the six-field market snapshot dict.

    Each value is ``(float_value, DataFreshness)``. Keys:
    ``sp500, vix, oil_wti, usd_nis, boi_rate, cpi_yoy``.

    Missing or failed series → ``(0.0, DataFreshness(is_stale=True))``.
    Never raises.
    """
    fred = FredAdapter()
    boi = BoiAdapter()
    result: dict[str, tuple[float, DataFreshness]] = {}

    # --- FRED series (synchronous via asyncio.run, mirroring inputs.py) ---
    _fred_series: list[tuple[str, str]] = [
        ("vix", _SERIES_VIX),
        ("oil_wti", _SERIES_OIL_WTI),
        ("sp500", _SERIES_SP500),
        ("boi_rate", _SERIES_BOI_RATE),
        ("cpi_yoy", _SERIES_CPI),  # special: needs YoY computation
    ]

    for field, series_id in _fred_series:
        try:
            rows = asyncio.run(fred.get_series(series_id))
        except Exception as exc:
            _log.warning(
                "market_snapshot.fred_series_failed",
                field=field,
                series=series_id,
                error=str(exc)[:200],
            )
            result[field] = (0.0, _freshness_missing(field, f"{series_id}:{exc!s:.60}"))
            continue

        if field == "cpi_yoy":
            yoy, ok = _compute_cpi_yoy(rows)
            if not ok:
                result[field] = (
                    0.0,
                    _freshness_missing(field, f"{series_id}:insufficient_history"),
                )
            else:
                result[field] = (
                    yoy,
                    _freshness_now(field, f"fred:{series_id}:yoy_computed"),
                )
        else:
            val = _latest_value(rows)
            if val is None:
                result[field] = (0.0, _freshness_missing(field, f"{series_id}:no_data"))
            else:
                result[field] = (val, _freshness_now(field, f"fred:{series_id}"))
            if field == "sp500":
                trend_pct, ok = _compute_sp_vs_trend(rows)
                if ok:
                    result["sp_vs_trend_pct"] = (
                        trend_pct, _freshness_now("sp_vs_trend_pct", f"fred:{series_id}:ma200"),
                    )
                else:
                    result["sp_vs_trend_pct"] = (
                        0.0,
                        _freshness_missing("sp_vs_trend_pct", f"{series_id}:insufficient_history"),
                    )

    # --- USD/NIS via BoI adapter ---
    try:
        fx_data = asyncio.run(boi.get_usd_nis())
        rate = float(fx_data.get("rate", 0.0))
        source = fx_data.get("source", "boi")
        as_of = fx_data.get("as_of", "")
        if rate <= 0:
            result["usd_nis"] = (0.0, _freshness_missing("usd_nis", "boi:rate_zero"))
        else:
            result["usd_nis"] = (
                rate,
                DataFreshness(
                    field="usd_nis",
                    fetched_at=_utcnow().isoformat(),
                    age_seconds=0.0,
                    source=f"{source}:{as_of}" if as_of else source,
                    is_stale=is_stale(0.0, _FX_MAX_AGE),
                ),
            )
    except Exception as exc:
        _log.warning(
            "market_snapshot.boi_failed",
            error=str(exc)[:200],
        )
        result["usd_nis"] = (0.0, _freshness_missing("usd_nis", f"boi:{exc!s:.60}"))

    # Ensure all six keys always present (defensive).
    for key in ("sp500", "sp_vs_trend_pct", "vix", "oil_wti", "usd_nis", "boi_rate", "cpi_yoy"):
        if key not in result:
            result[key] = (0.0, _freshness_missing(key, "unexpected_missing"))

    return result


__all__ = ["market_snapshot"]
