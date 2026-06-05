"""Tests for the retirement scenario runner core (codex MC review 2026-06-04).

``simulate_scenarios`` is the pure, DB-free core: given household/pension state
plus an explicit spend basis + BL stipend, it runs the codex-specified
scenario grid (base 4.5% real, bull 5.5%, bear −25% shock + 3% real decade) on
the lognormal MC, the 4.0/4.5/5.0/5.5 μ-grid sensitivity, a ₪277k T12
sensitivity, and the regime-switch fat-tail readout — all at the SAME basis so
the numbers are comparable.
"""
import json
from datetime import date, datetime, timezone

from argosy.services.cashflow_projection import HouseholdState, PensionState
from argosy.services.retirement.scenario_mc import (
    run_retirement_scenarios,
    simulate_scenarios,
)
from argosy.state.models import (
    AgentReport,
    PortfolioSnapshotRow,
    User,
    UserContext,
)


def _household() -> HouseholdState:
    return HouseholdState(
        monthly_expenses_nis=0.0,  # ignored — the core sets spend explicitly
        portfolio_value_nis=5_000_000.0,
        fx_usd_nis=3.7,
        current_age_years=43.8,
        monthly_savings_nis=0.0,
    )


def _pensions() -> PensionState:
    return PensionState(
        kupat_pensia_balance_nis=800_000.0,
        kupat_pensia_contribution_monthly_nis=3_400.0,
        executive_insurance_balance_nis=750_000.0,
        keren_hishtalmut_balance_nis=380_000.0,
        keren_hishtalmut_contribution_monthly_nis=2_700.0,
        kupat_gemel_balance_nis=75_000.0,
    )


CORE = dict(
    retirement_age=49.0,
    spend_basis_annual_nis=311_584.0,
    spend_t12_annual_nis=277_000.0,
    bl_monthly_nis=4_000.0,
    inflation_annual=0.025,
    sigma_annual=0.18,
    n_paths=400,
    seed=42,
    today=date(2026, 1, 1),
)


def _run(**over):
    return simulate_scenarios(
        household=_household(), pensions=_pensions(), **{**CORE, **over}
    )


def _by_name(grid, name):
    return next(s for s in grid.scenarios if s.name == name)


class TestShape:
    def test_three_named_scenarios(self):
        g = _run()
        names = {s.name for s in g.scenarios}
        assert names == {"bear", "base", "bull"}

    def test_mu_grid_is_the_four_codex_points(self):
        g = _run()
        reals = sorted(round(p.mu_real_pct, 3) for p in g.mu_grid)
        assert reals == [0.040, 0.045, 0.050, 0.055]

    def test_all_probabilities_in_unit_interval(self):
        g = _run()
        probs = (
            [s.p_solvent_95 for s in g.scenarios]
            + [p.p_solvent_95 for p in g.mu_grid]
            + [g.fat_tail_p_solvent_95, g.t12_sensitivity_p_solvent_95]
        )
        assert all(0.0 <= p <= 1.0 for p in probs)


class TestScenarioSemantics:
    def test_bull_at_least_as_solvent_as_base_at_least_as_bear(self):
        g = _run()
        bear, base, bull = _by_name(g, "bear"), _by_name(g, "base"), _by_name(g, "bull")
        assert bull.p_solvent_95 >= base.p_solvent_95 >= bear.p_solvent_95

    def test_bear_strictly_worse_than_base(self):
        """The −25% shock + low-return decade must materially hurt — not a tie."""
        g = _run()
        assert _by_name(g, "bear").p_solvent_95 < _by_name(g, "base").p_solvent_95

    def test_bear_carries_the_shock_others_do_not(self):
        g = _run()
        assert _by_name(g, "bear").initial_shock_pct == 0.25
        assert _by_name(g, "base").initial_shock_pct == 0.0
        assert _by_name(g, "bull").initial_shock_pct == 0.0

    def test_central_real_returns_are_codex_values(self):
        g = _run()
        assert round(_by_name(g, "base").mu_real_pct, 3) == 0.045
        assert round(_by_name(g, "bull").mu_real_pct, 3) == 0.055

    def test_nominal_is_real_plus_inflation(self):
        g = _run()
        base = _by_name(g, "base")
        assert abs(base.mu_nominal_pct - (base.mu_real_pct + 0.025)) < 1e-9


class TestSensitivities:
    def test_mu_grid_monotone_in_return(self):
        g = _run()
        pts = sorted(g.mu_grid, key=lambda p: p.mu_real_pct)
        probs = [p.p_solvent_95 for p in pts]
        assert probs == sorted(probs)  # non-decreasing in μ

    def test_lower_t12_spend_is_more_solvent_than_basis(self):
        """₪277k T12 < ₪311.6k permanent-equivalent basis → the T12 sensitivity
        must be at least as solvent as the base scenario."""
        g = _run()
        assert g.t12_sensitivity_p_solvent_95 >= _by_name(g, "base").p_solvent_95

    def test_spend_basis_recorded(self):
        g = _run()
        assert g.spend_basis_annual_nis == 311_584.0
        assert g.spend_t12_annual_nis == 277_000.0
        assert g.bl_monthly_nis == 4_000.0


class TestDeterminism:
    def test_same_seed_same_result(self):
        a = _run()
        b = _run()
        assert _by_name(a, "base").p_solvent_95 == _by_name(b, "base").p_solvent_95
        assert a.fat_tail_p_solvent_95 == b.fat_tail_p_solvent_95


_ADAPTER_IDENTITY = (
    "user_date_of_birth: '1982-08-28'\n"
    "fx_rate:\n  usd_nis: 3.7\n"
    "monthly_expenses_total_nis: 23084\n"
    "monthly_expenses_breakdown:\n  mortgage_nis: 2952\n"
    "mortgage_balance:\n  keret_1_nis: 350000\n"
    "pensions:\n"
    "  kupat_pensia:\n    balance_nis: 800000\n"
    "  keren_hishtalmut:\n    balance_nis: 380000\n"
    "  executive_insurance:\n    balance_nis: 755000\n"
    "  kupat_gemel:\n    balance_nis: 75000\n"
)
_ADAPTER_GOALS = (
    "education_funding_targets:\n  combined_household_contribution_nis: 1000000\n"
    "retirement_drawdown_style: capital_preservation_returns_only\n"
)


def _seed_full(session) -> None:
    if session.get(User, "ariel") is None:
        session.add(User(id="ariel", plan="free"))
    session.add(UserContext(
        user_id="ariel", identity_yaml=_ADAPTER_IDENTITY, goals_yaml=_ADAPTER_GOALS,
        constraints_yaml="", current_stage="complete",
    ))
    session.add(PortfolioSnapshotRow(
        user_id="ariel", snapshot_date=date(2026, 5, 1),
        imported_at=datetime.now(timezone.utc), source_path="/tmp/seed.tsv",
        positions_json="[]", allocations_json="[]", nvda_sales_json="[]",
        real_estate_json="[]",
        totals_json=json.dumps({"fx_usd_nis": 3.7, "total_usd_value_k": 3000.0}),
    ))
    session.add(AgentReport(
        user_id="ariel", agent_role="household_budget",
        response_text=json.dumps({"monthly_burn_nis": 23_084.0, "monthly_income_nis": 50_000.0}),
        decision_id="test",
    ))
    session.commit()


class TestDbAdapter:
    def test_resolves_permanent_spend_basis_and_bl(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full(s)
            g = run_retirement_scenarios(
                user_id="ariel", session=s, retirement_age=49.0,
                n_paths=200, seed=42, today=date(2026, 1, 1),
            )
        # Spend basis is the permanent-equivalent number, not the T12 burn.
        assert round(g.spend_basis_annual_nis) == 311_584
        assert round(g.spend_t12_annual_nis) == 277_008
        # BL income is credited (Ariel has a long insured history).
        assert g.bl_monthly_nis > 0.0
        assert "fi_methodology" in g.spend_basis_source
        # Pension annuity is netted of a sourced, non-zero income tax — NOT
        # credited gross (codex review 2026-06-04).
        assert 0.0 < g.annuity_tax_rate < 0.5
        assert "tax_engine" in g.annuity_tax_source
        # Shape sanity.
        assert {x.name for x in g.scenarios} == {"bear", "base", "bull"}
        assert len(g.mu_grid) == 4

    def test_route_returns_scenario_table(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full(s)
        r = client_with_db.get(
            "/api/retirement/projection/scenarios?user_id=ariel&n_paths=200&seed=42"
        )
        assert r.status_code == 200
        body = r.json()
        assert round(body["spend_basis_annual_nis"]) == 311_584
        assert body["bl_monthly_nis"] > 0.0
        assert {x["name"] for x in body["scenarios"]} == {"bear", "base", "bull"}
        assert len(body["mu_grid"]) == 4
        assert 0.0 <= body["fat_tail_p_solvent_95"] <= 1.0

    def test_route_404_when_no_fi_basis(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            if s.get(User, "ariel") is None:
                s.add(User(id="ariel", plan="free"))
            s.add(UserContext(
                user_id="ariel", identity_yaml="user_date_of_birth: '1982-08-28'\n",
                goals_yaml="", constraints_yaml="", current_stage="complete",
            ))
            s.commit()
        r = client_with_db.get(
            "/api/retirement/projection/scenarios?user_id=ariel&n_paths=50&seed=1"
        )
        assert r.status_code == 404

    def test_raises_when_no_fi_basis(self, client_with_db):
        """No identity spend → no permanent-equivalent basis → loud failure,
        never a fabricated constant."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            if s.get(User, "ariel") is None:
                s.add(User(id="ariel", plan="free"))
            s.add(UserContext(
                user_id="ariel", identity_yaml="user_date_of_birth: '1982-08-28'\n",
                goals_yaml="", constraints_yaml="", current_stage="complete",
            ))
            s.commit()
            import pytest
            with pytest.raises(ValueError):
                run_retirement_scenarios(
                    user_id="ariel", session=s, n_paths=50, seed=1,
                    today=date(2026, 1, 1),
                )


class TestEarliestFeasibleAge:
    """The ONE canonical retirement age every surface binds to: the earliest
    age the MC base case clears the solvency bar with the finite-liability
    reserve earmarked (codex/age-coherence 1b). Kills the deterministic 44."""

    def test_contract_reserve_netted_and_labeled(self, client_with_db):
        from argosy.services.retirement.scenario_mc import earliest_feasible_retire_age
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full(s)
            r = earliest_feasible_retire_age(
                session=s, user_id="ariel", target_p_solvent=0.90,
                operational_target_age=49.0, n_paths=400, seed=42, today=date(2026, 1, 1),
            )
        # Labeled anchors + the reserve genuinely earmarked out of the portfolio.
        assert r.target_p_solvent == 0.90
        assert r.statutory_lump_age == 60 and r.statutory_annuity_age == 67
        assert r.operational_target_age == 49.0
        assert r.reserve_netted_nis > 0
        # Portfolio used for the sweep is net of the earmarked reserve.
        assert r.basis["portfolio_after_reserve_nis"] >= 0
        assert r.basis["reserve_netted_nis"] == r.reserve_netted_nis
        # When an age IS found it must actually clear the bar and be ≥ current age
        # (NEVER the deterministic current-age 'retire now' artifact unless it
        # genuinely clears the sequence-aware MC bar).
        if r.earliest_feasible_age is not None:
            assert r.earliest_feasible_age >= r.current_age
            assert r.p_solvent_at_age >= 0.90

    def test_clears_with_a_large_portfolio(self, client_with_db):
        """A genuinely over-funded portfolio DOES clear the MC bar — proving the
        'found' path (not just always-None)."""
        from argosy.services.retirement.scenario_mc import earliest_feasible_retire_age
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_full(s)
            # Bump the snapshot to a clearly-over-funded portfolio.
            from argosy.state.models import PortfolioSnapshotRow
            from sqlalchemy import select, desc
            snap = s.execute(select(PortfolioSnapshotRow).where(
                PortfolioSnapshotRow.user_id == "ariel").order_by(desc(PortfolioSnapshotRow.id)).limit(1)).scalar_one()
            snap.totals_json = json.dumps({"fx_usd_nis": 3.7, "total_usd_value_k": 9000.0})  # ~₪33M
            s.commit()
            r = earliest_feasible_retire_age(
                session=s, user_id="ariel", target_p_solvent=0.90,
                n_paths=400, seed=42, today=date(2026, 1, 1),
            )
        assert r.earliest_feasible_age is not None
        assert r.p_solvent_at_age >= 0.90
