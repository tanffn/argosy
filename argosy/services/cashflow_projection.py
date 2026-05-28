"""Monthly-cashflow projection for the /plan retirement view.

Answers two questions:
  a. When can Ariel safely retire — earliest month where projected total
     monthly income (portfolio real-return + pension annuity) covers
     inflated expenses.
  b. How sensitive is that crossing to (retirement_age, mu, sigma,
     inflation, mekadem).

Pure-Python module: DB reads in ``extract_pension_state`` /
``extract_household_state``; everything below is pure math. The route
layer in ``argosy.api.routes.plan`` wraps this in a FastAPI endpoint.
"""

from __future__ import annotations

import calendar
import json
import math
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from sqlalchemy.orm import Session

from argosy.services.wealth_dashboard import (
    _latest_household_budget_report,
    _latest_snapshot,
    _load_user_context_yaml,
    _resolve_fx_usd_nis,
)

# Defaults — surfaced in the response ``assumptions`` block.
DEFAULT_MU_NOMINAL_ANNUAL = 0.08
DEFAULT_SIGMA_ANNUAL = 0.18
DEFAULT_INFLATION_ANNUAL = 0.025
DEFAULT_MEKADEM = 200.0
DEFAULT_TAX_RATE = 0.25
DEFAULT_LIFESTYLE_DRIFT_ANNUAL = 0.0
LUMP_PENSION_AGE = 60
ANNUITY_AGE = 67


@dataclass(frozen=True)
class PensionState:
    """Snapshot of all pension-bucket balances + monthly contribution
    rates extracted from ``identity_yaml`` at the start of projection.

    All amounts in NIS. ``contribution_monthly_nis`` is zero when the
    bucket is frozen (executive_insurance, kupat_gemel)."""
    kupat_pensia_balance_nis: float
    kupat_pensia_contribution_monthly_nis: float
    executive_insurance_balance_nis: float
    keren_hishtalmut_balance_nis: float
    keren_hishtalmut_contribution_monthly_nis: float
    kupat_gemel_balance_nis: float


@dataclass(frozen=True)
class HouseholdState:
    """Per-month financial state at projection start. NIS."""
    monthly_expenses_nis: float
    portfolio_value_nis: float
    fx_usd_nis: float
    current_age_years: float


@dataclass(frozen=True)
class CashflowPoint:
    """One projected month."""
    months_out: int
    age_years: float
    date_yyyy_mm: str
    portfolio_value_base_nis: float
    portfolio_value_bear_nis: float
    portfolio_value_bull_nis: float
    portfolio_income_base_monthly_nis: float
    portfolio_income_bear_monthly_nis: float
    portfolio_income_bull_monthly_nis: float
    # ``pension_annuity_monthly_nis`` is in NOMINAL NIS at time t. The
    # annuity locks at age 67 in real NIS (balance_at_lock / mekadem), but
    # is inflated forward at ``inflation_annual`` so it's directly
    # comparable with ``expenses_monthly_nis`` (which is also nominal at t).
    pension_annuity_monthly_nis: float
    # ``pension_lump_available_nis`` is CUMULATIVE-once-unlocked: 0 until
    # age 60, then equal to the total NIS that became available at unlock
    # (and stays at that value for all subsequent ticks). It is NOT a
    # per-month income figure — the unlocked amount is added to the
    # portfolio at unlock, so the recurring effect shows up in
    # ``portfolio_income_*_monthly_nis`` going forward.
    pension_lump_available_nis: float
    expenses_monthly_nis: float
    surplus_base_monthly_nis: float
    surplus_bear_monthly_nis: float
    surplus_bull_monthly_nis: float


@dataclass(frozen=True)
class CashflowProjection:
    """Top-level result returned to the route."""
    series: list[CashflowPoint]
    # Legacy fields — aliased to base for backward compatibility.
    retire_ready_age: float | None
    retire_ready_months_out: int | None
    # Per-scenario retire-ready fields.
    retire_ready_age_base: float | None
    retire_ready_age_bear: float | None
    retire_ready_age_bull: float | None
    retire_ready_months_out_base: int | None
    retire_ready_months_out_bear: int | None
    retire_ready_months_out_bull: int | None
    pension_state_at_start: PensionState
    household_state_at_start: HouseholdState
    retirement_age_assumed: float
    assumptions: dict


DEFAULT_N_PATHS = 1000


@dataclass(frozen=True)
class MonteCarloPoint:
    """One projected month — percentile aggregation across N paths."""
    months_out: int
    age_years: float
    date_yyyy_mm: str
    # Percentile bands of portfolio_value_nis across N paths
    portfolio_value_p10_nis: float
    portfolio_value_p25_nis: float
    portfolio_value_p50_nis: float  # median
    portfolio_value_p75_nis: float
    portfolio_value_p90_nis: float
    # Survival: fraction of paths with portfolio > 0 at this tick
    fraction_solvent: float
    # Deterministic helpers (same for all paths)
    pension_annuity_monthly_nis: float
    expenses_monthly_nis: float


@dataclass(frozen=True)
class MonteCarloProjection:
    series: list[MonteCarloPoint]
    n_paths: int
    # Failure probability before various ages (1 - survival).
    # Computed as fraction of paths where portfolio hit 0 before age X.
    p_failure_before_age_75: float
    p_failure_before_age_85: float
    p_failure_before_age_95: float
    pension_state_at_start: "PensionState"
    household_state_at_start: "HouseholdState"
    retirement_age_assumed: float
    assumptions: dict


def accumulate_pension_balance(
    *,
    starting_balance_nis: float,
    monthly_contribution_nis: float,
    real_return_annual: float,
    months: int,
) -> float:
    """Apply N months of (growth at real_return/12) + monthly contribution.

    Contribution is applied AFTER growth each month so the contribution
    starts earning at month t+1. Matches the convention in
    ``argosy.services.wealth_dashboard.project_wealth_curve``.
    """
    b = float(starting_balance_nis)
    monthly_rate = real_return_annual / 12.0
    for _ in range(months):
        b = b * (1.0 + monthly_rate) + monthly_contribution_nis
    return b


def compute_pension_annuity(
    *,
    kupat_pensia_balance_nis: float,
    executive_insurance_balance_nis: float,
    mekadem: float,
) -> float:
    """``(sum_of_balances) / mekadem`` — the standard Israeli
    monthly-stipend formula. ``mekadem`` typically 190-220; 200 is the
    common conservative default."""
    if mekadem <= 0:
        return 0.0
    return (kupat_pensia_balance_nis + executive_insurance_balance_nis) / mekadem


def portfolio_real_return_monthly(
    *,
    portfolio_value_nis: float,
    real_return_annual: float,
) -> float:
    """Monthly portfolio income under the ``capital_preservation_returns_only``
    drawdown style: take this month's share of the annual real return."""
    return portfolio_value_nis * real_return_annual / 12.0


def inflate_expenses(
    base_monthly_nis: float,
    inflation_annual: float,
    months_out: int,
) -> float:
    """Compound ``base_monthly_nis`` forward by ``months_out`` months at
    ``inflation_annual`` per year. Uses a fractional-year exponent
    (``months_out / 12``) so the curve is continuous across month
    boundaries — matches the per-tick semantics of the projection loop."""
    return base_monthly_nis * ((1.0 + inflation_annual) ** (months_out / 12.0))


def detect_retire_ready(
    series: Sequence[CashflowPoint],
    scenario: str = "base",
) -> tuple[int, float] | None:
    """Return (months_out, age_years) of the first row where portfolio
    income (scenario) + annuity >= expenses. ``scenario`` selects which
    income line drives the crossing detection."""
    field_map = {
        "base": "portfolio_income_base_monthly_nis",
        "bear": "portfolio_income_bear_monthly_nis",
        "bull": "portfolio_income_bull_monthly_nis",
    }
    income_field = field_map[scenario]
    for p in series:
        total = getattr(p, income_field) + p.pension_annuity_monthly_nis
        if total >= p.expenses_monthly_nis:
            return p.months_out, p.age_years
    return None


# ---------------------------------------------------------------------------
# DB extraction helpers — read real SQLAlchemy state into the dataclasses above
# ---------------------------------------------------------------------------

def _safe_float(d: dict, *keys, default: float = 0.0) -> float:
    """Walk ``d[keys[0]][keys[1]]...`` and return as float; default on
    missing/None/un-coerceable values. Used by the extractors to read
    pension + household scalars out of the loose identity_yaml shape."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def extract_pension_state(session: Session, user_id: str) -> PensionState:
    """Pull pension balances + active contribution rates from identity_yaml.

    Returns a PensionState with zeros for any missing keys (so the math
    layer can run unconditionally on under-populated users). Reads:
      - identity_yaml.pensions_ariel.{pension_nis, executive_insurance_nis,
        keren_hishtalmut_nis, provident_fund_nis} — the balance snapshot.
      - identity_yaml.clal_pension_{salary_basis_monthly_nis, employee_pct,
        employer_pct, severance_pct} — kupat_pensia monthly contribution.
      - identity_yaml.pensions.keren_hishtalmut.{contribution_rate_pct,
        employer_match_pct} — hishtalmut monthly contribution.

    Severance (8.33%) is folded into kupat_pensia monthly contributions
    — this is a documented simplification flagged for codex-tandem review."""
    ctx = _load_user_context_yaml(session, user_id)

    salary = _safe_float(ctx, "clal_pension_salary_basis_monthly_nis")
    emp_pct = _safe_float(ctx, "clal_pension_employee_pct")
    er_pct = _safe_float(ctx, "clal_pension_employer_pct")
    sev_pct = _safe_float(ctx, "clal_pension_severance_pct")
    pensia_contrib = salary * (emp_pct + er_pct + sev_pct) / 100.0

    hishtalmut_emp = _safe_float(ctx, "pensions", "keren_hishtalmut", "contribution_rate_pct")
    hishtalmut_er = _safe_float(ctx, "pensions", "keren_hishtalmut", "employer_match_pct")
    hishtalmut_contrib = salary * (hishtalmut_emp + hishtalmut_er) / 100.0

    return PensionState(
        kupat_pensia_balance_nis=_safe_float(ctx, "pensions_ariel", "pension_nis"),
        kupat_pensia_contribution_monthly_nis=pensia_contrib,
        executive_insurance_balance_nis=_safe_float(
            ctx, "pensions_ariel", "executive_insurance_nis"
        ),
        keren_hishtalmut_balance_nis=_safe_float(
            ctx, "pensions_ariel", "keren_hishtalmut_nis"
        ),
        keren_hishtalmut_contribution_monthly_nis=hishtalmut_contrib,
        kupat_gemel_balance_nis=_safe_float(
            ctx, "pensions_ariel", "provident_fund_nis"
        ),
    )


def _compute_age_years(date_of_birth_iso: str | None, today: date) -> float:
    """Decimal years between ``date_of_birth_iso`` and ``today``. Falls
    back to 43.0 (Ariel's approximate age) when DOB is missing or
    unparseable; this keeps the projection running on under-populated
    users rather than crashing."""
    if not date_of_birth_iso:
        return 43.0
    try:
        dob = date.fromisoformat(date_of_birth_iso)
    except ValueError:
        return 43.0
    days = (today - dob).days
    return days / 365.25


def extract_household_state(
    session: Session,
    user_id: str,
    *,
    today: date | None = None,
) -> HouseholdState:
    """Pull (monthly_burn_nis, portfolio_value_nis, fx, age) for the
    projection root. Falls back gracefully when any field is absent."""
    today = today or date.today()
    ctx = _load_user_context_yaml(session, user_id)
    budget = _latest_household_budget_report(session, user_id) or {}
    snapshot = _latest_snapshot(session, user_id)
    fx_usd_nis, _ = _resolve_fx_usd_nis(snapshot=snapshot, user_ctx=ctx)

    monthly_expenses_nis = _safe_float(budget, "monthly_burn_nis")

    # Portfolio value: read total_usd_value_k from the latest snapshot's
    # totals_json, convert via FX.
    portfolio_value_nis = 0.0
    if snapshot is not None:
        try:
            totals = json.loads(snapshot.totals_json or "{}")
            usd_k = float(totals.get("total_usd_value_k") or 0.0)
            portfolio_value_nis = usd_k * 1000.0 * fx_usd_nis
        except (ValueError, TypeError):
            portfolio_value_nis = 0.0

    # Accept either ``user_date_of_birth`` (the real key in identity_yaml,
    # to distinguish from spouse / children) or top-level ``date_of_birth``
    # for test-seed compatibility. First non-empty wins.
    dob_iso = None
    if isinstance(ctx, dict):
        dob_iso = ctx.get("user_date_of_birth") or ctx.get("date_of_birth")
    age = _compute_age_years(dob_iso, today)
    return HouseholdState(
        monthly_expenses_nis=monthly_expenses_nis,
        portfolio_value_nis=portfolio_value_nis,
        fx_usd_nis=fx_usd_nis,
        current_age_years=age,
    )


# ---------------------------------------------------------------------------
# Calendar helper
# ---------------------------------------------------------------------------

def _add_months(today: date, n: int) -> date:
    """Add n calendar months to ``today``. Day clamps to end-of-month."""
    y = today.year + (today.month - 1 + n) // 12
    m = (today.month - 1 + n) % 12 + 1
    d = min(today.day, calendar.monthrange(y, m)[1])
    return date(y, m, d)


# ---------------------------------------------------------------------------
# Core orchestrator — iterative monthly state machine
# ---------------------------------------------------------------------------

def project_cashflow(
    *,
    household: HouseholdState,
    pensions: PensionState,
    retirement_age: float,
    years: int,
    mu_nominal_annual: float = DEFAULT_MU_NOMINAL_ANNUAL,
    sigma_annual: float = DEFAULT_SIGMA_ANNUAL,
    inflation_annual: float = DEFAULT_INFLATION_ANNUAL,
    mekadem: float = DEFAULT_MEKADEM,
    tax_rate: float = DEFAULT_TAX_RATE,
    lifestyle_drift_annual: float = DEFAULT_LIFESTYLE_DRIFT_ANNUAL,
    today: date | None = None,
) -> CashflowProjection:
    """Project ``years * 12 + 1`` monthly cashflow points and detect
    retire-ready age. See module docstring for the math model.

    Portfolio path: iterative monthly compounding at ``mu_nominal/12``;
    bull/bear = base × exp(±sigma × sqrt(t/12)) re-derived from the
    running base at each tick so the lump-bump at age 60 composes
    correctly.

    Pension buckets: per-month accumulate-and-grow. Lump unlock at age
    60 transfers (keren_hishtalmut + kupat_gemel) into
    ``portfolio_base_nis``. Annuity lock at age 67:
    ``(pensia + exec_ins) / mekadem``; thereafter pensia + exec_ins are
    treated as consumed (frozen, not compounded)."""
    today = today or date.today()
    months = max(1, min(years, 50)) * 12
    real_return = mu_nominal_annual - inflation_annual
    monthly_growth = 1.0 + mu_nominal_annual / 12.0

    portfolio_base_nis = household.portfolio_value_nis
    pensia_balance = pensions.kupat_pensia_balance_nis
    exec_ins_balance = pensions.executive_insurance_balance_nis
    hishtalmut_balance = pensions.keren_hishtalmut_balance_nis
    kupat_gemel_balance = pensions.kupat_gemel_balance_nis

    lump_unlocked = False
    lump_amount_nis = 0.0
    annuity_monthly_nis = 0.0  # real NIS at lock — inflated to nominal at each emit
    annuity_locked = False
    annuity_lock_t: int | None = None

    out: list[CashflowPoint] = []

    for t in range(months + 1):
        age_t = household.current_age_years + t / 12.0
        t_years = t / 12.0

        # Step 1: advance portfolio_base by one month of nominal growth.
        # Skipped at t=0 so we emit today's actual state first.
        if t > 0:
            portfolio_base_nis *= monthly_growth

        # Step 2: advance pension bucket balances by one month.
        if t > 0:
            real_monthly = 1.0 + real_return / 12.0
            # Contribution-timing convention: at tick t, grow last month's
            # balance by real_monthly, THEN add the new month's contribution
            # (matches accumulate_pension_balance — contribution earns from
            # t+1 onward). Cutoff is age_t < retirement_age strict, so the
            # tick where age_t == retirement_age is the first no-contribution
            # month. This is the natural "stop contributing when you retire"
            # semantics; no fence-post is intended.
            if not annuity_locked:
                contrib_pensia = (
                    pensions.kupat_pensia_contribution_monthly_nis
                    if age_t < retirement_age else 0.0
                )
                pensia_balance = pensia_balance * real_monthly + contrib_pensia
                exec_ins_balance = exec_ins_balance * real_monthly  # frozen
            if not lump_unlocked:
                contrib_hisht = (
                    pensions.keren_hishtalmut_contribution_monthly_nis
                    if age_t < retirement_age else 0.0
                )
                hishtalmut_balance = (
                    hishtalmut_balance * real_monthly + contrib_hisht
                )
                kupat_gemel_balance = kupat_gemel_balance * real_monthly

        # Step 3: lump unlock at age 60 — add combined balance to
        # portfolio, zero the sources. Subsequent ticks will compound
        # the lump along with the rest of the portfolio.
        if age_t >= LUMP_PENSION_AGE and not lump_unlocked:
            lump_amount_nis = hishtalmut_balance + kupat_gemel_balance
            portfolio_base_nis += lump_amount_nis
            hishtalmut_balance = 0.0
            kupat_gemel_balance = 0.0
            lump_unlocked = True

        # Step 4: annuity lock at age 67.
        if age_t >= ANNUITY_AGE and not annuity_locked:
            annuity_monthly_nis = compute_pension_annuity(
                kupat_pensia_balance_nis=pensia_balance,
                executive_insurance_balance_nis=exec_ins_balance,
                mekadem=mekadem,
            )
            annuity_locked = True
            annuity_lock_t = t

        # Step 5: derive bull/bear from base via lognormal ±1σ band.
        # At t=0 the band collapses to base (no uncertainty yet).
        log_std = sigma_annual * math.sqrt(t_years)
        portfolio_bull_nis = portfolio_base_nis * math.exp(log_std)
        portfolio_bear_nis = portfolio_base_nis * math.exp(-log_std)

        # Step 6: derived series.
        net_factor = 1.0 - tax_rate
        portfolio_income_base = portfolio_real_return_monthly(
            portfolio_value_nis=portfolio_base_nis, real_return_annual=real_return
        ) * net_factor
        portfolio_income_bull = portfolio_real_return_monthly(
            portfolio_value_nis=portfolio_bull_nis, real_return_annual=real_return
        ) * net_factor
        portfolio_income_bear = portfolio_real_return_monthly(
            portfolio_value_nis=portfolio_bear_nis, real_return_annual=real_return
        ) * net_factor
        effective_expense_growth = inflation_annual + lifestyle_drift_annual
        expenses_t = inflate_expenses(
            household.monthly_expenses_nis, effective_expense_growth, t
        )
        # Inflate the real-at-lock annuity to nominal NIS at time t so it's
        # directly comparable with expenses_t (which is also nominal at t).
        if annuity_locked and annuity_lock_t is not None:
            annuity_nominal_t = annuity_monthly_nis * (
                (1.0 + inflation_annual) ** ((t - annuity_lock_t) / 12.0)
            )
        else:
            annuity_nominal_t = 0.0
        surplus_base = portfolio_income_base + annuity_nominal_t - expenses_t
        surplus_bear = portfolio_income_bear + annuity_nominal_t - expenses_t
        surplus_bull = portfolio_income_bull + annuity_nominal_t - expenses_t

        d = _add_months(today, t)
        out.append(CashflowPoint(
            months_out=t,
            age_years=age_t,
            date_yyyy_mm=d.strftime("%Y-%m"),
            portfolio_value_base_nis=portfolio_base_nis,
            portfolio_value_bear_nis=portfolio_bear_nis,
            portfolio_value_bull_nis=portfolio_bull_nis,
            portfolio_income_base_monthly_nis=portfolio_income_base,
            portfolio_income_bear_monthly_nis=portfolio_income_bear,
            portfolio_income_bull_monthly_nis=portfolio_income_bull,
            pension_annuity_monthly_nis=annuity_nominal_t,   # nominal at time t
            pension_lump_available_nis=lump_amount_nis if lump_unlocked else 0.0,  # CUMULATIVE-once-unlocked, not per-month — see field docstring
            expenses_monthly_nis=expenses_t,
            surplus_base_monthly_nis=surplus_base,
            surplus_bear_monthly_nis=surplus_bear,
            surplus_bull_monthly_nis=surplus_bull,
        ))

    retire_ready_base = detect_retire_ready(out, "base")  # noqa: E501 — intentional; see below
    retire_ready_bear = detect_retire_ready(out, "bear")
    retire_ready_bull = detect_retire_ready(out, "bull")
    return CashflowProjection(
        series=out,
        # Legacy fields aliased to base for backward compatibility.
        retire_ready_months_out=retire_ready_base[0] if retire_ready_base else None,
        retire_ready_age=retire_ready_base[1] if retire_ready_base else None,
        # Per-scenario fields.
        retire_ready_age_base=retire_ready_base[1] if retire_ready_base else None,
        retire_ready_age_bear=retire_ready_bear[1] if retire_ready_bear else None,
        retire_ready_age_bull=retire_ready_bull[1] if retire_ready_bull else None,
        retire_ready_months_out_base=retire_ready_base[0] if retire_ready_base else None,
        retire_ready_months_out_bear=retire_ready_bear[0] if retire_ready_bear else None,
        retire_ready_months_out_bull=retire_ready_bull[0] if retire_ready_bull else None,
        pension_state_at_start=pensions,
        household_state_at_start=household,
        retirement_age_assumed=retirement_age,
        assumptions={
            "mu_nominal_annual": mu_nominal_annual,
            "sigma_annual": sigma_annual,
            "real_return_annual": real_return,
            "inflation_annual": inflation_annual,
            "lifestyle_drift_annual": lifestyle_drift_annual,
            "effective_expense_growth": inflation_annual + lifestyle_drift_annual,
            "mekadem": mekadem,
            "tax_rate": tax_rate,
            "lump_pension_age": LUMP_PENSION_AGE,
            "annuity_age": ANNUITY_AGE,
            "model_notes": (
                "Iterative monthly compounding at mu_nominal/12 for the "
                "portfolio base; bull/bear = base × exp(±sigma × sqrt(t/12)). "
                "Real-return drawdown income = portfolio × (mu - inflation) / 12. "
                "Lump (keren_hishtalmut + kupat_gemel) unlocks at age 60, added "
                "to portfolio. Annuity (kupat_pensia + executive_insurance) "
                "locked at age 67 via balance / mekadem; balances frozen "
                "thereafter. Executive insurance modelled as frozen (no "
                "contributions). Severance (8.33%) is modelled as kupat_pensia "
                "contribution — this is an OPTIMISTIC bias for annuity adequacy: "
                "in Israeli practice severance typically goes into a separate "
                "pizurim account and may be withdrawn before 67 rather than "
                "annuitized. Documented after codex-tandem review."
                " Pension annuity at age 67 is locked as balance/mekadem in real "
                "NIS, then inflated at ``inflation_annual`` so the emitted "
                "``pension_annuity_monthly_nis`` is nominal NIS at time t "
                "(comparable with expenses)."
                " Portfolio income shown is NET of ``tax_rate`` (Israeli capital"
                " gains; default 25%); pension annuity is NOT tax-adjusted in"
                " this model (Israeli pension annuities have different tax"
                " treatment — partial exemption + brackets — captured in a"
                " future revision)."
                " Expenses grow at ``inflation_annual + lifestyle_drift_annual``"
                " per year; pension annuity grows at ``inflation_annual`` only"
                " (pensions index to CPI, not lifestyle)."
            ),
        },
    )


def project_monte_carlo(
    *,
    household: HouseholdState,
    pensions: PensionState,
    retirement_age: float,
    years: int,
    mu_nominal_annual: float = DEFAULT_MU_NOMINAL_ANNUAL,
    sigma_annual: float = DEFAULT_SIGMA_ANNUAL,
    inflation_annual: float = DEFAULT_INFLATION_ANNUAL,
    mekadem: float = DEFAULT_MEKADEM,
    tax_rate: float = DEFAULT_TAX_RATE,
    lifestyle_drift_annual: float = DEFAULT_LIFESTYLE_DRIFT_ANNUAL,
    n_paths: int = DEFAULT_N_PATHS,
    seed: int | None = None,
    today: date | None = None,
    withdrawal_policy_id: str = "bengen_4pct",
    apply_age_aware_tax: bool = True,
) -> MonteCarloProjection:
    """Random-walk Monte Carlo of consumption-tracking retirement paths.

    Each of ``n_paths`` paths simulates monthly returns ~ lognormal(mu, sigma).
    User withdraws inflated expenses minus pension annuity each month;
    portfolio shrinks under bad-return sequences. Path 'fails' when portfolio
    hits zero.

    Returns per-tick percentile bands + path-failure aggregate stats.

    ``seed``: pin the RNG for reproducible tests. ``None`` = nondeterministic
    (production usage)."""
    import numpy as np

    today = today or date.today()
    months = max(1, min(years, 50)) * 12
    log_drift = mu_nominal_annual / 12.0 - sigma_annual ** 2 / 24.0
    log_std = sigma_annual / math.sqrt(12)
    real_return = mu_nominal_annual - inflation_annual
    expense_growth = inflation_annual + lifestyle_drift_annual

    rng = np.random.default_rng(seed)
    # Pre-generate all random returns: shape (n_paths, months)
    log_returns = rng.normal(loc=log_drift, scale=log_std, size=(n_paths, months))

    # Per-path state, kept in numpy arrays for vectorization.
    portfolio = np.full(n_paths, household.portfolio_value_nis, dtype=np.float64)
    pensia_bal = np.full(n_paths, pensions.kupat_pensia_balance_nis, dtype=np.float64)
    exec_bal = np.full(n_paths, pensions.executive_insurance_balance_nis, dtype=np.float64)
    hisht_bal = np.full(n_paths, pensions.keren_hishtalmut_balance_nis, dtype=np.float64)
    gemel_bal = np.full(n_paths, pensions.kupat_gemel_balance_nis, dtype=np.float64)

    lump_unlocked = False
    annuity_locked = False
    annuity_real_monthly = 0.0
    annuity_lock_t = 0

    # Track which paths have permanently failed (hit 0 and been clipped).
    # Once failed, a path stays at 0 for all subsequent ticks — lump unlock
    # and annuity do NOT rescue a permanently-exhausted portfolio. This
    # ensures fraction_solvent is monotone non-increasing (paths only fail,
    # never recover), which is the correct model semantics for "sequence-of-
    # returns risk": if you run out of money you're done.
    failed = np.zeros(n_paths, dtype=bool)

    # Output buffers — per-tick percentile bands + survival counts.
    portfolio_history = np.zeros((months + 1, n_paths), dtype=np.float64)
    portfolio_history[0] = portfolio.copy()

    real_monthly = 1.0 + real_return / 12.0

    for t in range(1, months + 1):
        age_t = household.current_age_years + t / 12.0

        # Portfolio: stochastic step (only for non-failed paths)
        portfolio[~failed] = portfolio[~failed] * np.exp(log_returns[~failed, t - 1])

        # Pension buckets: deterministic (same across paths)
        if not annuity_locked:
            contrib_pensia = (
                pensions.kupat_pensia_contribution_monthly_nis
                if age_t < retirement_age else 0.0
            )
            pensia_bal = pensia_bal * real_monthly + contrib_pensia
            exec_bal = exec_bal * real_monthly
        if not lump_unlocked:
            contrib_hisht = (
                pensions.keren_hishtalmut_contribution_monthly_nis
                if age_t < retirement_age else 0.0
            )
            hisht_bal = hisht_bal * real_monthly + contrib_hisht
            gemel_bal = gemel_bal * real_monthly

        # Lump unlock (deterministic timing; only applied to solvent paths)
        if age_t >= LUMP_PENSION_AGE and not lump_unlocked:
            lump_total = hisht_bal[0] + gemel_bal[0]  # same on all paths
            portfolio[~failed] = portfolio[~failed] + lump_total
            hisht_bal[:] = 0.0
            gemel_bal[:] = 0.0
            lump_unlocked = True

        # Annuity lock (deterministic → same on all paths)
        if age_t >= ANNUITY_AGE and not annuity_locked:
            annuity_real_monthly = (pensia_bal[0] + exec_bal[0]) / mekadem
            annuity_locked = True
            annuity_lock_t = t

        # Nominal annuity at this tick
        if annuity_locked:
            annuity_nominal_t = annuity_real_monthly * (
                (1.0 + inflation_annual) ** ((t - annuity_lock_t) / 12.0)
            )
        else:
            annuity_nominal_t = 0.0

        # Expenses
        expenses_t = household.monthly_expenses_nis * (
            (1.0 + expense_growth) ** (t / 12.0)
        )

        # Consumption: withdraw based on the selected policy.
        # Default 'bengen_4pct': fixed-real spending — shortfall = (expenses -
        # annuity) inflated by CPI (already baked into expenses_t/annuity_nominal_t).
        # 'guyton_klinger': apply a 10% cut to the shortfall on paths where
        # portfolio has dropped > 20% from initial (capital-preservation rule).
        # 'vpw': spend a fixed % of CURRENT balance per age band (age-banded
        # rates ~3.5% pre-50 → 7-9% post-80). Replaces the expense-shortfall
        # withdrawal entirely.
        # 'bucket': behave like Bengen but cap draw at 5% of remaining
        # portfolio (cash-bucket equivalent).
        # See argosy/services/retirement/withdrawal_policy.py for full rules.
        shortfall = max(0.0, expenses_t - annuity_nominal_t)

        # Tax engine integration: age-aware effective rate captures the
        # life-stage mix of taxable / hishtalmut / annuity sources without
        # requiring a multi-account portfolio refactor. Calibrated to
        # Israeli rules per `argosy/services/retirement/tax_engine.py`.
        #   Pre-60:  25% (all from taxable equity; Israeli CGT)
        #   60-67:   15% (taxable + hishtalmut 6yr tax-free lump available)
        #   67+:     12% (pension annuity ~20% effective on post-67 rights-
        #                 fixation + hishtalmut tax-free)
        # Setting `apply_age_aware_tax=False` falls back to the legacy flat
        # `tax_rate` slider for back-compat.
        if apply_age_aware_tax:
            age_now = household.current_age_years + t / 12.0
            if age_now < LUMP_PENSION_AGE:
                effective_tax = 0.25
            elif age_now < ANNUITY_AGE:
                effective_tax = 0.15
            else:
                effective_tax = 0.12
            denom = max(1.0 - effective_tax, 0.01)
        else:
            denom = max(1.0 - tax_rate, 0.01)

        if withdrawal_policy_id == "vpw":
            # Age-banded VPW: spend a % of current balance.
            age_now = household.current_age_years + t / 12.0
            if age_now < 50:
                vpw_rate = 0.035
            elif age_now < 60:
                vpw_rate = 0.040
            elif age_now < 70:
                vpw_rate = 0.045
            elif age_now < 80:
                vpw_rate = 0.055
            elif age_now < 90:
                vpw_rate = 0.070
            else:
                vpw_rate = 0.090
            # Per-path withdraw (vectorized) — never exceeds the path's balance
            withdraw_pretax_per_path = portfolio * (vpw_rate / 12.0)
            portfolio[~failed] = portfolio[~failed] - withdraw_pretax_per_path[~failed]
        elif withdrawal_policy_id == "guyton_klinger":
            withdraw_base = shortfall / denom
            initial_portfolio_val = household.portfolio_value_nis
            # Per-path: cut 10% when portfolio drops > 20% from initial
            stressed_mask = portfolio < (initial_portfolio_val * 0.80)
            cut_factor = np.where(stressed_mask, 0.90, 1.0)
            withdraw_pretax_per_path = withdraw_base * cut_factor
            portfolio[~failed] = portfolio[~failed] - withdraw_pretax_per_path[~failed]
        elif withdrawal_policy_id == "bucket":
            withdraw_base = shortfall / denom
            cash_bucket_cap = portfolio * (0.05 / 12.0)
            withdraw_pretax_per_path = np.minimum(withdraw_base, cash_bucket_cap)
            portfolio[~failed] = portfolio[~failed] - withdraw_pretax_per_path[~failed]
        else:  # bengen_4pct or any unknown → legacy fixed-real behavior
            withdraw_pretax = shortfall / denom
            portfolio[~failed] = portfolio[~failed] - withdraw_pretax

        # Clip and mark newly-depleted paths as permanently failed.
        newly_failed = (~failed) & (portfolio <= 0.0)
        portfolio = np.maximum(portfolio, 0.0)
        failed |= newly_failed

        portfolio_history[t] = portfolio.copy()

    # Build output series
    out: list[MonteCarloPoint] = []
    for t in range(months + 1):
        age_t = household.current_age_years + t / 12.0
        # Percentiles of portfolio_value across paths
        p10, p25, p50, p75, p90 = np.percentile(
            portfolio_history[t], [10, 25, 50, 75, 90]
        )
        solvent = float((portfolio_history[t] > 0).mean())

        # Deterministic helpers at this tick — re-derive from state machine.
        ann_t = 0.0
        if annuity_locked and t >= annuity_lock_t:
            ann_t = annuity_real_monthly * (
                (1.0 + inflation_annual) ** ((t - annuity_lock_t) / 12.0)
            )
        exp_t = household.monthly_expenses_nis * (
            (1.0 + expense_growth) ** (t / 12.0)
        )
        d = _add_months(today, t)
        out.append(MonteCarloPoint(
            months_out=t,
            age_years=age_t,
            date_yyyy_mm=d.strftime("%Y-%m"),
            portfolio_value_p10_nis=float(p10),
            portfolio_value_p25_nis=float(p25),
            portfolio_value_p50_nis=float(p50),
            portfolio_value_p75_nis=float(p75),
            portfolio_value_p90_nis=float(p90),
            fraction_solvent=solvent,
            pension_annuity_monthly_nis=ann_t,
            expenses_monthly_nis=exp_t,
        ))

    # Failure-by-age probabilities
    def fail_before(target_age: float) -> float:
        target_t = max(0, min(months, int(round((target_age - household.current_age_years) * 12))))
        return float((portfolio_history[target_t] <= 0).mean())

    return MonteCarloProjection(
        series=out,
        n_paths=n_paths,
        p_failure_before_age_75=fail_before(75),
        p_failure_before_age_85=fail_before(85),
        p_failure_before_age_95=fail_before(95),
        pension_state_at_start=pensions,
        household_state_at_start=household,
        retirement_age_assumed=retirement_age,
        assumptions={
            "mu_nominal_annual": mu_nominal_annual,
            "sigma_annual": sigma_annual,
            "real_return_annual": real_return,
            "inflation_annual": inflation_annual,
            "mekadem": mekadem,
            "tax_rate": tax_rate,
            "lifestyle_drift_annual": lifestyle_drift_annual,
            "effective_expense_growth": expense_growth,
            "lump_pension_age": LUMP_PENSION_AGE,
            "annuity_age": ANNUITY_AGE,
            "n_paths": n_paths,
            "model_notes": (
                "Monte Carlo: each path simulates monthly log-returns ~ "
                "N(mu/12 - sigma^2/24, sigma/sqrt(12)). User withdraws "
                "(inflated_expenses - nominal_pension_annuity) / (1-tax) from "
                "portfolio each month. Path fails when portfolio hits zero. "
                "Captures sequence-of-returns risk that the deterministic "
                "real-return drawdown chart cannot show."
            ),
        },
    )
