"""`argosy run` — start the cadence orchestrator in the foreground (Phase 2).

Dev-only. Production deployment is Phase 6+ (systemd / launchd / docker).
Ctrl-C stops the scheduler cleanly.
"""

from __future__ import annotations

import asyncio
import signal

import typer

from argosy.logging import configure_logging, get_logger
from argosy.orchestrator import Scheduler
from argosy.state import db as db_mod


def run(
    user_id: str = typer.Option("ariel", "--user-id", help="Tenant id."),
) -> None:
    """Start the cadence scheduler. Ctrl-C to stop."""
    configure_logging()
    log = get_logger("argosy.cli.run")
    db_mod.init_engine()

    async def _main() -> None:
        scheduler = Scheduler(user_id=user_id)
        scheduler.register_default_loops()

        # Hook SIGINT/SIGTERM → scheduler.stop().
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, scheduler.stop)
            except NotImplementedError:  # pragma: no cover - Windows
                pass

        log.info("scheduler.starting", user_id=user_id)
        await scheduler.run_forever()
        log.info("scheduler.stopped")

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        typer.echo("Scheduler stopped.")
