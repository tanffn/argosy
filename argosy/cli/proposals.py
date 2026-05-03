"""`argosy proposals ...` — list / approve / reject proposals from the CLI.

Useful for headless review or scripted workflows. Each subcommand is a
thin wrapper around the same DB transitions the dashboard uses.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import typer
from sqlalchemy import select

from argosy.decisions.proposals import (
    IllegalTransitionError,
    ProposalStatus,
    assert_legal,
)
from argosy.logging import configure_logging
from argosy.state import db as db_mod
from argosy.state.models import (
    Approval,
    Proposal as ProposalRow,
    ProposalHistory,
)


app = typer.Typer(name="proposals", help="Proposal review commands.", no_args_is_help=True)


@app.command("list")
def list_cmd(
    user_id: str = typer.Option("ariel", "--user-id"),
    status: str | None = typer.Option(None, "--status", help="Optional status filter."),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List proposals; optional status filter."""
    configure_logging()
    db_mod.init_engine()

    async def _main() -> None:
        async with db_mod.get_session() as session:
            stmt = select(ProposalRow).where(ProposalRow.user_id == user_id)
            if status:
                stmt = stmt.where(ProposalRow.status == status)
            stmt = stmt.order_by(ProposalRow.created_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            typer.echo("(no proposals)")
            return
        for r in rows:
            typer.echo(
                f"#{r.id:>4} [{r.tier}] {r.status:<16} {r.action:<4} {r.ticker:<6} "
                f"size={r.size_shares_or_currency} acct={r.account_class}"
            )
            typer.echo(f"      rationale: {r.rationale_summary[:100]}")

    asyncio.run(_main())


@app.command("approve")
def approve_cmd(
    proposal_id: int = typer.Argument(...),
    user_id: str = typer.Option("ariel", "--user-id"),
    second_factor: bool = typer.Option(
        False,
        "--second-factor",
        help="Required for T3 proposals (Phase 5 wires the actual TOTP).",
    ),
    note: str = typer.Option("", "--note"),
) -> None:
    """Approve proposal `<id>`."""
    configure_logging()
    db_mod.init_engine()

    async def _main() -> int:
        async with db_mod.get_session() as session:
            row = await session.get(ProposalRow, proposal_id)
            if row is None or row.user_id != user_id:
                typer.echo(f"Proposal #{proposal_id} not found for user {user_id!r}")
                return 2
            if row.tier == "T3" and not second_factor:
                typer.echo(
                    "T3 approval requires --second-factor (stub for Phase 3)."
                )
                return 3
            try:
                assert_legal(ProposalStatus(row.status), ProposalStatus.APPROVED)
            except IllegalTransitionError as exc:
                typer.echo(f"Illegal transition: {exc}")
                return 4
            now = datetime.now(timezone.utc)
            row.status = ProposalStatus.APPROVED.value
            row.updated_at = now
            session.add(
                ProposalHistory(
                    proposal_id=row.id,
                    status=row.status,
                    transitioned_at=now,
                    transitioned_by=f"cli:{user_id}",
                    note=note or "Approved via CLI",
                )
            )
            session.add(
                Approval(
                    proposal_id=row.id,
                    user_id=user_id,
                    approved_at=now,
                    approval_channel="cli",
                    second_factor_used=second_factor,
                )
            )
            await session.commit()
            typer.echo(f"Approved proposal #{row.id}.")
            return 0

    rc = asyncio.run(_main())
    if rc != 0:
        raise typer.Exit(code=rc)


@app.command("reject")
def reject_cmd(
    proposal_id: int = typer.Argument(...),
    user_id: str = typer.Option("ariel", "--user-id"),
    note: str = typer.Option("", "--note"),
) -> None:
    """Reject proposal `<id>`."""
    configure_logging()
    db_mod.init_engine()

    async def _main() -> int:
        async with db_mod.get_session() as session:
            row = await session.get(ProposalRow, proposal_id)
            if row is None or row.user_id != user_id:
                typer.echo(f"Proposal #{proposal_id} not found for user {user_id!r}")
                return 2
            try:
                assert_legal(ProposalStatus(row.status), ProposalStatus.REJECTED)
            except IllegalTransitionError as exc:
                typer.echo(f"Illegal transition: {exc}")
                return 4
            now = datetime.now(timezone.utc)
            row.status = ProposalStatus.REJECTED.value
            row.updated_at = now
            session.add(
                ProposalHistory(
                    proposal_id=row.id,
                    status=row.status,
                    transitioned_at=now,
                    transitioned_by=f"cli:{user_id}",
                    note=note or "Rejected via CLI",
                )
            )
            await session.commit()
            typer.echo(f"Rejected proposal #{row.id}.")
            return 0

    rc = asyncio.run(_main())
    if rc != 0:
        raise typer.Exit(code=rc)
