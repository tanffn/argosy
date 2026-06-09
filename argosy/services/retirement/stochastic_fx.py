"""Stochastic FX modeling for retirement projections.

Closes HIGH #12 from the 2026-05-28 SDD review. Prior projection used a
single ``fx_usd_nis`` snapshot — frozen for the entire 30y horizon. For
an Israeli household with USD-heavy assets + NIS-denominated liabilities,
this is the #1 silent risk: a 30% NIS strengthening turns "retire-ready
at 49" into "retire-ready at 56".

Model: lognormal random walk on USD/NIS spot
  log(fx_t+1 / fx_t) ~ N(mu_fx/12 - sigma_fx^2/24, sigma_fx/sqrt(12))

σ_fx and μ_fx are DERIVED from Argosy's own ingested USD/NIS history (the
``fx_rates`` table, fed daily from Bank of Israel + Frankfurter via
``argosy.services.fx``), NOT frozen magic numbers:

  - σ_fx = annualized stdev of MONTHLY log-returns over a trailing window
    (``FX_VOL_WINDOW_YEARS``): take the last observed daily rate in each
    calendar month, form ``r_m = ln(fx_m / fx_{m-1})``, then
    ``σ_fx = stdev(r_m, sample) × sqrt(12)``.
  - μ_fx = annualized mean monthly log-return = ``mean(r_m) × 12``.

The lognormal MODEL above is unchanged; only the SOURCE of σ/μ changed
from frozen constants to Argosy-derived values. When the trailing window
holds too few monthly observations to estimate vol (fewer than
``FX_VOL_MIN_MONTHS`` returns), the derivation falls back to the explicit
named constants below — and LOGS that it did so (never a silent guess).

The result is consumed by future projection paths via FX-adjusted asset
valuations: USD positions translate to NIS at simulated FX, NIS positions
unchanged. The headline verdict gets re-expressed in NIS (the base
liability currency) so retirement adequacy is honest about FX risk.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 3 HIGH #12.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np

from argosy.logging import get_logger
from argosy.services.retirement.citations import ValueWithRationale

_log = get_logger("argosy.retirement.stochastic_fx")

# Trailing window (years) over which realized USD/NIS volatility/drift is
# estimated from Argosy's ingested daily FX history. A NAMED, documented
# parameter — not a magic literal. 10y balances "enough monthly observations
# for a stable estimate" against "recent enough to reflect the current FX
# regime"; callers may override.
FX_VOL_WINDOW_YEARS = 10
# Minimum monthly log-returns required before we trust the derived estimate.
# Below this the trailing window is too thin and the derivation falls back
# (and logs that it did so). 24 = two years of monthly returns.
FX_VOL_MIN_MONTHS = 24
# The currency whose ILS-denominated history drives the USD/NIS model.
FX_BASE_CURRENCY = "USD"

# FALLBACK ONLY — used when Argosy has too little FX history to derive σ/μ
# from the ``fx_rates`` table. These are NOT the default path; the default
# path derives σ/μ from ingested history (see ``derive_fx_sigma_mu``).
#
# Annualized realized USD/NIS volatility, post-2000 (Bank of Israel data
# implies ~7-9% depending on window; 0.08 is the conservative midpoint).
FALLBACK_FX_SIGMA = 0.08
# No long-term drift assumed; the long-run USD/NIS has been roughly mean-
# reverting around 3.3-3.7 over the past 25 years.
FALLBACK_FX_MU = 0.0

# Backward-compat aliases. Older callers (e.g. ruin_probability's fallback
# path) import these names. They now point at the explicit FALLBACK values —
# the derived σ/μ are the default for new projection paths via
# ``derive_fx_sigma_mu`` / ``simulate_stochastic_fx(session=...)``.
DEFAULT_FX_SIGMA = FALLBACK_FX_SIGMA
DEFAULT_FX_MU = FALLBACK_FX_MU


@dataclass(frozen=True)
class FxVolEstimate:
    """Derived (or fallback) σ/μ for the USD/NIS lognormal model."""
    sigma_fx: float          # annualized stdev of monthly log-returns
    mu_fx: float             # annualized mean monthly log-return
    n_monthly_returns: int   # how many monthly log-returns backed the estimate
    window_years: int        # trailing window the estimate was drawn from
    derived: bool            # True = from fx_rates history; False = fallback
    source: str              # human-readable provenance for audit


def _monthly_endpoints(
    pairs: list[tuple[date, float]],
) -> list[tuple[date, float]]:
    """Reduce daily (date, rate) rows to ONE endpoint per calendar month.

    Takes the last observed rate within each ``(year, month)`` bucket — the
    month-end close — so monthly log-returns are computed between successive
    month-ends. ``pairs`` is assumed sorted ascending by date.
    """
    by_month: dict[tuple[int, int], tuple[date, float]] = {}
    for d, r in pairs:
        key = (d.year, d.month)
        existing = by_month.get(key)
        if existing is None or d > existing[0]:
            by_month[key] = (d, r)
    return [by_month[k] for k in sorted(by_month)]


def monthly_log_returns(pairs: list[tuple[date, float]]) -> list[float]:
    """Monthly log-returns ``ln(fx_m / fx_{m-1})`` from daily (date, rate) rows.

    Rows are sorted, reduced to month-end endpoints, then differenced in log
    space. Non-positive or missing rates are skipped (can't take a log).
    """
    clean = sorted(
        (d, float(r)) for d, r in pairs if r is not None and float(r) > 0.0
    )
    endpoints = _monthly_endpoints(clean)
    returns: list[float] = []
    for i in range(1, len(endpoints)):
        prev = endpoints[i - 1][1]
        cur = endpoints[i][1]
        if prev > 0.0 and cur > 0.0:
            returns.append(math.log(cur / prev))
    return returns


def annualize_sigma_mu(monthly_returns: list[float]) -> tuple[float, float]:
    """Annualize a list of monthly log-returns into (σ_fx, μ_fx).

    σ_fx = sample stdev (ddof=1) of monthly log-returns × sqrt(12).
    μ_fx = mean monthly log-return × 12.

    Mirrors the lognormal convention in the module doc: monthly step std is
    ``σ_fx / sqrt(12)``, so the annualized σ_fx is ``monthly_std × sqrt(12)``.
    """
    arr = np.asarray(monthly_returns, dtype=np.float64)
    mu_monthly = float(np.mean(arr))
    sigma_monthly = float(np.std(arr, ddof=1))
    sigma_fx = sigma_monthly * math.sqrt(12.0)
    mu_fx = mu_monthly * 12.0
    return sigma_fx, mu_fx


def derive_fx_sigma_mu(
    session,
    *,
    currency: str = FX_BASE_CURRENCY,
    today: date | None = None,
    window_years: int = FX_VOL_WINDOW_YEARS,
    min_months: int = FX_VOL_MIN_MONTHS,
) -> FxVolEstimate:
    """Derive (σ_fx, μ_fx) from Argosy's ingested USD/NIS daily history.

    Reads the ``fx_rates`` table (the canonical FX series, fed from Bank of
    Israel + Frankfurter via ``argosy.services.fx``) over the trailing
    ``window_years``, reduces to month-end endpoints, computes monthly
    log-returns, and annualizes (see :func:`annualize_sigma_mu`).

    If fewer than ``min_months`` monthly returns are available the window is
    too thin to estimate vol; the function falls back to the explicit
    ``FALLBACK_FX_*`` constants and LOGS the fallback (never silent).
    """
    today = today or datetime.now(timezone.utc).date()
    start = today - timedelta(days=int(round(window_years * 365.25)))

    pairs: list[tuple[date, float]] = []
    try:
        from argosy.state.models import FxRate

        rows = (
            session.query(FxRate)
            .filter(
                FxRate.currency == currency,
                FxRate.date >= start,
                FxRate.date <= today,
            )
            .order_by(FxRate.date.asc())
            .all()
        )
        for row in rows:
            if row.rate is not None:
                pairs.append((row.date, float(row.rate)))
    except Exception as exc:  # noqa: BLE001 — DB hiccup must not crash a projection
        _log.warning(
            "stochastic_fx.history_query_failed currency=%s err=%s — "
            "using FALLBACK σ/μ",
            currency, exc,
        )
        pairs = []

    returns = monthly_log_returns(pairs)
    n = len(returns)

    if n < min_months:
        _log.warning(
            "stochastic_fx.insufficient_history currency=%s months=%d "
            "min_required=%d window_years=%d — falling back to "
            "FALLBACK_FX_SIGMA=%s FALLBACK_FX_MU=%s",
            currency, n, min_months, window_years,
            FALLBACK_FX_SIGMA, FALLBACK_FX_MU,
        )
        return FxVolEstimate(
            sigma_fx=FALLBACK_FX_SIGMA,
            mu_fx=FALLBACK_FX_MU,
            n_monthly_returns=n,
            window_years=window_years,
            derived=False,
            source=(
                f"FALLBACK (only {n} monthly {currency}/NIS returns in "
                f"fx_rates over {window_years}y; need {min_months})"
            ),
        )

    sigma_fx, mu_fx = annualize_sigma_mu(returns)
    _log.info(
        "stochastic_fx.derived currency=%s months=%d window_years=%d "
        "sigma_fx=%.4f mu_fx=%.4f",
        currency, n, window_years, sigma_fx, mu_fx,
    )
    return FxVolEstimate(
        sigma_fx=sigma_fx,
        mu_fx=mu_fx,
        n_monthly_returns=n,
        window_years=window_years,
        derived=True,
        source=(
            f"fx_rates {currency}/NIS: {n} monthly log-returns over "
            f"{window_years}y trailing window (annualized)"
        ),
    )


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
    mu_fx: float
    sigma_fx: float
    sigma_source: str


def simulate_stochastic_fx(
    *,
    initial_fx: float,
    months: int,
    n_paths: int = 2000,
    mu_fx: float | None = None,
    sigma_fx: float | None = None,
    session=None,
    seed: int | None = None,
) -> FxSimulation:
    """Run a lognormal random-walk simulation of USD/NIS spot.

    σ_fx / μ_fx resolution order:
      1. Explicit ``sigma_fx`` / ``mu_fx`` args (caller-supplied override).
      2. Otherwise, if a DB ``session`` is given, DERIVE both from the
         ``fx_rates`` history via :func:`derive_fx_sigma_mu`.
      3. Otherwise, the explicit ``FALLBACK_FX_*`` constants (logged).

    Returns per-tick percentile bands. Callers translate USD asset values
    to NIS by multiplying by the path's fx_t at each tick.
    """
    if initial_fx <= 0:
        raise ValueError(f"initial_fx must be > 0, got {initial_fx}")

    sigma_source: str
    if sigma_fx is not None or mu_fx is not None:
        # Partial override: fill any missing side from the derived/fallback
        # estimate so we never silently re-introduce a frozen literal.
        if sigma_fx is None or mu_fx is None:
            est = (
                derive_fx_sigma_mu(session)
                if session is not None
                else FxVolEstimate(
                    FALLBACK_FX_SIGMA, FALLBACK_FX_MU, 0,
                    FX_VOL_WINDOW_YEARS, False, "FALLBACK (no session)",
                )
            )
            if sigma_fx is None:
                sigma_fx = est.sigma_fx
            if mu_fx is None:
                mu_fx = est.mu_fx
            sigma_source = f"caller-override (partial) + {est.source}"
        else:
            sigma_source = "caller-supplied σ_fx/μ_fx"
    elif session is not None:
        est = derive_fx_sigma_mu(session)
        sigma_fx = est.sigma_fx
        mu_fx = est.mu_fx
        sigma_source = est.source
    else:
        _log.warning(
            "stochastic_fx.no_session — no DB session to derive σ/μ; using "
            "FALLBACK_FX_SIGMA=%s FALLBACK_FX_MU=%s",
            FALLBACK_FX_SIGMA, FALLBACK_FX_MU,
        )
        sigma_fx = FALLBACK_FX_SIGMA
        mu_fx = FALLBACK_FX_MU
        sigma_source = "FALLBACK (no session passed to simulate_stochastic_fx)"

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
        mu_fx=float(mu_fx),
        sigma_fx=float(sigma_fx),
        sigma_source=sigma_source,
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
        f"from initial ₪{sim.initial_fx:.2f}/$ with μ_fx={sim.mu_fx:.4f}, "
        f"σ_fx={sim.sigma_fx:.4f} ({sim.sigma_source})."
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
