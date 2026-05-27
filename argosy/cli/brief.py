"""`argosy brief --user-id <id>` — fire one Daily Brief on demand.

W9 — wired to T4.5's single-agent runner
(``argosy/services/daily_brief_runner.py::generate_daily_brief``).
The Phase 2 four-agent ``DailyBriefLoop`` is retired. Mocks NOTHING —
this calls a live agent.
"""

from __future__ import annotations

import asyncio

import typer

from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.logging import configure_logging
from argosy.services.daily_brief_runner import generate_daily_brief
from argosy.state import db as db_mod


def brief(
    user_id: str = typer.Option("ariel", "--user-id", help="Tenant id."),
) -> None:
    """Trigger a one-shot Daily Brief run for `user_id`."""
    configure_logging()
    db_mod.init_engine()

    async def _main() -> None:
        async with db_mod.get_session() as session:
            await generate_daily_brief(user_id, session)

    try:
        asyncio.run(_main())
        typer.echo("Daily Brief completed. See `daily_briefs` table or the dashboard.")
    except MissingAPIKeyError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc
    except AgentRunError as exc:
        typer.echo(f"Daily Brief failed: {exc}")
        raise typer.Exit(code=3) from exc
