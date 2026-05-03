"""`argosy secrets` subcommand.

A thin wrapper over `argosy.secrets` (OS keychain). Avoids printing secret
values by default; require `--reveal` to actually print on `get`.
"""

from __future__ import annotations

import typer

from argosy import secrets as secrets_mod

app = typer.Typer(no_args_is_help=True)


@app.command("set")
def set_secret(
    key: str = typer.Argument(..., help="Secret key, e.g. argosy.anthropic.api_key"),
    value: str = typer.Argument(..., help="Secret value to store."),
) -> None:
    """Store a secret in the OS keychain under the Argosy service."""
    secrets_mod.set_secret(key, value)
    typer.echo(f"Stored secret {key!r} in the OS keychain.")


@app.command("get")
def get_secret(
    key: str = typer.Argument(..., help="Secret key to read."),
    reveal: bool = typer.Option(False, "--reveal", help="Print the actual value."),
) -> None:
    """Read a secret. Without --reveal, prints only existence info."""
    val = secrets_mod.get_secret(key)
    if val is None:
        typer.echo(f"Secret {key!r} is NOT set.")
        raise typer.Exit(code=1)
    if reveal:
        typer.echo(val)
    else:
        typer.echo(f"Secret {key!r} is set ({len(val)} chars). Use --reveal to print.")


@app.command("delete")
def delete_secret(
    key: str = typer.Argument(..., help="Secret key to remove."),
) -> None:
    """Remove a secret from the OS keychain."""
    secrets_mod.delete_secret(key)
    typer.echo(f"Removed secret {key!r} (if it existed).")
