"""`argosy security` CLI subcommands (Phase 5).

  argosy security totp setup        — enroll a new TOTP secret
  argosy security totp verify <code>
"""

from __future__ import annotations

import asyncio

import typer

from argosy.security import totp as totp_mod


app = typer.Typer(no_args_is_help=True)
totp_app = typer.Typer(no_args_is_help=True)
app.add_typer(totp_app, name="totp", help="TOTP second-factor management.")


@totp_app.command("setup")
def totp_setup_cmd(
    user_id: str = typer.Option("ariel", "--user-id", help="User id."),
    label: str | None = typer.Option(None, "--label", help="Account label."),
) -> None:
    """Generate a fresh TOTP secret and print the provisioning URI."""

    async def _run() -> None:
        secret = totp_mod.generate_secret()
        await totp_mod.set_user_totp_secret(user_id, secret)
        from argosy import secrets as keychain

        keychain.set_totp_secret(user_id, secret)
        uri = totp_mod.provisioning_uri(
            secret=secret, account_name=(label or user_id)
        )
        typer.echo(f"Secret (base32): {secret}")
        typer.echo(f"Provisioning URI: {uri}")
        typer.echo(
            "\nScan the URI as a QR code with Google Authenticator / Authy /"
            " 1Password, then run `argosy security totp verify <code>` to confirm."
        )

    asyncio.run(_run())


@totp_app.command("verify")
def totp_verify_cmd(
    code: str = typer.Argument(..., help="6-digit TOTP code."),
    user_id: str = typer.Option("ariel", "--user-id", help="User id."),
) -> None:
    """Verify a TOTP code against the stored secret."""

    async def _run() -> None:
        secret = await totp_mod.get_user_totp_secret(user_id)
        if not secret:
            typer.echo("No TOTP secret enrolled. Run `argosy security totp setup`.")
            raise typer.Exit(code=1)
        try:
            result = totp_mod.verify_code(secret, code)
        except totp_mod.TOTPVerificationError as exc:
            typer.echo(f"Invalid code: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        await totp_mod.mark_verified(
            user_id, at=result.counter * totp_mod.DEFAULT_STEP_SECONDS
        )
        typer.echo("OK: code accepted.")

    asyncio.run(_run())


__all__ = ["app"]
