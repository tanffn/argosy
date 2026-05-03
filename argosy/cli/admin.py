"""Argosy admin CLI (Phase 6).

Tenant management commands. NOT exposed as public HTTP routes — these
are operator-only flows for onboarding new tenants and inspecting the
control plane.

Usage:

    argosy admin tenant create --user-id alice --email alice@example.com
    argosy admin tenant list
    argosy admin watchdog start
"""

from __future__ import annotations

import asyncio
import json

import typer

from argosy.tenancy.onboarding import (
    ensure_tenant_user_row,
    issue_setup_token,
    list_tenants,
    provision_tenant,
)


tenant_app = typer.Typer(
    name="tenant",
    help="Tenant lifecycle (create, list, status).",
    no_args_is_help=True,
)


watchdog_app = typer.Typer(
    name="watchdog",
    help="Operational watchdog (per-tenant health probes).",
    no_args_is_help=True,
)


admin_app = typer.Typer(
    name="admin",
    help="Operator-only commands (tenants, watchdog, telemetry).",
    no_args_is_help=True,
)
admin_app.add_typer(tenant_app, name="tenant")
admin_app.add_typer(watchdog_app, name="watchdog")


@tenant_app.command("create")
def tenant_create(
    user_id: str = typer.Option(..., "--user-id", "-u"),
    email: str = typer.Option(..., "--email", "-e"),
    plan: str = typer.Option("free", "--plan"),
) -> None:
    """Provision a new tenant; print a setup token for first login."""

    async def _run() -> None:
        tenant = await provision_tenant(user_id, email, plan=plan)
        await ensure_tenant_user_row(user_id, email=email)
        token = await issue_setup_token(user_id)
        out = {
            "user_id": tenant.user_id,
            "email": email,
            "plan": tenant.plan,
            "db_path": tenant.db_path,
            "setup_token": token,
            "next_step": (
                f"Visit /onboarding?token={token} to complete first login."
            ),
        }
        typer.echo(json.dumps(out, indent=2))

    asyncio.run(_run())


@tenant_app.command("list")
def tenant_list() -> None:
    """List all provisioned tenants."""

    async def _run() -> None:
        rows = await list_tenants()
        out = [
            {
                "user_id": t.user_id,
                "plan": t.plan,
                "status": t.status,
                "db_path": t.db_path,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "last_active_at": (
                    t.last_active_at.isoformat() if t.last_active_at else None
                ),
            }
            for t in rows
        ]
        typer.echo(json.dumps(out, indent=2))

    asyncio.run(_run())


@watchdog_app.command("start")
def watchdog_start(
    user_id: str = typer.Option(..., "--user-id", "-u"),
    interval: int = typer.Option(60, "--interval-seconds"),
    once: bool = typer.Option(False, "--once", help="Run a single probe and exit."),
) -> None:
    """Start the per-tenant watchdog loop (SDD §14.2).

    For Phase 6 this is a long-running CLI command; for hosted
    deploy we re-use the same code as a sidecar container.
    """
    from argosy.orchestrator.watchdog import run_watchdog

    asyncio.run(run_watchdog(user_id=user_id, interval_seconds=interval, once=once))


__all__ = ["admin_app", "tenant_app", "watchdog_app"]
