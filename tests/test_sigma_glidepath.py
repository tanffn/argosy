"""Unit tests for the sigma_glidepath service (Wave 8 v2.3)."""
from __future__ import annotations

import pytest

from argosy.services.sigma_glidepath import (
    DEFAULT_SIGMA_FLAT,
    SigmaCurve,
    interpolate_sigma_series,
    map_glidepath_class_to_sigma_class,
    sigma_from_composition,
)


class TestMapGlidepathClassToSigmaClass:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("NVDA", "concentrated_equity"),
            ("nvidia rsu band", "concentrated_equity"),
            ("Individual Stocks", "concentrated_equity"),
            ("Growth", "us_equity"),
            ("Core Equity", "us_equity"),
            ("Dividend", "us_equity"),
            ("Defensive", "bonds"),
            ("Treasury T-bills", "cash"),  # 't-bill' wins before treasury
            ("Cash", "cash"),
            ("International", "intl_equity"),
            ("Emerging Markets", "emerging_equity"),
            ("REIT", "real_estate"),
            ("real estate", "real_estate"),
            ("", "us_equity"),
            ("totally-unknown-label", "us_equity"),
        ],
    )
    def test_keyword_routing(self, label: str, expected: str) -> None:
        assert map_glidepath_class_to_sigma_class(label) == expected


class TestSigmaFromComposition:
    def test_today_nvda_heavy_higher_than_planned(self) -> None:
        # NVDA 65% + Growth 20% + Cash 15% → 0.3315
        today = sigma_from_composition(
            {"individual stocks": 65.0, "growth": 20.0, "cash": 15.0}
        )
        # NVDA 15%, Growth 60%, Defensive 25% → 0.1905
        planned = sigma_from_composition(
            {"nvda": 15.0, "growth": 60.0, "defensive": 25.0}
        )
        assert today == pytest.approx(0.3315, abs=0.001)
        assert planned == pytest.approx(0.1905, abs=0.001)
        assert planned < today

    def test_empty_composition_defaults_to_diversified(self) -> None:
        assert sigma_from_composition({}) == DEFAULT_SIGMA_FLAT

    def test_renormalizes_off_total(self) -> None:
        # Sum = 50; renormalize → all-NVDA → 0.45
        assert sigma_from_composition({"nvda": 50.0}) == pytest.approx(0.45)


class TestInterpolateSigmaSeries:
    def test_length_is_horizon_plus_one(self) -> None:
        s = interpolate_sigma_series(
            sigma_today=0.33,
            sigma_planned=0.19,
            months_to_steady_state=24,
            horizon_months=600,
        )
        assert len(s) == 601

    def test_anchors_at_endpoints(self) -> None:
        s = interpolate_sigma_series(
            sigma_today=0.33,
            sigma_planned=0.19,
            months_to_steady_state=24,
            horizon_months=600,
        )
        assert s[0] == pytest.approx(0.33)
        assert s[24] == pytest.approx(0.19)
        assert s[600] == pytest.approx(0.19)

    def test_monotone_decreasing_when_today_above_planned(self) -> None:
        s = interpolate_sigma_series(
            sigma_today=0.33,
            sigma_planned=0.19,
            months_to_steady_state=24,
            horizon_months=120,
        )
        for i in range(len(s) - 1):
            assert s[i + 1] <= s[i] + 1e-12

    def test_flat_when_months_to_steady_state_zero(self) -> None:
        s = interpolate_sigma_series(
            sigma_today=0.33,
            sigma_planned=0.19,
            months_to_steady_state=0,
            horizon_months=12,
        )
        assert s == [0.19] * 13

    def test_flat_extend_past_glidepath_end(self) -> None:
        s = interpolate_sigma_series(
            sigma_today=0.30,
            sigma_planned=0.20,
            months_to_steady_state=10,
            horizon_months=20,
        )
        for i in range(10, 21):
            assert s[i] == pytest.approx(0.20)


class TestSigmaCurveAtHelper:
    def test_curve_at_helper_clamps_past_end(self) -> None:
        curve = SigmaCurve(
            series=[0.33, 0.30, 0.27, 0.24, 0.21, 0.19],
            sigma_today=0.33,
            sigma_planned=0.19,
            months_to_steady_state=5,
        )
        assert curve.at(0) == 0.33
        assert curve.at(5) == 0.19
        assert curve.at(999) == 0.19
        assert curve.at(-5) == 0.33

    def test_empty_curve_at_returns_default(self) -> None:
        curve = SigmaCurve(
            series=[], sigma_today=0.0, sigma_planned=0.0,
            months_to_steady_state=0,
        )
        assert curve.at(0) == DEFAULT_SIGMA_FLAT
