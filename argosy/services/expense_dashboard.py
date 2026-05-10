"""Aggregation helpers for the /expenses dashboard endpoints.

Two endpoints share these helpers:

    GET /api/expenses/dashboard-overview  → "year-at-a-glance" tab
    GET /api/expenses/dashboard-monthly   → per-month detail tab

All helpers are sync, take a SQLAlchemy `Session`, and never call an LLM.
They return Pydantic models from `argosy.api.routes.expenses` (the route
module currently owns the schema; importing back from there is fine for
the v0 of this extraction).
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import extract, func, select as sa_select
from sqlalchemy.orm import Session

from argosy.state.models import ExpenseCategory, ExpenseTransaction


def _trailing_months(latest: date, n: int) -> list[str]:
    """Return n trailing 'YYYY-MM' strings ending at `latest`, oldest-first."""
    out: list[str] = []
    y, m = latest.year, latest.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))


def _latest_tx_month(session: Session, user_id: str) -> date | None:
    """Return the first-of-month date for the latest tx month, or None if no data."""
    row = session.execute(
        sa_select(func.max(ExpenseTransaction.occurred_on))
        .where(ExpenseTransaction.user_id == user_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    return date(row.year, row.month, 1)


def compute_savings_rate_trend(
    session: Session, user_id: str, months: int = 12
):
    """One savings_rate point per month for the trailing window.

    Returns `months` `SavingsRatePoint` entries, oldest-first. Months with
    zero recorded data still appear (income=0, spending=0, savings_rate=0).
    """
    from argosy.api.routes.expenses import SavingsRatePoint

    latest = _latest_tx_month(session, user_id)
    if latest is None:
        return []
    month_keys = _trailing_months(latest, months)

    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            ExpenseTransaction.direction,
            ExpenseTransaction.tx_type,
            ExpenseCategory.is_inflow,
            ExpenseCategory.is_excluded_from_spend,
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(
            ExpenseCategory,
            ExpenseTransaction.category_id == ExpenseCategory.id,
            isouter=True,
        )
        .where(ExpenseTransaction.user_id == user_id)
        .group_by(
            "y", "m",
            ExpenseTransaction.direction,
            ExpenseTransaction.tx_type,
            ExpenseCategory.is_inflow,
            ExpenseCategory.is_excluded_from_spend,
        )
    ).all()

    income: dict[str, float] = {k: 0.0 for k in month_keys}
    spending: dict[str, float] = {k: 0.0 for k in month_keys}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        if key not in income:
            continue
        if r.direction == "credit" and r.tx_type != "refund" and r.is_inflow:
            income[key] += float(r.nis or 0.0)
        elif r.direction == "debit" and not (r.is_inflow or r.is_excluded_from_spend):
            spending[key] += float(r.nis or 0.0)

    out = []
    for key in month_keys:
        inc = income[key]
        spend = spending[key]
        rate = (inc - spend) / inc if inc > 0 else 0.0
        out.append(SavingsRatePoint(
            month=key, income_nis=inc, spending_nis=spend, savings_rate=rate,
        ))
    return out
