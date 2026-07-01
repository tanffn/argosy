"""Fix B CONNECT — real kids' birth years flow into the solvency MC.

The life-stage expense curve (phase_expenses) used to hardcode the kids_peak /
empty_nest phases to parent ages 43-55 / 56-64, ignoring the household's real
children. ``extract_household_state`` now derives the kids' birth years from
identity_yaml and carries them on ``HouseholdState.kid_birth_years``; the two MC
call sites pass them (+ reference_year=today.year) so the projection uses real
data. These tests pin the derivation and the empty-tuple legacy fallback.
"""
from datetime import date

from argosy.services.cashflow_projection import (
    HouseholdState,
    _derive_kid_birth_years,
)
from argosy.services.retirement.phase_expenses import build_phase_expense_curve


TODAY = date(2026, 7, 1)


class TestDeriveKidBirthYears:
    def test_from_dependents_ages(self) -> None:
        ctx = {"dependents_ages": [10, 6]}
        assert _derive_kid_birth_years(ctx, TODAY) == (2016, 2020)

    def test_explicit_dob_wins_over_age_estimate(self) -> None:
        # Age says 10 -> 2016; the real DOB (2015-12) should replace the estimate
        # for that kid because it lands within a year.
        ctx = {
            "dependents_ages": [10, 6],
            "education_savings_accounts": {
                "adva_age_10": {"child_dob": "2015-12-30"},
            },
        }
        assert _derive_kid_birth_years(ctx, TODAY) == (2015, 2020)

    def test_children_age_fallback_when_no_dependents_ages(self) -> None:
        ctx = {"children": [{"age": 10}, {"age": 6}]}
        assert _derive_kid_birth_years(ctx, TODAY) == (2016, 2020)

    def test_explicit_dob_only_when_no_ages(self) -> None:
        ctx = {
            "education_savings_accounts": {
                "a": {"child_dob": "2016-03-15"},
                "b": {"child_dob": "2020-01-01"},
            }
        }
        assert _derive_kid_birth_years(ctx, TODAY) == (2016, 2020)

    def test_no_kid_data_returns_empty(self) -> None:
        assert _derive_kid_birth_years({}, TODAY) == ()
        assert _derive_kid_birth_years(None, TODAY) == ()

    def test_matches_real_adva_dob(self) -> None:
        # The real identity: dependents_ages [10, 6] + Adva child_dob 2016-03-15.
        ctx = {
            "dependents_ages": [10, 6],
            "education_savings_accounts": {
                "adva_age_10": {"child_dob": "2016-03-15"},
            },
        }
        assert _derive_kid_birth_years(ctx, TODAY) == (2016, 2020)


class TestPhaseWindowShift:
    """Real (younger) kids push the kids_peak phase LATER than the legacy 43-55
    assumption — the change that moves the retirement headline."""

    def _windows(self, curve) -> dict[str, tuple[int, int]]:
        return {
            p.label: (p.start_age, p.end_age)
            for p in curve
            if p.label in ("kids_peak", "empty_nest")
        }

    def test_real_birth_years_shift_windows_later(self) -> None:
        real = self._windows(
            build_phase_expense_curve(
                has_kids=True,
                kids_birth_years=[2016, 2020],
                parent_current_age=44.0,
                reference_year=2026,
            )
        )
        assert real["kids_peak"] == (46, 60)
        assert real["empty_nest"] == (61, 68)

    def test_legacy_fallback_when_no_birth_years(self) -> None:
        legacy = self._windows(build_phase_expense_curve(has_kids=True))
        assert legacy["kids_peak"] == (43, 55)
        assert legacy["empty_nest"] == (56, 64)


class TestHouseholdStateCarry:
    def test_default_is_empty_tuple(self) -> None:
        hh = HouseholdState(
            monthly_expenses_nis=40000.0,
            portfolio_value_nis=9_000_000.0,
            fx_usd_nis=3.7,
            current_age_years=44.0,
        )
        assert hh.kid_birth_years == ()

    def test_carries_provided_years(self) -> None:
        hh = HouseholdState(
            monthly_expenses_nis=40000.0,
            portfolio_value_nis=9_000_000.0,
            fx_usd_nis=3.7,
            current_age_years=44.0,
            kid_birth_years=(2016, 2020),
        )
        assert hh.kid_birth_years == (2016, 2020)
