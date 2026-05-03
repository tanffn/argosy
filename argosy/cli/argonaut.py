"""`argosy argonaut` CLI subcommands (Phase 5).

  argosy argonaut status                  — current account state
  argosy argonaut snapshot                — force a daily snapshot
  argosy argonaut mode <paper|live|queue_only>
"""

from __future__ import annotations

import asyncio

import typer

from argosy.accounts.argonaut import ArgonautAccount
from argosy.agent_settings import load_agent_settings, save_agent_settings


app = typer.Typer(no_args_is_help=True)


@app.command("status")
def status_cmd(
    user_id: str = typer.Option("ariel", "--user-id", help="User id."),
) -> None:
    """Print current Argonaut state."""
    settings = load_agent_settings(user_id)
    acct = ArgonautAccount(user_id=user_id, settings=settings)
    typer.echo(f"Argonaut for {user_id}:")
    typer.echo(f"  account_id          : {acct.account_id}")
    typer.echo(f"  configured_size_usd : {acct.configured_size_usd}")
    typer.echo(f"  execution_mode      : {acct.current_execution_mode()}")
    typer.echo(f"  autonomy_enabled    : {acct.is_autonomy_enabled()}")
    typer.echo(f"  per_decision_max_pct: {acct.per_decision_max_pct}")
    typer.echo(f"  daily_loss_limit_pct: {acct.daily_loss_limit_pct}")


@app.command("snapshot")
def snapshot_cmd(
    user_id: str = typer.Option("ariel", "--user-id", help="User id."),
) -> None:
    """Force a daily snapshot now."""

    async def _run() -> None:
        settings = load_agent_settings(user_id)
        acct = ArgonautAccount(user_id=user_id, settings=settings)
        payload = await acct.persist_daily_snapshot()
        typer.echo(
            f"Snapshot {payload.date}: total=${payload.total_value_usd:,.2f} "
            f"cash=${payload.cash_usd:,.2f} day_pnl=${payload.day_pnl_usd:,.2f}"
        )

    asyncio.run(_run())


@app.command("mode")
def mode_cmd(
    new_mode: str = typer.Argument(
        ..., help="paper | live | queue_only"
    ),
    user_id: str = typer.Option("ariel", "--user-id", help="User id."),
) -> None:
    """Set the limited account's execution mode (writes agent_settings.yaml)."""
    if new_mode not in ("paper", "live", "queue_only"):
        typer.echo(f"invalid mode: {new_mode!r}", err=True)
        raise typer.Exit(code=2)
    settings = load_agent_settings(user_id)
    prior = settings.limited_account.execution_mode
    if prior == new_mode:
        typer.echo(f"already {new_mode}; no change.")
        return
    settings.limited_account.execution_mode = new_mode  # type: ignore[assignment]
    save_agent_settings(user_id, settings)
    typer.echo(f"Argonaut mode {prior} -> {new_mode} (saved).")


__all__ = ["app"]
