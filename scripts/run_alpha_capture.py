"""Daily Alpha capture CLI and cheap sign-in/unlock catch-up trigger."""
import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run_registered(*, explicit_retry=False):
    """Use normal durable job receipts, without an unauthenticated HTTP trigger.

    The interactive Windows task can also catch up when the API is unavailable.
    The DB daily claim remains the cross-process browser-overlap guard.
    """
    from argosy.services.jobs import JobRegistry, RegisteredScheduler
    from argosy.services.jobs.alpha_capture_daily import (
        AlphaCaptureDailyJob,
        alpha_capture_metadata,
    )
    from argosy.state import db as db_mod
    from argosy.state.models import JobRun

    job = AlphaCaptureDailyJob(explicit_retry=explicit_retry)
    registry = JobRegistry()
    scheduler = RegisteredScheduler(registry=registry)
    registry.bind_scheduler(scheduler)
    scheduler.register_loop(job)
    registry.register(job=job, metadata=alpha_capture_metadata())
    run_id = await registry.fire_now(job.name, triggered_by="cli:alpha-capture")
    async with db_mod.get_session() as session:
        row = await session.get(JobRun, run_id)
        return {"job_run_id": run_id, "job_status": row.status,
                "result": json.loads(row.output_summary or "{}"), "error": row.error_message}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", action="store_true", help="Run registered job only when due (Windows sign-in/unlock trigger)")
    parser.add_argument("--retry", action="store_true", help="Explicitly authorize another capture attempt today")
    parser.add_argument("--import-file", type=Path, help="Recover/import an already saved page without opening Chrome")
    args = parser.parse_args()
    from argosy.services.jobs.alpha_capture_daily import ZONE, capture_due, configuration
    config = configuration()
    if args.trigger:
        from argosy.services.alpha_capture import research_session, source_row
        now = datetime.now(UTC).astimezone(ZONE)
        if not config.get("enabled"):
            return
        with research_session() as session:
            source = source_row(session, config["user_id"])
            state = json.loads(source.config_json or "{}")
            if not source.enabled or not capture_due(state, now):
                return
    if args.import_file:
        from argosy.services.alpha_capture import ingest_capture
        result = asyncio.run(ingest_capture(args.import_file, user_id=config["user_id"]))
    else:
        result = asyncio.run(run_registered(explicit_retry=args.retry))
    print(json.dumps(result, default=str, indent=2))
    if result.get("job_status") in {"error", "cancelled"}:
        raise SystemExit(1)


if __name__ == "__main__":
    if sys.stdout is None:
        # pythonw is used by the Windows task; never create a console window.
        (ROOT / "logs").mkdir(exist_ok=True)
        sys.stdout = (ROOT / "logs/alpha_capture_trigger.log").open("a", encoding="utf-8")
        sys.stderr = sys.stdout
    main()
