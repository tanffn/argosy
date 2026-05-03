"""`argosy lots ...` — import lots from a CSV/TSV."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from argosy.adapters.brokers.schwab_csv import SchwabCSVAdapter
from argosy.logging import configure_logging
from argosy.state import db as db_mod


app = typer.Typer(name="lots", help="Per-tax-lot import + listing.", no_args_is_help=True)


@app.command("import")
def import_cmd(
    broker: str = typer.Option(..., "--broker", help="Broker name: schwab | leumi"),
    path: Path = typer.Option(..., "--path", exists=True, dir_okay=False, readable=True,
                              help="Path to the CSV/TSV file."),
    user_id: str = typer.Option("ariel", "--user-id"),
    account_id: str | None = typer.Option(None, "--account-id"),
) -> None:
    """Import per-lot data from a broker export.

    Schwab: cost-basis CSV; rows persisted to `lots`.
    Leumi: TSV is position-aggregated and has no per-lot data; the
    importer warns and exits cleanly.
    """
    configure_logging()
    db_mod.init_engine()

    async def _main() -> int:
        if broker.lower() == "schwab":
            adapter = SchwabCSVAdapter(user_id=user_id)
            n = await adapter.import_cost_basis_csv(path, account_id=account_id)
            typer.echo(f"Imported {n} lots from {path} into account {account_id or '(default)'}.")
            return 0
        if broker.lower() == "leumi":
            typer.echo(
                "Leumi TSV is position-aggregated and provides no per-lot "
                "data. No rows imported. (Use `argosy ingest tsv <path>` for "
                "the snapshot view.)"
            )
            return 0
        typer.echo(f"Unknown broker: {broker!r}. Expected 'schwab' or 'leumi'.")
        return 2

    rc = asyncio.run(_main())
    if rc != 0:
        raise typer.Exit(code=rc)


__all__ = ["app"]
