"""Merchant rolling-statistics recompute service (sprint #2 commit #4).

Backs Bucket A anomaly detection (amount outliers). See spec
``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§1.1 for the design and `merchant_rolling_stats` table contract.

Statistics computed per (user_id, merchant_normalized, category_id) over
a trailing N-day window (default 180):

  * **median + MAD** (Median Absolute Deviation) — robust statistics
    used by Pattern A1 (category robust outlier). The 1.4826 × MAD
    scaling factor (applied downstream by the detector, not stored here)
    makes MAD an unbiased estimator of stdev for normally-distributed
    data. Real spend distributions are heavy-tailed — robust stats are
    insensitive to the exact outliers we want to flag.

  * **mean + stdev** — kept for backward-compat with
    ``expense_dashboard.py`` and used by Pattern A2 (merchant spike),
    which compares against the mean.

  * **min / max / first_seen / last_seen / txn_count** — descriptive
    context for the UI.

The service skips merchants with only one transaction in the window
(MAD requires ≥2 observations to be meaningful).

UPSERT semantics: the (user_id, merchant_normalized, category_id,
window_end) tuple is the natural key. Re-running the recompute on the
same as-of date updates rows in place; running with a different as-of
date inserts new rows (preserves history).

Decimal-money math: all amounts read from `expense_transactions.amount_nis`
are `Decimal(12, 2)`. Statistics are computed in Decimal where possible;
median/MAD use floats internally for the sort but are rounded back to
2-decimal Decimal before persistence.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseTransaction,
    MerchantRollingStats,
)


def _quantize(value: float | Decimal) -> Decimal:
    """Round to 2 decimal places using bankers'-rounding-equivalent
    (ROUND_HALF_UP to keep test expectations intuitive)."""
    if isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _median_abs_dev(values: list[float], med: float) -> float:
    """Median Absolute Deviation: median of |x_i - median|."""
    return statistics.median(abs(v - med) for v in values)


def recompute_merchant_stats(
    session: Session,
    user_id: str,
    *,
    window_days: int = 180,
    as_of: date | None = None,
) -> int:
    """For each unique ``merchant_normalized`` (× category_id) for this
    user with ≥2 txns in the trailing ``window_days``, compute robust +
    parametric statistics and UPSERT a row in ``merchant_rolling_stats``.

    Args:
      session: SQLAlchemy session (sync). Caller commits.
      user_id: user.id to recompute for.
      window_days: trailing window in days (default 180).
      as_of: window end date (default: today). The window covers
        [as_of - window_days + 1, as_of] inclusive.

    Returns:
      Count of merchants (unique (merchant_normalized, category_id)
      groups) for which a row was written. Merchants with only 1
      observation in the window are skipped.

    Notes:
      - Direction-agnostic: both debits and credits are included. The
        Bucket A detector filters at fire-time. (Refunds at the same
        merchant are typically same-direction-aware downstream.)
      - amount_nis NULL rows are skipped (idempotent — no stats to compute).
      - The UNIQUE constraint on (user_id, merchant_normalized,
        category_id, window_end) makes the recompute idempotent.
    """
    if as_of is None:
        as_of = date.today()
    window_start = as_of - timedelta(days=window_days - 1)

    # Group transactions by (merchant_normalized, category_id).
    rows = session.execute(
        sa_select(
            ExpenseTransaction.merchant_normalized,
            ExpenseTransaction.category_id,
            ExpenseTransaction.amount_nis,
            ExpenseTransaction.occurred_on,
        )
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.occurred_on >= window_start)
        .where(ExpenseTransaction.occurred_on <= as_of)
        .where(ExpenseTransaction.amount_nis.is_not(None))
    ).all()

    groups: dict[
        tuple[str, int | None],
        list[tuple[Decimal, date]],
    ] = defaultdict(list)
    for merchant, category_id, amount_nis, occurred_on in rows:
        # Take absolute magnitude: anomaly detection cares about size,
        # not direction (credits → refunds at the same merchant share a
        # baseline with the original debits).
        amt = abs(Decimal(amount_nis))
        groups[(merchant, category_id)].append((amt, occurred_on))

    written = 0
    for (merchant, category_id), observations in groups.items():
        if len(observations) < 2:
            # Single observation: no meaningful MAD/stdev. Skip.
            continue

        amounts = [float(amt) for amt, _ in observations]
        dates = [d for _, d in observations]
        amounts_sorted = sorted(amounts)

        med = statistics.median(amounts)
        mad = _median_abs_dev(amounts, med)
        mean = statistics.fmean(amounts)
        stdev = statistics.pstdev(amounts) if len(amounts) >= 2 else None

        existing = session.execute(
            sa_select(MerchantRollingStats)
            .where(MerchantRollingStats.user_id == user_id)
            .where(MerchantRollingStats.merchant_normalized == merchant)
            .where(MerchantRollingStats.category_id.is_(category_id) if category_id is None else MerchantRollingStats.category_id == category_id)
            .where(MerchantRollingStats.window_end == as_of)
        ).scalar_one_or_none()

        payload = dict(
            window_start=window_start,
            window_end=as_of,
            txn_count=len(observations),
            median_nis=_quantize(med),
            mad_nis=_quantize(mad),
            mean_nis=_quantize(mean),
            stdev_nis=_quantize(stdev) if stdev is not None else None,
            min_nis=_quantize(amounts_sorted[0]),
            max_nis=_quantize(amounts_sorted[-1]),
            first_seen_at=min(dates),
            last_seen_at=max(dates),
        )

        if existing is None:
            session.add(
                MerchantRollingStats(
                    user_id=user_id,
                    merchant_normalized=merchant,
                    category_id=category_id,
                    **payload,
                )
            )
        else:
            for key, value in payload.items():
                setattr(existing, key, value)

        written += 1

    session.flush()
    return written
