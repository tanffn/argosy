"""`argosy brief --user-id <id>` — fire one Daily Brief on demand.

Skips the cadence schedule. Useful for testing the loop end-to-end
without waiting until 09:00. Mocks NOTHING — this calls live agents.
"""

from __future__ import annotations

import asyncio

import typer

from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.logging import configure_logging
from argosy.orchestrator import Scheduler
from argosy.state import db as db_mod


def brief(
    user_id: str = typer.Option("ariel", "--user-id", help="Tenant id."),
) -> None:
    """Trigger a one-shot Daily Brief run for `user_id`."""
    configure_logging()
    db_mod.init_engine()

    async def _main() -> None:
        scheduler = Scheduler(user_id=user_id)
        scheduler.register_default_loops()
        await scheduler.fire_once("daily_brief")

    try:
        asyncio.run(_main())
        typer.echo("Daily Brief completed. See `daily_briefs` table or the dashboard.")
    except MissingAPIKeyError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc
    except AgentRunError as exc:
        typer.echo(f"Daily Brief failed: {exc}")
        raise typer.Exit(code=3) from exc
    except KeyError as exc:
        typer.echo(f"Daily Brief loop is not registered (cadence disabled?): {exc}")
        raise typer.Exit(code=4) from exc
