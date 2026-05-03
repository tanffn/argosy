"""`argosy execute <proposal_id>` — drive the execution router.

Useful for headless / dev / testing flows. Runs the Phase 4 execution
path against an APPROVED proposal: re-runs preflight, calls the broker
adapter, advances the state machine.
"""

from __future__ import annotations

import asyncio

import typer

from argosy.execution.router import ExecutionRouter
from argosy.logging import configure_logging
from argosy.state import db as db_mod


def execute(
    proposal_id: int = typer.Argument(..., help="Proposal id to execute."),
    user_id: str = typer.Option("ariel", "--user-id"),
    cash_available_usd: float = typer.Option(
        0.0,
        "--cash-available-usd",
        help="Cash available for the preflight check.",
    ),
    max_position_usd: float | None = typer.Option(
        None,
        "--max-position-usd",
        help="Optional per-trade size cap for the preflight check.",
    ),
) -> None:
    """Execute proposal `<proposal_id>` via the execution router."""
    configure_logging()
    db_mod.init_engine()

    async def _main() -> int:
        router = ExecutionRouter(user_id=user_id)
        try:
            result = await router.execute(
                proposal_id,
                cash_available_usd=cash_available_usd,
                max_position_usd=max_position_usd,
            )
        except LookupError as exc:
            typer.echo(str(exc))
            return 2
        except PermissionError as exc:
            typer.echo(str(exc))
            return 3
        typer.echo(f"Status: {result.status}")
        typer.echo(f"Broker: {result.broker}")
        typer.echo(f"Broker order id: {result.broker_order_id}")
        typer.echo(f"Paper: {result.paper}")
        typer.echo(f"Reason: {result.reason}")
        if result.fills:
            typer.echo(f"Fills ({len(result.fills)}):")
            for f in result.fills:
                typer.echo(
                    f"  - {f.action} {f.quantity} {f.ticker} @ {f.price} "
                    f"(commission {f.commission})"
                )
        return 0

    rc = asyncio.run(_main())
    if rc != 0:
        raise typer.Exit(code=rc)


__all__ = ["execute"]
