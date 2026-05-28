"""Tests for the Bituach Leumi old-age stipend estimator (Wave 1 · HIGH #6)."""
import pytest

from argosy.services.retirement.bituach_leumi import (
    BLStipendEstimate,
    _scale_for_history,
    estimate_bl_stipend,
)
from argosy.state.models import User, UserContext


def _seed_user(session, user_id: str = "ariel") -> None:
    if session.get(User, user_id) is None:
        session.add(User(id=user_id, plan="free"))
        session.commit()


def _seed_user_context(session, *, user_id: str = "ariel") -> None:
    session.add(
        UserContext(
            user_id=user_id,
            identity_yaml="date_of_birth: '1982-08-28'\n",
            goals_yaml="",
            constraints_yaml="",
            current_stage="complete",
        )
    )
    session.commit()


class TestHistoryScaleHelper:
    def test_zero_years_floors_at_minimum(self):
        assert _scale_for_history(0) == 0.50

    def test_full_history_returns_one(self):
        assert _scale_for_history(35) == 1.0

    def test_over_full_history_caps_at_one(self):
        assert _scale_for_history(45) == 1.0

    def test_linear_midpoint(self):
        # Midpoint 17.5y → (0.50 + 0.50 * 17.5/35) = 0.75
        assert _scale_for_history(17.5) == pytest.approx(0.75)


class TestBLEstimate:
    def test_full_history_no_spouse_matches_base(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            est = estimate_bl_stipend(
                current_age=43,
                contribution_history_years=35,
                spouse_eligible=False,
                user_id="ariel",
                session=s,
            )
        # Shipped seed: 2100 NIS/mo base
        assert isinstance(est, BLStipendEstimate)
        assert est.monthly_nis.value == pytest.approx(2100.0)
        assert est.monthly_nis.unit == "NIS/mo"
        assert est.contribution_history_factor.value == 1.0

    def test_full_history_with_spouse_adds_50_pct(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            est = estimate_bl_stipend(
                current_age=43,
                contribution_history_years=35,
                spouse_eligible=True,
                user_id="ariel",
                session=s,
            )
        # 2100 + 50% spouse = 3150
        assert est.monthly_nis.value == pytest.approx(3150.0)
        assert est.spouse_supplement_applied.value == 1

    def test_short_history_scales_down(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            est = estimate_bl_stipend(
                current_age=43,
                contribution_history_years=10,
                spouse_eligible=False,
                user_id="ariel",
                session=s,
            )
        # factor = 0.5 + 0.5 * (10/35) = 0.6428
        # → 2100 * 0.6428 = ~1350
        assert est.monthly_nis.value == pytest.approx(2100 * (0.5 + 0.5 * 10/35), abs=1.0)
        assert est.contribution_history_factor.value == pytest.approx(0.6429, abs=0.001)

    def test_band_low_lt_central_lt_high(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            est = estimate_bl_stipend(
                current_age=43,
                contribution_history_years=35,
                spouse_eligible=True,
                user_id="ariel",
                session=s,
            )
        assert est.monthly_nis_low.value < est.monthly_nis.value < est.monthly_nis_high.value

    def test_sensitivity_levers_for_under_eligible_user(self, client_with_db):
        # If user has only 20y history and no spouse, both "complete-to-35"
        # and "add-spouse-supplement" should appear as positive levers.
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            est = estimate_bl_stipend(
                current_age=43,
                contribution_history_years=20,
                spouse_eligible=False,
                user_id="ariel",
                session=s,
            )
        complete_history_lever = est.sensitivity_levers[0]
        assert complete_history_lever["delta_nis_per_mo"] > 0
        spouse_lever = est.sensitivity_levers[1]
        assert spouse_lever["delta_nis_per_mo"] > 0

    def test_central_estimate_has_canonical_source(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            est = estimate_bl_stipend(
                current_age=43,
                contribution_history_years=35,
                spouse_eligible=False,
                user_id="ariel",
                session=s,
            )
        assert est.monthly_nis.source_id == "bituach_leumi_old_age_2026"
        assert est.eligibility_age.value == 67
