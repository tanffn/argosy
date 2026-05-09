"""Argosy root CLI app.

Wires up the Phase 1+2 subcommands behind a single `argosy` entry point.
"""

from __future__ import annotations

import typer

from argosy.cli.admin import admin_app
from argosy.cli.argonaut import app as argonaut_app
from argosy.cli.expenses_admin import app as expenses_app
from argosy.cli.brief import brief
from argosy.cli.critique import critique
from argosy.cli.data import app as data_app
from argosy.cli.decide import decide
from argosy.cli.email import app as email_app
from argosy.cli.execute import execute
from argosy.cli.fills import app as fills_app
from argosy.cli.gemelnet import app as gemelnet_app
from argosy.cli.ingest import app as ingest_app
from argosy.cli.intake import intake
from argosy.cli.lots import app as lots_app
from argosy.cli.proposals import app as proposals_app
from argosy.cli.run import run
from argosy.cli.secrets import app as secrets_app
from argosy.cli.security import app as security_app

app = typer.Typer(
    name="argosy",
    help="Argosy multi-agent financial advisor — Phase 4 CLI.",
    no_args_is_help=True,
    add_completion=False,
)

# Standalone subcommands.
app.command("intake")(intake)
app.command("critique")(critique)
app.command("run")(run)
app.command("brief")(brief)
app.command("decide")(decide)
app.command("execute")(execute)

# Subcommand groups.
app.add_typer(ingest_app, name="ingest", help="Ingestion commands (TSV, plan).")
app.add_typer(secrets_app, name="secrets", help="OS-keychain secret management.")
app.add_typer(proposals_app, name="proposals", help="Proposals review commands.")
app.add_typer(fills_app, name="fills", help="Fills inspection commands.")
app.add_typer(lots_app, name="lots", help="Per-tax-lot import + listing.")
app.add_typer(email_app, name="email", help="Email approval channel commands.")
app.add_typer(argonaut_app, name="argonaut", help="Argonaut limited-account commands.")
app.add_typer(security_app, name="security", help="Security / second-factor management.")
app.add_typer(admin_app, name="admin", help="Operator commands (tenants, watchdog).")
app.add_typer(gemelnet_app, name="gemelnet",
              help="Israeli pension performance data (gemelnet.mof.gov.il).")
app.add_typer(data_app, name="data",
              help="Investor-event feeds: 13F, Form 4, capitoltrades, tipranks.")
app.add_typer(expenses_app, name="expenses",
              help="Household-expenses admin utilities (verify-file, backfill).")


def main() -> None:  # pragma: no cover
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
