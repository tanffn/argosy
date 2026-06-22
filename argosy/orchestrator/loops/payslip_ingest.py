"""Daily payslip-ingest cadence loop — the §102 RSU-withholding closed loop.

Once a day, discover the user's monthly Hilan payslip PDFs, catalog + parse any
new/changed ones, run the §102 equity-tax withholding adequacy check, and
persist the facts + verdict. This is what makes Argosy *answer* "is my RSU
withholding adequate?" itself (and re-check on each new payslip) instead of
asking the user.

Thin wrapper over :func:`argosy.services.payslip_ingest.ingest_payslips`. The
service is idempotent (skips bytes already ingested), tolerant of a missing
samples root, and deterministic — so a daily cadence is cheap and safe.

``source_kind='ingest'``. Manually triggerable via ``POST /api/jobs/{name}/run-now``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata

log = get_logger(__name__)

# Daily 06:30 IDT — early enough that a fresh month's payslip (typically posted
# overnight) is picked up before the morning brief, but it's a cheap idempotent
# scan so the exact time is not load-bearing.
_DEFAULT_CRON = "30 6 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def payslip_ingest_metadata() -> JobMetadata:
    """``source_kind='ingest'`` — payslip ingestion is a data-ingest job."""
    return JobMetadata(
        name="payslip_ingest",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 06:30 IDT",
        source_kind="ingest",
        description=(
            "Discovers the user's monthly Hilan payslip PDFs, catalogs + parses "
            "any new/changed ones, runs the §102 equity-tax withholding "
            "adequacy check, and persists the facts + verdict. Idempotent; this "
            "is the loop that lets Argosy answer 'is my RSU withholding "
            "adequate?' itself and re-check on each new payslip."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PayslipIngestLoop(CadenceLoop):
    """Daily payslip ingestion + §102 withholding check."""

    name = "payslip_ingest"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | None = None,
        ingest_fn: Callable[..., dict[str, Any]] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        if ingest_fn is None:
            from argosy.services.payslip_ingest import ingest_payslips

            ingest_fn = ingest_payslips
        self._ingest_fn = ingest_fn
        self._now_fn = now_fn or _utcnow
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        summary = await asyncio.to_thread(self._run_sync)
        self.last_output_summary = summary
        log.info("payslip_ingest.tick.done", user_id=self.user_id, **{
            k: v for k, v in summary.items() if k != "periods"
        })
        return summary

    def _run_sync(self) -> dict[str, Any]:
        return self._ingest_fn(
            self.user_id, session_factory=self._session_factory
        )


def run_payslip_ingest_now(
    *, user_id: str = "ariel", session_factory: sessionmaker | None = None
) -> dict[str, Any] | None:
    """Manual-trigger entry (the /api/jobs run-now route + backfills)."""
    loop = PayslipIngestLoop(user_id=user_id, session_factory=session_factory)
    return asyncio.run(loop.tick())


__all__ = [
    "PayslipIngestLoop",
    "payslip_ingest_metadata",
    "run_payslip_ingest_now",
]
