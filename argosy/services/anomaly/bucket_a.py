"""Bucket A anomaly detectors (amount outliers) — sprint #2 commit #5.

Two patterns per spec
``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§1.1:

  * **A1 — Category robust outlier.** For each new transaction in
    category C, compute the robust z-score
    ``r = (amount - median_C) / (1.4826 * MAD_C)`` against the
    median/MAD baseline from ``merchant_rolling_stats`` aggregated up to
    category-level. Fire when ``r >= 3`` AND ``abs(amount) >= 200``
    AND prior baseline txn_count in the category is >= 6. Severity
    bands: 3-4 -> info, 4-6 -> warning, >=6 -> critical.

  * **A2 — Merchant spike.** For each new transaction at merchant M,
    compare ``amount_nis`` to the trailing-window mean for M. Fire
    when ``amount >= 3 * mean_M`` AND ``mean_M >= 50`` AND prior
    occurrence count at M is >= 3. Severity: warning by default,
    critical if ``amount >= 5 * mean_M``.

Both patterns share these contracts:

  * Window: by default the detector scans the last 30 days of
    transactions and compares against the latest ``window_end`` per
    (merchant, category) in ``merchant_rolling_stats``.
  * Baselines must already exist — the detector does NOT compute them.
    Run ``recompute_merchant_stats`` first (sprint #2 commit #4).
  * Writes ``ExpenseReviewQueue`` rows with a deterministic
    ``dedup_key`` per spec §4. The partial unique index on
    ``(user_id, dedup_key) WHERE dedup_key IS NOT NULL AND status='open'``
    keeps the detector idempotent across reruns.
  * Uses SAVEPOINT-per-row insert pattern (cribbed from
    ``argosy/services/news_ingest.py``) so a unique-violation on one
    row never poisons the rest of the batch.

NO LLM — pure SQL + arithmetic. Decimal-money math throughout.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseReviewQueue,
    ExpenseTransaction,
    MerchantRollingStats,
)

logger = logging.getLogger(__name__)


# Tunables — surfaced as module-level constants so future rule tweaks
# bump the ``v1`` dedup prefix consciously (per spec §4 codex IMPORTANT
# #3: the dedup_key formula bakes in the rule params verbatim so that a
# threshold change yields a fresh key and the next run re-fires).
A1_Z_THRESHOLD = Decimal("3")
A1_MIN_ABS_AMOUNT_NIS = Decimal("200")
A1_MIN_BASELINE_COUNT = 6
A1_MAD_SCALE = Decimal("1.4826")  # makes MAD an unbiased stdev estimator.

A2_MIN_MULTIPLE = Decimal("3")
A2_CRITICAL_MULTIPLE = Decimal("5")
A2_MIN_MEAN_NIS = Decimal("50")
A2_MIN_BASELINE_COUNT = 3

DETECTION_LOOKBACK_DAYS = 30
DEFAULT_BASELINE_LOOKBACK_DAYS = 180


# ---------------------------------------------------------------------------
# Public types.
# ---------------------------------------------------------------------------


DetectorKind = Literal["a1_category_outlier", "a2_merchant_spike"]
Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class AmountOutlierFlag:
    """One A-bucket anomaly. kind='a1_category_outlier' or 'a2_merchant_spike'."""

    transaction_id: int
    user_id: str
    merchant_normalized: str
    amount_nis: float
    category_id: int | None
    detector: DetectorKind
    severity: Severity
    rationale: str  # one-line for the queue row.
    dedup_key: str


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def detect_bucket_a(
    session: Session,
    user_id: str,
    *,
    as_of: date | None = None,
    lookback_days_for_baseline: int = DEFAULT_BASELINE_LOOKBACK_DAYS,
) -> list[AmountOutlierFlag]:
    """Run both A1 and A2 detectors against transactions in the last 30 days.

    Compares each transaction against its corresponding
    ``merchant_rolling_stats`` baseline rows (matched on
    ``window_end == as_of`` first; the baseline is treated as
    "the most-recent recompute available for this (merchant, category)"
    when no exact window_end match exists). Returns the new flags fired
    AND writes ``ExpenseReviewQueue`` rows with ``dedup_key`` per
    spec §4.

    Args:
      session: SQLAlchemy session (sync). The caller commits.
      user_id: user.id to detect anomalies for.
      as_of: window end (default: today). The detection sweep covers
        transactions in [as_of - 30, as_of].
      lookback_days_for_baseline: matches the ``window_days`` passed to
        ``recompute_merchant_stats``; only baseline rows whose
        ``window_end >= as_of - lookback_days_for_baseline`` are
        considered (avoids matching against very stale baselines).

    Returns:
      List of flags that fired AND for which a queue row was successfully
      written. Duplicates suppressed by the partial unique index are
      NOT included (per spec §1.5 idempotency contract).
    """
    if as_of is None:
        as_of = date.today()
    detection_start = as_of - timedelta(days=DETECTION_LOOKBACK_DAYS)
    baseline_min_window_end = as_of - timedelta(days=lookback_days_for_baseline)

    # Pull candidate transactions for the detection window.
    tx_rows = session.execute(
        sa_select(ExpenseTransaction)
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.occurred_on >= detection_start)
        .where(ExpenseTransaction.occurred_on <= as_of)
        .where(ExpenseTransaction.amount_nis.is_not(None))
    ).scalars().all()

    if not tx_rows:
        return []

    # Pull all baselines for this user that are still fresh enough to
    # be considered. We index them two ways — by (merchant, category)
    # for A2 and aggregated by category for A1.
    baselines = session.execute(
        sa_select(MerchantRollingStats)
        .where(MerchantRollingStats.user_id == user_id)
        .where(MerchantRollingStats.window_end >= baseline_min_window_end)
        .where(MerchantRollingStats.window_end <= as_of)
    ).scalars().all()

    merchant_baseline_by_key = _index_merchant_baselines(baselines)
    category_baseline_by_id = _aggregate_category_baselines(baselines)

    fired: list[AmountOutlierFlag] = []

    for tx in tx_rows:
        flag_a1 = _evaluate_a1(tx, category_baseline_by_id, as_of=as_of)
        if flag_a1 is not None and _persist_flag(session, flag_a1, tx):
            fired.append(flag_a1)

        flag_a2 = _evaluate_a2(tx, merchant_baseline_by_key, as_of=as_of)
        if flag_a2 is not None and _persist_flag(session, flag_a2, tx):
            fired.append(flag_a2)

    return fired


# ---------------------------------------------------------------------------
# A1 — category robust outlier.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CategoryBaseline:
    """Aggregated baseline at category level. Combines all
    ``merchant_rolling_stats`` rows sharing the same category. The
    aggregation uses the freshest available window_end per merchant so
    a category-level signal is not dominated by stale rows."""

    category_id: int | None
    txn_count: int
    median_nis: Decimal
    mad_nis: Decimal | None
    window_end: date  # the freshest window_end across the contributing rows.


def _aggregate_category_baselines(
    baselines: list[MerchantRollingStats],
) -> dict[int | None, _CategoryBaseline]:
    """Aggregate per-merchant baselines up to category level.

    For each category we want median(amounts) + MAD across the whole
    category. The rolling-stats rows give us per-merchant medians; the
    cleanest aggregation that doesn't require re-reading raw
    transactions is to pool the per-merchant medians as observations
    and take the median/MAD of those. (This is a coarser baseline than
    re-aggregating from raw rows but it matches the spec's intent: A1
    is about category-level outliers, not merchant-level — those are A2.)

    For the txn_count gate (>= 6 prior transactions in the category)
    we sum across merchants.
    """
    # Pick the freshest row per (merchant, category) so the aggregate
    # isn't double-counted across historical recomputes.
    freshest: dict[tuple[str, int | None], MerchantRollingStats] = {}
    for row in baselines:
        key = (row.merchant_normalized, row.category_id)
        cur = freshest.get(key)
        if cur is None or row.window_end > cur.window_end:
            freshest[key] = row

    # Bucket by category.
    by_cat: dict[int | None, list[MerchantRollingStats]] = {}
    for row in freshest.values():
        by_cat.setdefault(row.category_id, []).append(row)

    out: dict[int | None, _CategoryBaseline] = {}
    for cat_id, rows in by_cat.items():
        medians = sorted(Decimal(r.median_nis) for r in rows)
        n = len(medians)
        if n == 0:
            continue
        # Median of merchant-medians.
        if n % 2 == 1:
            cat_median = medians[n // 2]
        else:
            cat_median = (medians[n // 2 - 1] + medians[n // 2]) / Decimal(2)
        # MAD of merchant-medians vs the category median.
        deviations = sorted(abs(m - cat_median) for m in medians)
        if n % 2 == 1:
            cat_mad: Decimal | None = deviations[n // 2]
        else:
            cat_mad = (
                (deviations[n // 2 - 1] + deviations[n // 2]) / Decimal(2)
            )
        # MAD of length-1 sample is 0 — treat as no usable spread.
        if n < 2 or cat_mad is None or cat_mad == 0:
            cat_mad = None
        txn_count = sum(r.txn_count for r in rows)
        window_end = max(r.window_end for r in rows)
        out[cat_id] = _CategoryBaseline(
            category_id=cat_id,
            txn_count=txn_count,
            median_nis=cat_median,
            mad_nis=cat_mad,
            window_end=window_end,
        )
    return out


def _evaluate_a1(
    tx: ExpenseTransaction,
    category_baselines: dict[int | None, _CategoryBaseline],
    *,
    as_of: date,
) -> AmountOutlierFlag | None:
    """Pattern A1 — category robust outlier. None if any gate fails."""
    if tx.amount_nis is None:
        return None
    amount = abs(Decimal(tx.amount_nis))
    # ₪200 absolute-amount gate — avoid noise on micro-transactions.
    if amount < A1_MIN_ABS_AMOUNT_NIS:
        return None

    baseline = category_baselines.get(tx.category_id)
    if baseline is None:
        return None
    # Baseline-sufficiency gate (>=6 prior txns in the category).
    if baseline.txn_count < A1_MIN_BASELINE_COUNT:
        return None
    if baseline.mad_nis is None or baseline.mad_nis == 0:
        return None

    # Robust z-score: r = (amount - median_C) / (1.4826 * MAD_C).
    scaled_mad = A1_MAD_SCALE * baseline.mad_nis
    if scaled_mad == 0:
        return None
    raw_amount = Decimal(tx.amount_nis)
    robust_z = (raw_amount - baseline.median_nis) / scaled_mad

    # One-tailed: only over-budget firings are interesting (spec wording
    # "outlier"; the unallocated-cash / underspend angles are out of
    # scope for v1).
    if robust_z < A1_Z_THRESHOLD:
        return None

    severity = _severity_for_a1(robust_z)
    # Codex BLOCKER (sprint #2 Buckets A+B review): dedup_key must
    # interpolate the LIVE threshold + min-amount values so a future
    # constant tweak invalidates the prior keys + emits fresh ones.
    # Hardcoded `thr:3|min:200` would silently suppress new fires
    # after a tuning change.
    dedup_key = (
        f"v1|a1|u:{tx.user_id}|cat:{tx.category_id}|tx:{tx.id}"
        f"|win_end:{as_of.isoformat()}"
        f"|thr:{A1_Z_THRESHOLD}|min:{A1_MIN_ABS_AMOUNT_NIS}"
    )
    rationale = (
        f"Amount NIS {raw_amount} is {robust_z:.1f}σ-equiv above "
        f"the category median NIS {baseline.median_nis} (MAD "
        f"NIS {baseline.mad_nis}, n={baseline.txn_count})."
    )
    return AmountOutlierFlag(
        transaction_id=tx.id,
        user_id=tx.user_id,
        merchant_normalized=tx.merchant_normalized,
        amount_nis=float(raw_amount),
        category_id=tx.category_id,
        detector="a1_category_outlier",
        severity=severity,
        rationale=rationale,
        dedup_key=dedup_key,
    )


def _severity_for_a1(robust_z: Decimal) -> Severity:
    """Severity bands per spec §1.1: 3-4 -> info, 4-6 -> warning, >=6 -> critical."""
    if robust_z >= Decimal("6"):
        return "critical"
    if robust_z >= Decimal("4"):
        return "warning"
    return "info"


# ---------------------------------------------------------------------------
# A2 — merchant spike.
# ---------------------------------------------------------------------------


def _index_merchant_baselines(
    baselines: list[MerchantRollingStats],
) -> dict[tuple[str, int | None], MerchantRollingStats]:
    """Pick the freshest baseline per (merchant, category) so we score
    each transaction against the latest available recompute. If multiple
    rows exist on the same window_end, last-write-wins (deterministic
    because the recompute is itself idempotent)."""
    out: dict[tuple[str, int | None], MerchantRollingStats] = {}
    for row in baselines:
        key = (row.merchant_normalized, row.category_id)
        cur = out.get(key)
        if cur is None or row.window_end > cur.window_end:
            out[key] = row
    return out


def _evaluate_a2(
    tx: ExpenseTransaction,
    merchant_baselines: dict[tuple[str, int | None], MerchantRollingStats],
    *,
    as_of: date,
) -> AmountOutlierFlag | None:
    """Pattern A2 — merchant spike. None if any gate fails."""
    if tx.amount_nis is None:
        return None
    raw_amount = Decimal(tx.amount_nis)
    amount = abs(raw_amount)
    baseline = merchant_baselines.get(
        (tx.merchant_normalized, tx.category_id)
    )
    if baseline is None:
        # Fall back to any-category baseline for this merchant
        # (merchant rolling-stats are also category-keyed; A2 is a
        # merchant-level signal, so let the merchant aggregate
        # dominate).
        any_cat = [
            r for (m, _), r in merchant_baselines.items()
            if m == tx.merchant_normalized
        ]
        if not any_cat:
            return None
        baseline = max(any_cat, key=lambda r: r.window_end)

    # Baseline-sufficiency gate (>=3 prior occurrences at this merchant).
    if baseline.txn_count < A2_MIN_BASELINE_COUNT:
        return None
    mean = Decimal(baseline.mean_nis)
    # Min-mean gate to avoid noise on rare merchants.
    if mean < A2_MIN_MEAN_NIS:
        return None

    multiple = amount / mean if mean > 0 else Decimal(0)
    if multiple < A2_MIN_MULTIPLE:
        return None

    severity: Severity = (
        "critical" if multiple >= A2_CRITICAL_MULTIPLE else "warning"
    )
    # Codex BLOCKER (sprint #2 Buckets A+B review): same fix as A1 —
    # interpolate the live A2_MIN_MULTIPLE so threshold tweaks emit
    # fresh dedup_keys instead of silently suppressing.
    dedup_key = (
        f"v1|a2|u:{tx.user_id}|m:{tx.merchant_normalized}|tx:{tx.id}"
        f"|win_end:{as_of.isoformat()}|mult:{A2_MIN_MULTIPLE}"
    )
    rationale = (
        f"Amount NIS {raw_amount} is {multiple:.1f}x the merchant "
        f"trailing mean NIS {mean} (n={baseline.txn_count})."
    )
    return AmountOutlierFlag(
        transaction_id=tx.id,
        user_id=tx.user_id,
        merchant_normalized=tx.merchant_normalized,
        amount_nis=float(raw_amount),
        category_id=tx.category_id,
        detector="a2_merchant_spike",
        severity=severity,
        rationale=rationale,
        dedup_key=dedup_key,
    )


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------


def _persist_flag(
    session: Session,
    flag: AmountOutlierFlag,
    tx: ExpenseTransaction,
) -> bool:
    """SAVEPOINT-per-row insert. Returns True iff a new row was written.

    The partial unique index ``ix_expense_review_queue_dedup`` on
    ``(user_id, dedup_key) WHERE dedup_key IS NOT NULL AND status='open'``
    catches duplicates from re-runs. We rely on it rather than a
    SELECT-then-INSERT race because the queue table has FK cascades
    that make pre-checks racy under concurrent ingest.
    """
    payload = {
        "detector": flag.detector,
        "severity": flag.severity,
        "rationale": flag.rationale,
        "transaction_id": flag.transaction_id,
        "merchant_normalized": flag.merchant_normalized,
        "amount_nis": flag.amount_nis,
        "category_id": flag.category_id,
    }
    row = ExpenseReviewQueue(
        user_id=flag.user_id,
        kind=flag.detector,
        status="open",
        payload_json=json.dumps(payload),
        related_tx_id=flag.transaction_id,
        bucket="amount",
        materiality=flag.severity,
        dedup_key=flag.dedup_key,
    )
    try:
        with session.begin_nested():  # SAVEPOINT.
            session.add(row)
            session.flush()
        return True
    except Exception as exc:  # pragma: no cover — covered by idempotency test.
        # SAVEPOINT auto-rolled back; surface as suppression.
        logger.debug(
            "bucket_a queue insert suppressed for tx %s (%s): %s",
            flag.transaction_id, flag.detector, exc,
        )
        return False


__all__ = [
    "A1_MIN_ABS_AMOUNT_NIS",
    "A1_MIN_BASELINE_COUNT",
    "A1_Z_THRESHOLD",
    "A2_CRITICAL_MULTIPLE",
    "A2_MIN_BASELINE_COUNT",
    "A2_MIN_MEAN_NIS",
    "A2_MIN_MULTIPLE",
    "AmountOutlierFlag",
    "detect_bucket_a",
]
