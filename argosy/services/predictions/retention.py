"""Retention pass — spec C commit #4 (spec §9.1 — codex IMPORTANT 4 fix).

See ``docs/superpowers/specs/2026-05-29-predictions-ledger-design.md``
§9.1. The retention pass is bundled into the daily evaluator loop's
``tick()`` so retention runs adjacent to the scoring cycle (rather
than as a separate weekly cron) — a small enough workload that
folding it into the evaluator avoids one more cron entry.

Two passes per call:

1. **Archive evaluated predictions older than ``retention_days``.**
   v1 sets ``archived = 1`` on the prediction row. The partial index
   ``ix_predictions_due_at WHERE archived = 0`` excludes archived rows
   from the evaluator's due-query so the hot-path stays bounded as
   the ledger grows. A prediction is "evaluated" iff it has at least
   one row in ``prediction_outcomes`` — un-evaluated old rows are NOT
   archived (the evaluator may still need to score them once an
   adapter-error backlog clears).

2. **Compact archived predictions older than ``archive_days``.** v1
   intentionally does NOTHING beyond the archive flag flip. Spec §9.1
   describes a full cold-store + 3-bar evidence compact + zstd
   archive file; that's a follow-on once the ledger volume justifies
   the storage tier. The :class:`RetentionSummary` shape carries a
   ``compacted_count`` field today returning 0 so a future migration
   can populate it without breaking the loop's output summary
   contract.

The pass is idempotent — running twice in a row makes the second
call a no-op (the first call's UPDATE already flipped the flag).

Sync session contract — same as the evaluator. The caller (the
:class:`PredictionsEvaluatorLoop` or a test) owns the transaction
boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import Prediction, PredictionOutcome

_log = get_logger("argosy.services.predictions.retention")


# Defaults per spec §9.1. Tunable per-call so tests can compress the
# windows; production reads the defaults baked into the loop class.
DEFAULT_RETENTION_DAYS: int = 365
DEFAULT_ARCHIVE_DAYS: int = 730


@dataclass
class RetentionSummary:
    """Per-pass totals returned by :func:`run_retention_pass`.

    * ``archived_count`` — predictions flipped from archived=0 → 1.
    * ``compacted_count`` — reserved for the follow-on cold-store
      compactor; always 0 in v1.
    * ``inspected_evaluated_count`` — total candidates (evaluated +
      older than retention_days) BEFORE the archive flip; lets the
      operator see "we looked at N, archived K" in the loop summary.
    """

    archived_count: int = 0
    compacted_count: int = 0
    inspected_evaluated_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "archived_count": self.archived_count,
            "compacted_count": self.compacted_count,
            "inspected_evaluated_count": self.inspected_evaluated_count,
        }


def compact_old_predictions(
    session: Session,
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    archive_days: int = DEFAULT_ARCHIVE_DAYS,  # noqa: ARG001 - reserved for cold-store follow-on
) -> RetentionSummary:
    """Single retention pass — archive evaluated predictions.

    A prediction qualifies for archive when ALL of:

      * ``predictions.event_at < now - retention_days``;
      * ``predictions.archived = 0``;
      * at least ONE ``prediction_outcomes`` row references it (i.e.
        the evaluator has scored it at least once under any method —
        we explicitly do NOT require a row under the CURRENT active
        method, since archive is about "we're done seeing it" not "we
        agreed on the final scoring method").

    The "at least one outcome row" filter is the codex-probe-worthy
    guarantee: a prediction that's never been scored — e.g. because
    the adapter has been down for the entire retention window — is
    NEVER archived, so the evaluator's backlog drain still works
    once the upstream comes back.

    Returns the per-call :class:`RetentionSummary`.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    cutoff = now - timedelta(days=retention_days)

    # Inspection count: how many evaluated rows currently sit OUTSIDE
    # the retention window? Sampled BEFORE the UPDATE so the count
    # matches "we considered N", not "we changed N".
    has_outcome = (
        select(PredictionOutcome.id)
        .where(PredictionOutcome.prediction_id == Prediction.id)
        .exists()
    )
    inspect_stmt = select(Prediction.id).where(
        Prediction.event_at < cutoff,
        Prediction.archived == 0,
        has_outcome,
    )
    inspected = list(session.execute(inspect_stmt).scalars().all())

    if not inspected:
        return RetentionSummary(
            archived_count=0,
            compacted_count=0,
            inspected_evaluated_count=0,
        )

    # Flip the archive flag in a single UPDATE — bypasses the ORM
    # session's identity-map traversal which would be O(N) on
    # ten-thousand-prediction backlogs.
    upd = (
        update(Prediction)
        .where(Prediction.id.in_(inspected))
        .values(archived=1)
        .execution_options(synchronize_session=False)
    )
    result = session.execute(upd)
    archived = int(result.rowcount or 0)

    summary = RetentionSummary(
        archived_count=archived,
        compacted_count=0,
        inspected_evaluated_count=len(inspected),
    )
    _log.info(
        "predictions.retention.compact",
        cutoff=cutoff.isoformat(),
        archived_count=archived,
        inspected=len(inspected),
    )
    return summary


def run_retention_pass(
    session: Session,
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    archive_days: int = DEFAULT_ARCHIVE_DAYS,
) -> RetentionSummary:
    """Public entry-point wrapping :func:`compact_old_predictions`.

    Kept as a thin wrapper so the loop's ``tick()`` can call one
    function name + future cold-store work has a single insertion
    point. The wrapper does NOT commit; the caller owns the
    transaction boundary.
    """
    return compact_old_predictions(
        session,
        now=now,
        retention_days=retention_days,
        archive_days=archive_days,
    )


__all__ = [
    "DEFAULT_ARCHIVE_DAYS",
    "DEFAULT_RETENTION_DAYS",
    "RetentionSummary",
    "compact_old_predictions",
    "run_retention_pass",
]
