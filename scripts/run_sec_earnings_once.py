"""Run the registered SEC earnings job through its audited real path."""
from __future__ import annotations

import asyncio
import json

from argosy.services.jobs.registered_scheduler import RegisteredScheduler
from argosy.services.jobs.registry import JobRegistry
from argosy.services.jobs.sec_earnings_daily import (
    SecEarningsDailyJob,
    sec_earnings_daily_metadata,
)


async def _main() -> None:
    registry = JobRegistry()
    scheduler = RegisteredScheduler(registry=registry)
    registry.bind_scheduler(scheduler)
    job = SecEarningsDailyJob()
    scheduler.register_loop(job)
    registry.register(job=job, metadata=sec_earnings_daily_metadata())
    run_id = await registry.fire_now(job.name, triggered_by="cli:sec-earnings")
    print(json.dumps({"job": job.name, "job_run_id": run_id}))


if __name__ == "__main__":
    asyncio.run(_main())
