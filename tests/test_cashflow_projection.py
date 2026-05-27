"""Unit tests for the cashflow projection math.

Covers:
  - Pure pension-balance accumulation (contribution path, frozen path)
  - Annuity computation at age 67 (mekadem 200, sum of two buckets)
  - Real-return income (portfolio * real_return / 12)
  - Inflation indexing of expenses
  - Retire-ready detection (crossing logic)
"""

from __future__ import annotations

import pytest

from argosy.services.cashflow_projection import (
    CashflowPoint,
    accumulate_pension_balance,
    compute_pension_annuity,
    detect_retire_ready,
    inflate_expenses,
    portfolio_real_return_monthly,
)


class TestAccumulatePensionBalance:
    def test_with_contributions_grows_above_compound_interest(self):
        # 100k starting, 5k/mo contribution, 5.5% real return, 12 months
        b = accumulate_pension_balance(
            starting_balance_nis=100_000.0,
            monthly_contribution_nis=5_000.0,
            real_return_annual=0.055,
            months=12,
        )
        # Hand-verified: iterating b = b*(1 + 0.055/12) + 5000 twelve times from
        # starting 100k gives 167,176.63 (rounded). pytest.approx with rel=1e-3
        # is tight enough to catch a wrong loop ordering (growth vs. contribution
        # swap) but loose enough not to fight rounding noise.
        assert b == pytest.approx(167_176.63, rel=1e-3)

    def test_frozen_bucket_grows_by_real_return_only(self):
        b = accumulate_pension_balance(
            starting_balance_nis=100_000.0,
            monthly_contribution_nis=0.0,
            real_return_annual=0.055,
            months=12,
        )
        # Monthly compounding: 100k * (1 + 0.055/12)^12
        expected = 100_000.0 * (1.0 + 0.055 / 12.0) ** 12
        assert b == pytest.approx(expected, rel=1e-9)

    def test_zero_months_returns_starting_balance(self):
        assert accumulate_pension_balance(
            starting_balance_nis=42_000.0,
            monthly_contribution_nis=0.0,
            real_return_annual=0.055,
            months=0,
        ) == pytest.approx(42_000.0)


class TestComputePensionAnnuity:
    def test_mekadem_200_divides_sum_of_buckets(self):
        # Sum 1.5M / 200 = 7,500 NIS/mo
        a = compute_pension_annuity(
            kupat_pensia_balance_nis=750_000.0,
            executive_insurance_balance_nis=750_000.0,
            mekadem=200.0,
        )
        assert a == pytest.approx(7_500.0)

    def test_zero_balances_zero_annuity(self):
        assert compute_pension_annuity(
            kupat_pensia_balance_nis=0.0,
            executive_insurance_balance_nis=0.0,
            mekadem=200.0,
        ) == 0.0


class TestPortfolioRealReturnMonthly:
    def test_basic_formula(self):
        # 1M * 0.055 / 12 ≈ 4,583
        assert portfolio_real_return_monthly(
            portfolio_value_nis=1_000_000.0,
            real_return_annual=0.055,
        ) == pytest.approx(1_000_000.0 * 0.055 / 12, rel=1e-9)


class TestInflateExpenses:
    def test_one_year_inflation(self):
        e = inflate_expenses(
            base_monthly_nis=20_000.0,
            inflation_annual=0.025,
            months_out=12,
        )
        assert e == pytest.approx(20_000.0 * 1.025, rel=1e-9)

    def test_t_zero_no_inflation(self):
        assert inflate_expenses(20_000.0, 0.025, 0) == pytest.approx(20_000.0)


class TestDetectRetireReady:
    def test_returns_first_crossing_month(self):
        series = [
            CashflowPoint(
                months_out=i, age_years=43+i/12,
                date_yyyy_mm="2026-05",
                portfolio_value_base_nis=0,
                portfolio_value_bear_nis=0,
                portfolio_value_bull_nis=0,
                portfolio_income_base_monthly_nis=(15_000 + i*100),
                portfolio_income_bear_monthly_nis=0,
                portfolio_income_bull_monthly_nis=0,
                pension_annuity_monthly_nis=0,
                pension_lump_available_nis=0,
                expenses_monthly_nis=20_000,
                surplus_base_monthly_nis=(15_000 + i*100) - 20_000,
            )
            for i in range(120)
        ]
        # 15000 + i*100 = 20000 at i=50
        out = detect_retire_ready(series)
        assert out is not None
        assert out[0] == 50  # months_out
        # age_years at i=50 = 43 + 50/12 ≈ 47.17
        assert 47.0 < out[1] < 47.5

    def test_returns_none_when_never_crosses(self):
        series = [
            CashflowPoint(
                months_out=i, age_years=43+i/12, date_yyyy_mm="2026-05",
                portfolio_value_base_nis=0, portfolio_value_bear_nis=0,
                portfolio_value_bull_nis=0,
                portfolio_income_base_monthly_nis=10_000,
                portfolio_income_bear_monthly_nis=0,
                portfolio_income_bull_monthly_nis=0,
                pension_annuity_monthly_nis=0, pension_lump_available_nis=0,
                expenses_monthly_nis=20_000,
                surplus_base_monthly_nis=-10_000,
            )
            for i in range(60)
        ]
        assert detect_retire_ready(series) is None
