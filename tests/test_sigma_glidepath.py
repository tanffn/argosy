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
            # Alternatives (gold/BTC) — its own ~0.268 class. The canonical
            # sleeve label contains "alternative", so these needles MUST win
            # before the generic "alternative"->us_equity fallback.
            ("Alternatives (gold/BTC)", "alternatives"),
            ("Gold ETC", "alternatives"),
            ("Bitcoin ETP", "alternatives"),
            ("BTC sleeve", "alternatives"),
            # A bare "alternative" with no gold/btc cue still falls back to equity.
            ("misc alternative", "us_equity"),
            ("", "us_equity"),
            ("totally-unknown-label", "us_equity"),
        ],
    )
    def test_keyword_routing(self, label: str, expected: str) -> None:
        assert map_glidepath_class_to_sigma_class(label) == expected


class TestMapGlidepathClassExclusionAndLowVol:
    """Allocation-panel caveats 1 + 2: the matcher must not mis-map an
    EXCLUSION of a concentrated ticker (``ex-NVDA``) as concentrated, and
    a min-vol EQUITY sleeve must model at its true ~0.13 risk, not the
    0.06 IG-bond floor (the phantom-bond bug)."""

    @pytest.mark.parametrize(
        "label,expected",
        [
            # Caveat 1 — the ex-/non-NVDA trap. An EXCLUSION of the ticker
            # must NOT classify the sleeve as concentrated single-stock.
            ("Growth-ex-NVDA", "us_equity"),
            ("US growth tilt (ex-NVDA)", "us_equity"),
            ("US-growth (non-NVDA)", "us_equity"),
            ("Diversified equity excluding NVDA", "us_equity"),
            # The genuine single-stock class STILL maps to concentrated.
            ("Strategic single-stock (NVDA)", "concentrated_equity"),
            ("NVDA strategic hold", "concentrated_equity"),
            ("nvidia rsu band", "concentrated_equity"),
            # Caveat 2 — a min-vol / low-vol EQUITY sleeve is its own class,
            # NOT bonds (0.06) and NOT plain diversified equity (0.18).
            ("US low-volatility equity", "low_vol_equity"),
            ("Min-vol equity sleeve", "low_vol_equity"),
        ],
    )
    def test_exclusion_and_low_vol_routing(self, label: str, expected: str) -> None:
        assert map_glidepath_class_to_sigma_class(label) == expected

    def test_alternatives_sleeve_blended_sigma(self) -> None:
        # The Alternatives (gold/BTC) sleeve models at its blended 0.268 sigma
        # (0.8*0.16 + 0.2*0.70), NOT the generic 0.18 the "alternative" fallback
        # would have given — that would silently understate the BTC tail.
        s = sigma_from_composition({"Alternatives (gold/BTC)": 100.0})
        assert s == pytest.approx(0.268, abs=0.001)

    def test_low_vol_equity_sigma_between_bonds_and_us_equity(self) -> None:
        # The phantom-bond bug modeled a low-vol equity sleeve at 0.06 —
        # less than half its true ~0.13 risk. It must sit BELOW diversified
        # equity (0.18) but well ABOVE IG bonds (0.06).
        s = sigma_from_composition({"US low-volatility equity": 100.0})
        assert 0.10 < s < 0.16


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
