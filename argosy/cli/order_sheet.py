"""One-command real-path portfolio allocation to a validated order sheet."""

from __future__ import annotations

import json

import typer
from fastapi.testclient import TestClient


def order_sheet(
    cash_usd: float = typer.Option(..., "--cash-usd", min=0.01),
    user_id: str = typer.Option("ariel", "--user-id"),
    no_sells: bool = typer.Option(
        False,
        "--no-sells",
        help="Forbid every sell/trim in this decision run.",
    ),
    horizon_min: int = typer.Option(1, "--horizon-min", min=1, max=30),
    horizon_max: int = typer.Option(5, "--horizon-max", min=1, max=30),
) -> None:
    """Run and persist the production decision-to-outcome path.

    The command used to call the read-only deploy preview, so a successful
    sheet never replaced the Inbox artifact.  It now uses the same explicit
    E2E endpoint as the UI: compose, validate, persist, then print the durable
    proof the UI will read.
    """

    from argosy.api.main import create_app

    response = TestClient(create_app()).post(
        "/api/e2e-proof/run",
        json={
            "user_id": user_id,
            "cash_usd": cash_usd,
            "allow_sells": not no_sells,
            "horizon_years_min": horizon_min,
            "horizon_years_max": horizon_max,
        },
    )
    if response.status_code != 200:
        typer.echo(response.text, err=True)
        raise typer.Exit(code=2)
    proof = response.json()
    typer.echo(json.dumps(proof, indent=2, sort_keys=True))
    if proof.get("stage") not in {
        "ready_to_accept",
        "awaiting_order_approval",
        "execution",
        "partially_filled",
        "complete",
    }:
        raise typer.Exit(code=3)


__all__ = ["order_sheet"]
