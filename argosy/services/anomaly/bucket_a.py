"""Bucket A anomaly detectors (amount outliers) — sprint #2 commit #5.

Two patterns per spec
``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§1.1:

  * **A1 — Category robust outlier.** For each new transaction in
    category C, compute a robust z-score
    ``r = (amount - center_C) / (1.4826 * spread_C)`` against a
    category baseline. The baseline uses **trimmed-mean + MAD over raw
    transactions** in the trailing window, with **sample-size shrinkage**
    toward a global cross-category baseline so small per-category
    samples don't yield noisy thresholds.

    Fire when ``r >= 3`` AND ``abs(amount) >= 200`` AND prior baseline
    txn_count in the category is >= 6. Severity bands: 3-4 -> info,
    4-6 -> warning, >=6 -> critical.

    Two baseline-computation paths (in priority order):
      1. **Raw transactions** in the baseline window. Preferred path —
         gives true category-level trimmed-mean + MAD. Used when the
         category has >= 6 raw transactions in window.
      2. **MerchantRollingStats fallback** (the v1 median-of-medians
         proxy). Used when raw history is absent (test scenarios,
         first-run before recompute). Documented limitation: aggregates
         per-merchant medians, so heteroscedasticity within a category
         is invisible to A1; merchant-level variance is handled by A2
         + merchant_rolling_stats.

  * **A2 — Merchant spike.** For each new transaction at merchant M,
    compare ``amount_nis`` to the trailing-window mean for M. Fire
    when ``amount >= 3 * mean_M`` AND ``mean_M >= 50`` AND prior
    occurrence count at M is >= 3. Severity: warning by default,
    critical if ``amount >= 5 * mean_M``.

Both patterns share these contracts:

  * Window: by default the detector scans the last 30 days of
    transactions and compares against the latest ``window_end`` per
    (merchant, category) in ``merchant_rolling_stats``.
  * A1 prefers RAW transactions over the rolling-stats aggregation
    (weighted-baselines follow-on, 2026-05-30). A2 still consumes
    rolling-stats baselines verbatim — merchant-level spike detection
    is what rolling-stats was designed for.
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
from dataclasses import dataclass
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

# Weighted-baselines follow-on tunables (2026-05-30).
# Trimmed-mean trim fraction per tail: 10% so we discard the top/bottom
# decile before averaging — robust to single-tail outliers while keeping
# the central mass intact.
A1_TRIM_FRACTION = Decimal("0.10")
# Sample-size shrinkage cap: a category with >= 30 raw observations gets
# weight=1.0 (pure per-category baseline). Below 30, the baseline blends
# linearly with the global cross-category baseline. At n=15, weight=0.5
# (per codex IMPORTANT — empirical-Bayes shrinkage; standard pattern).
A1_SHRINKAGE_SAMPLE_FLOOR = 30

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

    A1 (category outlier) prefers raw transactions over rolling-stats
    aggregates; A2 (merchant spike) consumes the ``merchant_rolling_stats``
    baseline directly. Baseline matches use ``window_end == as_of`` first;
    treated as "the most-recent recompute available for this (merchant,
    category)" when no exact window_end match exists. Returns the new
    flags fired AND writes ``ExpenseReviewQueue`` rows with ``dedup_key``
    per spec §4.

    Args:
      session: SQLAlchemy session (sync). The caller commits.
      user_id: user.id to detect anomalies for.
      as_of: window end (default: today). The detection sweep covers
        transactions in [as_of - 30, as_of].
      lookback_days_for_baseline: matches the ``window_days`` passed to
        ``recompute_merchant_stats``; only baseline rows whose
        ``window_end >= as_of - lookback_days_for_baseline`` are
        considered (avoids matching against very stale baselines). Also
        bounds the raw-transaction lookback used by the A1 weighted
        baselines path.

    Returns:
      List of flags that fired AND for which a queue row was successfully
      written. Duplicates suppressed by the partial unique index are
      NOT included (per spec §1.5 idempotency contract).
    """
    if as_of is None:
        as_of = date.today()
    detection_start = as_of - timedelta(days=DETECTION_LOOKBACK_DAYS)
    baseline_start = as_of - timedelta(days=lookback_days_for_baseline)

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
    detection_tx_ids = {tx.id for tx in tx_rows}

    # Pull all baselines for this user that are still fresh enough to
    # be considered. Used directly by A2 and as the A1 fallback when
    # raw category history is absent.
    baselines = session.execute(
        sa_select(MerchantRollingStats)
        .where(MerchantRollingStats.user_id == user_id)
        .where(MerchantRollingStats.window_end >= baseline_start)
        .where(MerchantRollingStats.window_end <= as_of)
    ).scalars().all()

    merchant_baseline_by_key = _index_merchant_baselines(baselines)
    fallback_category_baselines = _aggregate_category_baselines(baselines)

    # Weighted-baselines follow-on (2026-05-30): compute per-category
    # trimmed-mean + MAD baselines from raw transactions in the baseline
    # window. Excludes the detection-window candidates themselves so a
    # newly-arrived outlier doesn't pollute its own baseline.
    raw_category_baselines, global_baseline = _compute_raw_category_baselines(
        session,
        user_id=user_id,
        baseline_start=baseline_start,
        as_of=as_of,
        exclude_tx_ids=detection_tx_ids,
    )

    fired: list[AmountOutlierFlag] = []

    for tx in tx_rows:
        flag_a1 = _evaluate_a1(
            tx,
            raw_category_baselines=raw_category_baselines,
            global_baseline=global_baseline,
            fallback_category_baselines=fallback_category_baselines,
            as_of=as_of,
        )
        if flag_a1 is not None and _persist_flag(session, flag_a1, tx):
            fired.append(flag_a1)

        flag_a2 = _evaluate_a2(tx, merchant_baseline_by_key, as_of=as_of)
        if flag_a2 is not None and _persist_flag(session, flag_a2, tx):
            fired.append(flag_a2)

    return fired


# ---------------------------------------------------------------------------
# A1 — category robust outlier (weighted-baselines, raw-transaction path).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawCategoryBaseline:
    """Per-category baseline computed from RAW transactions.

    ``center_nis`` is the trimmed mean (symmetric 10% trim per tail) of
    raw transaction amounts in the baseline window. ``spread_nis`` is
    MAD around that center (NOT median; MAD-around-trimmed-mean is
    slightly less classical than MAD-around-median but matches our
    center-of-mass choice and stays robust to outliers in the spread
    estimator as well as the center).

    ``txn_count`` is the count after we drop NULL amounts and rows in
    the detection window; that is the gate value for ``A1_MIN_BASELINE_COUNT``
    and feeds into the shrinkage weight.
    """

    category_id: int | None
    txn_count: int
    center_nis: Decimal
    spread_nis: Decimal


@dataclass(frozen=True)
class _CategoryBaseline:
    """Fallback aggregated baseline at category level. Combines all
    ``merchant_rolling_stats`` rows sharing the same category. The
    aggregation uses the freshest available window_end per merchant so
    a category-level signal is not dominated by stale rows.

    Retained as the A1 fallback path when raw transactions in the
    baseline window are insufficient (e.g. first-run, or test scenarios
    that seed rolling stats but no raw history). Production callers
    should run ``recompute_merchant_stats`` once raw history accumulates;
    the raw path will then take over.
    """

    category_id: int | None
    txn_count: int
    median_nis: Decimal
    mad_nis: Decimal | None
    window_end: date  # the freshest window_end across the contributing rows.


def _compute_raw_category_baselines(
    session: Session,
    *,
    user_id: str,
    baseline_start: date,
    as_of: date,
    exclude_tx_ids: set[int],
) -> tuple[dict[int | None, _RawCategoryBaseline], _RawCategoryBaseline | None]:
    """Compute per-category trimmed-mean + MAD baselines from raw txns.

    Excludes ``exclude_tx_ids`` (the detection-window candidates) so a
    newly-arrived outlier doesn't poison its own baseline.

    Returns (per_category_map, global_baseline).
      - per_category_map: category_id -> _RawCategoryBaseline. Only
        categories with >= 2 retained observations get a row (MAD
        requires >= 2).
      - global_baseline: _RawCategoryBaseline aggregated across ALL
        categories. Used as the shrinkage target for low-sample
        per-category baselines. None if total observation count < 2.
    """
    rows = session.execute(
        sa_select(
            ExpenseTransaction.id,
            ExpenseTransaction.category_id,
            ExpenseTransaction.amount_nis,
        )
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.occurred_on >= baseline_start)
        .where(ExpenseTransaction.occurred_on <= as_of)
        .where(ExpenseTransaction.amount_nis.is_not(None))
    ).all()

    by_cat: dict[int | None, list[Decimal]] = {}
    all_amounts: list[Decimal] = []
    for tx_id, category_id, amount_nis in rows:
        if tx_id in exclude_tx_ids:
            continue
        # Absolute magnitude: anomaly cares about size, not sign.
        amt = abs(Decimal(amount_nis))
        by_cat.setdefault(category_id, []).append(amt)
        all_amounts.append(amt)

    per_category: dict[int | None, _RawCategoryBaseline] = {}
    for cat_id, amounts in by_cat.items():
        baseline = _trimmed_baseline(cat_id, amounts)
        if baseline is not None:
            per_category[cat_id] = baseline

    global_baseline = _trimmed_baseline(None, all_amounts)
    return per_category, global_baseline


def _trimmed_baseline(
    category_id: int | None,
    amounts: list[Decimal],
) -> _RawCategoryBaseline | None:
    """Trimmed-mean + MAD from a raw amount list. None if n < 2.

    Trim fraction is symmetric (``A1_TRIM_FRACTION`` per tail). For
    small samples where the trim would leave fewer than 2 values, we
    fall back to the un-trimmed mean. MAD is computed AROUND the
    trimmed mean (not the median) to keep center + spread aligned.
    """
    n = len(amounts)
    if n < 2:
        return None
    sorted_amounts = sorted(amounts)
    # Symmetric trim. For n=10 and trim=0.10 -> drop 1 from each tail.
    # int(...) truncates toward zero — exactly what we want.
    trim_count = int(n * A1_TRIM_FRACTION)
    if 2 * trim_count >= n:
        # Trim would leave nothing; fall back to no-trim.
        trim_count = 0
    trimmed = sorted_amounts[trim_count: n - trim_count] if trim_count > 0 else sorted_amounts
    if len(trimmed) < 2:
        trimmed = sorted_amounts

    # Trimmed mean — Decimal arithmetic.
    center = sum(trimmed, Decimal(0)) / Decimal(len(trimmed))
    # MAD around the trimmed mean.
    deviations = sorted(abs(a - center) for a in trimmed)
    m = len(deviations)
    if m % 2 == 1:
        spread = deviations[m // 2]
    else:
        spread = (deviations[m // 2 - 1] + deviations[m // 2]) / Decimal(2)

    return _RawCategoryBaseline(
        category_id=category_id,
        txn_count=n,  # report ALL observations as the gate count, not just retained-after-trim.
        center_nis=center,
        spread_nis=spread,
    )


def _aggregate_category_baselines(
    baselines: list[MerchantRollingStats],
) -> dict[int | None, _CategoryBaseline]:
    """Aggregate per-merchant baselines up to category level (FALLBACK PATH).

    Used by A1 when no raw-transaction history exists for the category.
    The aggregation pools per-merchant medians as observations and takes
    the median/MAD of those — coarser than the raw-transaction path but
    a usable baseline when only rolling-stats are seeded (test fixtures,
    first-run).

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


def _shrink_baseline(
    raw: _RawCategoryBaseline,
    global_baseline: _RawCategoryBaseline | None,
) -> tuple[Decimal, Decimal]:
    """Empirical-Bayes shrinkage of (center, spread) toward the global
    cross-category baseline.

    Weight = min(1, n / A1_SHRINKAGE_SAMPLE_FLOOR). At n >= 30 we trust
    the per-category baseline fully (weight=1.0). At n=15 the per-category
    baseline carries 50% weight; the rest pulls toward the global
    baseline. This is the codex-IMPORTANT shrinkage pattern — small
    per-category samples should not yield wildly tight thresholds just
    because the category happens to have low variance by chance.

    Returns (effective_center, effective_spread). When global_baseline
    is None (e.g. solo category, no global data), returns the raw
    per-category values unchanged.
    """
    if global_baseline is None:
        return raw.center_nis, raw.spread_nis
    n = raw.txn_count
    # Weight uses txn_count (all observations), not just the retained-
    # after-trim count, because the trim is a robustness device — the
    # underlying sample size is what governs how much we trust the
    # category-specific estimates.
    weight_num = min(n, A1_SHRINKAGE_SAMPLE_FLOOR)
    weight = Decimal(weight_num) / Decimal(A1_SHRINKAGE_SAMPLE_FLOOR)
    one_minus_w = Decimal(1) - weight
    center = weight * raw.center_nis + one_minus_w * global_baseline.center_nis
    spread = weight * raw.spread_nis + one_minus_w * global_baseline.spread_nis
    return center, spread


def _evaluate_a1(
    tx: ExpenseTransaction,
    *,
    raw_category_baselines: dict[int | None, _RawCategoryBaseline],
    global_baseline: _RawCategoryBaseline | None,
    fallback_category_baselines: dict[int | None, _CategoryBaseline],
    as_of: date,
) -> AmountOutlierFlag | None:
    """Pattern A1 — category robust outlier with weighted baselines.

    Priority:
      1. Raw-transaction baseline if available AND has >= A1_MIN_BASELINE_COUNT
         observations.
      2. Fallback to merchant_rolling_stats aggregate (the v1 proxy) when
         raw history is absent — preserves backwards-compat with seeds
         that only populate rolling-stats.

    None if any gate fails (below abs-amount floor, baseline too small,
    spread is zero, robust z below threshold).
    """
    if tx.amount_nis is None:
        return None
    raw_amount = Decimal(tx.amount_nis)
    amount = abs(raw_amount)
    # ₪200 absolute-amount gate — avoid noise on micro-transactions.
    if amount < A1_MIN_ABS_AMOUNT_NIS:
        return None

    center, spread, sample_count, baseline_source = _select_a1_baseline(
        tx.category_id,
        raw_category_baselines=raw_category_baselines,
        global_baseline=global_baseline,
        fallback_category_baselines=fallback_category_baselines,
    )
    if center is None or spread is None:
        return None
    # Baseline-sufficiency gate (>=6 prior txns in the category).
    if sample_count < A1_MIN_BASELINE_COUNT:
        return None
    if spread <= 0:
        return None

    # Robust z-score: r = (amount - center_C) / (1.4826 * spread_C).
    scaled_spread = A1_MAD_SCALE * spread
    if scaled_spread == 0:
        return None
    robust_z = (raw_amount - center) / scaled_spread

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
        f"the category center NIS {center:.2f} (spread "
        f"NIS {spread:.2f}, n={sample_count}, src={baseline_source})."
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


def _select_a1_baseline(
    category_id: int | None,
    *,
    raw_category_baselines: dict[int | None, _RawCategoryBaseline],
    global_baseline: _RawCategoryBaseline | None,
    fallback_category_baselines: dict[int | None, _CategoryBaseline],
) -> tuple[Decimal | None, Decimal | None, int, str]:
    """Pick a baseline and apply shrinkage if applicable.

    Returns (center, spread, sample_count, source_label) where
    source_label is one of:
      - "raw"      raw-transaction baseline with shrinkage applied.
      - "rolling"  fallback from merchant_rolling_stats aggregation.
      - "none"     no usable baseline.

    Behavior (codex BLOCKER, 2026-05-30 review): once ANY raw history
    exists for this category, the raw path wins — sparse raw must NOT
    silently fall back to a stale rolling-stats aggregate. The
    A1_MIN_BASELINE_COUNT count gate is applied inside ``_evaluate_a1``
    against whichever baseline we return here; that gate is the right
    place to suppress firing on insufficient samples, not here.

    Rolling-stats fallback only kicks in when raw history is COMPLETELY
    absent for the category (e.g. first-run before any raw txns
    accumulate, or test scenarios that only seed rolling-stats).
    """
    raw = raw_category_baselines.get(category_id)
    if raw is not None:
        # Raw path wins as soon as ANY raw history exists. If the count
        # is below the gate, _evaluate_a1 will suppress the fire — but
        # we don't fall through to a stale proxy.
        center, spread = _shrink_baseline(raw, global_baseline)
        return center, spread, raw.txn_count, "raw"

    fallback = fallback_category_baselines.get(category_id)
    if fallback is not None and fallback.txn_count >= A1_MIN_BASELINE_COUNT \
            and fallback.mad_nis is not None and fallback.mad_nis > 0:
        return fallback.median_nis, fallback.mad_nis, fallback.txn_count, "rolling"

    return None, None, 0, "none"


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
    "A1_SHRINKAGE_SAMPLE_FLOOR",
    "A1_TRIM_FRACTION",
    "A1_Z_THRESHOLD",
    "A2_CRITICAL_MULTIPLE",
    "A2_MIN_BASELINE_COUNT",
    "A2_MIN_MEAN_NIS",
    "A2_MIN_MULTIPLE",
    "AmountOutlierFlag",
    "detect_bucket_a",
]
