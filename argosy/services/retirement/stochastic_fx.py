"""Stochastic FX modeling for retirement projections.

Closes HIGH #12 from the 2026-05-28 SDD review. Prior projection used a
single ``fx_usd_nis`` snapshot — frozen for the entire 30y horizon. For
an Israeli household with USD-heavy assets + NIS-denominated liabilities,
this is the #1 silent risk: a 30% NIS strengthening turns "retire-ready
at 49" into "retire-ready at 56".

Model: lognormal random walk on USD/NIS spot
  log(fx_t+1 / fx_t) ~ N(mu_fx/12 - sigma_fx^2/24, sigma_fx/sqrt(12))

Defaults: μ_fx = 0 (no long-term drift assumed), σ_fx = 0.08 annualized
(post-2000 USD/NIS realized vol).

The result is consumed by future projection paths via FX-adjusted asset
valuations: USD positions translate to NIS at simulated FX, NIS positions
unchanged. The headline verdict gets re-expressed in NIS (the base
liability currency) so retirement adequacy is honest about FX risk.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 3 HIGH #12.
"""
import math
from dataclasses import dataclass

import numpy as np

from argosy.services.retirement.citations import ValueWithRationale


# Annualized realized USD/NIS volatility, post-2000 (Bank of Israel data
# implies ~7-9% depending on window; 0.08 is the conservative midpoint).
DEFAULT_FX_SIGMA = 0.08
# No long-term drift assumed; the long-run USD/NIS has been roughly mean-
# reverting around 3.3-3.7 over the past 25 years.
DEFAULT_FX_MU = 0.0


@dataclass(frozen=True)
class FxSimulation:
    """Per-tick percentile bands of USD/NIS across N paths."""
    fx_p10: np.ndarray  # shape (months+1,)
    fx_p25: np.ndarray
    fx_p50: np.ndarray
    fx_p75: np.ndarray
    fx_p90: np.ndarray
    months: int
    n_paths: int
    initial_fx: float


def simulate_stochastic_fx(
    *,
    initial_fx: float,
    months: int,
    n_paths: int = 2000,
    mu_fx: float = DEFAULT_FX_MU,
    sigma_fx: float = DEFAULT_FX_SIGMA,
    seed: int | None = None,
) -> FxSimulation:
    """Run a lognormal random-walk simulation of USD/NIS spot.

    Returns per-tick percentile bands. Callers translate USD asset values
    to NIS by multiplying by the path's fx_t at each tick.
    """
    if initial_fx <= 0:
        raise ValueError(f"initial_fx must be > 0, got {initial_fx}")
    rng = np.random.default_rng(seed)
    drift = mu_fx / 12.0 - (sigma_fx ** 2) / 24.0
    std = sigma_fx / math.sqrt(12.0)
    log_steps = rng.normal(loc=drift, scale=std, size=(n_paths, months))
    cumulative_log = np.cumsum(log_steps, axis=1)  # shape (n_paths, months)
    # Insert initial fx at tick 0
    fx_paths = np.empty((n_paths, months + 1), dtype=np.float64)
    fx_paths[:, 0] = initial_fx
    fx_paths[:, 1:] = initial_fx * np.exp(cumulative_log)

    p10 = np.percentile(fx_paths, 10, axis=0)
    p25 = np.percentile(fx_paths, 25, axis=0)
    p50 = np.percentile(fx_paths, 50, axis=0)
    p75 = np.percentile(fx_paths, 75, axis=0)
    p90 = np.percentile(fx_paths, 90, axis=0)

    return FxSimulation(
        fx_p10=p10,
        fx_p25=p25,
        fx_p50=p50,
        fx_p75=p75,
        fx_p90=p90,
        months=months,
        n_paths=n_paths,
        initial_fx=initial_fx,
    )


def fx_band_at_horizon(
    sim: FxSimulation,
    *,
    months_out: int | None = None,
) -> dict[str, ValueWithRationale]:
    """Return ValueWithRationale per percentile at the given horizon.

    Default horizon: end of simulation (most useful for "30 years from now,
    what's the USD/NIS range?").
    """
    if months_out is None:
        months_out = sim.months
    months_out = max(0, min(months_out, sim.months))
    base_rationale = (
        f"USD/NIS at month {months_out} from start. Lognormal random walk "
        f"from initial ₪{sim.initial_fx:.2f}/$ with μ_fx={DEFAULT_FX_MU}, "
        f"σ_fx={DEFAULT_FX_SIGMA} (post-2000 realized vol; conservative)."
    )

    def _w(arr: np.ndarray, label: str) -> ValueWithRationale:
        return ValueWithRationale(
            value=round(float(arr[months_out]), 4),
            unit="NIS/USD",
            source_id="argosy_derived",
            rationale=f"{label}. {base_rationale}",
            confidence="medium",
        )

    return {
        "p10": _w(sim.fx_p10, "10th percentile (NIS strongest)"),
        "p25": _w(sim.fx_p25, "25th percentile"),
        "p50": _w(sim.fx_p50, "Median"),
        "p75": _w(sim.fx_p75, "75th percentile"),
        "p90": _w(sim.fx_p90, "90th percentile (NIS weakest)"),
    }
