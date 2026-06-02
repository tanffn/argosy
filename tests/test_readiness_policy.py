"""Unit tests for the per-policy retire-ready detector (Wave 8 v2.3)."""
from __future__ import annotations

import pytest

from argosy.services.cashflow_projection import CashflowPoint, detect_retire_ready
from argosy.services.retirement.readiness_policy import (
    ReadinessVerdict,
    detect_retire_ready_all_policies,
    detect_retire_ready_by_policy,
)


def _point(
    months_out: int,
    *,
    age: float,
    portfolio_value: float,
    portfolio_income: float,
    annuity: float,
    expenses: float,
) -> CashflowPoint:
    """Minimal CashflowPoint factory — base/bear/bull mirror base for tests."""
    return CashflowPoint(
        months_out=months_out,
        age_years=age,
        date_yyyy_mm=f"2026-{(months_out % 12) + 1:02d}",
        portfolio_value_base_nis=portfolio_value,
        portfolio_value_bear_nis=portfolio_value,
        portfolio_value_bull_nis=portfolio_value,
        portfolio_income_base_monthly_nis=portfolio_income,
        portfolio_income_bear_monthly_nis=portfolio_income,
        portfolio_income_bull_monthly_nis=portfolio_income,
        pension_annuity_monthly_nis=annuity,
        pension_lump_available_nis=0.0,
        expenses_monthly_nis=expenses,
        surplus_base_monthly_nis=portfolio_income + annuity - expenses,
        surplus_bear_monthly_nis=portfolio_income + annuity - expenses,
        surplus_bull_monthly_nis=portfolio_income + annuity - expenses,
    )


def _build_growth_series(
    *,
    starting_portfolio: float,
    starting_income: float,
    real_return_annual: float,
    expenses: float,
    annuity: float,
    months: int,
    starting_age: float,
) -> list[CashflowPoint]:
    """Synthesize a deterministic growing series."""
    monthly_rate = real_return_annual / 12.0
    series: list[CashflowPoint] = []
    portfolio = starting_portfolio
    for m in range(months):
        income = portfolio * monthly_rate if m > 0 else starting_income
        series.append(
            _point(
                m,
                age=starting_age + m / 12.0,
                portfolio_value=portfolio,
                portfolio_income=income,
                annuity=annuity,
                expenses=expenses,
            )
        )
        portfolio = portfolio * (1.0 + monthly_rate)
    return series


class TestReturnsOnlyPolicyParity:
    def test_returns_only_matches_legacy_detect_retire_ready(self):
        series = _build_growth_series(
            starting_portfolio=2_000_000.0,
            starting_income=8_000.0,
            real_return_annual=0.06,
            expenses=30_000.0,
            annuity=0.0,
            months=360,
            starting_age=44.0,
        )
        legacy = detect_retire_ready(series, scenario="base")
        verdict = detect_retire_ready_by_policy(
            series,
            policy="returns_only",
            current_portfolio_value_nis=2_000_000.0,
            target_annual_spend_nis=360_000.0,
        )
        assert legacy is not None
        legacy_months, legacy_age = legacy
        assert verdict.retire_ready_months_out == legacy_months
        assert verdict.retire_ready_age == pytest.approx(legacy_age)


class TestSwrOrdering:
    def test_swr_3_5_fires_later_than_returns_only(self):
        series = _build_growth_series(
            starting_portfolio=4_000_000.0,
            starting_income=20_000.0,
            real_return_annual=0.06,
            expenses=30_000.0,
            annuity=0.0,
            months=480,
            starting_age=44.0,
        )
        ret = detect_retire_ready_by_policy(
            series, policy="returns_only",
            current_portfolio_value_nis=4_000_000.0,
            target_annual_spend_nis=360_000.0,
        )
        swr35 = detect_retire_ready_by_policy(
            series, policy="swr_3_5",
            current_portfolio_value_nis=4_000_000.0,
            target_annual_spend_nis=360_000.0,
        )
        assert ret.retire_ready_age is not None
        assert swr35.retire_ready_age is not None
        assert swr35.retire_ready_age > ret.retire_ready_age

    def test_swr_4_0_fires_earlier_than_swr_3_5(self):
        series = _build_growth_series(
            starting_portfolio=4_000_000.0,
            starting_income=20_000.0,
            real_return_annual=0.06,
            expenses=30_000.0,
            annuity=0.0,
            months=480,
            starting_age=44.0,
        )
        swr35 = detect_retire_ready_by_policy(
            series, policy="swr_3_5",
            current_portfolio_value_nis=4_000_000.0,
            target_annual_spend_nis=360_000.0,
        )
        swr40 = detect_retire_ready_by_policy(
            series, policy="swr_4_0",
            current_portfolio_value_nis=4_000_000.0,
            target_annual_spend_nis=360_000.0,
        )
        assert swr35.retire_ready_age is not None
        assert swr40.retire_ready_age is not None
        assert swr40.retire_ready_age < swr35.retire_ready_age


class TestRationaleStrings:
    def test_all_three_verdicts_have_nonempty_rationale(self):
        series = _build_growth_series(
            starting_portfolio=4_000_000.0,
            starting_income=20_000.0,
            real_return_annual=0.06,
            expenses=30_000.0,
            annuity=0.0,
            months=480,
            starting_age=44.0,
        )
        verdicts = detect_retire_ready_all_policies(
            series,
            current_portfolio_value_nis=4_000_000.0,
            target_annual_spend_nis=360_000.0,
        )
        assert len(verdicts) == 3
        assert [v.policy for v in verdicts] == ["returns_only", "swr_3_5", "swr_4_0"]
        for v in verdicts:
            assert isinstance(v, ReadinessVerdict)
            assert v.rationale.strip()
            assert len(v.rationale) > 30


class TestNoCrossing:
    def test_none_when_horizon_too_short(self):
        series = _build_growth_series(
            starting_portfolio=100_000.0,
            starting_income=500.0,
            real_return_annual=0.04,
            expenses=50_000.0,
            annuity=0.0,
            months=24,
            starting_age=44.0,
        )
        verdicts = detect_retire_ready_all_policies(
            series,
            current_portfolio_value_nis=100_000.0,
            target_annual_spend_nis=600_000.0,
        )
        for v in verdicts:
            assert v.retire_ready_age is None
            assert v.retire_ready_months_out is None
            assert "never crosses" in v.rationale


class TestImmediateCrossing:
    def test_all_three_same_age_when_immediate_crossing(self):
        series = _build_growth_series(
            starting_portfolio=100_000_000.0,
            starting_income=500_000.0,
            real_return_annual=0.06,
            expenses=30_000.0,
            annuity=0.0,
            months=120,
            starting_age=44.0,
        )
        verdicts = detect_retire_ready_all_policies(
            series,
            current_portfolio_value_nis=100_000_000.0,
            target_annual_spend_nis=360_000.0,
        )
        ages = [v.retire_ready_age for v in verdicts]
        assert all(a is not None for a in ages)
        assert all(a == pytest.approx(44.0) for a in ages)
        assert all(v.retire_ready_months_out == 0 for v in verdicts)


class TestEmptySeries:
    def test_empty_series_returns_never_crossed_for_all(self):
        verdicts = detect_retire_ready_all_policies(
            [],
            current_portfolio_value_nis=2_000_000.0,
            target_annual_spend_nis=360_000.0,
        )
        for v in verdicts:
            assert v.retire_ready_age is None
            assert "0-month horizon" in v.rationale
