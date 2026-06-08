"""Tests for the regime-switch Monte Carlo (Wave 3 · HIGH #11)."""
import json
from datetime import date, datetime, timezone

import numpy as np
import pytest

from argosy.services.cashflow_projection import (
    HouseholdState,
    PensionState,
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


def _direct_household(spend: float, age: float = 44.0) -> HouseholdState:
    return HouseholdState(
        monthly_expenses_nis=spend,
        portfolio_value_nis=5_000_000.0,
        fx_usd_nis=3.7,
        current_age_years=age,
        monthly_savings_nis=0.0,
    )


def _direct_pensions() -> PensionState:
    return PensionState(
        kupat_pensia_balance_nis=800_000.0,
        kupat_pensia_contribution_monthly_nis=3_400.0,
        executive_insurance_balance_nis=750_000.0,
        keren_hishtalmut_balance_nis=380_000.0,
        keren_hishtalmut_contribution_monthly_nis=2_700.0,
        kupat_gemel_balance_nis=75_000.0,
    )


class TestStationaryPensionGrowth:
    """Codex review BLOCKER 3: pension/hishtalmut/gemel balances must grow at
    the stationary-distribution-weighted regime return (calm dominates ~75%),
    NOT the simple average of the three regime μs — that average is ~−7.8% real
    and made pensions SHRINK, contaminating the fat-tail solvency."""

    def test_long_run_real_return_is_positive(self):
        from argosy.services.retirement.regime_switch_mc import (
            DEFAULT_REGIME_PARAMS,
            DEFAULT_TRANSITION_MATRIX,
            stationary_real_return,
        )
        r = stationary_real_return(
            DEFAULT_REGIME_PARAMS, DEFAULT_TRANSITION_MATRIX, inflation_annual=0.025,
        )
        # Calm-dominated chain → ~5% real, decisively positive — not the
        # −7.8% simple-average artifact.
        assert r > 0.02

    def test_simple_average_would_have_been_negative(self):
        """Guards the regression: the OLD simple-average real return was
        negative, which is exactly the artifact we removed."""
        from argosy.services.retirement.regime_switch_mc import DEFAULT_REGIME_PARAMS
        simple_avg = sum(v[0] for v in DEFAULT_REGIME_PARAMS.values()) / 3.0 - 0.025
        assert simple_avg < 0.0


class TestRegimeBituachLeumi:
    """BL income must be creditable in the regime engine too, so the fat-tail
    readout uses the same basis as the scenario table (codex MC review Q1 #5)."""

    COMMON = dict(retirement_age=49.0, years=52, n_paths=400, seed=7)

    def test_bl_income_reduces_failure(self):
        h = _direct_household(spend=40_000.0)
        base = simulate_regime_switch(
            household=h, pensions=_direct_pensions(), **self.COMMON,
        )
        with_bl = simulate_regime_switch(
            household=h, pensions=_direct_pensions(),
            bl_annuity_monthly_nis=8_000.0, **self.COMMON,
        )
        assert with_bl.p_failure_before_age[95] < base.p_failure_before_age[95]

    def test_bl_default_zero_is_unchanged(self):
        h = _direct_household(spend=40_000.0)
        base = simulate_regime_switch(
            household=h, pensions=_direct_pensions(), **self.COMMON,
        )
        explicit_zero = simulate_regime_switch(
            household=h, pensions=_direct_pensions(),
            bl_annuity_monthly_nis=0.0, **self.COMMON,
        )
        assert explicit_zero.p_failure_before_age[95] == base.p_failure_before_age[95]


class TestRegimeSigmaScale:
    """H8: the regime fat-tail engine must follow a per-month sigma-SCALE path
    (the dual-track glide expressed against the engine's own stationary vol), so
    the hero P(solvent) reflects the user's NVDA concentration declining over the
    sell-down — not a fixed diversified vol that ignores it."""

    def test_stationary_sigma_is_diversified_like(self):
        from argosy.services.retirement.regime_switch_mc import (
            DEFAULT_REGIME_PARAMS,
            DEFAULT_TRANSITION_MATRIX,
            stationary_sigma,
        )

        s = stationary_sigma(DEFAULT_REGIME_PARAMS, DEFAULT_TRANSITION_MATRIX)
        # calm-dominated chain -> blended vol ~0.15-0.19 (diversified-equity-like)
        assert 0.14 < s < 0.20

    def test_idio_overlay_raises_failure_via_normal_regimes(self):
        # The variance-additive idiosyncratic overlay lifts the calm/turbulent
        # vols (concentration risk in normal markets) -> more ruin by 95. The
        # crisis regime is left unscaled (systematic), so this is NOT the
        # over-punishing uniform multiply. None == default (back-compat).
        import numpy as np

        h = _direct_household(spend=30_000.0)
        common = dict(retirement_age=49.0, years=52, n_paths=600, seed=11)
        months = max(1, min(52, 60)) * 12
        base = simulate_regime_switch(
            household=h, pensions=_direct_pensions(), **common,
        )
        # idio variance ~ (0.30^2 - 0.18^2): a concentrated book above diversified.
        idio = np.full(months, 0.30 ** 2 - 0.18 ** 2)
        overlaid = simulate_regime_switch(
            household=h, pensions=_direct_pensions(),
            idio_var_path=idio, **common,
        )
        assert overlaid.p_failure_before_age[95] > base.p_failure_before_age[95]


class TestRegimePhaseExpenses:
    """H3: the documented life-stage phases (empty-nest dip + heavy late-life LTC
    tail) must shape the regime ruin math too — not only project_monte_carlo —
    so the fat-tail readout and the dual-track headline share one expense basis."""

    COMMON = dict(retirement_age=49.0, years=52, n_paths=600, seed=11)

    def test_phases_worsen_late_life_failure(self):
        # The premium-driven late-life ramp (up to ~2x current real burn by the
        # 90s) outweighs the earlier empty-nest dip, so ruin-by-95 rises.
        h = _direct_household(spend=30_000.0)
        flat = simulate_regime_switch(
            household=h, pensions=_direct_pensions(), **self.COMMON,
        )
        phased = simulate_regime_switch(
            household=h, pensions=_direct_pensions(),
            apply_expense_phases=True, **self.COMMON,
        )
        assert phased.p_failure_before_age[95] > flat.p_failure_before_age[95]

    def test_default_off_is_unchanged(self):
        h = _direct_household(spend=30_000.0)
        base = simulate_regime_switch(
            household=h, pensions=_direct_pensions(), **self.COMMON,
        )
        explicit_false = simulate_regime_switch(
            household=h, pensions=_direct_pensions(),
            apply_expense_phases=False, **self.COMMON,
        )
        assert base.p_failure_before_age[95] == explicit_false.p_failure_before_age[95]
