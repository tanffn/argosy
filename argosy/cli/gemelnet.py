"""`argosy gemelnet ...` — Israeli Ministry of Finance pension data.

Subcommands:

  argosy gemelnet list                   list all funds (filter --type)
  argosy gemelnet returns <fund_id>      show one fund's returns
  argosy gemelnet search "<query>"       fuzzy-search by name/manager
  argosy gemelnet refresh-user           refresh `identity.pensions` and
                                         persist `pension_fund_snapshots`

The adapter raises `MissingDataSourceError` when the public site is
unreachable; the CLI catches it and exits with code 2 so callers / cron
loops get a non-zero exit without an unhelpful traceback.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer
import yaml

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.gemelnet_adapter import (
    GemelnetAdapter,
    persist_pension_snapshot,
)
from argosy.logging import configure_logging
from argosy.state import db as db_mod

app = typer.Typer(name="gemelnet", help="Israeli pension data (gemelnet.mof.gov.il).",
                  no_args_is_help=True)


def _adapter() -> GemelnetAdapter:
    """Construct the default adapter; tests override via DI in the
    adapter constructor and call functions directly rather than going
    through the CLI."""
    return GemelnetAdapter()


@app.command("list")
def list_cmd(
    fund_type: str | None = typer.Option(
        None, "--type",
        help="Filter: kupat_gemel | keren_hishtalmut | kupat_pensia",
    ),
    limit: int = typer.Option(50, "--limit", help="Max rows to print."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """List all funds known to the MoF gemelnet site."""
    configure_logging()

    async def _run() -> int:
        adapter = _adapter()
        try:
            funds = await adapter.list_funds(fund_type=fund_type)
        except MissingDataSourceError as exc:
            typer.echo(f"gemelnet unavailable: {exc}", err=True)
            return 2
        funds = funds[:limit]
        if as_json:
            typer.echo(json.dumps(funds, ensure_ascii=False, indent=2))
            return 0
        for f in funds:
            typer.echo(
                f"  {f.get('fund_id', ''):>8}  "
                f"{(f.get('type') or '?'):<18}  "
                f"{(f.get('name') or '')[:60]:<60}  "
                f"{f.get('manager') or ''}"
            )
        typer.echo(f"({len(funds)} fund(s))")
        return 0

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


@app.command("returns")
def returns_cmd(
    fund_id: str = typer.Argument(..., help="MoF fund_id."),
    period: str = typer.Option("12m", "--period",
                               help="One of: 12m | 36m | 60m | ytd"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show returns for a single fund."""
    configure_logging()

    async def _run() -> int:
        adapter = _adapter()
        try:
            payload = await adapter.get_fund_returns(fund_id, period=period)
        except (MissingDataSourceError, ValueError) as exc:
            typer.echo(f"gemelnet error: {exc}", err=True)
            return 2
        if as_json:
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        typer.echo(f"Fund {payload.get('fund_id')}  ({payload.get('fund_name') or '—'})")
        typer.echo(f"  manager           : {payload.get('manager') or '—'}")
        typer.echo(f"  type              : {payload.get('fund_type') or '—'}")
        typer.echo(f"  period            : {payload.get('period')}")
        typer.echo(f"  return_pct        : {payload.get('return_pct')}")
        typer.echo(f"  benchmark_pct     : {payload.get('benchmark_return_pct')}")
        typer.echo(f"  relative_pct      : {payload.get('relative_to_benchmark_pct')}")
        typer.echo(f"  last_updated      : {payload.get('last_updated') or '—'}")
        typer.echo(f"  source_url        : {payload.get('source_url')}")
        return 0

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


@app.command("search")
def search_cmd(
    query: str = typer.Argument(..., help="Fund name / manager substring."),
    limit: int = typer.Option(10, "--limit"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Fuzzy-search funds by name + manager."""
    configure_logging()

    async def _run() -> int:
        adapter = _adapter()
        try:
            hits = await adapter.search_funds(query, limit=limit)
        except MissingDataSourceError as exc:
            typer.echo(f"gemelnet unavailable: {exc}", err=True)
            return 2
        if as_json:
            typer.echo(json.dumps(hits, ensure_ascii=False, indent=2))
            return 0
        for f in hits:
            typer.echo(
                f"  {f.get('fund_id', ''):>8}  "
                f"{(f.get('type') or '?'):<18}  "
                f"{(f.get('name') or '')[:60]:<60}  "
                f"{f.get('manager') or ''}"
            )
        typer.echo(f"({len(hits)} match(es))")
        return 0

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


@app.command("refresh-user")
def refresh_user_cmd(
    user_id: str = typer.Option("ariel", "--user-id"),
    period: str = typer.Option("12m", "--period"),
) -> None:
    """Refresh every fund in `identity.pensions` and snapshot the data.

    Reads the user's `identity_yaml`, expects an optional
    ``pensions: [{fund_id, fund_name, type, balance_nis,
    last_refreshed}, ...]`` list. For each entry with a ``fund_id``,
    calls the adapter and writes a `pension_fund_snapshots` row.
    Updates the `last_refreshed` timestamp on each entry in
    `identity_yaml`.

    Funds without a ``fund_id`` (e.g. legacy free-form entries) are
    skipped with a warning so users can be onboarded incrementally.
    """
    configure_logging()
    # Engine is lazily initialized by `db_mod.get_session()` on first
    # use against the configured Settings. Tests pre-init the engine
    # to point at a temp DB so we don't force re-init here.

    async def _run() -> int:
        from datetime import datetime, timezone

        from sqlalchemy import select

        from argosy.state.models import UserContext

        adapter = _adapter()
        async with db_mod.get_session() as session:
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == user_id)
                )
            ).scalar_one_or_none()
            if ctx is None:
                typer.echo(f"no user_context for user_id={user_id!r}", err=True)
                return 2
            identity_yaml = ctx.identity_yaml or ""
            try:
                identity: dict[str, Any] = yaml.safe_load(identity_yaml) or {}
            except yaml.YAMLError as exc:
                typer.echo(f"identity_yaml parse failed: {exc}", err=True)
                return 2
            if not isinstance(identity, dict):
                identity = {}
            pensions = identity.get("pensions") or []

            # If `pensions` is the legacy single-string field, we have
            # nothing to refresh; treat as no funds.
            if not isinstance(pensions, list):
                typer.echo(
                    "identity.pensions is not a list yet; "
                    "no funds to refresh. Add fund-level structure first."
                )
                return 0

            refreshed = 0
            skipped = 0
            for entry in pensions:
                if not isinstance(entry, dict):
                    skipped += 1
                    continue
                fund_id = str(entry.get("fund_id") or "").strip()
                if not fund_id:
                    typer.echo(
                        f"  - skipping pension entry without fund_id: "
                        f"{entry.get('fund_name') or entry}"
                    )
                    skipped += 1
                    continue
                try:
                    payload = await adapter.get_fund_returns(fund_id, period=period)
                except (MissingDataSourceError, ValueError) as exc:
                    typer.echo(f"  - {fund_id}: error: {exc}", err=True)
                    skipped += 1
                    continue
                balance_nis = entry.get("balance_nis")
                try:
                    balance_nis_f = (
                        float(balance_nis) if balance_nis is not None else None
                    )
                except (TypeError, ValueError):
                    balance_nis_f = None
                snap_id = await persist_pension_snapshot(
                    user_id=user_id,
                    fund_returns=payload,
                    balance_nis=balance_nis_f,
                )
                entry["last_refreshed"] = datetime.now(timezone.utc).isoformat()
                entry["fund_name"] = entry.get("fund_name") or payload.get("fund_name")
                refreshed += 1
                typer.echo(
                    f"  + {fund_id}: return={payload.get('return_pct')} "
                    f"benchmark={payload.get('benchmark_return_pct')} "
                    f"rel={payload.get('relative_to_benchmark_pct')} "
                    f"snapshot_id={snap_id}"
                )

            # Persist updated identity yaml so `last_refreshed` sticks.
            identity["pensions"] = pensions
            ctx.identity_yaml = yaml.safe_dump(
                identity, allow_unicode=True, sort_keys=False
            )
            await session.commit()

        typer.echo(f"refreshed {refreshed} fund(s); skipped {skipped}.")
        return 0

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


__all__ = ["app"]
