"""Unit tests for the cashflow projection math.

Covers:
  - Pure pension-balance accumulation (contribution path, frozen path)
  - Annuity computation at age 67 (mekadem 200, sum of two buckets)
  - Real-return income (portfolio * real_return / 12)
  - Inflation indexing of expenses
  - Retire-ready detection (crossing logic)
  - DB extraction: PensionState, HouseholdState
"""

from __future__ import annotations

import json
import yaml
from datetime import date, datetime, timezone

import pytest

from argosy.services.cashflow_projection import (
    CashflowPoint,
    accumulate_pension_balance,
    compute_pension_annuity,
    detect_retire_ready,
    inflate_expenses,
    portfolio_real_return_monthly,
)
from argosy.state.models import (
    AgentReport,
    PortfolioSnapshotRow,
    User,
    UserContext,
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


# ---------------------------------------------------------------------------
# DB extraction tests — Task 2
# ---------------------------------------------------------------------------


def _seed_full_state(session, *, user_id="ariel"):
    """Seed a complete user with all the pension + budget + snapshot data
    the cashflow projection needs. Mirrors the real DB shape for ``ariel``.

    Used by both extraction tests in this module and the integration test
    in tests/test_plan_draft_api.py (Task 4)."""
    session.add(User(id=user_id, email="a@e"))
    identity = {
        "date_of_birth": "1982-08-28",
        "clal_pension_salary_basis_monthly_nis": 27101,
        "clal_pension_employee_pct": 6.0,
        "clal_pension_employer_pct": 6.5,
        "clal_pension_severance_pct": 8.33,
        "pensions_ariel": {
            "pension_nis": 800_147,
            "executive_insurance_nis": 755_907,
            "keren_hishtalmut_nis": 384_000,
            "provident_fund_nis": 75_000,
            "total_nis": 2_015_054,
            "data_date": "2025-12",
        },
        "pensions": {
            "kupat_pensia": {
                "balance_nis": 800_147,
                "contribution_rate_pct": 6.0,
                "employer_match_pct": 6.5,
            },
            "keren_hishtalmut": {
                "balance_nis": 384_000,
                "contribution_rate_pct": 2.5,
                "employer_match_pct": 7.5,
            },
            "executive_insurance": {"balance_nis": 755_907},
            "kupat_gemel": {"balance_nis": 75_000},
        },
        "fx_rate": {"usd_nis": 2.94},
    }
    session.add(UserContext(
        user_id=user_id,
        identity_yaml=yaml.safe_dump(identity),
        goals_yaml="",
        constraints_yaml="",
        current_stage="complete",
    ))
    session.add(PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=date(2026, 5, 1),
        imported_at=datetime.now(timezone.utc),
        source_path="/tmp/seed.tsv",
        positions_json="[]",
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps({"total_usd_value_k": 1500.0}),
        fx_usd_nis=2.94,
        fx_usd_eur=4.0,
        parse_warnings_json="[]",
    ))
    body = {
        "runway_class": "comfortable",
        "monthly_burn_nis": 23_084.0,
        "monthly_income_nis": 54_835.0,
        "monthly_net_nis": 31_751.0,
        "safe_withdrawal_monthly_usd": 11_800.0,
        "headroom_summary": "seeded",
        "key_concerns": [],
        "confidence": "MEDIUM",
        "cited_sources": [],
    }
    session.add(AgentReport(
        user_id=user_id, agent_role="household_budget", decision_id=None,
        prompt_hash="x", response_text=f"```json\n{json.dumps(body)}\n```",
        tokens_in=0, tokens_out=0, cost_usd=0, model="seed",
    ))
    session.commit()


class TestExtractPensionState:
    def test_reads_all_four_buckets_and_contribution_rates(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import extract_pension_state
            state = extract_pension_state(s, "ariel")
        assert state.kupat_pensia_balance_nis == 800_147
        assert state.executive_insurance_balance_nis == 755_907
        assert state.keren_hishtalmut_balance_nis == 384_000
        assert state.kupat_gemel_balance_nis == 75_000
        # kupat_pensia monthly contribution = 27101 * (6 + 6.5 + 8.33)/100 = 5,646
        expected_pensia_contrib = 27101 * (6.0 + 6.5 + 8.33) / 100.0
        assert state.kupat_pensia_contribution_monthly_nis == pytest.approx(
            expected_pensia_contrib, rel=1e-3
        )
        # hishtalmut contribution = 27101 * (2.5 + 7.5)/100 = 2,710.1
        expected_hishtalmut_contrib = 27101 * (2.5 + 7.5) / 100.0
        assert state.keren_hishtalmut_contribution_monthly_nis == pytest.approx(
            expected_hishtalmut_contrib, rel=1e-3
        )

    def test_returns_zeros_when_no_identity(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            from argosy.services.cashflow_projection import extract_pension_state
            state = extract_pension_state(s, "missing-user")
        assert state.kupat_pensia_balance_nis == 0.0
        assert state.kupat_pensia_contribution_monthly_nis == 0.0


class TestExtractHouseholdState:
    def test_reads_burn_portfolio_fx_age(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import extract_household_state
            state = extract_household_state(s, "ariel", today=date(2026, 5, 27))
        assert state.monthly_expenses_nis == pytest.approx(23_084.0)
        # 1500k USD * 2.94 = 4,410,000 NIS
        assert state.portfolio_value_nis == pytest.approx(4_410_000.0, rel=1e-3)
        assert state.fx_usd_nis == pytest.approx(2.94)
        # 1982-08-28 → 2026-05-27 ≈ 43.74 years
        assert 43.6 < state.current_age_years < 43.9


# ---------------------------------------------------------------------------
# Task 3: project_cashflow orchestrator tests
# ---------------------------------------------------------------------------


class TestProjectCashflow:
    def test_full_projection_at_seeded_state(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                DEFAULT_INFLATION_ANNUAL,
                DEFAULT_MEKADEM,
                DEFAULT_MU_NOMINAL_ANNUAL,
                DEFAULT_SIGMA_ANNUAL,
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh,
            pensions=pen,
            retirement_age=49.0,
            years=30,
            mu_nominal_annual=DEFAULT_MU_NOMINAL_ANNUAL,
            sigma_annual=DEFAULT_SIGMA_ANNUAL,
            inflation_annual=DEFAULT_INFLATION_ANNUAL,
            mekadem=DEFAULT_MEKADEM,
            today=date(2026, 5, 27),
        )
        assert len(proj.series) == 30 * 12 + 1

        first = proj.series[0]
        assert first.months_out == 0
        # At t=0: portfolio_income_base = 4.41M * 0.055 / 12 ≈ 20,212 NIS
        assert first.portfolio_income_base_monthly_nis == pytest.approx(
            4_410_000.0 * 0.055 / 12.0, rel=1e-3
        )
        assert first.pension_annuity_monthly_nis == 0
        assert first.pension_lump_available_nis == 0
        assert first.expenses_monthly_nis == pytest.approx(23_084.0, rel=1e-6)

    def test_lump_unlocks_at_age_60(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh, pensions=pen,
            retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        lump_idx = next(
            i for i, p in enumerate(proj.series) if p.age_years >= 60.0
        )
        assert proj.series[lump_idx - 1].pension_lump_available_nis == 0
        assert proj.series[lump_idx].pension_lump_available_nis > 0
        # Original 459,000 NIS (384k + 75k) grown ~16 years should be >> 459k.
        assert proj.series[lump_idx].pension_lump_available_nis >= 459_000

    def test_annuity_kicks_in_at_age_67(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh, pensions=pen,
            retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        annuity_idx = next(
            i for i, p in enumerate(proj.series) if p.age_years >= 67.0
        )
        assert proj.series[annuity_idx - 1].pension_annuity_monthly_nis == 0
        assert proj.series[annuity_idx].pension_annuity_monthly_nis > 0
        # Original 1,556,054 NIS combined; should be >> at lock time.
        assert proj.series[annuity_idx].pension_annuity_monthly_nis >= 1_556_054 / 200

    def test_contributions_stop_at_retirement_age(self, client_with_db):
        """retirement_age=60 vs 49 → larger annuity at 67 (more contributions)."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj_retire_49 = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        proj_retire_60 = project_cashflow(
            household=hh, pensions=pen, retirement_age=60.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        idx_49 = next(i for i, p in enumerate(proj_retire_49.series) if p.age_years >= 67)
        idx_60 = next(i for i, p in enumerate(proj_retire_60.series) if p.age_years >= 67)
        assert (
            proj_retire_60.series[idx_60].pension_annuity_monthly_nis
            > proj_retire_49.series[idx_49].pension_annuity_monthly_nis
        )

    def test_retire_ready_detected_when_crossing_exists(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        assert proj.retire_ready_months_out is not None
        assert proj.retire_ready_age is not None
        assert proj.retire_ready_age >= hh.current_age_years

    def test_portfolio_keeps_compounding_after_lump_unlock(self, client_with_db):
        """KEY INVARIANT: After the lump bump at age 60, the portfolio base
        should continue compounding at mu_nominal/12 per month — i.e.,
        value 12 months post-unlock must equal value-at-unlock × (1 + mu/12)^12.
        Guards against a regression where the lump-bump path desyncs the
        portfolio's compound state."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        unlock_idx = next(
            i for i, p in enumerate(proj.series) if p.pension_lump_available_nis > 0
        )
        v_at_unlock = proj.series[unlock_idx].portfolio_value_base_nis
        v_plus_12 = proj.series[unlock_idx + 12].portfolio_value_base_nis
        expected = v_at_unlock * ((1.0 + 0.08 / 12.0) ** 12)
        assert v_plus_12 == pytest.approx(expected, rel=1e-6)

    def test_annuity_inflates_nominally_after_lock(self, client_with_db):
        """After age 67 the annuity should grow nominally at inflation_annual,
        not stay flat in nominal terms. This was a real/nominal-mismatch bug
        caught by codex-tandem review."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
                project_cashflow,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")

        proj = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        lock_idx = next(
            i for i, p in enumerate(proj.series) if p.pension_annuity_monthly_nis > 0
        )
        # At lock: real value (no inflation yet)
        v_at_lock = proj.series[lock_idx].pension_annuity_monthly_nis
        # 12 months later: nominal value = real × (1.025)^1
        v_plus_12 = proj.series[lock_idx + 12].pension_annuity_monthly_nis
        expected = v_at_lock * 1.025
        assert v_plus_12 == pytest.approx(expected, rel=1e-6)
