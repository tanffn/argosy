"""Operator recovery through real registered ticks, with durable receipts.

Use one operator process per job, away from the job's normal cron slot.
This does not start a second scheduler or submit/approve trades. The live
scheduler sees the running receipt and leaves recovery to this operator.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from argosy.agent_settings import load_agent_settings
from argosy.config import get_settings
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.services.decision_readiness import effective_status, failure_guidance
from argosy.services.jobs import JobMetadata, JobRegistry, RegisteredScheduler
from argosy.state import db as db_mod
from argosy.state.models import JobRun

JOBS = {
    "youtube_subscriptions": ("argosy.orchestrator.loops.youtube_subscriptions", "YouTubeSubscriptionsLoop"),
    "job_runs_retention": ("argosy.orchestrator.loops.job_runs_retention", "JobRunsRetentionLoop"),
    "knowledge_recheck": ("argosy.services.jobs.knowledge_recheck", "KnowledgeRecheckJob"),
    "annual": ("argosy.orchestrator.loops.annual", "AnnualLoop"),
    "news_daily": ("argosy.services.jobs.news_daily", "NewsDailyJob"),
    "state_observer_daily": ("argosy.orchestrator.loops.state_observer", "StateObserverLoop"),
    "thesis_monitor_daily": ("argosy.orchestrator.loops.thesis_monitor", "ThesisMonitorLoop"),
    "holdings_review": ("argosy.services.jobs.holdings_review", "HoldingsReviewJob"),
    "watchlist": ("argosy.orchestrator.loops.watchlist", "WatchlistLoop"),
    "discovery_funnel": ("argosy.orchestrator.loops.discovery_funnel_loop", "DiscoveryFunnelLoop"),
    "decision_funnel": ("argosy.orchestrator.loops.decision_funnel_loop", "DecisionFunnelLoop"),
    "predictions_evaluator": ("argosy.orchestrator.loops.predictions_evaluator", "PredictionsEvaluatorLoop"),
    "period_directive_daily": ("argosy.services.jobs.period_directive_daily", "PeriodDirectiveDailyJob"),
    "earnings_calendar_daily": ("argosy.services.jobs.earnings_calendar_daily", "EarningsCalendarDailyJob"),
    "sec_earnings_daily": ("argosy.services.jobs.sec_earnings_daily", "SecEarningsDailyJob"),
    "signal_streams_daily": ("argosy.orchestrator.loops.signal_streams_daily", "SignalStreamsDailyLoop"),
}


async def main(name: str, *, retry_failed_research: bool = False, documents=()) -> int:
    if documents and name != "knowledge_recheck":
        raise ValueError("--document applies only to knowledge_recheck")
    if retry_failed_research and name != "period_directive_daily":
        raise ValueError("--retry-failed-research applies only to period_directive_daily")
    if get_settings().decision_funnel_autoact:
        raise RuntimeError("Recovery requires decision_funnel_autoact=False; no trade execution authorized")
    module, class_name = JOBS[name]
    factory = getattr(importlib.import_module(module), class_name)
    kwargs = {}
    if name == "job_runs_retention":
        import httpx

        # The live registry, not a hardcoded bot list, owns long-running jobs.
        response = httpx.get("http://127.0.0.1:8000/api/jobs", timeout=10)
        response.raise_for_status()
        protected = tuple(job["name"] for job in response.json()["jobs"] if job["long_running"])
        kwargs["long_running_names_fn"] = lambda: protected
    if documents:
        kwargs["documents"] = documents
    if retry_failed_research:
        kwargs["retry_failed_research"] = True
    if name in ("watchlist", "annual"):
        config = getattr(load_agent_settings("ariel").cadences, name)
        kwargs = {"schedule": LoopSchedule.from_config(config), "enabled": config.enabled}
    job = factory(**kwargs)
    if not job.enabled:
        raise RuntimeError(f"{name} is disabled")
    # Operator runs must not race the normal cron (locks are process-local).
    now = datetime.now(UTC)
    next_due = job.schedule.next_due_after(now)
    if job.schedule.cron and next_due and next_due - now < timedelta(hours=2):
        raise RuntimeError(f"{name} is scheduled within two hours; use the live scheduler instead")
    async with db_mod.get_session() as session:
        running = (await session.execute(select(JobRun.id).where(
            JobRun.job_name == name, JobRun.status == "running",
        ))).scalars().all()
    if running:
        raise RuntimeError(f"{name} already running: {running}")
    registry = JobRegistry()
    scheduler = RegisteredScheduler(registry=registry)
    registry.bind_scheduler(scheduler)
    scheduler.register_loop(job)
    registry.register(job=job, metadata=JobMetadata(
        name=name, schedule_cron=job.schedule.cron, schedule_human="Operator recovery",
        source_kind="monitor", description="Real registered analysis recovery", long_running=False,
    ))
    run_id = await registry.fire_now(name, triggered_by="cli:user-requested-recovery")
    async with db_mod.get_session() as session:
        row = await session.get(JobRun, run_id)
        status = effective_status(row)
        print(json.dumps({"job": name, "job_run_id": run_id, "status": status,
                          "summary": json.loads(row.output_summary or "{}"),
                          "failure": failure_guidance(row) if status == "error" else None}, default=str), flush=True)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=JOBS)
    parser.add_argument("--document", action="append", default=[], help="Explicit knowledge recheck path (maximum two)")
    parser.add_argument("--retry-failed-research", action="store_true",
                        help="Explicitly retry failed due research once; retains failure history")
    args = parser.parse_args()
    if len(args.document) > 2:
        parser.error("At most two documents per bounded recheck")
    raise SystemExit(asyncio.run(main(args.job, retry_failed_research=args.retry_failed_research, documents=args.document)))
