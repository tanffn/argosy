"""Tests for the probability-of-ruin verdict gate (Wave 3 · BLOCKER #1)."""
import json
from datetime import date, datetime, timezone

import pytest

from argosy.services.retirement.ruin_probability import (
    RuinProbabilityVerdict,
    _verdict_from_ci,
    compute_ruin_probability,
)
from argosy.state.models import (
    AgentReport,
    PortfolioSnapshotRow,
    User,
    UserContext,
)


def _seed_minimum(session, *, monthly_burn_nis: float = 20_000.0) -> None:
    """Seed user + context + snapshot + budget so compute_ruin_probability has
    enough data to run extract_household_state + extract_pension_state."""
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
            totals_json=json.dumps({
                "fx_usd_nis": 3.0,
                "total_usd_value_k": 3500.0,  # $3.5M portfolio
            }),
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


class TestVerdictFromCI:
    def test_on_track_when_ci_low_at_or_above_target(self):
        assert _verdict_from_ci(0.91, 0.95, target=0.90) == "ON_TRACK"
        assert _verdict_from_ci(0.90, 0.95, target=0.90) == "ON_TRACK"

    def test_off_track_when_ci_high_below_target(self):
        assert _verdict_from_ci(0.50, 0.70, target=0.90) == "OFF_TRACK"

    def test_uncertain_when_ci_straddles(self):
        assert _verdict_from_ci(0.85, 0.95, target=0.90) == "UNCERTAIN"


class TestComputeRuinProbability:
    def test_returns_verdict_shape(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_minimum(s, monthly_burn_nis=20_000.0)
            v = compute_ruin_probability(
                user_id="ariel",
                session=s,
                retirement_age=49.0,
                years=40,
                n_paths=300,  # smaller for test speed
                bootstrap_ci_samples=100,
                seed=42,
            )
        assert isinstance(v, RuinProbabilityVerdict)
        assert v.verdict in ("ON_TRACK", "OFF_TRACK", "UNCERTAIN", "WARN")
        # All P(solvent) values are in [0, 1]
        for vwr in (v.p_solvent_at_75, v.p_solvent_at_85, v.p_solvent_at_95):
            assert 0.0 <= vwr.value <= 1.0
        # CI bounds: low <= high
        assert v.p_solvent_at_95_ci_low.value <= v.p_solvent_at_95_ci_high.value

    def test_p_solvent_monotone_decreasing_with_age(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_minimum(s, monthly_burn_nis=20_000.0)
            v = compute_ruin_probability(
                user_id="ariel",
                session=s,
                retirement_age=49.0,
                n_paths=300,
                bootstrap_ci_samples=100,
                seed=42,
            )
        # P(solvent at 75) >= P(solvent at 85) >= P(solvent at 95)
        assert v.p_solvent_at_75.value >= v.p_solvent_at_85.value
        assert v.p_solvent_at_85.value >= v.p_solvent_at_95.value

    def test_seed_reproducibility(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_minimum(s, monthly_burn_nis=20_000.0)
            v1 = compute_ruin_probability(
                user_id="ariel", session=s, retirement_age=49.0,
                n_paths=300, bootstrap_ci_samples=100, seed=42,
            )
            v2 = compute_ruin_probability(
                user_id="ariel", session=s, retirement_age=49.0,
                n_paths=300, bootstrap_ci_samples=100, seed=42,
            )
        assert v1.p_solvent_at_95.value == v2.p_solvent_at_95.value
        assert v1.verdict == v2.verdict

    def test_high_burn_off_track(self, client_with_db):
        """Aggressive burn relative to assets should push verdict toward
        OFF_TRACK or UNCERTAIN (not ON_TRACK)."""
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_minimum(s, monthly_burn_nis=80_000.0)  # very high
            v = compute_ruin_probability(
                user_id="ariel",
                session=s,
                retirement_age=49.0,
                n_paths=300,
                bootstrap_ci_samples=100,
                seed=42,
            )
        assert v.verdict != "ON_TRACK"
        assert v.p_solvent_at_95.value < 0.90

    def test_target_appears_in_action_text(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_minimum(s, monthly_burn_nis=20_000.0)
            v = compute_ruin_probability(
                user_id="ariel",
                session=s,
                retirement_age=49.0,
                target_p_solvent=0.85,
                n_paths=300,
                bootstrap_ci_samples=100,
                seed=42,
            )
        # The 85% target should appear in the suggested-action text
        assert "85" in str(v.suggested_action.value)


def _fake_canonical_basis(sigma_hi: float):
    """A controlled CanonicalBasis so we can vary ONLY the calibrated sigma and
    confirm it threads through to the hero verdict (H8)."""
    from argosy.services.cashflow_projection import HouseholdState, PensionState
    from argosy.services.retirement.retirement_plan import CanonicalBasis

    hh = HouseholdState(
        monthly_expenses_nis=0.0, portfolio_value_nis=0.0, fx_usd_nis=3.0,
        current_age_years=44.0, monthly_savings_nis=0.0,
    )
    pens = PensionState(
        kupat_pensia_balance_nis=800_000.0, kupat_pensia_contribution_monthly_nis=0.0,
        executive_insurance_balance_nis=755_000.0, keren_hishtalmut_balance_nis=380_000.0,
        keren_hishtalmut_contribution_monthly_nis=0.0, kupat_gemel_balance_nis=75_000.0,
    )
    return CanonicalBasis(
        household=hh, pensions=pens, deployable_nis=8_000_000.0,
        full_portfolio_nis=10_000_000.0, cgt_haircut_nis=0.0, reserve_raw_nis=0.0,
        reserve_pv_nis=0.0, sigma_hi=sigma_hi, spend_central_nis=320_000.0,
        spend_stress_nis=350_000.0, bl_monthly_nis=0.0, bl_source="test",
        annuity_tax_rate=0.15,
    )


class TestHeroSigmaReconciliation:
    """H8: the regime ruin hero must follow the calibrated sigma (via the canonical
    basis), so a concentrated book (high sigma) yields a LOWER P(solvent) than a
    diversified one — recalibrating sigma MUST move the hero."""

    def test_p_solvent_drops_with_higher_calibrated_sigma(self, client_with_db, monkeypatch):
        from argosy.services.retirement import retirement_plan as rp

        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_minimum(s)
            monkeypatch.setattr(rp, "resolve_canonical_basis",
                                lambda *a, **k: _fake_canonical_basis(0.34))
            v_hi = compute_ruin_probability(
                user_id="ariel", session=s, retirement_age=49.0, n_paths=800, seed=3,
            )
            monkeypatch.setattr(rp, "resolve_canonical_basis",
                                lambda *a, **k: _fake_canonical_basis(0.18))
            v_lo = compute_ruin_probability(
                user_id="ariel", session=s, retirement_age=49.0, n_paths=800, seed=3,
            )
        assert v_hi.p_solvent_at_95.value < v_lo.p_solvent_at_95.value
