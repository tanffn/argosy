"""``PendingReevaluationDailyLoop`` — auto-retry pending /consult runs.

Sweeps ``pending_reevaluations`` daily at 04:00 IDT (after the
inferred-life-event detector at 03:00, before the news pipeline /
state observer at 17:00). For each ``status='pending'`` row, re-fires
the consult with the original ticker + tier + consult_mode +
user_constraints. Three outcomes:

  - **Resolved**: the retry returned a real BUY/HOLD/SELL verdict
    (not INSUFFICIENT_DATA). Row marked ``status='resolved'`` with
    ``resolved_decision_run_id`` pointing at the new run.
  - **Attempted**: the retry STILL returned INSUFFICIENT_DATA.
    ``attempt_count`` increments + ``last_failure_reason`` refreshes.
    Will try again tomorrow.
  - **Abandoned**: attempt_count exceeds ``MAX_REEVAL_ATTEMPTS`` (7
    by default). Row marked ``status='abandoned'`` — caller must
    manually re-fire the consult (e.g. after configuring a Finnhub
    API key or pointing at SEC EDGAR direct) to reset the retry.

The loop holds no state across ticks — the queue table is the
truth. Cancelling mid-tick is safe (each row's transition runs in
its own session).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from argosy.api.routes.decisions import RunRequest, run_decision_flow
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.services.pending_reevaluation import (
    MAX_REEVAL_ATTEMPTS,
    list_pending_for_sweep,
    mark_abandoned,
    mark_resolved,
    record_attempt,
)


_log = get_logger("argosy.loops.pending_reevaluation_daily")


_DEFAULT_CRON = "0 4 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def pending_reevaluation_daily_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` row the registry surfaces."""
    return JobMetadata(
        name="pending_reevaluation_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 04:00 Asia/Jerusalem",
        source_kind="maintenance",
        description=(
            "Sweeps pending_reevaluations queue; re-fires each consult "
            "with the original params; marks resolved on real verdict, "
            "records attempt on persistent INSUFFICIENT_DATA, abandons "
            f"after {MAX_REEVAL_ATTEMPTS} attempts."
        ),
    )


# Callback type: given a RunRequest, return the RunResponse. Tests inject
# a stub; production binds the real ``run_decision_flow``.
ConsultRunner = Callable[[RunRequest], Awaitable[Any]]


class PendingReevaluationDailyLoop(CadenceLoop):
    """Daily sweep of the pending_reevaluations queue."""

    name = "pending_reevaluation_daily"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        consult_runner: ConsultRunner | None = None,
        max_attempts: int = MAX_REEVAL_ATTEMPTS,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._consult_runner = consult_runner or run_decision_flow
        self._max_attempts = max_attempts
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self) -> dict[str, Any] | None:
        rows = await list_pending_for_sweep(user_id=self.user_id)
        if not rows:
            summary = {
                "swept": 0,
                "resolved": 0,
                "still_pending": 0,
                "abandoned": 0,
            }
            self.last_output_summary = summary
            _log.info("pending_reevaluation_daily.no_rows", **summary)
            return summary

        resolved = 0
        still_pending = 0
        abandoned = 0
        for row in rows:
            try:
                body = RunRequest(
                    user_id=row.user_id,
                    ticker=row.ticker,
                    tier=row.tier_value,
                    consult_mode=row.consult_mode,
                    analyst_report_ids=[],
                    user_constraints=row.user_constraints or "",
                    portfolio_value_usd=1.0,
                )
                resp = await self._consult_runner(body)
            except Exception as exc:  # noqa: BLE001 - per-row defensive
                _log.warning(
                    "pending_reevaluation_daily.consult_failed",
                    row_id=row.id, ticker=row.ticker, error=str(exc)[:200],
                )
                new_attempt = await record_attempt(
                    row_id=row.id,
                    failure_reason=f"consult call raised: {type(exc).__name__}: {str(exc)[:200]}",
                )
                if new_attempt >= self._max_attempts:
                    await mark_abandoned(
                        row_id=row.id,
                        final_reason=(
                            f"retry exception after {new_attempt} attempts: "
                            f"{type(exc).__name__}"
                        ),
                    )
                    abandoned += 1
                else:
                    still_pending += 1
                continue

            blocked_by = getattr(resp, "blocked_by", None)
            status = getattr(resp, "status", None)
            run_id = getattr(resp, "decision_run_id", None)

            if blocked_by == "trader_insufficient_data":
                # Still insufficient — record attempt + maybe abandon.
                new_attempt = await record_attempt(
                    row_id=row.id,
                    failure_reason=getattr(resp, "blocked_reason", "")[:500],
                )
                if new_attempt >= self._max_attempts:
                    await mark_abandoned(
                        row_id=row.id,
                        final_reason=(
                            f"INSUFFICIENT_DATA persisted across {new_attempt} attempts"
                        ),
                    )
                    abandoned += 1
                else:
                    still_pending += 1
                continue

            # Any other outcome — resolved (approved OR a real
            # BUY/HOLD/SELL block from trader/risk/FM means the
            # analysis completed and we got a real answer, even if
            # it was HOLD).
            await mark_resolved(row_id=row.id, decision_run_id=run_id)
            resolved += 1

        summary = {
            "swept": len(rows),
            "resolved": resolved,
            "still_pending": still_pending,
            "abandoned": abandoned,
        }
        self.last_output_summary = summary
        _log.info("pending_reevaluation_daily.summary", **summary)
        return summary


__all__ = [
    "PendingReevaluationDailyLoop",
    "pending_reevaluation_daily_metadata",
]
