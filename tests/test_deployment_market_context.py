"""Tests for deployment_market_context — Tasks 1 and 2.

All tests are pure (no network, no DB). The module is exercised by constructing
dataclasses directly and calling the helper functions.
"""
from __future__ import annotations

import pytest

from argosy.services.deployment_market_context import (
    DataFreshness,
    DeploymentMarketContext,
    NvdaVerification,
)


# ---------------------------------------------------------------------------
# Task 1: frozen dataclass construction + field access
# ---------------------------------------------------------------------------


class TestDataFreshness:
    def test_field_access(self):
        df = DataFreshness(
            field="vix",
            fetched_at="2026-06-12T10:00:00Z",
            age_seconds=300.0,
            source="fred",
            is_stale=False,
        )
        assert df.field == "vix"
        assert df.fetched_at == "2026-06-12T10:00:00Z"
        assert df.age_seconds == 300.0
        assert df.source == "fred"
        assert df.is_stale is False

    def test_is_frozen(self):
        df = DataFreshness("vix", "2026-06-12T10:00:00Z", 300.0, "fred", False)
        with pytest.raises(AttributeError):
            df.age_seconds = 999.0  # type: ignore[misc]

    def test_stale_flag_propagated(self):
        df = DataFreshness("oil", "2026-06-11T00:00:00Z", 200_000.0, "fred", True)
        assert df.is_stale is True


class TestNvdaVerification:
    def test_field_access_consistent(self):
        nv = NvdaVerification(price=130.0, shares=24.4e9, market_cap=3.172e12,
                              consistent=True, note="within 10%")
        assert nv.price == 130.0
        assert nv.consistent is True
        assert "10%" in nv.note

    def test_field_access_inconsistent(self):
        nv = NvdaVerification(price=130.0, shares=24.4e9, market_cap=5e12,
                              consistent=False, note="drift > 10%")
        assert nv.consistent is False

    def test_field_access_none_consistent(self):
        nv = NvdaVerification(price=130.0, shares=None, market_cap=None,
                              consistent=None, note="shares missing")
        assert nv.consistent is None

    def test_is_frozen(self):
        nv = NvdaVerification(130.0, None, None, None, "")
        with pytest.raises(AttributeError):
            nv.price = 200.0  # type: ignore[misc]


class TestDeploymentMarketContext:
    def _make_context(
        self,
        freshness: tuple[DataFreshness, ...] = (),
        nvda: NvdaVerification | None = None,
    ) -> DeploymentMarketContext:
        return DeploymentMarketContext(
            snapshot={"vix": 18.0, "sp500": 5400.0},
            freshness=freshness,
            nvda=nvda,
            overall_age_label="fresh",
        )

    def test_field_access(self):
        ctx = self._make_context()
        assert ctx.snapshot["vix"] == 18.0
        assert ctx.overall_age_label == "fresh"
        assert ctx.freshness == ()
        assert ctx.nvda is None

    def test_is_any_stale_all_fresh_no_nvda(self):
        df = DataFreshness("vix", "2026-06-12T10:00:00Z", 300.0, "fred", False)
        ctx = self._make_context(freshness=(df,))
        assert ctx.is_any_stale is False

    def test_is_any_stale_one_stale_feed(self):
        fresh = DataFreshness("vix", "2026-06-12T10:00:00Z", 300.0, "fred", False)
        stale = DataFreshness("oil", "2026-06-11T00:00:00Z", 200_000.0, "fred", True)
        ctx = self._make_context(freshness=(fresh, stale))
        assert ctx.is_any_stale is True

    def test_is_any_stale_nvda_inconsistent(self):
        df = DataFreshness("quotes", "2026-06-12T10:00:00Z", 60.0, "yfinance", False)
        nv = NvdaVerification(130.0, 24.4e9, 5e12, False, "drift > 10%")
        ctx = self._make_context(freshness=(df,), nvda=nv)
        assert ctx.is_any_stale is True

    def test_is_any_stale_nvda_none_consistent(self):
        """consistent=None (missing data) should also count as stale/flagged."""
        df = DataFreshness("quotes", "2026-06-12T10:00:00Z", 60.0, "yfinance", False)
        nv = NvdaVerification(130.0, None, None, None, "shares missing")
        ctx = self._make_context(freshness=(df,), nvda=nv)
        assert ctx.is_any_stale is True

    def test_is_any_stale_nvda_consistent_and_fresh(self):
        df = DataFreshness("quotes", "2026-06-12T10:00:00Z", 60.0, "yfinance", False)
        nv = NvdaVerification(130.0, 24.4e9, 3.172e12, True, "within 10%")
        ctx = self._make_context(freshness=(df,), nvda=nv)
        assert ctx.is_any_stale is False

    def test_is_frozen(self):
        ctx = self._make_context()
        with pytest.raises((AttributeError, TypeError)):
            ctx.overall_age_label = "stale"  # type: ignore[misc]
