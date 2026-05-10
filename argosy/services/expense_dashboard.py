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


def compute_top_movers(session: Session, user_id: str, window: str = "trailing_12"):
    """Top growing/shrinking categories, current vs prior period."""
    from argosy.api.routes.expenses import CategoryDelta, TopMovers

    latest = _latest_tx_month(session, user_id)
    if latest is None:
        return TopMovers(grew=[], shrank=[], reason="insufficient_history")

    if window == "calendar_year":
        # current = Jan 1 .. latest_month_end of latest year
        # prior = Jan 1 .. latest_month_end of prior year
        end_month = latest.month
        cur_year = latest.year
        prior_year = cur_year - 1
        cur_keys = [f"{cur_year:04d}-{m:02d}" for m in range(1, end_month + 1)]
        prior_keys = [f"{prior_year:04d}-{m:02d}" for m in range(1, end_month + 1)]
    else:
        all_keys = _trailing_months(latest, 12)     # 12 trailing total: 6 prior + 6 current
        prior_keys = all_keys[:6]
        cur_keys = all_keys[6:]

    # Need at least one tx in prior_keys[0] month or earlier for "sufficient history".
    earliest = session.execute(
        sa_select(func.min(ExpenseTransaction.occurred_on))
        .where(ExpenseTransaction.user_id == user_id)
    ).scalar_one_or_none()
    if earliest is None:
        return TopMovers(grew=[], shrank=[], reason="insufficient_history")
    earliest_key = f"{earliest.year:04d}-{earliest.month:02d}"
    if earliest_key > prior_keys[0]:
        return TopMovers(grew=[], shrank=[], reason="insufficient_history")

    # Aggregate per (category, period).
    rows = session.execute(
        sa_select(
            ExpenseCategory.slug,
            ExpenseCategory.label_en,
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
            ExpenseCategory.is_inflow.is_(False),
            ExpenseCategory.is_excluded_from_spend.is_(False),
        )
        .group_by(ExpenseCategory.slug, ExpenseCategory.label_en, "y", "m")
    ).all()

    cur_totals: dict[str, dict] = {}  # slug -> {label, total}
    prior_totals: dict[str, dict] = {}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        bucket = cur_totals if key in cur_keys else (prior_totals if key in prior_keys else None)
        if bucket is None:
            continue
        e = bucket.setdefault(r.slug, {"label": r.label_en, "total": 0.0})
        e["total"] += float(r.nis or 0.0)

    all_slugs = set(cur_totals) | set(prior_totals)
    deltas: list[CategoryDelta] = []
    for slug in all_slugs:
        cur = cur_totals.get(slug, {"label": "", "total": 0.0})
        prior = prior_totals.get(slug, {"label": "", "total": 0.0})
        label = cur["label"] or prior["label"] or slug
        delta_nis = cur["total"] - prior["total"]
        delta_pct = (delta_nis / prior["total"]) if prior["total"] > 0 else None
        deltas.append(CategoryDelta(
            slug=slug, label=label,
            current_nis=cur["total"], prior_nis=prior["total"],
            delta_nis=delta_nis, delta_pct=delta_pct,
        ))

    grew = sorted([d for d in deltas if d.delta_nis > 0],
                  key=lambda d: d.delta_nis, reverse=True)[:5]
    shrank = sorted([d for d in deltas if d.delta_nis < 0],
                    key=lambda d: d.delta_nis)[:5]
    return TopMovers(grew=grew, shrank=shrank, reason=None)
