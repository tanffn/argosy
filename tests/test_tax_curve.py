"""Tests for the unified age-banded tax curve (Wave 8 v2.3 audit #2)."""
from __future__ import annotations

import pytest

from argosy.services.tax_curve import (
    ANNUITY_AGE,
    LUMP_PENSION_AGE,
    LUMP_WINDOW_RATE,
    POST_67_RATE,
    PRE_60_RATE,
    TaxCurvePoint,
    build_tax_curve,
    effective_tax_rate_at_age,
)


class TestEffectiveTaxRateAtAge:
    def test_pre_60_returns_25pct(self):
        assert effective_tax_rate_at_age(45) == pytest.approx(0.25)

    def test_lump_window_returns_15pct(self):
        assert effective_tax_rate_at_age(62) == pytest.approx(0.15)

    def test_post_67_returns_12pct(self):
        assert effective_tax_rate_at_age(70) == pytest.approx(0.12)

    def test_constants_match_returned_rates(self):
        assert effective_tax_rate_at_age(45) == PRE_60_RATE
        assert effective_tax_rate_at_age(62) == LUMP_WINDOW_RATE
        assert effective_tax_rate_at_age(70) == POST_67_RATE

    def test_override_flat_overrides_at_every_age(self):
        for age in (20, 45, 60, 67, 80, 95):
            assert effective_tax_rate_at_age(age, override_flat=0.30) == pytest.approx(0.30)

    def test_override_flat_zero_is_honored(self):
        assert effective_tax_rate_at_age(45, override_flat=0.0) == pytest.approx(0.0)

    def test_override_flat_clipped_to_unit_interval(self):
        assert effective_tax_rate_at_age(45, override_flat=1.5) == pytest.approx(1.0)
        assert effective_tax_rate_at_age(45, override_flat=-0.2) == pytest.approx(0.0)


class TestBandBoundaries:
    def test_exactly_at_age_60_is_lump_window(self):
        assert effective_tax_rate_at_age(LUMP_PENSION_AGE) == pytest.approx(LUMP_WINDOW_RATE)

    def test_just_below_60_is_pre_60(self):
        assert effective_tax_rate_at_age(LUMP_PENSION_AGE - 1e-6) == pytest.approx(PRE_60_RATE)

    def test_exactly_at_age_67_is_post_67(self):
        assert effective_tax_rate_at_age(ANNUITY_AGE) == pytest.approx(POST_67_RATE)

    def test_just_below_67_is_lump_window(self):
        assert effective_tax_rate_at_age(ANNUITY_AGE - 1e-6) == pytest.approx(LUMP_WINDOW_RATE)


class TestDefensiveBounds:
    def test_negative_age_clamps_to_pre_60(self):
        assert effective_tax_rate_at_age(-5) == pytest.approx(PRE_60_RATE)

    def test_very_old_age_clamps_to_post_67(self):
        assert effective_tax_rate_at_age(150) == pytest.approx(POST_67_RATE)

    def test_age_100_returns_post_67(self):
        assert effective_tax_rate_at_age(100) == pytest.approx(POST_67_RATE)


class TestBuildTaxCurve:
    def test_returns_horizon_plus_one_points(self):
        curve = build_tax_curve(current_age=45, horizon_months=12)
        assert len(curve) == 13

    def test_zero_horizon_yields_single_point(self):
        curve = build_tax_curve(current_age=45, horizon_months=0)
        assert len(curve) == 1
        assert curve[0].age_years == pytest.approx(45.0)
        assert curve[0].effective_rate == pytest.approx(PRE_60_RATE)

    def test_negative_horizon_yields_single_point(self):
        curve = build_tax_curve(current_age=45, horizon_months=-12)
        assert len(curve) == 1

    def test_age_progresses_monthly(self):
        curve = build_tax_curve(current_age=45, horizon_months=24)
        assert curve[0].age_years == pytest.approx(45.0)
        assert curve[12].age_years == pytest.approx(46.0)
        assert curve[24].age_years == pytest.approx(47.0)

    def test_rates_monotone_non_increasing_age_45_to_80(self):
        curve = build_tax_curve(current_age=45, horizon_months=35 * 12)
        rates = [p.effective_rate for p in curve]
        for prev, nxt in zip(rates, rates[1:]):
            assert nxt <= prev, f"rate increased from {prev} to {nxt}"
        assert rates[0] == pytest.approx(PRE_60_RATE)
        assert rates[-1] == pytest.approx(POST_67_RATE)

    def test_band_transitions_present_in_long_curve(self):
        curve = build_tax_curve(current_age=45, horizon_months=35 * 12)
        bands = {p.source_band for p in curve}
        assert "pre_60_cgt" in bands
        assert "lump_window_60_67" in bands
        assert "post_67_pension" in bands

    def test_override_flat_flat_across_curve(self):
        curve = build_tax_curve(
            current_age=45, horizon_months=35 * 12, override_flat=0.30
        )
        assert all(p.effective_rate == pytest.approx(0.30) for p in curve)
        assert all(p.source_band == "override_flat" for p in curve)

    def test_returned_points_are_dataclass_instances(self):
        curve = build_tax_curve(current_age=45, horizon_months=1)
        assert isinstance(curve[0], TaxCurvePoint)


class TestEffectiveWithdrawalTaxAtAge:
    """T3.4 — the single-source EFFECTIVE withdrawal-tax curve the MC uses
    instead of the retired flat-10% shortcut. Pre-pension draws are taxable-
    brokerage CGT on the realized-gain fraction (statutory CGT ×
    taxable_gain_fraction); post-67 is the pension rights-fixation effective
    rate. Both rates are DERIVED from constants, never magic numbers."""

    def test_pre_67_is_cgt_times_gain_fraction(self):
        from argosy.services.tax_curve import (
            ISRAELI_CGT_RATE,
            TAXABLE_GAIN_FRACTION,
            effective_withdrawal_tax_at_age,
        )
        expected = ISRAELI_CGT_RATE * TAXABLE_GAIN_FRACTION  # 0.25 * 0.6 = 0.15
        assert effective_withdrawal_tax_at_age(46) == pytest.approx(expected)
        assert effective_withdrawal_tax_at_age(62) == pytest.approx(expected)
        assert expected == pytest.approx(0.15)

    def test_post_67_is_pension_effective_rate(self):
        from argosy.services.tax_curve import (
            POST_67_RATE,
            effective_withdrawal_tax_at_age,
        )
        assert effective_withdrawal_tax_at_age(70) == pytest.approx(POST_67_RATE)
        assert effective_withdrawal_tax_at_age(70) == pytest.approx(0.12)

    def test_boundary_at_67_switches_to_pension(self):
        from argosy.services.tax_curve import effective_withdrawal_tax_at_age
        assert effective_withdrawal_tax_at_age(ANNUITY_AGE - 1e-6) == pytest.approx(0.15)
        assert effective_withdrawal_tax_at_age(ANNUITY_AGE) == pytest.approx(0.12)

    def test_not_the_retired_flat_10pct_shortcut(self):
        """The whole point of T3.4: the MC tax is no longer a flat 10%."""
        from argosy.services.tax_curve import effective_withdrawal_tax_at_age
        assert effective_withdrawal_tax_at_age(46) != pytest.approx(0.10)
        assert effective_withdrawal_tax_at_age(70) != pytest.approx(0.10)

    def test_override_flat_honored(self):
        from argosy.services.tax_curve import effective_withdrawal_tax_at_age
        assert effective_withdrawal_tax_at_age(46, override_flat=0.0) == pytest.approx(0.0)
        assert effective_withdrawal_tax_at_age(46, override_flat=0.30) == pytest.approx(0.30)

    def test_defensive_clamps(self):
        from argosy.services.tax_curve import effective_withdrawal_tax_at_age
        assert effective_withdrawal_tax_at_age(-5) == pytest.approx(0.15)
        assert effective_withdrawal_tax_at_age(150) == pytest.approx(0.12)


class TestAnnualSurtax:
    """T5.7 — Israeli surtax (mas yesef) on annual income above the threshold:
    3% ordinary, 5% capital (3% base + 2% capital surcharge, 2025+)."""

    def test_below_threshold_is_zero(self):
        from argosy.services.tax_curve import annual_surtax, SURTAX_THRESHOLD_ANNUAL_NIS
        assert annual_surtax(SURTAX_THRESHOLD_ANNUAL_NIS - 1, is_capital=True) == 0.0
        assert annual_surtax(280_000, is_capital=True) == 0.0  # a retirement draw
        assert annual_surtax(280_000, is_capital=False) == 0.0

    def test_at_threshold_is_zero(self):
        from argosy.services.tax_curve import annual_surtax, SURTAX_THRESHOLD_ANNUAL_NIS
        assert annual_surtax(SURTAX_THRESHOLD_ANNUAL_NIS) == pytest.approx(0.0)

    def test_ordinary_above_threshold_is_3pct_of_excess(self):
        from argosy.services.tax_curve import annual_surtax, SURTAX_THRESHOLD_ANNUAL_NIS
        income = SURTAX_THRESHOLD_ANNUAL_NIS + 1_000_000
        assert annual_surtax(income, is_capital=False) == pytest.approx(1_000_000 * 0.03)

    def test_capital_above_threshold_is_5pct_of_excess(self):
        from argosy.services.tax_curve import annual_surtax, SURTAX_THRESHOLD_ANNUAL_NIS
        income = SURTAX_THRESHOLD_ANNUAL_NIS + 1_000_000
        assert annual_surtax(income, is_capital=True) == pytest.approx(1_000_000 * 0.05)

    def test_threshold_override(self):
        from argosy.services.tax_curve import annual_surtax
        # Only the excess above the supplied threshold is taxed.
        assert annual_surtax(600_000, is_capital=True, threshold_nis=500_000) == pytest.approx(100_000 * 0.05)

    def test_negative_income_is_zero(self):
        from argosy.services.tax_curve import annual_surtax
        assert annual_surtax(-50_000, is_capital=True) == 0.0
