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
from types import SimpleNamespace
import yaml
from datetime import date, datetime, timezone

import pytest

from argosy.services.cashflow_projection import (
    CashflowPoint,
    HouseholdState,
    PensionState,
    accumulate_pension_balance,
    compute_pension_annuity,
    detect_retire_ready,
    inflate_expenses,
    portfolio_real_return_monthly,
    project_cashflow,
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
                surplus_bear_monthly_nis=-20_000,
                surplus_bull_monthly_nis=(15_000 + i*100) - 20_000,
            )
            for i in range(120)
        ]
        # 15000 + i*100 = 20000 at i=50
        out = detect_retire_ready(series, scenario="base")
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
                surplus_bear_monthly_nis=-10_000,
                surplus_bull_monthly_nis=-10_000,
            )
            for i in range(60)
        ]
        assert detect_retire_ready(series, scenario="base") is None


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
            tax_rate=0.0,  # gross income to verify the raw formula
            today=date(2026, 5, 27),
        )
        assert len(proj.series) == 30 * 12 + 1

        first = proj.series[0]
        assert first.months_out == 0
        # At t=0: portfolio_income_base = 4.41M * 0.055 / 12 ≈ 20,212 NIS (gross, tax_rate=0)
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


class TestTaxRate:
    def test_default_tax_25_reduces_portfolio_income(self, client_with_db):
        """tax_rate=0.25 should reduce base portfolio income by 25%
        vs tax_rate=0.0."""
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

        proj_no_tax = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=5,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.0,
            today=date(2026, 5, 27),
        )
        proj_tax = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=5,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.25,
            today=date(2026, 5, 27),
        )
        # Tax should reduce portfolio income by exactly 25%
        assert proj_tax.series[0].portfolio_income_base_monthly_nis == pytest.approx(
            proj_no_tax.series[0].portfolio_income_base_monthly_nis * 0.75,
            rel=1e-9,
        )

    def test_tax_does_not_affect_pension_annuity(self, client_with_db):
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

        proj_no_tax = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.0,
            today=date(2026, 5, 27),
        )
        proj_tax = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.25,
            today=date(2026, 5, 27),
        )
        # Find first annuity-active point in both
        ann_idx = next(i for i, p in enumerate(proj_no_tax.series) if p.pension_annuity_monthly_nis > 0)
        assert proj_tax.series[ann_idx].pension_annuity_monthly_nis == pytest.approx(
            proj_no_tax.series[ann_idx].pension_annuity_monthly_nis, rel=1e-9
        )


class TestScenarioRetireReady:
    def test_bull_retires_at_or_before_base(self, client_with_db):
        """Bull (higher income) should retire-ready earlier than or
        at the same time as base; bear later than or never."""
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
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.0,
            today=date(2026, 5, 27),
        )
        # When all three cross, bull <= base <= bear (months_out).
        if (
            proj.retire_ready_months_out_bull is not None
            and proj.retire_ready_months_out_base is not None
        ):
            assert proj.retire_ready_months_out_bull <= proj.retire_ready_months_out_base
        if (
            proj.retire_ready_months_out_base is not None
            and proj.retire_ready_months_out_bear is not None
        ):
            assert proj.retire_ready_months_out_base <= proj.retire_ready_months_out_bear

    def test_legacy_retire_ready_equals_base(self, client_with_db):
        """Backward compat: ``retire_ready_age`` (deprecated) equals
        ``retire_ready_age_base`` exactly."""
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
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.0,
            today=date(2026, 5, 27),
        )
        assert proj.retire_ready_age == proj.retire_ready_age_base
        assert proj.retire_ready_months_out == proj.retire_ready_months_out_base


class TestMonteCarloSimulator:
    def test_seed_makes_run_reproducible(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state, extract_pension_state, project_monte_carlo,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")
        kwargs = dict(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.25,
            n_paths=200, today=date(2026, 5, 27),
        )
        a = project_monte_carlo(**kwargs, seed=42)
        b = project_monte_carlo(**kwargs, seed=42)
        # Same seed → identical P50 path
        for pa, pb in zip(a.series, b.series):
            assert pa.portfolio_value_p50_nis == pytest.approx(pb.portfolio_value_p50_nis)

    def test_p10_below_p50_below_p90(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state, extract_pension_state, project_monte_carlo,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")
        proj = project_monte_carlo(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.25,
            n_paths=500, seed=42, today=date(2026, 5, 27),
        )
        # Skip t=0 (band collapsed); check year 10
        p = proj.series[120]
        assert p.portfolio_value_p10_nis < p.portfolio_value_p25_nis
        assert p.portfolio_value_p25_nis < p.portfolio_value_p50_nis
        assert p.portfolio_value_p50_nis < p.portfolio_value_p75_nis
        assert p.portfolio_value_p75_nis < p.portfolio_value_p90_nis

    def test_failure_probability_higher_at_higher_age(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state, extract_pension_state, project_monte_carlo,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")
        proj = project_monte_carlo(
            household=hh, pensions=pen, retirement_age=49.0, years=50,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.25,
            n_paths=500, seed=42, today=date(2026, 5, 27),
        )
        assert proj.p_failure_before_age_75 <= proj.p_failure_before_age_85
        assert proj.p_failure_before_age_85 <= proj.p_failure_before_age_95

    def test_fraction_solvent_decreasing(self, client_with_db):
        """fraction_solvent should monotonically decrease (paths only fail, never recover)."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state, extract_pension_state, project_monte_carlo,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")
        proj = project_monte_carlo(
            household=hh, pensions=pen, retirement_age=49.0, years=50,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.25,
            n_paths=300, seed=42, today=date(2026, 5, 27),
        )
        for i in range(1, len(proj.series)):
            assert proj.series[i].fraction_solvent <= proj.series[i-1].fraction_solvent + 1e-9


class TestLifestyleDrift:
    def test_drift_increases_expense_growth(self, client_with_db):
        """lifestyle_drift_annual=0.015 → expenses inflate at 4% instead of 2.5%."""
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

        proj_no_drift = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=20,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.0,
            lifestyle_drift_annual=0.0,
            today=date(2026, 5, 27),
        )
        proj_drift = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=20,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.0,
            lifestyle_drift_annual=0.015,
            today=date(2026, 5, 27),
        )
        # At t=0 expenses are equal (no inflation yet)
        assert proj_no_drift.series[0].expenses_monthly_nis == pytest.approx(
            proj_drift.series[0].expenses_monthly_nis, rel=1e-9
        )
        # At t=12 months: no-drift expenses = base * 1.025; drift expenses = base * 1.04.
        # Ratio should be 1.04/1.025 ≈ 1.0146
        idx_12 = 12
        ratio = (
            proj_drift.series[idx_12].expenses_monthly_nis
            / proj_no_drift.series[idx_12].expenses_monthly_nis
        )
        assert ratio == pytest.approx(1.04 / 1.025, rel=1e-3)

    def test_drift_does_not_affect_pension_annuity(self, client_with_db):
        """Pension annuity post-lock inflates at inflation_annual only,
        ignoring lifestyle_drift."""
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

        proj_no_drift = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.0,
            lifestyle_drift_annual=0.0,
            today=date(2026, 5, 27),
        )
        proj_drift = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0, tax_rate=0.0,
            lifestyle_drift_annual=0.05,
            today=date(2026, 5, 27),
        )
        # Find first annuity-active point in both
        ann_idx = next(i for i, p in enumerate(proj_no_drift.series) if p.pension_annuity_monthly_nis > 0)
        # Annuity values 5 years post-lock should be IDENTICAL across the two projections
        annuity_no = proj_no_drift.series[ann_idx + 60].pension_annuity_monthly_nis
        annuity_with = proj_drift.series[ann_idx + 60].pension_annuity_monthly_nis
        assert annuity_no == pytest.approx(annuity_with, rel=1e-9)


class TestLifeEventWireIn:
    """Spec D commit #3 — life events feed the cashflow projection
    through ``apply_life_event_deltas`` inside ``project_cashflow``.

    The shape contract:
      - Passing ``life_events=None`` or ``life_events=[]`` is byte-
        identical to the legacy path (no life-event terms).
      - A ``phase_change_start`` with POSITIVE monthly_delta_usd
        (kids leave home -> less expense) reduces the expense series
        from phase_start_date onward AND drives the solvency crossing
        earlier in time.
      - A heavy recurring expense (every 5y) increases the expense
        series at each occurrence AND pushes the solvency crossing
        later (or off the horizon entirely).
      - ``delta_kind='none'`` events are skipped (no effect).

    These tests are deterministic — same fixtures, same FX, same horizon.
    """

    def _household(self) -> HouseholdState:
        return HouseholdState(
            monthly_expenses_nis=30_000.0,
            portfolio_value_nis=4_000_000.0,
            fx_usd_nis=3.7,
            current_age_years=45.0,
        )

    def _pensions(self) -> PensionState:
        return PensionState(
            kupat_pensia_balance_nis=800_000.0,
            kupat_pensia_contribution_monthly_nis=3_400.0,
            executive_insurance_balance_nis=750_000.0,
            keren_hishtalmut_balance_nis=380_000.0,
            keren_hishtalmut_contribution_monthly_nis=2_700.0,
            kupat_gemel_balance_nis=75_000.0,
        )

    def _phase_change_start(
        self,
        *,
        start_date: date,
        monthly_delta_usd: float,
    ):
        return SimpleNamespace(
            delta_kind="phase_change_start",
            phase_start_date=start_date,
            phase_end_date=None,
            monthly_delta_usd=monthly_delta_usd,
            target_date=None,
            one_shot_amount_usd=None,
            recurring_amount_usd=None,
            recurring_period_years=None,
        )

    def _one_shot(self, *, target_date: date, amount_usd: float):
        return SimpleNamespace(
            delta_kind="one_shot",
            target_date=target_date,
            one_shot_amount_usd=amount_usd,
            phase_start_date=None,
            phase_end_date=None,
            monthly_delta_usd=None,
            recurring_amount_usd=None,
            recurring_period_years=None,
        )

    def _recurring(
        self,
        *,
        anchor_date: date,
        amount_usd: float,
        period_years: int,
    ):
        return SimpleNamespace(
            delta_kind="recurring_every_n_years",
            target_date=anchor_date,
            recurring_amount_usd=amount_usd,
            recurring_period_years=period_years,
            one_shot_amount_usd=None,
            monthly_delta_usd=None,
            phase_start_date=None,
            phase_end_date=None,
        )

    def _none(self):
        return SimpleNamespace(
            delta_kind="none",
            target_date=None,
            phase_start_date=None,
            phase_end_date=None,
            monthly_delta_usd=None,
            one_shot_amount_usd=None,
            recurring_amount_usd=None,
            recurring_period_years=None,
        )

    def _kwargs(self) -> dict:
        return dict(
            household=self._household(),
            pensions=self._pensions(),
            retirement_age=49.0,
            years=30,
            mu_nominal_annual=0.08,
            sigma_annual=0.18,
            inflation_annual=0.025,
            mekadem=200.0,
            tax_rate=0.25,
            today=date(2026, 5, 29),
        )

    def test_life_events_none_byte_identical_to_legacy_path(self):
        """``life_events=None`` should produce a projection identical to
        the pre-Spec-D-commit-3 path (no life-event terms applied)."""
        baseline = project_cashflow(**self._kwargs())
        explicit_none = project_cashflow(**self._kwargs(), life_events=None)
        empty_list = project_cashflow(**self._kwargs(), life_events=[])
        for t, p in enumerate(baseline.series):
            assert (
                p.expenses_monthly_nis
                == pytest.approx(explicit_none.series[t].expenses_monthly_nis)
            )
            assert (
                p.expenses_monthly_nis
                == pytest.approx(empty_list.series[t].expenses_monthly_nis)
            )

    def test_phase_change_kids_leave_home_shifts_expense_series(self):
        """A phase_change_start at year 8 with +1500 USD/mo (kids leave
        home -> less expense) reduces the expense series from month 96
        onward by 1500 * fx, additive on top of the inflated baseline."""
        kwargs = self._kwargs()
        baseline = project_cashflow(**kwargs)
        with_event = project_cashflow(
            **kwargs,
            life_events=[
                self._phase_change_start(
                    start_date=date(2034, 5, 29),  # 96 months from 2026-05-29
                    monthly_delta_usd=1500.0,
                )
            ],
        )
        # Before month 96 — unchanged.
        for t in (0, 12, 60, 95):
            assert (
                with_event.series[t].expenses_monthly_nis
                == pytest.approx(baseline.series[t].expenses_monthly_nis)
            )
        # From month 96 — baseline minus 1500 * fx (sign convention:
        # positive amount_usd = income / expense reduction).
        fx = self._household().fx_usd_nis
        for t in (96, 120, 200, 359):
            expected = baseline.series[t].expenses_monthly_nis - 1500.0 * fx
            assert (
                with_event.series[t].expenses_monthly_nis
                == pytest.approx(expected)
            )

    def test_phase_change_drops_retire_age_earlier(self):
        """Reducing forward expenses (positive monthly_delta) means the
        solvency crossing happens EARLIER (lower retire-ready age)."""
        kwargs = self._kwargs()
        baseline = project_cashflow(**kwargs)
        with_event = project_cashflow(
            **kwargs,
            life_events=[
                self._phase_change_start(
                    start_date=date(2030, 1, 1),
                    monthly_delta_usd=2000.0,  # +2000 USD/mo from 2030
                )
            ],
        )
        assert baseline.retire_ready_age_base is not None
        assert with_event.retire_ready_age_base is not None
        assert (
            with_event.retire_ready_age_base
            <= baseline.retire_ready_age_base
        )

    def test_heavy_recurring_expense_pushes_retire_age_later(self):
        """A recurring -67k USD car every 5y (negative = expense)
        increases cumulative expense and pushes solvency crossing
        later in time vs the baseline (or off the horizon)."""
        kwargs = self._kwargs()
        baseline = project_cashflow(**kwargs)
        with_event = project_cashflow(
            **kwargs,
            life_events=[
                self._recurring(
                    anchor_date=date(2027, 3, 15),
                    amount_usd=-67_000.0,
                    period_years=5,
                )
            ],
        )
        # If both projections cross, with_event >= baseline (later
        # crossing).  If with_event never crosses, that's also a valid
        # "pushed past horizon" result — heavier expense load.
        if (
            baseline.retire_ready_age_base is not None
            and with_event.retire_ready_age_base is not None
        ):
            assert (
                with_event.retire_ready_age_base
                >= baseline.retire_ready_age_base
            )
        # The spike months themselves have the expected delta.
        fx = self._household().fx_usd_nis
        spike_idx = (2027 - 2026) * 12 + (3 - 5)  # = 10
        expected_spike = (
            baseline.series[spike_idx].expenses_monthly_nis
            + 67_000.0 * fx
        )
        assert (
            with_event.series[spike_idx].expenses_monthly_nis
            == pytest.approx(expected_spike)
        )

    def test_none_kind_events_have_no_cashflow_effect(self):
        """``delta_kind='none'`` events are display-only — they must
        NOT shift the expense series or the retire-ready age."""
        kwargs = self._kwargs()
        baseline = project_cashflow(**kwargs)
        with_none = project_cashflow(
            **kwargs,
            life_events=[self._none(), self._none(), self._none()],
        )
        for t in range(0, len(baseline.series), 60):
            assert (
                with_none.series[t].expenses_monthly_nis
                == pytest.approx(baseline.series[t].expenses_monthly_nis)
            )
        assert (
            with_none.retire_ready_age_base
            == baseline.retire_ready_age_base
        )

    def test_one_shot_spike_lands_on_correct_month(self):
        """A one_shot at year 5 lands on the correct month-offset with
        the correct sign-flipped magnitude."""
        kwargs = self._kwargs()
        baseline = project_cashflow(**kwargs)
        # 2031-06 is 61 months past 2026-05.
        with_event = project_cashflow(
            **kwargs,
            life_events=[
                self._one_shot(
                    target_date=date(2031, 6, 10),
                    amount_usd=-50_000.0,  # negative = expense
                )
            ],
        )
        # Spike month: baseline + 50000 * fx.
        fx = self._household().fx_usd_nis
        spike_idx = 61
        assert (
            with_event.series[spike_idx].expenses_monthly_nis
            == pytest.approx(
                baseline.series[spike_idx].expenses_monthly_nis
                + 50_000.0 * fx
            )
        )
        # Neighbors unchanged.
        for t in (spike_idx - 1, spike_idx + 1):
            assert (
                with_event.series[t].expenses_monthly_nis
                == pytest.approx(baseline.series[t].expenses_monthly_nis)
            )

    def test_explicit_fx_override_overrides_household_fx(self):
        """Caller can pass ``fx_usd_nis_for_events`` to override the
        household FX (per spec §2.4 — future scenario-keyed FX)."""
        kwargs = self._kwargs()
        events = [
            self._one_shot(
                target_date=date(2030, 1, 15),
                amount_usd=-1000.0,  # negative = expense
            )
        ]
        # Baseline (no events) to subtract out inflation/lifestyle drift.
        baseline = project_cashflow(**kwargs)
        # Default: uses household.fx_usd_nis = 3.7
        default_fx = project_cashflow(**kwargs, life_events=events)
        # Override to 5.0
        override_fx = project_cashflow(
            **kwargs, life_events=events, fx_usd_nis_for_events=5.0,
        )
        spike_idx = (2030 - 2026) * 12 + (1 - 5)  # 44
        default_event_delta = (
            default_fx.series[spike_idx].expenses_monthly_nis
            - baseline.series[spike_idx].expenses_monthly_nis
        )
        override_event_delta = (
            override_fx.series[spike_idx].expenses_monthly_nis
            - baseline.series[spike_idx].expenses_monthly_nis
        )
        # Pure event contribution at default FX: -(-1000 * 3.7) = +3700.
        assert default_event_delta == pytest.approx(3700.0, rel=1e-9)
        # Pure event contribution at override FX: -(-1000 * 5.0) = +5000.
        assert override_event_delta == pytest.approx(5000.0, rel=1e-9)
        # delta ratio = fx ratio = 5.0 / 3.7
        assert (
            override_event_delta
            == pytest.approx(default_event_delta * 5.0 / 3.7, rel=1e-9)
        )


class TestEffectiveRetireReadyAgeLifeEventClampRemoved:
    """Spec D commit #3 — verify the old life-event clamp branch is
    GONE.

    These tests pin the contract from spec §7.3:
      - ``EffectiveRetireReadyAge`` no longer carries
        ``life_event_clamp_date`` (attribute does not exist).
      - ``clamp_reason`` never returns the string ``'life_event'``
        regardless of what life events are seeded.
      - The retire-ready age is determined ONLY by (a) base cashflow
        feasibility and (b) the RSU vest clamp.  A legacy-shape
        ``retirement_milestone:target_retire_year_change`` row stored
        with ``delta_kind='none'`` has zero effect on the result.
    """

    def test_dataclass_has_no_life_event_clamp_date_field(self):
        """Regression: the field was removed in commit #3.  Anyone
        accessing it should get a clear AttributeError, not a silent
        None."""
        from argosy.services.cashflow_projection import EffectiveRetireReadyAge
        fields = {f.name for f in EffectiveRetireReadyAge.__dataclass_fields__.values()}
        assert "life_event_clamp_date" not in fields
        assert "rsu_clamp_date" in fields  # rsu clamp remains

    def test_clamp_reason_literal_does_not_include_life_event(self):
        """The clamp-reason candidate tuple in effective_retire_ready_age
        no longer contains the ``life_event`` entry.  Source-level
        assertion via reading the docstring of the dataclass — it
        enumerates valid reasons and must NOT include 'life_event'."""
        from argosy.services.cashflow_projection import EffectiveRetireReadyAge
        doc = EffectiveRetireReadyAge.__doc__ or ""
        # The clamp_reason values are enumerated in the docstring.
        # The 'life_event' value must NOT appear as a clamp_reason
        # (the removal note explicitly calls out the removal).
        assert "'life_event'" not in doc or "REMOVED" in doc


class TestProjectCashflowWiringRegression:
    """The bare project_cashflow signature still accepts the old kwarg
    set unchanged — new args (life_events, fx_usd_nis_for_events) are
    optional and default to no-op behavior."""

    def test_legacy_kwarg_set_still_works(self, client_with_db):
        """No regression on existing callers (plan.py etc.) — calling
        project_cashflow with only the historical kwargs returns the
        same projection it always did."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full_state(s)
            from argosy.services.cashflow_projection import (
                extract_household_state,
                extract_pension_state,
            )
            hh = extract_household_state(s, "ariel", today=date(2026, 5, 27))
            pen = extract_pension_state(s, "ariel")
        proj = project_cashflow(
            household=hh, pensions=pen, retirement_age=49.0, years=30,
            mu_nominal_annual=0.08, sigma_annual=0.18,
            inflation_annual=0.025, mekadem=200.0,
            today=date(2026, 5, 27),
        )
        # Sanity: a 30-year projection produces 361 monthly points.
        assert len(proj.series) == 30 * 12 + 1
        # And the retire-ready fields are populated as before.
        assert proj.retire_ready_age_base is not None


class TestPreRetirementSavingsContribution:
    """Wave 8 v2 polish — savings during the accumulation phase.

    Pre-fix: cashflow_projection withdrew expenses every month from
    age 44 onward regardless of retirement_age, treating every user
    as already retired today. The MC simulation's "worst-10% depletes
    by age 55" was a direct consequence of that bug. Post-fix: when
    age_t < retirement_age, the household's monthly_savings_nis flows
    INTO the portfolio and the expenses-shortfall withdrawal is
    suppressed. These tests pin the new contract.
    """

    def _pensions(self) -> PensionState:
        return PensionState(
            kupat_pensia_balance_nis=1_000_000.0,
            kupat_pensia_contribution_monthly_nis=3_500.0,
            executive_insurance_balance_nis=500_000.0,
            keren_hishtalmut_balance_nis=400_000.0,
            keren_hishtalmut_contribution_monthly_nis=2_500.0,
            kupat_gemel_balance_nis=100_000.0,
        )

    def test_deterministic_savings_grows_portfolio_pre_retirement(self) -> None:
        from argosy.services.cashflow_projection import (
            HouseholdState,
            project_cashflow,
        )

        with_savings = project_cashflow(
            household=HouseholdState(
                monthly_expenses_nis=25_000.0,
                portfolio_value_nis=3_000_000.0,
                fx_usd_nis=3.0,
                current_age_years=44.0,
                monthly_savings_nis=30_000.0,
            ),
            pensions=self._pensions(),
            retirement_age=49.0,
            years=10,
        )
        without_savings = project_cashflow(
            household=HouseholdState(
                monthly_expenses_nis=25_000.0,
                portfolio_value_nis=3_000_000.0,
                fx_usd_nis=3.0,
                current_age_years=44.0,
                monthly_savings_nis=0.0,
            ),
            pensions=self._pensions(),
            retirement_age=49.0,
            years=10,
        )
        # At 5-year mark (right at retirement_age=49), the with-savings
        # portfolio should be meaningfully larger than the no-savings
        # baseline — at least 30k * 60 months = 1.8M NIS of cumulative
        # contributions minus inflation drift.
        with_at_5y = with_savings.series[60].portfolio_value_base_nis
        without_at_5y = without_savings.series[60].portfolio_value_base_nis
        assert with_at_5y > without_at_5y + 1_500_000.0

    def test_monte_carlo_skips_withdrawal_pre_retirement(self) -> None:
        from argosy.services.cashflow_projection import (
            HouseholdState,
            project_monte_carlo,
        )

        mc = project_monte_carlo(
            household=HouseholdState(
                monthly_expenses_nis=25_000.0,
                portfolio_value_nis=3_000_000.0,
                fx_usd_nis=3.0,
                current_age_years=44.0,
                monthly_savings_nis=30_000.0,
            ),
            pensions=self._pensions(),
            retirement_age=49.0,
            years=5,
            n_paths=200,
            seed=42,
            sigma_annual=0.18,
        )
        # All paths solvent at age 49 — no withdrawals during accumulation.
        last = mc.series[-1]
        assert last.fraction_solvent == 1.0
        assert mc.p_failure_before_age_75 == 0.0

    def test_higher_retirement_age_lowers_p_broke(self) -> None:
        """The user's regression complaint: higher retire age was
        making P(broke) go UP. Post-fix, working longer should
        ALWAYS hurt less for a saver."""
        from argosy.services.cashflow_projection import (
            HouseholdState,
            project_monte_carlo,
        )

        common = dict(
            household=HouseholdState(
                monthly_expenses_nis=25_000.0,
                portfolio_value_nis=3_000_000.0,
                fx_usd_nis=3.0,
                current_age_years=44.0,
                monthly_savings_nis=30_000.0,
            ),
            pensions=self._pensions(),
            years=50,
            n_paths=200,
            seed=42,
            sigma_annual=0.34,  # NVDA-heavy vol to provoke failures
        )
        retire_49 = project_monte_carlo(retirement_age=49.0, **common)
        retire_67 = project_monte_carlo(retirement_age=67.0, **common)
        assert (
            retire_67.p_failure_before_age_95
            <= retire_49.p_failure_before_age_95
        )

    def test_mc_income_composition_fields(self) -> None:
        """Deterministic income-composition fields drive the cashflow-coverage
        chart: BL stipend starts at bl_start_age, the age-60 lump fires at one
        tick, and the post-67 portfolio draw collapses once the annuity + BL
        cover most of the spend. Existing fields are untouched."""
        from argosy.services.cashflow_projection import (
            HouseholdState,
            PensionState,
            project_monte_carlo,
        )

        current_age = 44.0
        retirement_age = 60.0
        bl_start_age = 67.0
        infl = 0.025
        annuity_tax_rate = 0.15
        bl_real_monthly = 5_000.0

        pensions = PensionState(
            kupat_pensia_balance_nis=1_500_000.0,
            kupat_pensia_contribution_monthly_nis=3_500.0,
            executive_insurance_balance_nis=900_000.0,
            keren_hishtalmut_balance_nis=600_000.0,
            keren_hishtalmut_contribution_monthly_nis=2_500.0,
            kupat_gemel_balance_nis=200_000.0,
        )
        initial_portfolio = 4_000_000.0
        mc = project_monte_carlo(
            household=HouseholdState(
                monthly_expenses_nis=25_000.0,
                portfolio_value_nis=initial_portfolio,
                fx_usd_nis=3.0,
                current_age_years=current_age,
                monthly_savings_nis=20_000.0,
            ),
            pensions=pensions,
            retirement_age=retirement_age,
            years=30,  # reaches age 74 — past BL start at 67
            mu_nominal_annual=0.08,
            sigma_annual=0.18,
            inflation_annual=infl,
            mekadem=200.0,
            n_paths=200,
            seed=42,
            today=date(2026, 5, 27),
            bl_annuity_monthly_nis=bl_real_monthly,
            bl_start_age=bl_start_age,
            annuity_tax_rate=annuity_tax_rate,
            apply_age_aware_tax=True,
        )

        # --- Spot-check an existing field is unchanged: p50 at t=0 == portfolio
        assert mc.series[0].portfolio_value_p50_nis == pytest.approx(
            initial_portfolio
        )

        # --- bl_monthly_nis: 0 before bl_start_age, > 0 after.
        before_bl = [
            p for p in mc.series if p.age_years < bl_start_age
        ]
        after_bl = [
            p for p in mc.series if p.age_years >= bl_start_age
        ]
        assert all(p.bl_monthly_nis == 0.0 for p in before_bl)
        assert all(p.bl_monthly_nis > 0.0 for p in after_bl)
        # And it is the real figure nominalized from t=0 at the first BL tick.
        first_bl = after_bl[0]
        t = first_bl.months_out
        assert first_bl.bl_monthly_nis == pytest.approx(
            bl_real_monthly * ((1.0 + infl) ** (t / 12.0))
        )

        # --- lump_amount_nis: nonzero at exactly one tick near age 60, 0 else.
        lump_ticks = [p for p in mc.series if p.lump_amount_nis > 0.0]
        assert len(lump_ticks) == 1
        lump_pt = lump_ticks[0]
        assert 60.0 <= lump_pt.age_years < 60.1  # fires the first tick past 60
        assert lump_pt.lump_amount_nis > 0.0

        # --- portfolio_net_draw post-67 << just-before-67 (annuity + BL cover
        # most spend). Compare the tick just before age 67 to the one just after.
        just_before_67 = max(
            (p for p in mc.series if p.age_years < bl_start_age),
            key=lambda p: p.age_years,
        )
        just_after_67 = min(
            (p for p in mc.series if p.age_years >= bl_start_age),
            key=lambda p: p.age_years,
        )
        assert just_before_67.portfolio_net_draw_monthly_nis > 0.0
        assert (
            just_after_67.portfolio_net_draw_monthly_nis
            < 0.5 * just_before_67.portfolio_net_draw_monthly_nis
        )

        # --- gross withdrawal grosses up the net draw by (1 - eff_tax); at 67+
        # the age-band rate is 12%, so gross == net / 0.88.
        assert just_after_67.portfolio_gross_withdrawal_monthly_nis == pytest.approx(
            just_after_67.portfolio_net_draw_monthly_nis / (1.0 - 0.12)
        )
        # Pre-retirement (accumulation) ticks have zero draw.
        pre_ret = [p for p in mc.series if p.age_years < retirement_age]
        assert all(
            p.portfolio_net_draw_monthly_nis == 0.0
            and p.portfolio_gross_withdrawal_monthly_nis == 0.0
            for p in pre_ret
        )


class TestProjectCashflowAnnuityNominalization:
    """Codex review 2026-06-04: the deterministic /plan cashflow engine must
    nominalize the real-grown annuity + age-60 lump from t=0 (the same fix
    applied to the MC), so /plan and /retirement agree on pension income."""

    def test_annuity_at_68_nominalized_from_t0(self):
        from datetime import date

        from argosy.services.cashflow_projection import (
            HouseholdState,
            PensionState,
            project_cashflow,
        )

        current_age, mekadem = 44.0, 200.0
        mu, infl = 0.07, 0.025
        real_monthly = 1.0 + (mu - infl) / 12.0
        p = PensionState(
            kupat_pensia_balance_nis=800_000.0,
            kupat_pensia_contribution_monthly_nis=0.0,
            executive_insurance_balance_nis=750_000.0,
            keren_hishtalmut_balance_nis=0.0,
            keren_hishtalmut_contribution_monthly_nis=0.0,
            kupat_gemel_balance_nis=0.0,
        )
        h = HouseholdState(
            monthly_expenses_nis=20_000.0, portfolio_value_nis=10_000_000.0,
            fx_usd_nis=3.7, current_age_years=current_age, monthly_savings_nis=0.0,
        )
        proj = project_cashflow(
            household=h, pensions=p, retirement_age=49.0, years=30,
            mu_nominal_annual=mu, inflation_annual=infl, mekadem=mekadem,
            today=date(2026, 1, 1),
        )
        t_lock = round((67.0 - current_age) * 12)
        bal_at_lock = (800_000.0 + 750_000.0) * (real_monthly ** t_lock)
        annuity_real = bal_at_lock / mekadem
        t68 = round((68.0 - current_age) * 12)
        expected = annuity_real * ((1.0 + infl) ** (t68 / 12.0))
        assert proj.series[t68].pension_annuity_monthly_nis == pytest.approx(expected, rel=1e-6)
