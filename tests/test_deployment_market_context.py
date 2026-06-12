"""Tests for deployment_market_context — Tasks 1 and 2.

All tests are pure (no network, no DB). The module is exercised by constructing
dataclasses directly and calling the helper functions.
"""
from __future__ import annotations

import pytest

from argosy.services.deployment_market_context import (
    DataFreshness,
    DeploymentMarketContext,
    DEPLOY_FRESHNESS_MAX_AGE,
    NvdaVerification,
    is_stale,
    nvda_consistency,
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


# ---------------------------------------------------------------------------
# Task 2: DEPLOY_FRESHNESS_MAX_AGE + is_stale + nvda_consistency
# ---------------------------------------------------------------------------


class TestDeployFreshnessMaxAge:
    def test_keys_present(self):
        assert "quotes" in DEPLOY_FRESHNESS_MAX_AGE
        assert "macro" in DEPLOY_FRESHNESS_MAX_AGE
        assert "fx" in DEPLOY_FRESHNESS_MAX_AGE
        assert "news" in DEPLOY_FRESHNESS_MAX_AGE

    def test_quotes_ttl(self):
        assert DEPLOY_FRESHNESS_MAX_AGE["quotes"] == 900

    def test_macro_ttl(self):
        assert DEPLOY_FRESHNESS_MAX_AGE["macro"] == 86_400

    def test_fx_ttl(self):
        assert DEPLOY_FRESHNESS_MAX_AGE["fx"] == 86_400

    def test_news_ttl(self):
        assert DEPLOY_FRESHNESS_MAX_AGE["news"] == 172_800


class TestIsStale:
    def test_fresh_well_within_ttl(self):
        assert is_stale(300.0, 900) is False

    def test_stale_just_over_ttl(self):
        assert is_stale(901.0, 900) is True

    def test_boundary_exactly_at_ttl_is_not_stale(self):
        assert is_stale(900.0, 900) is False

    def test_age_zero_never_stale(self):
        assert is_stale(0.0, 900) is False

    def test_macro_within_24h(self):
        assert is_stale(80_000.0, DEPLOY_FRESHNESS_MAX_AGE["macro"]) is False

    def test_macro_over_24h(self):
        assert is_stale(86_401.0, DEPLOY_FRESHNESS_MAX_AGE["macro"]) is True

    def test_news_within_48h(self):
        assert is_stale(170_000.0, DEPLOY_FRESHNESS_MAX_AGE["news"]) is False

    def test_news_over_48h(self):
        assert is_stale(172_801.0, DEPLOY_FRESHNESS_MAX_AGE["news"]) is True


class TestNvdaConsistency:
    """Unit tests for the pinned consistency formula: abs(mktcap/shares - price)/price <= 0.10"""

    def test_consistent_at_exact_match(self):
        # price = mktcap/shares exactly → drift = 0
        price = 130.0
        shares = 24_400_000_000.0
        market_cap = price * shares
        assert nvda_consistency(price, shares, market_cap) is True

    def test_consistent_at_10pct_drift(self):
        # drift == 0.10 (boundary) → still consistent (<=)
        price = 100.0
        shares = 1_000_000.0
        market_cap = 110.0 * shares  # implied = 110, drift = 10/100 = 0.10
        assert nvda_consistency(price, shares, market_cap) is True

    def test_inconsistent_at_11pct_drift(self):
        price = 100.0
        shares = 1_000_000.0
        market_cap = 111.0 * shares  # implied = 111, drift = 11/100 = 0.11
        assert nvda_consistency(price, shares, market_cap) is False

    def test_inconsistent_large_drift(self):
        # Clearly wrong market cap
        price = 130.0
        shares = 24_400_000_000.0
        market_cap = 5_000_000_000_000.0  # way too high
        assert nvda_consistency(price, shares, market_cap) is False

    def test_none_when_shares_is_none(self):
        assert nvda_consistency(130.0, None, 3_172_000_000_000.0) is None

    def test_none_when_market_cap_is_none(self):
        assert nvda_consistency(130.0, 24_400_000_000.0, None) is None

    def test_none_when_shares_zero(self):
        assert nvda_consistency(130.0, 0.0, 3_172_000_000_000.0) is None

    def test_none_when_market_cap_zero(self):
        assert nvda_consistency(130.0, 24_400_000_000.0, 0.0) is None

    def test_none_when_shares_negative(self):
        assert nvda_consistency(130.0, -1.0, 3_172_000_000_000.0) is None

    def test_none_when_market_cap_negative(self):
        assert nvda_consistency(130.0, 24_400_000_000.0, -1.0) is None

    def test_none_when_both_missing(self):
        assert nvda_consistency(130.0, None, None) is None
