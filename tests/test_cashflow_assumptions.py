"""Unit tests for cashflow_assumptions (Wave 8 Piece C).

Per-field source resolution: sigma_calibrator success / fallback,
goals_yaml present / missing, hardcoded defaults pinned, validation
of out-of-range goals_yaml values.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from argosy.services.cashflow_assumptions import (
    DEFAULT_INFLATION_ANNUAL,
    DEFAULT_LIFESTYLE_DRIFT_ANNUAL,
    DEFAULT_MU_NOMINAL_ANNUAL,
    DEFAULT_RETIREMENT_AGE,
    DEFAULT_SIGMA_ANNUAL,
    DEFAULT_TAX_RATE,
    AssumptionField,
    _resolve_lifestyle_drift,
    _resolve_retirement_age,
    _resolve_sigma,
    _resolve_tax_rate,
    get_default_assumptions,
)


# Hardcoded fields (mu, inflation) ---------------------------------------


class TestHardcodedDefaults:
    def test_mu_is_hardcoded_default(self) -> None:
        r = get_default_assumptions(session=None, user_id="ariel")
        assert r.mu_nominal_annual.value == DEFAULT_MU_NOMINAL_ANNUAL
        assert r.mu_nominal_annual.source == "default"
        assert r.mu_nominal_annual.rationale_md
        assert "7-10%" in r.mu_nominal_annual.rationale_md

    def test_inflation_is_hardcoded_default(self) -> None:
        r = get_default_assumptions(session=None, user_id="ariel")
        assert r.inflation_annual.value == DEFAULT_INFLATION_ANNUAL
        assert r.inflation_annual.source == "default"
        assert "Bank of Israel" in r.inflation_annual.rationale_md


# Sigma calibrator -------------------------------------------------------


class TestResolveSigma:
    def test_none_session_returns_default(self) -> None:
        out = _resolve_sigma(session=None, user_id="ariel")
        assert out.value == DEFAULT_SIGMA_ANNUAL
        assert out.source == "default"

    def test_calibrator_success_uses_calibrated_value(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(
            sigma_annual=SimpleNamespace(
                value=0.29, rationale="weighted average heavy NVDA"
            )
        )
        with patch(
            "argosy.services.retirement.sigma_calibration"
            ".calibrate_sigma_from_holdings",
            return_value=fake,
        ):
            out = _resolve_sigma(session=object(), user_id="ariel")
        assert out.value == pytest.approx(0.29)
        assert out.source == "sigma_calibrator"
        assert "29.0%" in out.rationale_md
        assert "18%" in out.rationale_md

    def test_calibrator_zero_value_falls_back_to_default(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(
            sigma_annual=SimpleNamespace(value=0.0, rationale="empty portfolio")
        )
        with patch(
            "argosy.services.retirement.sigma_calibration"
            ".calibrate_sigma_from_holdings",
            return_value=fake,
        ):
            out = _resolve_sigma(session=object(), user_id="ariel")
        assert out.value == DEFAULT_SIGMA_ANNUAL
        assert out.source == "default"

    def test_calibrator_raises_falls_back_to_default(self) -> None:
        with patch(
            "argosy.services.retirement.sigma_calibration"
            ".calibrate_sigma_from_holdings",
            side_effect=RuntimeError("DB down"),
        ):
            out = _resolve_sigma(session=object(), user_id="ariel")
        assert out.value == DEFAULT_SIGMA_ANNUAL
        assert out.source == "default"


# Tax rate ---------------------------------------------------------------


class TestResolveTaxRate:
    def test_uses_goals_yaml_when_in_range(self) -> None:
        out = _resolve_tax_rate({"tax_rate_pct": 0.30})
        assert out.value == pytest.approx(0.30)
        assert out.source == "goals_yaml"

    def test_falls_back_to_default_when_missing(self) -> None:
        out = _resolve_tax_rate({})
        assert out.value == DEFAULT_TAX_RATE
        assert out.source == "default"

    def test_falls_back_when_value_out_of_range(self) -> None:
        out = _resolve_tax_rate({"tax_rate_pct": 1.5})
        assert out.value == DEFAULT_TAX_RATE
        assert out.source == "default"

    def test_falls_back_when_value_uncoerceable(self) -> None:
        out = _resolve_tax_rate({"tax_rate_pct": "not-a-number"})
        assert out.value == DEFAULT_TAX_RATE
        assert out.source == "default"


# Retirement age ---------------------------------------------------------


class TestResolveRetirementAge:
    def test_uses_goals_yaml_when_in_range(self) -> None:
        out = _resolve_retirement_age({"retirement_target_age": 55})
        assert out.value == pytest.approx(55.0)
        assert out.source == "goals_yaml"

    def test_falls_back_to_default_when_missing(self) -> None:
        out = _resolve_retirement_age({})
        assert out.value == DEFAULT_RETIREMENT_AGE
        assert out.source == "default"

    def test_falls_back_when_out_of_range(self) -> None:
        out = _resolve_retirement_age({"retirement_target_age": 120})
        assert out.value == DEFAULT_RETIREMENT_AGE
        assert out.source == "default"


# Lifestyle drift --------------------------------------------------------


class TestResolveLifestyleDrift:
    def test_uses_goals_yaml_when_in_range(self) -> None:
        out = _resolve_lifestyle_drift({"lifestyle_drift_annual": 0.015})
        assert out.value == pytest.approx(0.015)
        assert out.source == "goals_yaml"

    def test_zero_is_a_valid_goals_yaml_value(self) -> None:
        # 0 is the conservative default but also a legitimate explicit
        # user value; respect it as goals_yaml-sourced when present.
        out = _resolve_lifestyle_drift({"lifestyle_drift_annual": 0.0})
        assert out.value == 0.0
        assert out.source == "goals_yaml"

    def test_falls_back_to_default_when_missing(self) -> None:
        out = _resolve_lifestyle_drift({})
        assert out.value == DEFAULT_LIFESTYLE_DRIFT_ANNUAL
        assert out.source == "default"

    def test_falls_back_when_out_of_range(self) -> None:
        out = _resolve_lifestyle_drift({"lifestyle_drift_annual": 0.5})
        assert out.value == DEFAULT_LIFESTYLE_DRIFT_ANNUAL
        assert out.source == "default"


# Top-level entry --------------------------------------------------------


class TestGetDefaultAssumptions:
    def test_all_six_fields_present(self) -> None:
        r = get_default_assumptions(session=None, user_id="ariel")
        for f in (
            r.mu_nominal_annual,
            r.sigma_annual,
            r.tax_rate,
            r.inflation_annual,
            r.retirement_age,
            r.lifestyle_drift_annual,
        ):
            assert isinstance(f, AssumptionField)
            assert f.rationale_md  # non-empty rationale on every field
            assert f.source in {"sigma_calibrator", "goals_yaml", "default"}
