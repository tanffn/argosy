"""Tests for stochastic FX modeling (Wave 3 · HIGH #12)."""
import numpy as np
import pytest

from argosy.services.retirement.stochastic_fx import (
    DEFAULT_FX_SIGMA,
    FxSimulation,
    fx_band_at_horizon,
    simulate_stochastic_fx,
)


class TestStochasticFX:
    def test_shape_and_initial(self):
        sim = simulate_stochastic_fx(
            initial_fx=3.4, months=360, n_paths=500, seed=42,
        )
        assert isinstance(sim, FxSimulation)
        assert sim.fx_p50.shape[0] == 361
        # All percentile arrays start at initial_fx
        for arr in (sim.fx_p10, sim.fx_p25, sim.fx_p50, sim.fx_p75, sim.fx_p90):
            assert arr[0] == pytest.approx(3.4)

    def test_percentile_ordering(self):
        sim = simulate_stochastic_fx(
            initial_fx=3.4, months=120, n_paths=500, seed=42,
        )
        # P10 <= P25 <= P50 <= P75 <= P90 at every tick
        assert np.all(sim.fx_p10 <= sim.fx_p25 + 1e-9)
        assert np.all(sim.fx_p25 <= sim.fx_p50 + 1e-9)
        assert np.all(sim.fx_p50 <= sim.fx_p75 + 1e-9)
        assert np.all(sim.fx_p75 <= sim.fx_p90 + 1e-9)

    def test_band_widens_over_time(self):
        sim = simulate_stochastic_fx(
            initial_fx=3.4, months=120, n_paths=1000, seed=42,
        )
        # Band width at later ticks > band width at earlier ticks
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

    def test_default_sigma_is_8_pct(self):
        assert DEFAULT_FX_SIGMA == pytest.approx(0.08)


class TestFxBandAtHorizon:
    def test_returns_5_percentiles(self):
        sim = simulate_stochastic_fx(initial_fx=3.4, months=360, n_paths=500, seed=42)
        band = fx_band_at_horizon(sim)
        assert set(band.keys()) == {"p10", "p25", "p50", "p75", "p90"}
        # All units are NIS/USD
        for vwr in band.values():
            assert vwr.unit == "NIS/USD"

    def test_p50_close_to_initial_under_zero_drift(self):
        sim = simulate_stochastic_fx(initial_fx=3.4, months=360, n_paths=2000, seed=42)
        band = fx_band_at_horizon(sim)
        # Under μ=0, median should be close to initial (within ~5%)
        assert abs(band["p50"].value - 3.4) < 0.5
