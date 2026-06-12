"""Tests for market_snapshot — Task 3.

All tests are pure (monkeypatched adapters, no network, no DB).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from argosy.services.deployment_market_context import DataFreshness
from argosy.services.market_snapshot import market_snapshot


# ---------------------------------------------------------------------------
# Helpers to build fake adapter return values
# ---------------------------------------------------------------------------


def _fred_rows(*values: tuple[str, float]) -> list[dict[str, Any]]:
    """Build a FRED-style list[{date, value}] with the given (date, value) pairs."""
    return [{"date": d, "value": v} for d, v in values]


def _make_fred_adapter(series_map: dict[str, list[dict[str, Any]]]) -> Any:
    """Return a fake FredAdapter whose get_series is an AsyncMock."""
    adapter = MagicMock()

    async def _get_series(series_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return series_map.get(series_id, [])

    adapter.get_series = _get_series
    return adapter


def _make_boi_adapter(rate: float = 3.65, source: str = "boi") -> Any:
    """Return a fake BoiAdapter whose get_usd_nis is an AsyncMock."""
    adapter = MagicMock()

    async def _get_usd_nis(**kwargs: Any) -> dict[str, Any]:
        return {"rate": rate, "source": source, "as_of": "2026-06-12"}

    adapter.get_usd_nis = _get_usd_nis
    return adapter


# ---------------------------------------------------------------------------
# Nominal path — all series available
# ---------------------------------------------------------------------------


FRED_DATA: dict[str, list[dict[str, Any]]] = {
    "VIXCLS": _fred_rows(("2026-06-10", 17.5), ("2026-06-11", 18.2)),
    "DCOILWTICO": _fred_rows(("2026-06-10", 77.3), ("2026-06-11", 78.0)),
    "SP500": _fred_rows(("2026-06-10", 5350.0), ("2026-06-11", 5400.0)),
    "CPIAUCSL": _fred_rows(
        ("2025-05-01", 310.0),   # ~12 months ago
        ("2026-05-01", 317.0),   # latest
    ),
    "IRSTCI01ILM156N": _fred_rows(("2026-04-01", 4.5), ("2026-05-01", 4.25)),
}

# S&P needs >=50 observations for the 200-day trend computation; give the nominal
# fixture a realistic ~60-point ramp ending at 5400 (so sp500 latest stays 5400).
_sp_base = datetime(2026, 3, 1, tzinfo=timezone.utc).date()
FRED_DATA["SP500"] = [
    {"date": (_sp_base + __import__("datetime").timedelta(days=i)).isoformat(),
     "value": float(5341 + i)}
    for i in range(60)
]


class TestMarketSnapshotNominal:
    """All adapters return data — verify values and DataFreshness stamps."""

    @pytest.fixture(autouse=True)
    def _patch_adapters(self, monkeypatch):
        fred_adapter = _make_fred_adapter(FRED_DATA)
        boi_adapter = _make_boi_adapter(rate=3.65)

        monkeypatch.setattr(
            "argosy.services.market_snapshot.FredAdapter",
            lambda **kw: fred_adapter,
        )
        monkeypatch.setattr(
            "argosy.services.market_snapshot.BoiAdapter",
            lambda **kw: boi_adapter,
        )

    def test_returns_all_six_keys(self):
        result = market_snapshot(session=None)
        assert set(result.keys()) == {"sp500", "sp_vs_trend_pct", "vix", "oil_wti", "usd_nis", "boi_rate", "cpi_yoy"}

    def test_vix_value(self):
        result = market_snapshot(session=None)
        val, fresh = result["vix"]
        assert val == pytest.approx(18.2)

    def test_oil_wti_value(self):
        result = market_snapshot(session=None)
        val, fresh = result["oil_wti"]
        assert val == pytest.approx(78.0)

    def test_sp500_value(self):
        result = market_snapshot(session=None)
        val, fresh = result["sp500"]
        assert val == pytest.approx(5400.0)

    def test_usd_nis_value(self):
        result = market_snapshot(session=None)
        val, fresh = result["usd_nis"]
        assert val == pytest.approx(3.65)

    def test_boi_rate_value(self):
        result = market_snapshot(session=None)
        val, fresh = result["boi_rate"]
        assert val == pytest.approx(4.25)

    def test_cpi_yoy_computed(self):
        """CPI YoY = (317/310 - 1) * 100 ≈ 2.258%."""
        result = market_snapshot(session=None)
        val, fresh = result["cpi_yoy"]
        assert val == pytest.approx((317.0 / 310.0 - 1.0) * 100.0, abs=0.01)

    def test_all_values_are_floats(self):
        result = market_snapshot(session=None)
        for key, (val, fresh) in result.items():
            assert isinstance(val, float), f"{key}: expected float, got {type(val)}"

    def test_all_freshness_are_datafreshness(self):
        result = market_snapshot(session=None)
        for key, (val, fresh) in result.items():
            assert isinstance(fresh, DataFreshness), f"{key}: expected DataFreshness, got {type(fresh)}"

    def test_freshness_field_names_match_keys(self):
        result = market_snapshot(session=None)
        for key, (val, fresh) in result.items():
            assert fresh.field == key

    def test_freshness_not_stale_for_fresh_data(self):
        result = market_snapshot(session=None)
        for key, (val, fresh) in result.items():
            # When adapters succeed with fresh data, is_stale should be False
            # (age_seconds will be ~0 since we just fetched)
            assert not fresh.is_stale, f"{key} unexpectedly stale"

    def test_freshness_source_contains_provider(self):
        result = market_snapshot(session=None)
        # FRED-sourced series
        for key in ("vix", "oil_wti", "sp500", "boi_rate", "cpi_yoy"):
            _, fresh = result[key]
            assert "fred" in fresh.source.lower(), f"{key}: expected fred in source, got {fresh.source}"
        # USD/NIS from BoI adapter
        _, fresh = result["usd_nis"]
        assert fresh.source  # non-empty


# ---------------------------------------------------------------------------
# Graceful degradation — series unavailable
# ---------------------------------------------------------------------------


class TestMarketSnapshotMissingData:
    """Verify that missing/empty series produce (0.0, stale DataFreshness) not an exception."""

    @pytest.fixture(autouse=True)
    def _patch_adapters_empty(self, monkeypatch):
        fred_adapter = _make_fred_adapter({})  # no series data
        boi_adapter = _make_boi_adapter(rate=3.65)

        monkeypatch.setattr(
            "argosy.services.market_snapshot.FredAdapter",
            lambda **kw: fred_adapter,
        )
        monkeypatch.setattr(
            "argosy.services.market_snapshot.BoiAdapter",
            lambda **kw: boi_adapter,
        )

    def test_still_returns_all_six_keys(self):
        result = market_snapshot(session=None)
        assert set(result.keys()) == {"sp500", "sp_vs_trend_pct", "vix", "oil_wti", "usd_nis", "boi_rate", "cpi_yoy"}

    def test_missing_fred_series_flagged_stale(self):
        result = market_snapshot(session=None)
        for key in ("vix", "oil_wti", "sp500", "boi_rate", "cpi_yoy"):
            val, fresh = result[key]
            assert fresh.is_stale, f"{key}: expected is_stale=True for missing data"
            assert "MISSING" in fresh.source or val == 0.0 or val != val  # 0.0 or NaN

    def test_usd_nis_still_works_from_boi(self):
        result = market_snapshot(session=None)
        val, fresh = result["usd_nis"]
        assert val == pytest.approx(3.65)
        assert not fresh.is_stale


class TestMarketSnapshotBoiFailure:
    """BoI adapter raises — USD/NIS falls back gracefully."""

    @pytest.fixture(autouse=True)
    def _patch_adapters_boi_fails(self, monkeypatch):
        fred_adapter = _make_fred_adapter(FRED_DATA)

        boi_adapter = MagicMock()

        async def _get_usd_nis(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("BoI unreachable")

        boi_adapter.get_usd_nis = _get_usd_nis

        monkeypatch.setattr(
            "argosy.services.market_snapshot.FredAdapter",
            lambda **kw: fred_adapter,
        )
        monkeypatch.setattr(
            "argosy.services.market_snapshot.BoiAdapter",
            lambda **kw: boi_adapter,
        )

    def test_returns_six_keys_despite_boi_failure(self):
        result = market_snapshot(session=None)
        assert set(result.keys()) == {"sp500", "sp_vs_trend_pct", "vix", "oil_wti", "usd_nis", "boi_rate", "cpi_yoy"}

    def test_usd_nis_flagged_stale_on_failure(self):
        result = market_snapshot(session=None)
        val, fresh = result["usd_nis"]
        assert fresh.is_stale


# ---------------------------------------------------------------------------
# CPI YoY edge cases
# ---------------------------------------------------------------------------


class TestCpiYoY:
    """Verify YoY CPI calculation handles edge cases."""

    def _run_with_cpi(self, cpi_rows: list[dict[str, Any]], monkeypatch) -> tuple[float, DataFreshness]:
        fred_data = dict(FRED_DATA)
        fred_data["CPIAUCSL"] = cpi_rows
        fred_adapter = _make_fred_adapter(fred_data)
        boi_adapter = _make_boi_adapter()

        monkeypatch.setattr(
            "argosy.services.market_snapshot.FredAdapter",
            lambda **kw: fred_adapter,
        )
        monkeypatch.setattr(
            "argosy.services.market_snapshot.BoiAdapter",
            lambda **kw: boi_adapter,
        )
        result = market_snapshot(session=None)
        return result["cpi_yoy"]

    def test_insufficient_cpi_history_flagged(self, monkeypatch):
        """Only one data point — can't compute YoY."""
        val, fresh = self._run_with_cpi(
            _fred_rows(("2026-05-01", 317.0)), monkeypatch
        )
        assert fresh.is_stale

    def test_more_than_12_months_uses_nearest_year_ago(self, monkeypatch):
        """With many rows, use the row closest to 12 months ago."""
        rows = _fred_rows(
            ("2024-05-01", 300.0),
            ("2025-04-01", 308.0),
            ("2025-05-01", 309.0),
            ("2026-04-01", 316.0),
            ("2026-05-01", 317.0),
        )
        val, fresh = self._run_with_cpi(rows, monkeypatch)
        # latest=317, year_ago=309 → (317/309 - 1)*100 ≈ 2.589
        assert val == pytest.approx((317.0 / 309.0 - 1.0) * 100.0, abs=0.1)
        assert not fresh.is_stale


class TestSpVsTrend:
    """_compute_sp_vs_trend: S&P deviation from its trailing-window mean."""

    def test_above_trend_is_positive(self):
        from argosy.services.market_snapshot import _compute_sp_vs_trend
        # 200 obs at 100 then a final 110 -> latest 110 vs mean ~100.05 => ~+9.9%
        rows = [{"date": f"2025-{1+i//28:02d}-{1+i%28:02d}", "value": 100.0} for i in range(200)]
        rows.append({"date": "2026-01-01", "value": 110.0})
        pct, ok = _compute_sp_vs_trend(rows)
        assert ok is True
        assert pct > 8.0

    def test_insufficient_history_not_ok(self):
        from argosy.services.market_snapshot import _compute_sp_vs_trend
        rows = [{"date": "2026-01-01", "value": 100.0}]
        pct, ok = _compute_sp_vs_trend(rows)
        assert ok is False
        assert pct == 0.0
