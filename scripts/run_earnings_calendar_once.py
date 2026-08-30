"""Run the registered earnings-calendar job through its audited real path."""
from __future__ import annotations

import asyncio
import json

from argosy.services.jobs.earnings_calendar_daily import (
    EarningsCalendarDailyJob,
    earnings_calendar_daily_metadata,
)
from argosy.services.jobs.registered_scheduler import RegisteredScheduler
from argosy.services.jobs.registry import JobRegistry


async def _main() -> None:
    registry = JobRegistry()
    scheduler = RegisteredScheduler(registry=registry)
    registry.bind_scheduler(scheduler)
    job = EarningsCalendarDailyJob()
    scheduler.register_loop(job)
    registry.register(job=job, metadata=earnings_calendar_daily_metadata())
    run_id = await registry.fire_now(
        job.name,
        triggered_by="cli:earnings-calendar",
    )
    print(json.dumps({"job": job.name, "job_run_id": run_id}))


if __name__ == "__main__":
    asyncio.run(_main())
