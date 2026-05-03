"""`argosy email ...` — email-channel utilities (Phase 4)."""

from __future__ import annotations

import asyncio

import typer

from argosy.channels.email import send_approval_email
from argosy.logging import configure_logging
from argosy.state import db as db_mod
from argosy.state.models import Proposal as ProposalRow


app = typer.Typer(
    name="email",
    help="Email approval channel commands.",
    no_args_is_help=True,
)


@app.command("send-approval")
def send_approval_cmd(
    proposal_id: int = typer.Argument(..., help="Proposal id"),
    recipient: str = typer.Option(..., "--to", help="Recipient email"),
    user_id: str = typer.Option("ariel", "--user-id"),
) -> None:
    """Send an approval email for proposal `<proposal_id>`.

    Reads SMTP config from `configs/<user_id>/email_settings.yaml`. The
    SMTP password (if needed) is read from the OS keychain under
    `argosy.email.smtp_password`.
    """
    configure_logging()
    db_mod.init_engine()

    async def _main() -> int:
        async with db_mod.get_session() as session:
            row = await session.get(ProposalRow, proposal_id)
            if row is None or row.user_id != user_id:
                typer.echo(f"Proposal #{proposal_id} not found for {user_id!r}")
                return 2
        subject, _body = await send_approval_email(
            proposal=row, recipient=recipient, user_id=user_id
        )
        typer.echo(f"Sent: {subject!r} to {recipient}")
        return 0

    rc = asyncio.run(_main())
    if rc != 0:
        raise typer.Exit(code=rc)


__all__ = ["app"]
