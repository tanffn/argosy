"""Bucket C anomaly detectors (merchant-cache anomalies) — sprint #2 commit #8.

Two patterns per spec
``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§1.3:

  * **C1 — Novel merchant.** Fire when a transaction's
    ``merchant_normalized`` has no prior occurrence in
    ``expense_transactions`` for this user (excluding the transaction
    itself). Rate-limited to users with >=100 historical transactions
    so early-adopter accounts (where every merchant is novel) don't
    spam the queue. Severity: ``info`` — informational, not actionable.

  * **C2 — Category drift.** Fire when a ``MerchantCategoryCache``
    rule's ``last_hit_at`` is older than ``stale_days`` (default 180)
    AND a recent transaction at the same merchant uses a different
    category than the cache rule. Severity: ``warning`` — pairs with
    an inline "click to confirm new category" action (UI). One row
    per (merchant, observation_month).

Both patterns share these contracts:

  * No new tables. C1 reads only ``expense_transactions``; C2 reads
    ``MerchantCategoryCache`` + ``expense_transactions``.
  * Writes ``ExpenseReviewQueue`` rows with deterministic
    ``dedup_key`` per spec §4. The partial unique index on
    ``(user_id, dedup_key) WHERE dedup_key IS NOT NULL AND status='open'``
    keeps the detectors idempotent across reruns.
  * Uses SAVEPOINT-per-row insert pattern (cribbed from ``bucket_a``)
    so a unique-violation on one row never poisons the rest of the
    batch.
  * Detection window: by default the detector scans the last 30 days
    of transactions when looking for "recent" txns. The C1 novelty
    check itself is global (no window — "first-seen ever").

NO LLM — pure SQL + arithmetic.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select as sa_select
from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseReviewQueue,
    ExpenseTransaction,
    MerchantCategoryCache,
)

logger = logging.getLogger(__name__)


# Tunables — surfaced as module-level constants so future rule tweaks
# bump the ``v1`` dedup prefix consciously.
C1_MIN_HISTORICAL_TXNS = 100
C2_STALE_DAYS_DEFAULT = 180
DETECTION_LOOKBACK_DAYS = 30


# ---------------------------------------------------------------------------
# Public types.
# ---------------------------------------------------------------------------


DetectorKind = Literal["c1_novel_merchant", "c2_category_drift"]
Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class MerchantCacheFlag:
    """One C-bucket anomaly. kind='c1_novel_merchant' or 'c2_category_drift'."""

    transaction_id: int
    user_id: str
    merchant_normalized: str
    category_id: int | None
    detector: DetectorKind
    severity: Severity
    rationale: str  # one-line for the queue row.
    dedup_key: str


# ---------------------------------------------------------------------------
# C1 — novel merchant.
# ---------------------------------------------------------------------------


def detect_novel_merchants(
    session: Session,
    user_id: str,
    *,
    as_of: date | None = None,
) -> list[MerchantCacheFlag]:
    """Find transactions whose ``merchant_normalized`` never appeared
    before for this user.

    Rate-limit: only fire when the user has at least
    ``C1_MIN_HISTORICAL_TXNS`` (=100) historical transactions. Below
    that threshold the entire merchant universe is "novel" and
    surfacing every one is noise.

    Scan window for "new" transactions is the last
    ``DETECTION_LOOKBACK_DAYS`` (=30) days ending at ``as_of``. The
    novelty check itself is global: the merchant must have no prior
    occurrence anywhere in the user's history (excluding the
    transaction itself).

    Args:
      session: SQLAlchemy session (sync). Caller commits.
      user_id: user.id to detect novel merchants for.
      as_of: window end (default: today). Scan covers
        [as_of - 30, as_of].

    Returns:
      Flags that fired AND for which a queue row was successfully
      written. Duplicates suppressed by the partial unique index are
      NOT included (per spec §1.5 idempotency contract).
    """
    if as_of is None:
        as_of = date.today()
    detection_start = as_of - timedelta(days=DETECTION_LOOKBACK_DAYS)

    # Rate-limit gate: skip entirely if user has < 100 historical txns.
    total_txns = session.execute(
        sa_select(func.count(ExpenseTransaction.id))
        .where(ExpenseTransaction.user_id == user_id)
    ).scalar_one()
    if (total_txns or 0) < C1_MIN_HISTORICAL_TXNS:
        return []

    # Candidate transactions in the detection window.
    candidates = session.execute(
        sa_select(ExpenseTransaction)
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.occurred_on >= detection_start)
        .where(ExpenseTransaction.occurred_on <= as_of)
        .order_by(ExpenseTransaction.occurred_on, ExpenseTransaction.id)
    ).scalars().all()

    if not candidates:
        return []

    # Build a "first seen tx id per merchant" map by scanning history
    # once. The first-seen txn is the one whose merchant is "novel"; any
    # subsequent occurrence isn't novel anymore.
    first_seen: dict[str, int] = {}
    all_history = session.execute(
        sa_select(
            ExpenseTransaction.id,
            ExpenseTransaction.merchant_normalized,
        )
        .where(ExpenseTransaction.user_id == user_id)
        .order_by(ExpenseTransaction.occurred_on, ExpenseTransaction.id)
    ).all()
    for tx_id, merchant in all_history:
        if merchant not in first_seen:
            first_seen[merchant] = tx_id

    fired: list[MerchantCacheFlag] = []
    for tx in candidates:
        if first_seen.get(tx.merchant_normalized) != tx.id:
            # Either there was an earlier occurrence (not novel) or the
            # candidate itself has earlier siblings in history.
            continue
        flag = _build_c1_flag(tx)
        if _persist_flag(session, flag):
            fired.append(flag)

    return fired


def _build_c1_flag(tx: ExpenseTransaction) -> MerchantCacheFlag:
    dedup_key = (
        f"v1|c1|u:{tx.user_id}|m:{tx.merchant_normalized}|first_tx:{tx.id}"
    )
    rationale = (
        f"First-seen merchant '{tx.merchant_normalized}' "
        f"(no prior occurrence in your history)."
    )
    return MerchantCacheFlag(
        transaction_id=tx.id,
        user_id=tx.user_id,
        merchant_normalized=tx.merchant_normalized,
        category_id=tx.category_id,
        detector="c1_novel_merchant",
        severity="info",
        rationale=rationale,
        dedup_key=dedup_key,
    )


# ---------------------------------------------------------------------------
# C2 — category drift.
# ---------------------------------------------------------------------------


def detect_category_drift(
    session: Session,
    user_id: str,
    *,
    as_of: date | None = None,
    stale_days: int = C2_STALE_DAYS_DEFAULT,
) -> list[MerchantCacheFlag]:
    """Find merchants whose ``MerchantCategoryCache`` rule is stale
    AND recent transactions use a DIFFERENT category than the cache.

    A merchant matches when:
      * its cache row's ``last_hit_at`` is older than
        ``stale_days`` (default 180) before ``as_of``; AND
      * there exists at least one transaction in the last
        ``DETECTION_LOOKBACK_DAYS`` (=30) days at that merchant whose
        ``category_id`` differs from the cache's ``category_id``.

    One flag per (merchant, observation_month). The "observed" tx
    used as the queue row's ``related_tx_id`` is the most recent
    drifting transaction in that month at that merchant.

    Args:
      session: SQLAlchemy session (sync). Caller commits.
      user_id: user.id to detect drift for.
      as_of: window end (default: today).
      stale_days: how old the cache rule must be to qualify as stale
        (default 180).

    Returns:
      Flags fired AND for which a queue row was successfully written.
    """
    if as_of is None:
        as_of = date.today()
    detection_start = as_of - timedelta(days=DETECTION_LOOKBACK_DAYS)
    # last_hit_at is a timezone-aware datetime — convert the stale
    # cutoff to a datetime at midnight UTC so the comparison is sound
    # regardless of the stored TZ offset.
    stale_cutoff = datetime.combine(
        as_of - timedelta(days=stale_days), datetime.min.time(),
    ).replace(tzinfo=timezone.utc)

    # Pull stale cache rows.
    stale_cache_rows = session.execute(
        sa_select(MerchantCategoryCache)
        .where(MerchantCategoryCache.user_id == user_id)
        .where(MerchantCategoryCache.last_hit_at.is_not(None))
        .where(MerchantCategoryCache.last_hit_at < stale_cutoff)
    ).scalars().all()

    if not stale_cache_rows:
        return []

    fired: list[MerchantCacheFlag] = []
    for cache in stale_cache_rows:
        # Drift candidates: recent txns at this merchant whose category
        # differs from the cache's. We match on merchant_normalized ==
        # cache.merchant_pattern (regex is_regex=True is out of scope —
        # the cache supports both shapes; for drift detection we treat
        # the pattern as a literal merchant key when is_regex is False
        # and skip regex rows for now).
        if cache.is_regex:
            continue

        drifting = session.execute(
            sa_select(ExpenseTransaction)
            .where(ExpenseTransaction.user_id == user_id)
            .where(
                ExpenseTransaction.merchant_normalized == cache.merchant_pattern
            )
            .where(ExpenseTransaction.occurred_on >= detection_start)
            .where(ExpenseTransaction.occurred_on <= as_of)
            .where(ExpenseTransaction.category_id.is_not(None))
            .where(ExpenseTransaction.category_id != cache.category_id)
            .order_by(
                ExpenseTransaction.occurred_on.desc(),
                ExpenseTransaction.id.desc(),
            )
        ).scalars().all()

        # Group drifting txns by observation_month (yyyy-mm); one flag
        # per (merchant, month). Use the most recent tx within each
        # month as the related_tx_id.
        seen_months: set[str] = set()
        for tx in drifting:
            obs_month = tx.occurred_on.strftime("%Y-%m")
            if obs_month in seen_months:
                continue
            seen_months.add(obs_month)

            flag = _build_c2_flag(tx, cache, obs_month=obs_month)
            if _persist_flag(session, flag):
                fired.append(flag)

    return fired


def _build_c2_flag(
    tx: ExpenseTransaction,
    cache: MerchantCategoryCache,
    *,
    obs_month: str,
) -> MerchantCacheFlag:
    dedup_key = (
        f"v1|c2|u:{tx.user_id}|m:{tx.merchant_normalized}"
        f"|cache_cat:{cache.category_id}|obs_month:{obs_month}"
    )
    rationale = (
        f"Cache rule for '{tx.merchant_normalized}' (cat #{cache.category_id}) "
        f"hasn't been confirmed since "
        f"{cache.last_hit_at.date().isoformat() if cache.last_hit_at else '?'}; "
        f"recent transaction in {obs_month} uses cat #{tx.category_id} instead."
    )
    return MerchantCacheFlag(
        transaction_id=tx.id,
        user_id=tx.user_id,
        merchant_normalized=tx.merchant_normalized,
        category_id=tx.category_id,
        detector="c2_category_drift",
        severity="warning",
        rationale=rationale,
        dedup_key=dedup_key,
    )


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------


def _persist_flag(
    session: Session,
    flag: MerchantCacheFlag,
) -> bool:
    """SAVEPOINT-per-row insert. Returns True iff a new row was written.

    The partial unique index ``ix_expense_review_queue_dedup`` on
    ``(user_id, dedup_key) WHERE dedup_key IS NOT NULL AND status='open'``
    catches duplicates from re-runs. SAVEPOINT isolates per-row
    failures so one suppressed insert doesn't poison the batch.
    """
    payload = {
        "detector": flag.detector,
        "severity": flag.severity,
        "rationale": flag.rationale,
        "transaction_id": flag.transaction_id,
        "merchant_normalized": flag.merchant_normalized,
        "category_id": flag.category_id,
    }
    row = ExpenseReviewQueue(
        user_id=flag.user_id,
        kind=flag.detector,
        status="open",
        payload_json=json.dumps(payload),
        related_tx_id=flag.transaction_id,
        bucket="cache",
        materiality=flag.severity,
        dedup_key=flag.dedup_key,
    )
    try:
        with session.begin_nested():  # SAVEPOINT.
            session.add(row)
            session.flush()
        return True
    except Exception as exc:  # pragma: no cover — exercised by idempotency tests.
        logger.debug(
            "bucket_c queue insert suppressed for tx %s (%s): %s",
            flag.transaction_id, flag.detector, exc,
        )
        return False


__all__ = [
    "C1_MIN_HISTORICAL_TXNS",
    "C2_STALE_DAYS_DEFAULT",
    "MerchantCacheFlag",
    "detect_category_drift",
    "detect_novel_merchants",
]
