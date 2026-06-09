"""Tests for stochastic FX modeling (Wave 3 · HIGH #12).

T5.3: σ_fx is DERIVED from Argosy's ingested USD/NIS history (the
``fx_rates`` table), not the frozen 0.08 literal. μ_fx is deliberately held
at 0 (driftless USD/NIS) — a ~10y sample can't estimate a 30y drift. The
derivation math is hand-verified below.
"""
import math
from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from argosy.services.retirement.stochastic_fx import (
    DEFAULT_FX_MU,
    DEFAULT_FX_SIGMA,
    FALLBACK_FX_MU,
    FALLBACK_FX_SIGMA,
    FX_VOL_MIN_MONTHS,
    FX_VOL_WINDOW_YEARS,
    FxSimulation,
    FxVolEstimate,
    annualize_sigma_mu,
    derive_fx_sigma_mu,
    fx_band_at_horizon,
    monthly_log_returns,
    simulate_stochastic_fx,
)

# ----------------------------------------------------------------------
# HAND-VERIFIED TEST SERIES
#
# Chosen MONTHLY log-returns g = [0.06, -0.02, 0.06, -0.02]:
#   mean        = (0.06 - 0.02 + 0.06 - 0.02) / 4 = 0.08 / 4 = 0.02
#   deviations  = [+0.04, -0.04, +0.04, -0.04]
#   sum(dev^2)  = 4 * 0.0016 = 0.0064
#   var(ddof=1) = 0.0064 / (4 - 1) = 0.00213333...
#   stdev       = sqrt(0.00213333...) = 0.046188021...
#   sigma (ann) = 0.046188021... * sqrt(12) = 0.16   (exact)
#   mu    (ann) = 0.02 * 12 = 0.24                    (exact)
#
# The month-end RATE series is built by exponentiating g from fx0 = 3.0, so
# the rates are exact-by-construction and the derived sigma/mu recover
# 0.16 / 0.24.
# ----------------------------------------------------------------------
_MONTHLY_LOG_RETURNS = [0.06, -0.02, 0.06, -0.02]
_EXPECTED_SIGMA = 0.16
_EXPECTED_MU = 0.24

# Month-end dates (one endpoint per calendar month), ascending.
_MONTH_END_DATES = [
    date(2025, 1, 31),
    date(2025, 2, 28),
    date(2025, 3, 31),
    date(2025, 4, 30),
    date(2025, 5, 31),
]


def _build_rate_series() -> list[tuple[date, float]]:
    """fx0 = 3.0; each next month-end = prev * exp(g_i). Returns (date, rate)."""
    fx = 3.0
    series = [(_MONTH_END_DATES[0], fx)]
    for i, g in enumerate(_MONTHLY_LOG_RETURNS):
        fx = fx * math.exp(g)
        series.append((_MONTH_END_DATES[i + 1], fx))
    return series


def _seed_fx(session, series: list[tuple[date, float]], currency: str = "USD"):
    from argosy.state.models import FxRate
    for d, r in series:
        session.add(
            FxRate(date=d, currency=currency, rate=Decimal(str(r)), source="test")
        )
    session.flush()


# ----------------------------------------------------------------------
# Pure-math: monthly_log_returns + annualize_sigma_mu (no DB).
# ----------------------------------------------------------------------
class TestDerivationMath:
    def test_monthly_log_returns_recovers_chosen_returns(self):
        series = _build_rate_series()
        returns = monthly_log_returns(series)
        assert len(returns) == len(_MONTHLY_LOG_RETURNS)
        for got, want in zip(returns, _MONTHLY_LOG_RETURNS):
            assert got == pytest.approx(want, abs=1e-12)

    def test_annualize_matches_hand_computation(self):
        sigma, mu = annualize_sigma_mu(_MONTHLY_LOG_RETURNS)
        assert sigma == pytest.approx(_EXPECTED_SIGMA, abs=1e-9)
        assert mu == pytest.approx(_EXPECTED_MU, abs=1e-9)

    def test_month_end_endpoint_is_last_observation(self):
        # Add an intra-month row BEFORE the month-end; the resampler must keep
        # the month-end (last) observation, leaving log-returns unchanged.
        series = _build_rate_series()
        noisy = series + [(date(2025, 3, 10), 99.0)]  # mid-March outlier
        returns = monthly_log_returns(noisy)
        for got, want in zip(returns, _MONTHLY_LOG_RETURNS):
            assert got == pytest.approx(want, abs=1e-12)


# ----------------------------------------------------------------------
# DB-backed derivation from the fx_rates table.
# ----------------------------------------------------------------------
class TestDeriveFxSigmaMu:
    def test_derives_sigma_mu_from_seeded_history(self, alembic_engine_at_head):
        from sqlalchemy.orm import Session
        with Session(alembic_engine_at_head) as s:
            _seed_fx(s, _build_rate_series())
            s.commit()
            est = derive_fx_sigma_mu(
                s,
                currency="USD",
                today=date(2025, 6, 1),
                min_months=len(_MONTHLY_LOG_RETURNS),  # 4 returns is enough here
            )
        assert isinstance(est, FxVolEstimate)
        assert est.derived is True
        assert est.n_monthly_returns == len(_MONTHLY_LOG_RETURNS)
        # σ is derived (DB column is Numeric(12,6) → ~6e-7 quantization, so
        # abs=1e-5 not 1e-9). μ is held at 0 by design (not the realized drift).
        assert est.sigma_fx == pytest.approx(_EXPECTED_SIGMA, abs=1e-5)
        assert est.mu_fx == pytest.approx(FALLBACK_FX_MU)
        # sigma is NOT the frozen 0.08; the whole point of T5.3.
        assert est.sigma_fx != pytest.approx(0.08)
        assert "fx_rates" in est.source
        assert "driftless" in est.source  # μ=0 is a documented, audited choice

    def test_falls_back_explicitly_when_history_too_thin(
        self, alembic_engine_at_head
    ):
        from sqlalchemy.orm import Session
        with Session(alembic_engine_at_head) as s:
            _seed_fx(s, _build_rate_series())  # only 4 returns
            s.commit()
            est = derive_fx_sigma_mu(
                s,
                currency="USD",
                today=date(2025, 6, 1),
                min_months=FX_VOL_MIN_MONTHS,  # 24 > 4 available
            )
        assert est.derived is False
        assert est.sigma_fx == pytest.approx(FALLBACK_FX_SIGMA)
        assert est.mu_fx == pytest.approx(FALLBACK_FX_MU)
        assert "FALLBACK" in est.source

    def test_falls_back_when_no_history_at_all(self, alembic_engine_at_head):
        from sqlalchemy.orm import Session
        with Session(alembic_engine_at_head) as s:
            est = derive_fx_sigma_mu(s, currency="USD", today=date(2025, 6, 1))
        assert est.derived is False
        assert est.n_monthly_returns == 0
        assert est.sigma_fx == pytest.approx(FALLBACK_FX_SIGMA)
        assert est.mu_fx == pytest.approx(FALLBACK_FX_MU)

    def test_window_excludes_rows_older_than_window_years(
        self, alembic_engine_at_head
    ):
        # A pre-window outlier month-end must be ignored so it can't pollute
        # the volatility estimate.
        from sqlalchemy.orm import Session
        with Session(alembic_engine_at_head) as s:
            series = _build_rate_series()
            # An ancient row ~30y before `today`, well outside a 10y window.
            ancient = [(date(1995, 6, 30), 1.0)]
            _seed_fx(s, ancient + series)
            s.commit()
            est = derive_fx_sigma_mu(
                s,
                currency="USD",
                today=date(2025, 6, 1),
                window_years=FX_VOL_WINDOW_YEARS,
                min_months=len(_MONTHLY_LOG_RETURNS),
            )
        # Still exactly our 4 in-window returns → derived σ unchanged; μ=0.
        assert est.n_monthly_returns == len(_MONTHLY_LOG_RETURNS)
        assert est.sigma_fx == pytest.approx(_EXPECTED_SIGMA, abs=1e-5)
        assert est.mu_fx == pytest.approx(FALLBACK_FX_MU)


# ----------------------------------------------------------------------
# The frozen literals are gone from the DEFAULT path.
# ----------------------------------------------------------------------
class TestNoFrozenDefaults:
    def test_simulate_carries_derived_sigma_not_frozen_literal(
        self, alembic_engine_at_head
    ):
        from sqlalchemy.orm import Session
        with Session(alembic_engine_at_head) as s:
            _seed_fx(s, _build_rate_series())
            s.commit()
            est = derive_fx_sigma_mu(
                s, today=date(2025, 6, 1), min_months=len(_MONTHLY_LOG_RETURNS)
            )
            sim = simulate_stochastic_fx(
                initial_fx=3.4,
                months=120,
                n_paths=300,
                mu_fx=est.mu_fx,
                sigma_fx=est.sigma_fx,
                seed=42,
            )
        assert sim.sigma_fx == pytest.approx(_EXPECTED_SIGMA, abs=1e-5)
        assert sim.mu_fx == pytest.approx(FALLBACK_FX_MU)  # μ held at 0 by design
        # The carried sigma is the derived value, not the frozen 0.08.
        assert sim.sigma_fx != pytest.approx(0.08)

    def test_simulate_with_session_derives_sigma_mu(self, alembic_engine_at_head):
        # Passing a session with sufficient history makes simulate() derive
        # sigma/mu itself (default path), not use the frozen literal.
        from sqlalchemy.orm import Session
        # Build a >=24-month series so the default min_months passes.
        import math as _math
        from datetime import timedelta as _td

        with Session(alembic_engine_at_head) as s:
            from argosy.state.models import FxRate
            fx = 3.0
            d = date(2022, 1, 31)
            rng = np.random.default_rng(7)
            for _ in range(30):  # 30 month-ends → 29 returns ≥ 24
                s.add(FxRate(date=d, currency="USD",
                             rate=Decimal(str(round(fx, 6))), source="test"))
                step = float(rng.normal(0.0, 0.03))
                fx = fx * _math.exp(step)
                # advance ~one month
                d = (d.replace(day=1) + _td(days=32)).replace(day=28)
            s.commit()
            sim = simulate_stochastic_fx(
                initial_fx=3.4, months=60, n_paths=200,
                session=s, seed=42,
            )
        assert "fx_rates" in sim.sigma_source
        assert sim.sigma_fx > 0.0
        # Derived from ~3% monthly noise → annualized well away from 0.08 flat.
        assert sim.sigma_fx != pytest.approx(0.08)

    def test_simulate_no_session_uses_explicit_fallback_not_silent(self):
        # No session + no overrides → explicit FALLBACK (logged), recorded in
        # the simulation's sigma_source so an auditor can see it wasn't derived.
        sim = simulate_stochastic_fx(
            initial_fx=3.4, months=60, n_paths=200, seed=42,
        )
        assert sim.sigma_fx == pytest.approx(FALLBACK_FX_SIGMA)
        assert sim.mu_fx == pytest.approx(FALLBACK_FX_MU)
        assert "FALLBACK" in sim.sigma_source

    def test_default_constants_are_explicit_fallbacks(self):
        # The legacy names survive ONLY as aliases of the explicit fallback,
        # so out-of-scope importers (ruin_probability) keep working — but they
        # are no longer the silent default of the derivation path.
        assert DEFAULT_FX_SIGMA == FALLBACK_FX_SIGMA
        assert DEFAULT_FX_MU == FALLBACK_FX_MU
        assert FX_VOL_WINDOW_YEARS == 10


# ----------------------------------------------------------------------
# Existing simulation invariants (unchanged model).
# ----------------------------------------------------------------------
class TestStochasticFX:
    def test_shape_and_initial(self):
        sim = simulate_stochastic_fx(
            initial_fx=3.4, months=360, n_paths=500, seed=42,
        )
        assert isinstance(sim, FxSimulation)
        assert sim.fx_p50.shape[0] == 361
        for arr in (sim.fx_p10, sim.fx_p25, sim.fx_p50, sim.fx_p75, sim.fx_p90):
            assert arr[0] == pytest.approx(3.4)

    def test_percentile_ordering(self):
        sim = simulate_stochastic_fx(
            initial_fx=3.4, months=120, n_paths=500, seed=42,
        )
        assert np.all(sim.fx_p10 <= sim.fx_p25 + 1e-9)
        assert np.all(sim.fx_p25 <= sim.fx_p50 + 1e-9)
        assert np.all(sim.fx_p50 <= sim.fx_p75 + 1e-9)
        assert np.all(sim.fx_p75 <= sim.fx_p90 + 1e-9)

    def test_band_widens_over_time(self):
        sim = simulate_stochastic_fx(
            initial_fx=3.4, months=120, n_paths=1000, seed=42,
        )
        width_at_12 = sim.fx_p90[12] - sim.fx_p10[12]
        width_at_120 = sim.fx_p90[120] - sim.fx_p10[120]
        assert width_at_120 > width_at_12

    def test_seed_reproducibility(self):
        a = simulate_stochastic_fx(initial_fx=3.4, months=60, n_paths=300, seed=42)
        b = simulate_stochastic_fx(initial_fx=3.4, months=60, n_paths=300, seed=42)
        assert a.fx_p50.tolist() == b.fx_p50.tolist()

    def test_zero_initial_fx_raises(self):
        with pytest.raises(ValueError, match="initial_fx must be"):
            simulate_stochastic_fx(initial_fx=0, months=12, n_paths=100)


class TestFxBandAtHorizon:
    def test_returns_5_percentiles(self):
        sim = simulate_stochastic_fx(initial_fx=3.4, months=360, n_paths=500, seed=42)
        band = fx_band_at_horizon(sim)
        assert set(band.keys()) == {"p10", "p25", "p50", "p75", "p90"}
        for vwr in band.values():
            assert vwr.unit == "NIS/USD"

    def test_p50_close_to_initial_under_zero_drift(self):
        # Fallback path → μ=0, so the median stays close to initial.
        sim = simulate_stochastic_fx(initial_fx=3.4, months=360, n_paths=2000, seed=42)
        band = fx_band_at_horizon(sim)
        assert abs(band["p50"].value - 3.4) < 0.5
