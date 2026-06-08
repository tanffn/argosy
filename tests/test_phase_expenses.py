"""Unit tests for the phase-expense factor series (H3).

The series scales the solvency-MC per-tick expense by the documented life-stage
phases RELATIVE to the household's current burn: an empty-nest dip, then the
post-65 healthcare ramp and late-life LTC tail driven mainly by the compounding
above-CPI inflation premium. (codex H3 verdict 2026-06-08.)
"""
import pytest

from argosy.services.retirement.phase_expenses import phase_expense_factor_series


def _at_age(factors: list[float], current_age: float, target_age: float) -> float:
    """factors[t-1] applies at age = current_age + t/12."""
    t = round((target_age - current_age) * 12)
    return factors[t - 1]


class TestPhaseExpenseFactorSeries:
    def test_t0_equals_current_burn(self) -> None:
        # A 44yo is ALREADY inside kids_peak (1.10x). The first tick must be ~1.0
        # (rel_mult = 1.10/1.10 = 1.0) — no double-count of today's elevation.
        f = phase_expense_factor_series(current_age=44.0, months=12)
        assert f[0] == pytest.approx(1.0, abs=0.01)

    def test_empty_nest_dips_below_current(self) -> None:
        # 56-64 empty_nest (0.85x) normalized by kids_peak (1.10) -> 0.773, times
        # a modest premium carry -> clearly below today's burn.
        f = phase_expense_factor_series(current_age=44.0, months=12 * 30)  # -> age 74
        v60 = _at_age(f, 44.0, 60.0)
        assert 0.78 < v60 < 0.86

    def test_healthcare_ramp_midpoint(self) -> None:
        # By age 80 (end of healthcare_ramp) the +1.5%/yr premium has compounded
        # ~1.25x and rel_mult is 1.10/1.10 = 1.0 -> factor ~1.3.
        f = phase_expense_factor_series(current_age=44.0, months=12 * 52)  # -> age 96
        v80 = _at_age(f, 44.0, 80.0)
        assert 1.20 < v80 < 1.40

    def test_late_life_roughly_doubles_real(self) -> None:
        # The premium dominates: +1.5%/yr (65-80) then +3%/yr (81-95) compounding
        # -> ~2x the current REAL burn by the mid-90s (codex H3 ~2.05x).
        f = phase_expense_factor_series(current_age=44.0, months=12 * 52)
        v94 = _at_age(f, 44.0, 94.0)
        assert 1.8 < v94 < 2.3

    def test_monotone_rising_through_ltc_tail(self) -> None:
        # Through the LTC tail the factor strictly rises (premium compounding).
        f = phase_expense_factor_series(current_age=44.0, months=12 * 52)
        i85 = round((85.0 - 44.0) * 12) - 1
        i95 = round((95.0 - 44.0) * 12) - 1
        assert f[i95] > f[i85] > 1.0

    def test_length_matches_months(self) -> None:
        f = phase_expense_factor_series(current_age=44.0, months=240)
        assert len(f) == 240
