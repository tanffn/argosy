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

from sqlalchemy import case, extract, func, select as sa_select
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


def compute_currency_mix(session: Session, user_id: str, months: int = 12):
    from argosy.api.routes.expenses import CurrencyMixPoint

    latest = _latest_tx_month(session, user_id)
    if latest is None:
        return []
    month_keys = _trailing_months(latest, months)

    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(ExpenseTransaction.currency_orig, "NIS").label("ccy"),
            func.coalesce(func.sum(case(
                (ExpenseTransaction.currency_orig == "USD",
                 ExpenseTransaction.amount_orig),
                else_=ExpenseTransaction.amount_nis,
            )), 0.0).label("amt"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id, isouter=True)
        # spending filter — categorised rows must be non-inflow, non-excluded
        # (uncategorised rows pass the outer-join sieve already; treat as NIS spend)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
            (ExpenseCategory.is_inflow.is_(False) |
             ExpenseCategory.is_inflow.is_(None)),
            (ExpenseCategory.is_excluded_from_spend.is_(False) |
             ExpenseCategory.is_excluded_from_spend.is_(None)),
        )
        .group_by("y", "m", "ccy")
    ).all()

    nis: dict[str, float] = {k: 0.0 for k in month_keys}
    usd: dict[str, float] = {k: 0.0 for k in month_keys}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        if key not in nis:
            continue
        if r.ccy == "USD":
            usd[key] += float(r.amt or 0.0)
        else:
            nis[key] += float(r.amt or 0.0)

    return [
        CurrencyMixPoint(month=k, nis=nis[k], usd=usd[k]) for k in month_keys
    ]


def _shift_month_key(s: str, delta: int) -> str:
    """Shift a 'YYYY-MM' month key by `delta` months (positive = future)."""
    y, m = int(s[:4]), int(s[5:7])
    m += delta
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def _months_between(a: str, b: str) -> int:
    """Return signed count of months from a -> b. b later than a => positive."""
    ay, am = int(a[:4]), int(a[5:7])
    by, bm = int(b[:4]), int(b[5:7])
    return (by - ay) * 12 + (bm - am)


def compute_chart_window(session: Session, user_id: str, focal_month: str):
    """Return 12 ChartWindowBar entries per the A-rule (spec §5.3).

    1. Find oldest/newest months with data for the user.
    2. Ideal window = focal_month - 6 .. focal_month + 5 (12 months total).
    3. Slide right at the past edge if ideal_left < oldest.
    4. Slide left at the future edge if right > newest, but never past oldest.
    5. Mark out-of-range bars `is_padding=True` with zero totals.
    6. Mark the focal month `is_selected=True`.
    """
    from argosy.api.routes.expenses import ChartWindowBar

    bounds = session.execute(
        sa_select(
            func.min(ExpenseTransaction.occurred_on).label("oldest"),
            func.max(ExpenseTransaction.occurred_on).label("newest"),
        ).where(ExpenseTransaction.user_id == user_id)
    ).one()
    if bounds.oldest is None:
        return []
    oldest_key = f"{bounds.oldest.year:04d}-{bounds.oldest.month:02d}"
    newest_key = f"{bounds.newest.year:04d}-{bounds.newest.month:02d}"

    # Compute ideal window centred on focal.
    ideal = [_shift_month_key(focal_month, -6 + i) for i in range(12)]
    left, right = ideal[0], ideal[-1]

    # Slide right at past edge.
    if left < oldest_key:
        shift = _months_between(left, oldest_key)
        ideal = [_shift_month_key(k, shift) for k in ideal]
        left, right = ideal[0], ideal[-1]

    # Slide left at future edge — but never push left past oldest.
    if right > newest_key:
        shift = -_months_between(newest_key, right)
        ideal = [_shift_month_key(k, shift) for k in ideal]
        if ideal[0] < oldest_key:
            shift_back = _months_between(ideal[0], oldest_key)
            ideal = [_shift_month_key(k, shift_back) for k in ideal]

    # Aggregate per month from DB (spending only — same filters as currency_mix).
    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(ExpenseTransaction.currency_orig, "NIS").label("ccy"),
            func.coalesce(func.sum(case(
                (ExpenseTransaction.currency_orig == "USD",
                 ExpenseTransaction.amount_orig),
                else_=ExpenseTransaction.amount_nis,
            )), 0.0).label("amt"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id, isouter=True)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
            (ExpenseCategory.is_inflow.is_(False) |
             ExpenseCategory.is_inflow.is_(None)),
            (ExpenseCategory.is_excluded_from_spend.is_(False) |
             ExpenseCategory.is_excluded_from_spend.is_(None)),
        )
        .group_by("y", "m", "ccy")
    ).all()
    nis: dict[str, float] = {}
    usd: dict[str, float] = {}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        if r.ccy == "USD":
            usd[key] = usd.get(key, 0.0) + float(r.amt or 0.0)
        else:
            nis[key] = nis.get(key, 0.0) + float(r.amt or 0.0)

    out = []
    for key in ideal:
        is_padding = key < oldest_key or key > newest_key
        out.append(ChartWindowBar(
            month=key,
            total_nis=0.0 if is_padding else nis.get(key, 0.0),
            total_usd=0.0 if is_padding else usd.get(key, 0.0),
            is_padding=is_padding,
            is_selected=(key == focal_month),
        ))
    return out


def compute_hero_stats_monthly(session: Session, user_id: str, month: str):
    """Hero-stat bundle for the Monthly tab.

    `value_nis` mirrors the existing `current_month_*` semantics from the
    overview endpoint. `mom_delta_pct` is `None` if the prior month is
    missing or zero. `vs_trailing12_pct` is `None` if fewer than 3 of the
    12 immediately preceding months had any data, or if their average is 0.
    """
    from argosy.api.routes.expenses import HeroMetric, HeroStatsMonthly

    spending_by_month = _spending_by_month_dict(session, user_id)
    income_by_month = _income_by_month_dict(session, user_id)
    refunds_by_month = _refunds_by_month_dict(session, user_id)

    def metric(by_month: dict[str, float], key: str) -> "HeroMetric":
        cur = by_month.get(key, 0.0)
        prev_key = _shift_month_key(key, -1)
        prev = by_month.get(prev_key)
        mom = (cur - prev) / prev if (prev is not None and prev > 0) else None

        # trailing-12 = 12 months immediately before `key`
        trailing_keys = [_shift_month_key(key, -i) for i in range(1, 13)]
        prior_vals = [by_month[k] for k in trailing_keys if k in by_month]
        if len(prior_vals) >= 3 and sum(prior_vals) > 0:
            avg = sum(prior_vals) / len(prior_vals)
            vs12 = (cur - avg) / avg if avg > 0 else None
        else:
            vs12 = None

        return HeroMetric(value_nis=cur, mom_delta_pct=mom, vs_trailing12_pct=vs12)

    statements_reconciled = _count_reconciled_statements_for_month(session, user_id, month)
    # Anomalies: 0 placeholder. Surfacing the dashboard-overview's inline
    # anomaly logic here requires an extraction that is deferred to a later
    # task; the hero card renders fine with a 0.
    anomalies_count = 0

    return HeroStatsMonthly(
        spent=metric(spending_by_month, month),
        income=metric(income_by_month, month),
        refunds=metric(refunds_by_month, month),
        statements_reconciled=statements_reconciled,
        anomalies_count=anomalies_count,
    )


def compute_categories_vs_typical(session: Session, user_id: str, month: str):
    """Top-3 spending categories with the largest |z-score| vs trailing-12 typical.

    For each spending-only category:
      - mean+std of NIS spending across the 12 months strictly before `month`
      - std floored at ₪50
      - excluded if fewer than 3 prior-month observations
      - z = (this_month_nis - mean) / std

    Returns top-3 by `|z_score|`, sorted by z_score desc (most-positive first).
    """
    from argosy.api.routes.expenses import CategoryDeviation
    import statistics

    # All spending-only rows grouped by (slug, month).
    rows = session.execute(
        sa_select(
            ExpenseCategory.slug,
            ExpenseCategory.label_en,
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(ExpenseCategory, ExpenseTransaction.category_id == ExpenseCategory.id)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
            ExpenseCategory.is_inflow.is_(False),
            ExpenseCategory.is_excluded_from_spend.is_(False),
        )
        .group_by(ExpenseCategory.slug, ExpenseCategory.label_en, "y", "m")
    ).all()

    # Build {slug: {month_key: total}}.
    by_slug: dict[str, dict[str, float]] = {}
    labels: dict[str, str] = {}
    for r in rows:
        key = f"{int(r.y):04d}-{int(r.m):02d}"
        by_slug.setdefault(r.slug, {})[key] = float(r.nis or 0.0)
        labels[r.slug] = r.label_en

    trailing_keys = [_shift_month_key(month, -i) for i in range(1, 13)]
    out: list[CategoryDeviation] = []
    for slug, monthly in by_slug.items():
        prior = [monthly[k] for k in trailing_keys if k in monthly]
        if len(prior) < 3:
            continue
        cur = monthly.get(month, 0.0)
        mean = sum(prior) / len(prior)
        std = statistics.pstdev(prior) if len(prior) >= 2 else 0.0
        std = max(std, 50.0)
        z = (cur - mean) / std if std > 0 else 0.0
        delta_pct = (cur - mean) / mean if mean > 0 else None
        out.append(CategoryDeviation(
            slug=slug, label=labels[slug], this_month_nis=cur,
            typical_mean_nis=mean, typical_std_nis=std,
            z_score=z, delta_pct=delta_pct,
        ))

    out.sort(key=lambda d: abs(d.z_score), reverse=True)
    return out[:3]


def compute_largest_transactions(
    session: Session, user_id: str, month: str, limit: int = 5
):
    """Top `limit` transactions by |amount_nis| in the focal month.

    Filter: ``direction='debit'`` AND spending-only (non-inflow,
    non-excluded; uncategorised rows pass via outer-join sieve) AND
    ``occurred_on`` in the focal month.
    Order: ``ABS(amount_nis)`` desc, ties broken by ``occurred_on`` desc.
    """
    from argosy.api.routes.expenses import _tx_to_out

    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    if m == 12:
        last_excl = date(y + 1, 1, 1)
    else:
        last_excl = date(y, m + 1, 1)

    rows = session.execute(
        sa_select(ExpenseTransaction)
        .join(
            ExpenseCategory,
            ExpenseTransaction.category_id == ExpenseCategory.id,
            isouter=True,
        )
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.direction == "debit",
            ExpenseTransaction.occurred_on >= first,
            ExpenseTransaction.occurred_on < last_excl,
            (ExpenseCategory.is_inflow.is_(False) |
             ExpenseCategory.is_inflow.is_(None)),
            (ExpenseCategory.is_excluded_from_spend.is_(False) |
             ExpenseCategory.is_excluded_from_spend.is_(None)),
        )
        .order_by(
            func.abs(ExpenseTransaction.amount_nis).desc(),
            ExpenseTransaction.occurred_on.desc(),
        )
        .limit(limit)
    ).scalars().all()

    cat_by_id = {
        c.id: c.slug for c in session.query(ExpenseCategory).filter_by(
            user_id=user_id,
        ).all()
    }
    return [_tx_to_out(r, cat_by_id) for r in rows]


# ----------------- shared per-month accumulators -----------------

def _spending_by_month_dict(session: Session, user_id: str) -> dict[str, float]:
    """Sum debits per month, excluding inflow/excluded categories and card payments.

    Mirrors the existing `current_month_spending_nis` filters from the
    overview endpoint.
    """
    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id, isouter=True)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.is_card_payment.is_(False),
            ExpenseTransaction.amount_nis.is_not(None),
            ExpenseTransaction.direction == "debit",
            (ExpenseCategory.is_inflow.is_(False) | ExpenseCategory.is_inflow.is_(None)),
            (ExpenseCategory.is_excluded_from_spend.is_(False) |
             ExpenseCategory.is_excluded_from_spend.is_(None)),
        )
        .group_by("y", "m")
    ).all()
    return {f"{int(r.y):04d}-{int(r.m):02d}": float(r.nis or 0.0) for r in rows}


def _income_by_month_dict(session: Session, user_id: str) -> dict[str, float]:
    """Sum credits per month with is_inflow=True and tx_type != 'refund'."""
    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .join(ExpenseCategory,
              ExpenseTransaction.category_id == ExpenseCategory.id)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.is_card_payment.is_(False),
            ExpenseTransaction.amount_nis.is_not(None),
            ExpenseTransaction.direction == "credit",
            ExpenseTransaction.tx_type != "refund",
            ExpenseCategory.is_inflow.is_(True),
        )
        .group_by("y", "m")
    ).all()
    return {f"{int(r.y):04d}-{int(r.m):02d}": float(r.nis or 0.0) for r in rows}


def _refunds_by_month_dict(session: Session, user_id: str) -> dict[str, float]:
    """Sum credits per month with tx_type == 'refund'."""
    rows = session.execute(
        sa_select(
            extract("year", ExpenseTransaction.occurred_on).label("y"),
            extract("month", ExpenseTransaction.occurred_on).label("m"),
            func.coalesce(func.sum(ExpenseTransaction.amount_nis), 0.0).label("nis"),
        )
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.is_card_payment.is_(False),
            ExpenseTransaction.amount_nis.is_not(None),
            ExpenseTransaction.direction == "credit",
            ExpenseTransaction.tx_type == "refund",
        )
        .group_by("y", "m")
    ).all()
    return {f"{int(r.y):04d}-{int(r.m):02d}": float(r.nis or 0.0) for r in rows}


def _count_reconciled_statements_for_month(
    session: Session, user_id: str, month: str
) -> int:
    """Count statements whose period overlaps `month` and whose declared/parsed
    totals reconcile (gap < 0.5, matching `_gap_status`'s 'green' band).

    NOTE: the `ExpenseStatement.status` column stores parser-state values
    like ``'parsed'``, NOT the gap-derived ``'green'/'yellow'/'red'``
    vocabulary. The "reconciled" hero counter is derived from the
    parsed-vs-declared gap to match the overview endpoint's `_gap_status`.
    """
    from argosy.state.models import ExpenseStatement

    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    if m == 12:
        last_excl = date(y + 1, 1, 1)
    else:
        last_excl = date(y, m + 1, 1)

    # Statement overlaps month if period_start < last_excl AND period_end >= first.
    # "Reconciled" = both totals present and |parsed - declared| < 0.5.
    rows = session.execute(
        sa_select(
            ExpenseStatement.parsed_total_nis,
            ExpenseStatement.declared_total_nis,
        )
        .where(
            ExpenseStatement.user_id == user_id,
            ExpenseStatement.period_start < last_excl,
            ExpenseStatement.period_end >= first,
            ExpenseStatement.declared_total_nis.is_not(None),
        )
    ).all()

    count = 0
    for parsed, declared in rows:
        if parsed is None or declared is None:
            continue
        if abs(float(parsed) - float(declared)) < 0.5:
            count += 1
    return count
