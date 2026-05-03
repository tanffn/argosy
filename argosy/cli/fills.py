"""`argosy fills ...` — list fills."""

from __future__ import annotations

import asyncio

import typer
from sqlalchemy import select

from argosy.logging import configure_logging
from argosy.state import db as db_mod
from argosy.state.models import Fill as FillRow


app = typer.Typer(name="fills", help="Fills inspection commands.", no_args_is_help=True)


@app.command("list")
def list_cmd(
    user_id: str = typer.Option("ariel", "--user-id"),
    proposal: int | None = typer.Option(None, "--proposal", help="Filter by proposal id"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List fills for a user, optionally filtered by proposal id."""
    configure_logging()
    db_mod.init_engine()

    async def _main() -> None:
        async with db_mod.get_session() as session:
            stmt = select(FillRow).where(FillRow.user_id == user_id)
            if proposal is not None:
                stmt = stmt.where(FillRow.proposal_id == proposal)
            stmt = stmt.order_by(FillRow.filled_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            typer.echo("(no fills)")
            return
        for r in rows:
            mark = "[paper]" if r.paper else "[live]"
            typer.echo(
                f"#{r.id:>4} {mark:7} {r.broker:10} {r.action:<4} {r.ticker:<6} "
                f"qty={r.quantity} price={r.price} commission={r.commission} "
                f"proposal={r.proposal_id} at {r.filled_at.isoformat()}"
            )

    asyncio.run(_main())


__all__ = ["app"]
