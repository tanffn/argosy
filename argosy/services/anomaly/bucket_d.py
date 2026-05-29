"""Bucket D anomaly detector (cross-card duplicate / fraud) — sprint #2 commit #9.

Single pattern per spec
``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§1.4:

  * **D1 — Cross-card duplicate.** Fire when two transactions exist
    within a ``window_days`` (default 7-day) window with the same
    ``merchant_normalized`` AND ``amount_nis`` (±``amount_tolerance_nis``,
    default ₪0.50 to absorb FX/rounding) AND from DIFFERENT cards
    (different ``ExpenseSource``). Excludes:

      - Same-source transactions (legit duplicates on the same card).
      - ``is_card_payment=TRUE`` rows (statement-to-statement
        movements, not real spend).

    Severity: ``warning`` by default; ``critical`` if either
    transaction's ``amount_nis >= ₪1000``.

Contracts:

  * No new tables — reads only ``expense_transactions``.
  * Writes ``ExpenseReviewQueue`` rows with deterministic
    ``dedup_key`` per spec §4. The partial unique index on
    ``(user_id, dedup_key) WHERE dedup_key IS NOT NULL AND status='open'``
    keeps the detector idempotent across reruns.
  * Uses SAVEPOINT-per-row insert pattern (cribbed from ``bucket_a``)
    so a unique-violation on one row never poisons the rest of the
    batch.
  * Detection window: scans the last 30 days of transactions for the
    "primary" leg. Each primary leg is then paired against any other
    txn within ±``window_days`` to find cross-card matches.

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
)

logger = logging.getLogger(__name__)


# Tunables — surfaced as module-level constants so future rule tweaks
# bump the ``v1`` dedup prefix consciously.
DEFAULT_WINDOW_DAYS = 7
DEFAULT_AMOUNT_TOLERANCE_NIS = Decimal("0.50")
CRITICAL_AMOUNT_THRESHOLD_NIS = Decimal("1000")
DETECTION_LOOKBACK_DAYS = 30


# ---------------------------------------------------------------------------
# Public types.
# ---------------------------------------------------------------------------


DetectorKind = Literal["d1_cross_card_duplicate"]
Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class CrossCardDuplicateFlag:
    """One D-bucket anomaly. Pairs (min_tx_id, max_tx_id) on different cards."""

    user_id: str
    min_tx_id: int
    max_tx_id: int
    merchant_normalized: str
    amount_nis: float
    detector: DetectorKind
    severity: Severity
    rationale: str
    dedup_key: str


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def detect_cross_card_duplicates(
    session: Session,
    user_id: str,
    *,
    as_of: date | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    amount_tolerance_nis: Decimal | float = DEFAULT_AMOUNT_TOLERANCE_NIS,
) -> list[CrossCardDuplicateFlag]:
    """Find transaction pairs within ``window_days`` at the same merchant
    + amount on DIFFERENT cards (different ``source_id``).

    Excludes:
      * Same-card pairs (legit duplicate purchases on one card).
      * ``is_card_payment=TRUE`` rows (statement closeouts, not spend).
      * Transactions with NULL ``amount_nis`` (no comparison possible).

    Args:
      session: SQLAlchemy session (sync). Caller commits.
      user_id: user.id to detect duplicates for.
      as_of: window end (default: today). The scan covers
        [as_of - 30, as_of]; pair candidates outside this window are
        only considered if at least one leg lies inside it.
      window_days: pair window in days (default 7).
      amount_tolerance_nis: ± tolerance on ``amount_nis`` equality
        (default ₪0.50 to absorb FX rounding).

    Returns:
      Flags fired AND for which a queue row was successfully written.
      Each pair is reported exactly once (canonicalised on
      (min_tx_id, max_tx_id)).
    """
    if as_of is None:
        as_of = date.today()
    if not isinstance(amount_tolerance_nis, Decimal):
        amount_tolerance_nis = Decimal(str(amount_tolerance_nis))

    detection_start = as_of - timedelta(days=DETECTION_LOOKBACK_DAYS)
    # Pair-window expansion: a tx on day X can pair with one on day
    # X ± window_days. The outer leg can lie just outside the
    # detection window if the inner leg is inside it, so pull a
    # slightly-wider snapshot.
    pair_buffer_start = detection_start - timedelta(days=window_days)
    pair_buffer_end = as_of + timedelta(days=window_days)

    txns = session.execute(
        sa_select(ExpenseTransaction)
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.occurred_on >= pair_buffer_start)
        .where(ExpenseTransaction.occurred_on <= pair_buffer_end)
        .where(ExpenseTransaction.is_card_payment.is_(False))
        .where(ExpenseTransaction.amount_nis.is_not(None))
        .order_by(
            ExpenseTransaction.merchant_normalized,
            ExpenseTransaction.occurred_on,
            ExpenseTransaction.id,
        )
    ).scalars().all()

    if not txns:
        return []

    # Bucket by merchant so the O(n^2) pair scan stays O(k^2) per
    # merchant (k typically << n).
    by_merchant: dict[str, list[ExpenseTransaction]] = {}
    for tx in txns:
        by_merchant.setdefault(tx.merchant_normalized, []).append(tx)

    fired: list[CrossCardDuplicateFlag] = []
    seen_pairs: set[tuple[int, int]] = set()

    for merchant, rows in by_merchant.items():
        # Pair scan within merchant group.
        for i, left in enumerate(rows):
            for right in rows[i + 1:]:
                # Pair filter — same card or same id excluded.
                if left.source_id == right.source_id:
                    continue
                # Date window.
                delta_days = abs(
                    (right.occurred_on - left.occurred_on).days
                )
                if delta_days > window_days:
                    # rows are sorted by date — once we exceed the
                    # window, all further right legs are also outside.
                    break
                # Amount tolerance.
                if left.amount_nis is None or right.amount_nis is None:
                    continue
                left_amt = abs(Decimal(left.amount_nis))
                right_amt = abs(Decimal(right.amount_nis))
                if abs(left_amt - right_amt) > amount_tolerance_nis:
                    continue
                # At least one leg must lie inside the detection window
                # so re-running the detector daily doesn't re-fire for
                # ancient pairs every time.
                if not (
                    detection_start <= left.occurred_on <= as_of
                    or detection_start <= right.occurred_on <= as_of
                ):
                    continue

                min_id = min(left.id, right.id)
                max_id = max(left.id, right.id)
                pair = (min_id, max_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                # Severity ladder — critical if EITHER leg is large.
                max_amount = max(left_amt, right_amt)
                severity: Severity = (
                    "critical"
                    if max_amount >= CRITICAL_AMOUNT_THRESHOLD_NIS
                    else "warning"
                )
                dedup_key = (
                    f"v1|d1|u:{user_id}|pair:{min_id}-{max_id}"
                )
                rationale = (
                    f"Possible cross-card duplicate at '{merchant}': "
                    f"tx#{min_id} (NIS {left_amt if left.id == min_id else right_amt}) "
                    f"and tx#{max_id} (NIS {right_amt if right.id == max_id else left_amt}) "
                    f"within {delta_days}d on different cards."
                )
                flag = CrossCardDuplicateFlag(
                    user_id=user_id,
                    min_tx_id=min_id,
                    max_tx_id=max_id,
                    merchant_normalized=merchant,
                    amount_nis=float(max_amount),
                    detector="d1_cross_card_duplicate",
                    severity=severity,
                    rationale=rationale,
                    dedup_key=dedup_key,
                )
                if _persist_flag(session, flag):
                    fired.append(flag)

    return fired


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------


def _persist_flag(
    session: Session,
    flag: CrossCardDuplicateFlag,
) -> bool:
    """SAVEPOINT-per-row insert. Returns True iff a new row was written.

    Mirrors ``bucket_a._persist_flag``: the partial unique index
    ``ix_expense_review_queue_dedup`` catches duplicate keys; SAVEPOINT
    keeps a single failed insert from poisoning the batch transaction.
    """
    payload = {
        "detector": flag.detector,
        "severity": flag.severity,
        "rationale": flag.rationale,
        "min_tx_id": flag.min_tx_id,
        "max_tx_id": flag.max_tx_id,
        "merchant_normalized": flag.merchant_normalized,
        "amount_nis": flag.amount_nis,
    }
    row = ExpenseReviewQueue(
        user_id=flag.user_id,
        kind=flag.detector,
        status="open",
        payload_json=json.dumps(payload),
        # Anchor the queue row to the EARLIER transaction in the pair —
        # the "later" leg can be located by the UI from payload.max_tx_id.
        related_tx_id=flag.min_tx_id,
        bucket="duplicate",
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
            "bucket_d queue insert suppressed for pair (%s, %s): %s",
            flag.min_tx_id, flag.max_tx_id, exc,
        )
        return False


__all__ = [
    "CRITICAL_AMOUNT_THRESHOLD_NIS",
    "CrossCardDuplicateFlag",
    "DEFAULT_AMOUNT_TOLERANCE_NIS",
    "DEFAULT_WINDOW_DAYS",
    "detect_cross_card_duplicates",
]
