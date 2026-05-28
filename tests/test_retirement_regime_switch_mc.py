"""Tests for the regime-switch Monte Carlo (Wave 3 · HIGH #11)."""
import json
from datetime import date, datetime, timezone

import numpy as np
import pytest

from argosy.services.cashflow_projection import (
    extract_household_state,
    extract_pension_state,
)
from argosy.services.retirement.regime_switch_mc import (
    DEFAULT_REGIME_PARAMS,
    RegimeSwitchResult,
    regime_summary_value,
    simulate_regime_switch,
)
from argosy.state.models import (
    AgentReport,
    PortfolioSnapshotRow,
    User,
    UserContext,
)


def _seed_minimum(session, *, monthly_burn_nis: float = 20_000.0) -> None:
    if session.get(User, "ariel") is None:
        session.add(User(id="ariel", plan="free"))
    session.add(
        UserContext(
            user_id="ariel",
            identity_yaml=(
                "user_date_of_birth: '1982-08-28'\n"
                "fx_rate:\n  usd_nis: 3.0\n"
                "pensions:\n"
                "  kupat_pensia:\n    balance_nis: 800000\n"
                "    contribution_rate_pct: 6.0\n"
                "    employer_match_pct: 6.5\n"
                "  keren_hishtalmut:\n    balance_nis: 380000\n"
                "    contribution_rate_pct: 2.5\n"
                "    employer_match_pct: 7.5\n"
                "  executive_insurance:\n    balance_nis: 755000\n"
                "  kupat_gemel:\n    balance_nis: 75000\n"
                "clal_pension_salary_basis_monthly_nis: 27000\n"
                "clal_pension_employee_pct: 6.0\n"
                "clal_pension_employer_pct: 6.5\n"
                "clal_pension_severance_pct: 8.33\n"
            ),
            goals_yaml="",
            constraints_yaml="",
            current_stage="complete",
        )
    )
    session.add(
        PortfolioSnapshotRow(
            user_id="ariel",
            snapshot_date=date(2026, 5, 1),
            imported_at=datetime.now(timezone.utc),
            source_path="/tmp/seed.tsv",
            positions_json="[]",
            allocations_json="[]",
            nvda_sales_json="[]",
            real_estate_json="[]",
            totals_json=json.dumps({"fx_usd_nis": 3.0, "total_usd_value_k": 3500.0}),
        )
    )
    session.add(
        AgentReport(
            user_id="ariel",
            agent_role="household_budget",
            response_text=json.dumps({
                "monthly_burn_nis": monthly_burn_nis,
                "monthly_income_nis": 50_000.0,
            }),
            decision_id="test",
        )
    )
    session.commit()


class TestRegimeSwitch:
    def test_returns_expected_shape(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_minimum(s, monthly_burn_nis=20_000.0)
            household = extract_household_state(s, user_id="ariel")
            pensions = extract_pension_state(s, user_id="ariel")
            r = simulate_regime_switch(
                household=household,
                pensions=pensions,
                retirement_age=49.0,
                years=40,
                n_paths=300,
                seed=42,
            )
        assert isinstance(r, RegimeSwitchResult)
        assert r.n_paths == 300
        assert r.portfolio_p50.shape[0] == r.months + 1
        # P10 <= P50 <= P90 at every tick
        assert np.all(r.portfolio_p10 <= r.portfolio_p50 + 1e-6)
        assert np.all(r.portfolio_p50 <= r.portfolio_p90 + 1e-6)
        # Fraction solvent is monotone non-increasing
        diffs = np.diff(r.fraction_solvent_per_month)
        assert np.all(diffs <= 1e-9)
        # Regime occupancies sum to ≈ 1.0
        total_occ = sum(r.regime_occupancy.values())
        assert total_occ == pytest.approx(1.0, abs=0.001)
        # Calm is the dominant regime
        assert r.regime_occupancy["calm"] > r.regime_occupancy["turbulent"]
        assert r.regime_occupancy["calm"] > r.regime_occupancy["crisis"]

    def test_seed_reproducibility(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_minimum(s, monthly_burn_nis=20_000.0)
            household = extract_household_state(s, user_id="ariel")
            pensions = extract_pension_state(s, user_id="ariel")
            r1 = simulate_regime_switch(
                household=household, pensions=pensions, retirement_age=49.0,
                n_paths=300, seed=42,
            )
            r2 = simulate_regime_switch(
                household=household, pensions=pensions, retirement_age=49.0,
                n_paths=300, seed=42,
            )
        assert r1.p_failure_before_age == r2.p_failure_before_age
        assert r1.regime_occupancy == r2.regime_occupancy

    def test_regime_summary_value(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_minimum(s, monthly_burn_nis=20_000.0)
            household = extract_household_state(s, user_id="ariel")
            pensions = extract_pension_state(s, user_id="ariel")
            r = simulate_regime_switch(
                household=household, pensions=pensions, retirement_age=49.0,
                n_paths=300, seed=42,
            )
        v = regime_summary_value(r)
        assert 0.0 <= v.value <= 1.0
        assert v.unit == "fraction"
        assert "regime" in v.rationale.lower()

    def test_default_regime_params_are_reasonable(self):
        params = DEFAULT_REGIME_PARAMS
        # Crisis has negative drift; calm has positive
        assert params["crisis"][0] < 0
        assert params["calm"][0] > 0
        # Crisis sigma > turbulent > calm
        assert params["crisis"][1] > params["turbulent"][1]
        assert params["turbulent"][1] > params["calm"][1]
