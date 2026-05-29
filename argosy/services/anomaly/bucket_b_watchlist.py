"""Bucket B Pattern B1 — watchlist fee-waiver state tracker (sprint #2 commit #6).

Backs spec ``docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md``
§1.2 Pattern B1.

The existing ``AnomalyDetectionAgent`` (``argosy/agents/anomaly_detection.py``)
reasons one-statement-at-a-time and has no concept of historical state. This
module is the persistence + state-machine half that closes that gap for the
Card 2923 fee-waiver watchlist entry (and any future watchlist entry that
fits the same 4-state contract).

Four-state machine (codex BLOCKER #1 — disambiguate statement-missing from
pattern-missing):

  * ``MATCHED``   — statement present, charge line + matching discount line
                    (sum within ₪0.01 of zero).
  * ``MISSING``   — statement present, charge line found, discount missing.
  * ``PARTIAL``   — statement present, charge line missing entirely (likely
                    a billing-period gap).
  * ``UNKNOWN``   — statement for this period not present.

Fire rule: a single transition ``MATCHED → MISSING`` between consecutive
observations writes ONE ``ExpenseReviewQueue`` row with bucket='recurring'
and a stable dedup_key. All other transitions are no-ops:

  * ``UNKNOWN → MISSING`` — we never had a baseline; signal is moot.
  * ``MATCHED → UNKNOWN`` — statement is late, not the discount line.
  * ``PARTIAL → MISSING`` — charge wasn't there last period; waiver-vs-no
                            waiver distinction is moot.
  * ``MISSING → MISSING`` — duplicate alert noise.

Severity ladder:
  * Default: ``warning`` (a fee disappearing is a real account-level change
    but typically not urgent).
  * Bumped to ``critical`` when the missing discount amount is > ₪50 — the
    user is now silently paying more than the noise floor.

Idempotency:
  * ``track_watchlist_observation`` is UPSERT-keyed on
    ``(user_id, watchlist_entry_id, observation_period)``.
  * ``check_fee_waiver_transition`` writes to ``ExpenseReviewQueue`` under a
    SAVEPOINT and relies on the partial unique index
    ``ix_expense_review_queue_dedup`` (migration 0047) to swallow duplicates
    when the same transition is re-evaluated.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from argosy.state.models import (
    ExpenseReviewQueue,
    WatchlistObservation,
)


logger = logging.getLogger(__name__)


WatchlistStatus = Literal["MATCHED", "MISSING", "PARTIAL", "UNKNOWN"]

# Severity threshold: if the missing discount/fee amount exceeds this, bump
# the queue row from warning → critical. ₪50 is a deliberate "above noise
# floor" cut: the Card 2923 fee is ~₪10-25 (warning), but a future
# watchlist entry could cover a larger waiver where silent loss matters
# more.
CRITICAL_AMOUNT_THRESHOLD_NIS = Decimal("50.00")


@dataclass(frozen=True)
class FireResult:
    """Outcome of one ``check_fee_waiver_transition`` call.

    ``fired`` is True iff a queue row was newly written (or already existed
    under the same dedup_key — the partial unique index makes both
    indistinguishable from the caller's perspective and we treat them as
    "alert recorded for this transition").
    """

    fired: bool
    prior_status: WatchlistStatus | None
    current_status: WatchlistStatus
    dedup_key: str | None
    queue_row_id: int | None


def track_watchlist_observation(
    session: Session,
    *,
    user_id: str,
    watchlist_entry_id: str,
    observation_period: date,
    status: WatchlistStatus,
    evidence_tx_ids: list[int] | None = None,
) -> WatchlistObservation:
    """UPSERT a ``watchlist_observations`` row.

    The natural key is ``(user_id, watchlist_entry_id, observation_period)``
    — re-running the agent over the same period updates the row in place.
    ``evidence_tx_ids`` is persisted as a JSON-encoded list of
    ``expense_transactions.id`` values; pass ``None`` to clear / leave it
    empty (``[]``).

    Returns the persisted row (already flushed; caller commits).
    """
    if status not in ("MATCHED", "MISSING", "PARTIAL", "UNKNOWN"):
        raise ValueError(
            f"track_watchlist_observation: invalid status {status!r} — "
            "must be one of MATCHED, MISSING, PARTIAL, UNKNOWN."
        )

    evidence_json = json.dumps(list(evidence_tx_ids or []))

    existing = session.execute(
        sa_select(WatchlistObservation)
        .where(WatchlistObservation.user_id == user_id)
        .where(WatchlistObservation.watchlist_entry_id == watchlist_entry_id)
        .where(WatchlistObservation.observation_period == observation_period)
    ).scalar_one_or_none()

    if existing is None:
        row = WatchlistObservation(
            user_id=user_id,
            watchlist_entry_id=watchlist_entry_id,
            observation_period=observation_period,
            status=status,
            evidence_tx_ids=evidence_json,
        )
        session.add(row)
        session.flush()
        return row

    existing.status = status
    existing.evidence_tx_ids = evidence_json
    session.flush()
    return existing


def _build_dedup_key(
    *,
    user_id: str,
    watchlist_entry_id: str,
    observation_period: date,
) -> str:
    """Per spec #2 §4 dedup-key formula for Pattern B1.

    Format: ``v1|b1|u:<user_id>|watch:<entry_id>|period:<yyyy-mm-01>|
    transition:matched_missing``

    The ``v1`` prefix lets us rev the rule later (e.g. tightening the
    PARTIAL→MISSING gate) without false-suppressing fresh alerts under the
    new logic.
    """
    period_iso = observation_period.isoformat()
    return (
        f"v1|b1|u:{user_id}|watch:{watchlist_entry_id}"
        f"|period:{period_iso}|transition:matched_missing"
    )


def check_fee_waiver_transition(
    session: Session,
    user_id: str,
    watchlist_entry_id: str,
    current_period: date,
    *,
    missing_amount_nis: Decimal | float | int | None = None,
    related_tx_id: int | None = None,
) -> FireResult:
    """Evaluate the firing rule for the most-recent transition.

    Looks up the most recent observation strictly before ``current_period``
    (per ``observation_period`` ordering) and the observation at
    ``current_period`` itself. Fires iff prior=``MATCHED`` and
    current=``MISSING``.

    Writes one ``ExpenseReviewQueue`` row when firing:
      * ``bucket='recurring'``
      * ``kind='bucket_b_fee_waiver_missing'``
      * ``dedup_key`` per spec formula (v1|b1|...).
      * ``materiality='warning'`` by default; ``'critical'`` when
        ``missing_amount_nis`` exceeds CRITICAL_AMOUNT_THRESHOLD_NIS.

    Idempotency via the partial unique index on (user_id, dedup_key) WHERE
    status='open' — a duplicate insert raises ``IntegrityError`` which we
    swallow and report as ``fired=True`` with the existing row id.
    """
    current = session.execute(
        sa_select(WatchlistObservation)
        .where(WatchlistObservation.user_id == user_id)
        .where(WatchlistObservation.watchlist_entry_id == watchlist_entry_id)
        .where(WatchlistObservation.observation_period == current_period)
    ).scalar_one_or_none()

    if current is None:
        # No observation recorded for the current period yet — nothing to
        # evaluate. Caller should track_watchlist_observation() first.
        return FireResult(
            fired=False,
            prior_status=None,
            current_status="UNKNOWN",
            dedup_key=None,
            queue_row_id=None,
        )

    current_status: WatchlistStatus = current.status  # type: ignore[assignment]

    prior = session.execute(
        sa_select(WatchlistObservation)
        .where(WatchlistObservation.user_id == user_id)
        .where(WatchlistObservation.watchlist_entry_id == watchlist_entry_id)
        .where(WatchlistObservation.observation_period < current_period)
        .order_by(WatchlistObservation.observation_period.desc())
        .limit(1)
    ).scalar_one_or_none()

    prior_status: WatchlistStatus | None = (
        prior.status if prior is not None else None  # type: ignore[assignment]
    )

    if prior_status != "MATCHED" or current_status != "MISSING":
        return FireResult(
            fired=False,
            prior_status=prior_status,
            current_status=current_status,
            dedup_key=None,
            queue_row_id=None,
        )

    dedup_key = _build_dedup_key(
        user_id=user_id,
        watchlist_entry_id=watchlist_entry_id,
        observation_period=current_period,
    )

    materiality = "warning"
    if missing_amount_nis is not None:
        amt = abs(Decimal(str(missing_amount_nis)))
        if amt > CRITICAL_AMOUNT_THRESHOLD_NIS:
            materiality = "critical"

    payload = {
        "watchlist_entry_id": watchlist_entry_id,
        "observation_period": current_period.isoformat(),
        "prior_status": prior_status,
        "current_status": current_status,
        "missing_amount_nis": (
            str(Decimal(str(missing_amount_nis)))
            if missing_amount_nis is not None
            else None
        ),
        "transition": "matched_missing",
    }

    queue_row = ExpenseReviewQueue(
        user_id=user_id,
        kind="bucket_b_fee_waiver_missing",
        status="open",
        payload_json=json.dumps(payload, ensure_ascii=False),
        related_tx_id=related_tx_id,
        materiality=materiality,
        dedup_key=dedup_key,
        bucket="recurring",
    )

    # SAVEPOINT per row so a duplicate-key collision doesn't poison the
    # surrounding transaction (matches the pattern in news_ingest.py and
    # what spec #2 §1.5 calls out as the idempotency contract).
    try:
        with session.begin_nested():
            session.add(queue_row)
            session.flush()
        return FireResult(
            fired=True,
            prior_status=prior_status,
            current_status=current_status,
            dedup_key=dedup_key,
            queue_row_id=queue_row.id,
        )
    except IntegrityError:
        # Duplicate: an open row with the same dedup_key already exists
        # for this user. Look it up so the caller has the id for any
        # downstream linking. The partial unique index is the contract.
        logger.debug(
            "B1 dedup hit for user=%s entry=%s period=%s — alert already open.",
            user_id, watchlist_entry_id, current_period.isoformat(),
        )
        existing = session.execute(
            sa_select(ExpenseReviewQueue)
            .where(ExpenseReviewQueue.user_id == user_id)
            .where(ExpenseReviewQueue.dedup_key == dedup_key)
            .where(ExpenseReviewQueue.status == "open")
        ).scalar_one_or_none()
        return FireResult(
            fired=True,
            prior_status=prior_status,
            current_status=current_status,
            dedup_key=dedup_key,
            queue_row_id=existing.id if existing is not None else None,
        )


__all__ = [
    "CRITICAL_AMOUNT_THRESHOLD_NIS",
    "FireResult",
    "WatchlistStatus",
    "check_fee_waiver_transition",
    "track_watchlist_observation",
]
