"""Bucket B Pattern B2 — recurring-charge learner + missing detector
(sprint #2 commit #7).

Backs spec ``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§1.2 Pattern B2.

Two surfaces:

  1. ``learn_recurring_patterns`` — scan ``expense_transactions`` for repeating
     (merchant, amount, cadence) triples and UPSERT into
     ``recurring_charge_patterns``. A pattern is established when:
       * ≥ ``min_occurrences`` (default 3) transactions
       * at the same normalized merchant
       * with amounts within ±``amount_tolerance`` (default 0.15 = ±15%)
         of the median amount
       * with median inter-transaction gap in
         [``cadence_min_days``, ``cadence_max_days``] (default 28–32d).
     Median (not mean) for cadence robustness against one-off missed
     months.

  2. ``detect_missing_recurring`` — for each ``active`` pattern, fire when
     ``last_seen + cadence_days + grace_days`` has elapsed without a fresh
     matching transaction. Writes ``ExpenseReviewQueue`` rows with
     ``bucket='recurring'`` + dedup_key per spec §4.

Severity ladder for missing-recurring:
  * Default: ``warning`` (subscription cancellation is usually intentional
    but unreported; we want it visible without screaming).
  * Bumped to ``critical`` when the pattern's ``expected_amount_nis`` is
    ≥ ₪500 — at that scale the most-likely cause shifts toward "payment
    method failure" / "lost auto-pay" which IS urgent.

User dismissal:
  Patterns with ``status='user_dismissed'`` are excluded from the
  missing-detector scan but kept in the table for history. Patterns with
  ``status='dormant'`` are likewise excluded — they're paused, not
  monitored.

Idempotency:
  * ``learn_recurring_patterns`` is UPSERT-keyed on
    ``(user_id, merchant_normalized, expected_amount_nis)``.
  * ``detect_missing_recurring`` writes under SAVEPOINT-per-row; the
    partial unique index on (user_id, dedup_key) WHERE status='open'
    swallows reruns.
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseReviewQueue,
    ExpenseTransaction,
    RecurringChargePattern,
)


logger = logging.getLogger(__name__)


# Severity threshold: missing recurring charge of ≥₪500 → critical. Below
# that the user-experience is "I cancelled Netflix and Argosy noticed" —
# fine, just informative. Above that we're talking insurance / rent /
# bigger SaaS, where a missed auto-pay is a real action item.
CRITICAL_AMOUNT_THRESHOLD_NIS = Decimal("500.00")

# How far back to look when learning patterns (trailing days). 12 months
# matches the spec wording "trailing 12 months".
DEFAULT_LEARN_LOOKBACK_DAYS = 365


@dataclass(frozen=True)
class MissingRecurringFire:
    """One ``detect_missing_recurring`` firing.

    Returned so the caller can correlate to the queue row (e.g. for
    structured logging or live API responses).
    """

    pattern_id: int
    merchant_normalized: str
    expected_amount_nis: Decimal
    expected_on: date
    days_overdue: int
    dedup_key: str
    queue_row_id: int | None
    materiality: str  # "warning" | "critical"


def _quantize(value: Decimal | float | int) -> Decimal:
    if isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _median_int(values: Iterable[int]) -> int:
    """Median of an int sequence, rounded to nearest int (banker's
    rounding would change ties — we use HALF_UP for predictability)."""
    sorted_vals = sorted(values)
    if not sorted_vals:
        raise ValueError("median of empty sequence")
    return int(
        Decimal(str(statistics.median(sorted_vals))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def learn_recurring_patterns(
    session: Session,
    user_id: str,
    *,
    as_of: date | None = None,
    min_occurrences: int = 3,
    amount_tolerance: float = 0.15,
    cadence_min_days: int = 28,
    cadence_max_days: int = 32,
    lookback_days: int = DEFAULT_LEARN_LOOKBACK_DAYS,
) -> int:
    """Scan ``expense_transactions`` for repeating (merchant, amount,
    cadence) patterns and UPSERT them into ``recurring_charge_patterns``.

    Returns the count of patterns NEWLY-LEARNED OR REFRESHED in this run.
    A pattern is "refreshed" when its existing row's
    ``last_seen``/``occurrence_count``/``cadence_days`` change.

    Algorithm:
      * For each user merchant with ≥``min_occurrences`` debit txns in the
        lookback window:
          1. Compute median amount across the window's txns.
          2. Filter to txns whose amount is within ±``amount_tolerance``
             of that median.
          3. If still ≥``min_occurrences`` survive, compute median
             inter-transaction gap (in days).
          4. If median gap ∈ [cadence_min, cadence_max], persist a pattern
             keyed on (user_id, merchant_normalized, median_amount).

    Refunds / credits are excluded (direction != 'debit').
    Card payments are excluded (``is_card_payment=TRUE``) — those are
    cross-statement reconciliation, not a subscription pattern.

    User-dismissed patterns are preserved as-is — re-running the learner
    does NOT resurrect them to ``active``.
    """
    if as_of is None:
        as_of = date.today()
    if min_occurrences < 3:
        # Spec: ≥3 is a hard floor. The DB CHECK constraint enforces
        # occurrence_count >= 3 too — guarding here gives a clearer error.
        raise ValueError(
            f"learn_recurring_patterns: min_occurrences={min_occurrences} "
            "violates spec floor of 3."
        )
    if amount_tolerance <= 0 or amount_tolerance >= 1:
        raise ValueError(
            f"learn_recurring_patterns: amount_tolerance={amount_tolerance} "
            "must be in (0, 1)."
        )
    if cadence_min_days >= cadence_max_days:
        raise ValueError(
            f"learn_recurring_patterns: cadence_min_days={cadence_min_days} "
            f">= cadence_max_days={cadence_max_days}."
        )

    window_start = as_of - timedelta(days=lookback_days)

    rows = session.execute(
        sa_select(
            ExpenseTransaction.merchant_normalized,
            ExpenseTransaction.amount_nis,
            ExpenseTransaction.occurred_on,
        )
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.occurred_on >= window_start)
        .where(ExpenseTransaction.occurred_on <= as_of)
        .where(ExpenseTransaction.amount_nis.is_not(None))
        .where(ExpenseTransaction.direction == "debit")
        .where(ExpenseTransaction.is_card_payment.is_(False))
    ).all()

    # Group by merchant.
    by_merchant: dict[str, list[tuple[Decimal, date]]] = {}
    for merchant, amount, occurred_on in rows:
        by_merchant.setdefault(merchant, []).append(
            (abs(Decimal(amount)), occurred_on)
        )

    written_or_refreshed = 0
    for merchant, observations in by_merchant.items():
        if len(observations) < min_occurrences:
            continue

        amounts = [float(a) for a, _ in observations]
        median_amount = statistics.median(amounts)
        if median_amount <= 0:
            continue

        # Filter to observations within ±tolerance of median amount.
        lo = median_amount * (1.0 - amount_tolerance)
        hi = median_amount * (1.0 + amount_tolerance)
        in_band = [
            (amt, d) for (amt, d) in observations
            if lo <= float(amt) <= hi
        ]
        if len(in_band) < min_occurrences:
            continue

        # Cadence check: median inter-tx gap.
        in_band.sort(key=lambda pair: pair[1])
        gaps = [
            (in_band[i + 1][1] - in_band[i][1]).days
            for i in range(len(in_band) - 1)
        ]
        if not gaps:
            continue
        # Drop any zero-day gaps (same-day duplicate txns). Without this a
        # merchant that gets billed twice on the same day will pull the
        # median below the floor and we'd miss legit monthly cadence.
        gaps_filtered = [g for g in gaps if g > 0]
        if not gaps_filtered:
            continue
        median_gap = _median_int(gaps_filtered)
        if not (cadence_min_days <= median_gap <= cadence_max_days):
            continue

        # All gates passed. Compute persistence fields.
        # Persist the median amount (quantized) — this is what the dedup
        # natural key is built on.
        expected_amount = _quantize(median_amount)
        first_seen = min(d for _, d in in_band)
        last_seen = max(d for _, d in in_band)
        occurrence_count = len(in_band)

        existing = session.execute(
            sa_select(RecurringChargePattern)
            .where(RecurringChargePattern.user_id == user_id)
            .where(RecurringChargePattern.merchant_normalized == merchant)
            .where(
                RecurringChargePattern.expected_amount_nis == expected_amount
            )
        ).scalar_one_or_none()

        if existing is None:
            session.add(
                RecurringChargePattern(
                    user_id=user_id,
                    merchant_normalized=merchant,
                    expected_amount_nis=expected_amount,
                    amount_tolerance=_quantize_tolerance(amount_tolerance),
                    cadence_days=median_gap,
                    cadence_tolerance_days=7,
                    first_seen=first_seen,
                    last_seen=last_seen,
                    occurrence_count=occurrence_count,
                    status="active",
                )
            )
            written_or_refreshed += 1
        else:
            # Don't resurrect user-dismissed patterns — preserve user
            # intent. Refresh occurrence_count/last_seen for history.
            changed = False
            if existing.last_seen != last_seen:
                existing.last_seen = last_seen
                changed = True
            if existing.first_seen > first_seen:
                # We've found earlier evidence; pull first_seen back.
                existing.first_seen = first_seen
                changed = True
            if existing.occurrence_count != occurrence_count:
                existing.occurrence_count = occurrence_count
                changed = True
            if existing.cadence_days != median_gap:
                existing.cadence_days = median_gap
                changed = True
            if changed:
                written_or_refreshed += 1

    session.flush()
    return written_or_refreshed


def _quantize_tolerance(tol: float) -> Decimal:
    """Tolerance column is Numeric(4,3). 0.15 → '0.150'."""
    return Decimal(str(tol)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _build_dedup_key(
    *,
    user_id: str,
    pattern_id: int,
    expected_on: date,
) -> str:
    """Per spec #2 §4 dedup-key formula for Pattern B2.

    Format: ``v1|b2|u:<user_id>|pat:<pattern_id>|expected:<yyyy-mm-dd>``.

    Includes ``expected_on`` so the same pattern can fire again next
    month if the user lets the alert go stale — different expected date,
    different key.
    """
    return f"v1|b2|u:{user_id}|pat:{pattern_id}|expected:{expected_on.isoformat()}"


def detect_missing_recurring(
    session: Session,
    user_id: str,
    *,
    as_of: date | None = None,
    grace_days: int = 7,
) -> list[MissingRecurringFire]:
    """For each active pattern, fire when the expected next occurrence
    (``last_seen + cadence_days``) plus ``grace_days`` has elapsed without
    a matching transaction.

    "Matching" = same merchant_normalized + amount within
    ``±amount_tolerance`` of ``expected_amount_nis`` + occurred_on strictly
    after the pattern's ``last_seen``.

    Returns the list of new firings (queue rows written this run).
    Idempotent — re-running with the same data writes 0 new rows because
    the dedup_key collides on the partial unique index.
    """
    if as_of is None:
        as_of = date.today()

    patterns = session.execute(
        sa_select(RecurringChargePattern)
        .where(RecurringChargePattern.user_id == user_id)
        .where(RecurringChargePattern.status == "active")
    ).scalars().all()

    fires: list[MissingRecurringFire] = []

    # Codex IMPORTANT (sprint #2 Buckets A+B review): auto-dormancy
    # to prevent a permanently cancelled subscription from firing
    # forever. After N consecutive deadlines pass without any new
    # occurrence, transition status to 'dormant'. The user can
    # manually re-activate it (or just let learn_recurring_patterns
    # re-establish the pattern when transactions resume).
    DORMANCY_AFTER_MISSED_CYCLES = 2

    for pat in patterns:
        expected_on = pat.last_seen + timedelta(days=pat.cadence_days)
        deadline = expected_on + timedelta(days=grace_days)
        if as_of <= deadline:
            # Still within the grace window — not missing yet.
            continue

        # Dormancy check: how many full cycles have passed since
        # last_seen? If more than DORMANCY_AFTER_MISSED_CYCLES, the
        # subscription is most likely intentionally cancelled. Stop
        # firing and mark dormant.
        days_since_last_seen = (as_of - pat.last_seen).days
        cycles_missed = max(0, days_since_last_seen // max(pat.cadence_days, 1) - 1)
        if cycles_missed >= DORMANCY_AFTER_MISSED_CYCLES:
            pat.status = "dormant"
            session.flush()
            continue

        # Look for a fresh matching transaction since last_seen.
        amount_lo = pat.expected_amount_nis * (
            Decimal("1.00") - Decimal(str(pat.amount_tolerance))
        )
        amount_hi = pat.expected_amount_nis * (
            Decimal("1.00") + Decimal(str(pat.amount_tolerance))
        )
        match = session.execute(
            sa_select(ExpenseTransaction.id)
            .where(ExpenseTransaction.user_id == user_id)
            .where(
                ExpenseTransaction.merchant_normalized == pat.merchant_normalized
            )
            .where(ExpenseTransaction.amount_nis.is_not(None))
            .where(ExpenseTransaction.direction == "debit")
            .where(ExpenseTransaction.occurred_on > pat.last_seen)
            .where(ExpenseTransaction.occurred_on <= as_of)
            .where(ExpenseTransaction.amount_nis >= amount_lo)
            .where(ExpenseTransaction.amount_nis <= amount_hi)
            .limit(1)
        ).scalar_one_or_none()
        if match is not None:
            # A fresh charge exists — pattern is healthy. (The next
            # ``learn_recurring_patterns`` run will refresh last_seen.)
            continue

        dedup_key = _build_dedup_key(
            user_id=user_id, pattern_id=pat.id, expected_on=expected_on,
        )
        materiality = (
            "critical"
            if pat.expected_amount_nis >= CRITICAL_AMOUNT_THRESHOLD_NIS
            else "warning"
        )
        days_overdue = (as_of - expected_on).days

        payload = {
            "pattern_id": pat.id,
            "merchant_normalized": pat.merchant_normalized,
            "expected_amount_nis": str(pat.expected_amount_nis),
            "expected_on": expected_on.isoformat(),
            "last_seen": pat.last_seen.isoformat(),
            "cadence_days": pat.cadence_days,
            "grace_days": grace_days,
            "days_overdue": days_overdue,
        }

        queue_row = ExpenseReviewQueue(
            user_id=user_id,
            kind="bucket_b_recurring_missing",
            status="open",
            payload_json=json.dumps(payload, ensure_ascii=False),
            materiality=materiality,
            dedup_key=dedup_key,
            bucket="recurring",
        )

        try:
            with session.begin_nested():
                session.add(queue_row)
                session.flush()
            fires.append(
                MissingRecurringFire(
                    pattern_id=pat.id,
                    merchant_normalized=pat.merchant_normalized,
                    expected_amount_nis=pat.expected_amount_nis,
                    expected_on=expected_on,
                    days_overdue=days_overdue,
                    dedup_key=dedup_key,
                    queue_row_id=queue_row.id,
                    materiality=materiality,
                )
            )
        except IntegrityError:
            # Already-open dedup row exists. Re-look it up for the id, but
            # do NOT include in fires (no new write this run).
            logger.debug(
                "B2 dedup hit for user=%s pattern=%s expected=%s.",
                user_id, pat.id, expected_on.isoformat(),
            )
            # Intentionally don't append: ``fires`` is the *new* firings
            # this run, used for "did anything change" callers.

    return fires


__all__ = [
    "CRITICAL_AMOUNT_THRESHOLD_NIS",
    "DEFAULT_LEARN_LOOKBACK_DAYS",
    "MissingRecurringFire",
    "detect_missing_recurring",
    "learn_recurring_patterns",
]
