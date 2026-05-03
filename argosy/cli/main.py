"""Argosy root CLI app.

Wires up the Phase 1+2 subcommands behind a single `argosy` entry point.
"""

from __future__ import annotations

import typer

from argosy.cli.brief import brief
from argosy.cli.critique import critique
from argosy.cli.decide import decide
from argosy.cli.ingest import app as ingest_app
from argosy.cli.intake import intake
from argosy.cli.proposals import app as proposals_app
from argosy.cli.run import run
from argosy.cli.secrets import app as secrets_app

app = typer.Typer(
    name="argosy",
    help="Argosy multi-agent financial advisor — Phase 3 CLI.",
    no_args_is_help=True,
    add_completion=False,
)

# Standalone subcommands.
app.command("intake")(intake)
app.command("critique")(critique)
app.command("run")(run)
app.command("brief")(brief)
app.command("decide")(decide)

# Subcommand groups.
app.add_typer(ingest_app, name="ingest", help="Ingestion commands (TSV, plan).")
app.add_typer(secrets_app, name="secrets", help="OS-keychain secret management.")
app.add_typer(proposals_app, name="proposals", help="Proposals review commands.")


def main() -> None:  # pragma: no cover
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
