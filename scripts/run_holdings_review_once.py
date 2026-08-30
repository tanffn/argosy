"""Run the registered holdings-review job through its audited real path."""
from __future__ import annotations

import asyncio
import json

from argosy.services.jobs.holdings_review import (
    HoldingsReviewJob,
    holdings_review_metadata,
)
from argosy.services.jobs.registered_scheduler import RegisteredScheduler
from argosy.services.jobs.registry import JobRegistry


async def _main() -> None:
    registry = JobRegistry()
    scheduler = RegisteredScheduler(registry=registry)
    registry.bind_scheduler(scheduler)
    job = HoldingsReviewJob()
    scheduler.register_loop(job)
    registry.register(job=job, metadata=holdings_review_metadata())
    run_id = await registry.fire_now(job.name, triggered_by="cli:holdings-review")
    print(json.dumps({"job": job.name, "job_run_id": run_id}))


if __name__ == "__main__":
    asyncio.run(_main())
