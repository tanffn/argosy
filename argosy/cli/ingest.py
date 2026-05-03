"""`argosy ingest` subcommands."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from argosy.ingest.plan import parse_plan_markdown
from argosy.ingest.tsv import parse_portfolio_tsv

app = typer.Typer(no_args_is_help=True)


# ----------------------------------------------------------------------
# `argosy ingest tsv <path>`
# ----------------------------------------------------------------------


@app.command("tsv")
def ingest_tsv(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                help="Path to a Family Finances Status TSV."),
    user_id: str = typer.Option("ariel", "--user-id", help="Tenant id (default 'ariel')."),
    show_summary: bool = typer.Option(True, "--summary/--no-summary",
                                      help="Print a human-readable summary."),
) -> None:
    """Parse a portfolio TSV and print a summary.

    Phase 1 does not write positions to the DB (the holdings table arrives
    in Phase 2). The parser produces a `PortfolioSnapshot` that the
    plan-critique agent consumes via `argosy critique`.
    """
    snap = parse_portfolio_tsv(path)
    if show_summary:
        typer.echo(snap.summary_text())
    typer.echo("")
    typer.echo(f"Parsed OK: {len(snap.positions)} positions, "
               f"{len(snap.real_estate)} real-estate rows, "
               f"{len(snap.allocations)} allocation rows, "
               f"{len(snap.nvda_sales)} NVDA sales, "
               f"{len(snap.pensions)} pension entries.")
    if snap.parse_warnings:
        typer.echo(f"Warnings ({len(snap.parse_warnings)}):")
        for w in snap.parse_warnings:
            typer.echo(f"  - {w}")
    _ = user_id  # reserved for Phase 2 DB write


# ----------------------------------------------------------------------
# `argosy ingest plan <path>`
# ----------------------------------------------------------------------


@app.command("plan")
def ingest_plan(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                help="Path to the plan markdown."),
    user_id: str = typer.Option("ariel", "--user-id", help="Tenant id (default 'ariel')."),
    version_label: str = typer.Option(
        "", "--version-label", help="Version label, e.g. v2.0. Defaults to file name."
    ),
) -> None:
    """Parse a plan markdown and persist it as a new `plan_versions` row.

    The most recent `plan_versions` row is what `argosy critique` reads by
    default. Multiple imports produce multiple versions.
    """
    doc = parse_plan_markdown(path)
    label = version_label or path.stem
    typer.echo(f"Parsed: {doc.summary()}")
    asyncio.run(_persist_plan(user_id=user_id, version_label=label, source_path=str(path),
                              raw_markdown=doc.raw_markdown))
    typer.echo(f"Persisted plan version {label!r} for user_id={user_id!r}.")


async def _persist_plan(*, user_id: str, version_label: str, source_path: str,
                         raw_markdown: str) -> None:
    from sqlalchemy import select

    from argosy.state import db as db_mod
    from argosy.state.models import PlanVersion, User

    db_mod.init_engine()
    async with db_mod.get_session() as session:
        # Ensure the user row exists.
        existing = await session.execute(select(User).where(User.id == user_id))
        if existing.scalar_one_or_none() is None:
            session.add(User(id=user_id))
            await session.flush()
        pv = PlanVersion(
            user_id=user_id,
            version_label=version_label,
            source_path=source_path,
            raw_markdown=raw_markdown,
        )
        session.add(pv)
        await session.commit()
