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
from datetime import UTC
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


# Canonical fund-type vehicle keys, matching
# `gemelnet_adapter.HEBREW_TYPE_MAP.values()`. Used by `refresh-user` and
# `update_user_pension_holdings` to bucket fund-level data into the right
# vehicle slot under `identity.pensions`.
_VEHICLE_KEYS: tuple[str, ...] = ("keren_hishtalmut", "kupat_gemel", "kupat_pensia")


def _coerce_vehicle_key(raw: Any) -> str | None:
    """Map a free-form ``type`` value to a canonical vehicle key, or None."""
    if not raw:
        return None
    s = str(raw).strip().lower()
    if s in _VEHICLE_KEYS:
        return s
    # Tolerate the Hebrew labels and some common aliases.
    aliases = {
        "קרן השתלמות": "keren_hishtalmut",
        "hishtalmut": "keren_hishtalmut",
        "קופת גמל": "kupat_gemel",
        "gemel": "kupat_gemel",
        "kupat gemel": "kupat_gemel",
        "פנסיה": "kupat_pensia",
        "קרן פנסיה": "kupat_pensia",
        "pensia": "kupat_pensia",
        "pension": "kupat_pensia",
    }
    return aliases.get(s) or aliases.get(str(raw).strip())


def _ensure_vehicle_dict(pensions: Any) -> dict[str, Any]:
    """Coerce ``pensions`` (list, dict, or other) into a vehicle-keyed dict.

    Migration helper: legacy ``identity.pensions`` was a flat list of fund
    entries; the gap-tracker now expects a dict keyed by vehicle
    (``keren_hishtalmut`` / ``kupat_gemel`` / ``kupat_pensia``). This
    converts on the fly so the CLI can be re-run on either shape.

    Each vehicle dict carries:
      - ``balance_nis``: aggregated across funds of that vehicle
      - ``contribution_rate_pct`` / ``employer_match_pct``: first-seen
      - ``funds``: list of {fund_id, fund_name, last_refreshed_at, ...}
    """
    if isinstance(pensions, dict):
        # Already-dict shape — pass through (we'll still merge fresh
        # snapshots into the right vehicle's `funds` list below).
        out = dict(pensions)
        for vk in _VEHICLE_KEYS:
            if vk in out and not isinstance(out[vk], dict):
                # Defensive: somebody handwrote a scalar at a vehicle key.
                out[vk] = {}
        return out
    if not isinstance(pensions, list):
        return {}

    # List → dict migration. Group entries by vehicle, aggregating
    # ``balance_nis`` and merging fund-level metadata.
    out: dict[str, Any] = {}
    for entry in pensions:
        if not isinstance(entry, dict):
            continue
        vk = _coerce_vehicle_key(entry.get("type"))
        if vk is None:
            # Unknown type → drop into a generic bucket so we don't lose data.
            vk = "kupat_gemel"  # safest default — locked-till-retirement
        bucket = out.setdefault(vk, {"funds": []})
        # Aggregate balance.
        bal = entry.get("balance_nis")
        try:
            bal_f = float(bal) if bal is not None else None
        except (TypeError, ValueError):
            bal_f = None
        if bal_f is not None:
            bucket["balance_nis"] = (bucket.get("balance_nis") or 0.0) + bal_f
        # First-seen contribution rates.
        for k in ("contribution_rate_pct", "employer_match_pct"):
            if entry.get(k) is not None and bucket.get(k) is None:
                bucket[k] = entry.get(k)
        # Append the fund-level entry.
        fund_record = {
            "fund_id": entry.get("fund_id"),
            "fund_name": entry.get("fund_name"),
        }
        if entry.get("last_refreshed"):
            fund_record["last_refreshed_at"] = entry.get("last_refreshed")
        bucket.setdefault("funds", []).append(fund_record)
    return out


@app.command("refresh-user")
def refresh_user_cmd(
    user_id: str = typer.Option("ariel", "--user-id"),
    period: str = typer.Option("12m", "--period"),
) -> None:
    """Refresh every fund in `identity.pensions` and snapshot the data.

    Reads the user's `identity_yaml`. ``identity.pensions`` is a dict
    keyed by canonical vehicle (``keren_hishtalmut`` / ``kupat_gemel``
    / ``kupat_pensia``); each value carries an aggregated
    ``balance_nis`` plus a ``funds`` list of
    ``{fund_id, fund_name, last_refreshed_at}`` entries. The legacy
    flat-list shape is migrated on the fly (and persisted back as dict)
    so re-running the CLI is idempotent.

    For each fund with a ``fund_id``, calls the adapter and writes a
    `pension_fund_snapshots` row. Funds without a ``fund_id`` (e.g.
    legacy free-form entries) are skipped with a warning.
    """
    configure_logging()
    # Engine is lazily initialized by `db_mod.get_session()` on first
    # use against the configured Settings. Tests pre-init the engine
    # to point at a temp DB so we don't force re-init here.

    async def _run() -> int:
        from datetime import datetime

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
            pensions_raw = identity.get("pensions")

            if pensions_raw is None:
                typer.echo(
                    "identity.pensions is empty; "
                    "no funds to refresh. Add fund-level structure first."
                )
                return 0

            pensions = _ensure_vehicle_dict(pensions_raw)

            refreshed = 0
            skipped = 0
            for vk in _VEHICLE_KEYS:
                bucket = pensions.get(vk)
                if not isinstance(bucket, dict):
                    continue
                funds = bucket.get("funds") or []
                if not isinstance(funds, list):
                    continue
                for fund in funds:
                    if not isinstance(fund, dict):
                        skipped += 1
                        continue
                    fund_id = str(fund.get("fund_id") or "").strip()
                    if not fund_id:
                        typer.echo(
                            f"  - skipping {vk} fund without fund_id: "
                            f"{fund.get('fund_name') or fund}"
                        )
                        skipped += 1
                        continue
                    try:
                        payload = await adapter.get_fund_returns(
                            fund_id, period=period
                        )
                    except (MissingDataSourceError, ValueError) as exc:
                        typer.echo(f"  - {fund_id}: error: {exc}", err=True)
                        skipped += 1
                        continue
                    # Per-fund balance is only meaningful at the
                    # vehicle aggregate level; we pass the bucket's
                    # aggregated balance through to the snapshot if a
                    # fund-specific value isn't available.
                    balance_nis = fund.get("balance_nis")
                    if balance_nis is None:
                        balance_nis = bucket.get("balance_nis")
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
                    fund["last_refreshed_at"] = datetime.now(
                        UTC
                    ).isoformat()
                    fund["fund_name"] = (
                        fund.get("fund_name") or payload.get("fund_name")
                    )
                    refreshed += 1
                    typer.echo(
                        f"  + {fund_id} ({vk}): "
                        f"return={payload.get('return_pct')} "
                        f"benchmark={payload.get('benchmark_return_pct')} "
                        f"rel={payload.get('relative_to_benchmark_pct')} "
                        f"snapshot_id={snap_id}"
                    )

            # Persist updated identity yaml in the new dict shape so
            # `last_refreshed_at` sticks AND the legacy list-shape gets
            # auto-migrated on first refresh.
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


def update_user_pension_holdings(
    identity: dict[str, Any], fund_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Public helper: merge a list of fund rows into ``identity.pensions``.

    Used by callers (CLI, ingest) that have a flat list of
    ``{fund_id, fund_name, type, balance_nis}`` rows and need to write
    them into the canonical dict-keyed-by-vehicle shape under
    ``identity.pensions``. Returns a new identity dict (does not mutate
    the input).
    """
    out = dict(identity)
    existing = _ensure_vehicle_dict(out.get("pensions") or {})
    for row in fund_rows or []:
        if not isinstance(row, dict):
            continue
        vk = _coerce_vehicle_key(row.get("type"))
        if vk is None:
            vk = "kupat_gemel"
        bucket = existing.setdefault(vk, {"funds": []})
        bal = row.get("balance_nis")
        try:
            bal_f = float(bal) if bal is not None else None
        except (TypeError, ValueError):
            bal_f = None
        if bal_f is not None:
            bucket["balance_nis"] = (bucket.get("balance_nis") or 0.0) + bal_f
        for k in ("contribution_rate_pct", "employer_match_pct"):
            if row.get(k) is not None and bucket.get(k) is None:
                bucket[k] = row.get(k)
        bucket.setdefault("funds", []).append(
            {
                "fund_id": row.get("fund_id"),
                "fund_name": row.get("fund_name"),
            }
        )
    out["pensions"] = existing
    return out


__all__ = ["app", "update_user_pension_holdings"]
