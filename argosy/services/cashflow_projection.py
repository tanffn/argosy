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
    pension_annuity_monthly_nis: float
    pension_lump_available_nis: float
    expenses_monthly_nis: float
    surplus_base_monthly_nis: float


@dataclass(frozen=True)
class CashflowProjection:
    """Top-level result returned to the route."""
    series: list[CashflowPoint]
    retire_ready_age: float | None
    retire_ready_months_out: int | None
    pension_state_at_start: PensionState
    household_state_at_start: HouseholdState
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
) -> tuple[int, float] | None:
    """Return ``(months_out, age_years)`` of the first row where
    ``portfolio_income_base + pension_annuity >= expenses``. ``None``
    if no such row in the supplied window."""
    for p in series:
        total = p.portfolio_income_base_monthly_nis + p.pension_annuity_monthly_nis
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
    # totals_json, convert via FX. Matches the existing
    # get_draft_projection path so the new chart and the old one show
    # the same "today's value" baseline (until Task 6 retires the old
    # chart).
    portfolio_value_nis = 0.0
    if snapshot is not None:
        try:
            totals = json.loads(snapshot.totals_json or "{}")
            usd_k = float(totals.get("total_usd_value_k") or 0.0)
            portfolio_value_nis = usd_k * 1000.0 * fx_usd_nis
        except (ValueError, TypeError):
            portfolio_value_nis = 0.0

    age = _compute_age_years(
        ctx.get("date_of_birth") if isinstance(ctx, dict) else None,
        today,
    )
    return HouseholdState(
        monthly_expenses_nis=monthly_expenses_nis,
        portfolio_value_nis=portfolio_value_nis,
        fx_usd_nis=fx_usd_nis,
        current_age_years=age,
    )
