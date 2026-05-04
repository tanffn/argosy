"""`argosy data ...` — investor-event data adapters (Phase 4).

Subcommand groups:

  argosy data 13f recent --days 30
  argosy data 13f filer 0001067983 --quarters 4
  argosy data form4 ticker NVDA --days 30
  argosy data form4 filer 0001067983 --days 90
  argosy data politicians --days 30
  argosy data politicians --ticker NVDA
  argosy data analyst NVDA

All commands print a short text table by default and JSON with ``--json``.
On adapter failure the CLI catches ``MissingDataSourceError`` and exits
with code 2 — same convention as `argosy gemelnet`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.capitoltrades_adapter import CapitolTradesAdapter
from argosy.adapters.data.sec_13f_adapter import Sec13FAdapter
from argosy.adapters.data.sec_form4_adapter import SecForm4Adapter
from argosy.adapters.data.tipranks_adapter import TipRanksAdapter
from argosy.logging import configure_logging

app = typer.Typer(
    name="data",
    help="Investor-event data feeds: 13F, Form 4, capitoltrades, tipranks.",
    no_args_is_help=True,
)


# Sub-app: argosy data 13f ...
sec13f_app = typer.Typer(name="13f", help="SEC 13F-HR filings.", no_args_is_help=True)
form4_app = typer.Typer(name="form4", help="SEC Form 4 insider transactions.",
                        no_args_is_help=True)
app.add_typer(sec13f_app, name="13f")
app.add_typer(form4_app, name="form4")


# ----------------------------------------------------------------------
# Adapter factories — replaceable in tests via ``monkeypatch.setattr``.
# ----------------------------------------------------------------------


def _sec13f() -> Sec13FAdapter:
    return Sec13FAdapter()


def _form4() -> SecForm4Adapter:
    return SecForm4Adapter()


def _capitoltrades() -> CapitolTradesAdapter:
    return CapitolTradesAdapter()


def _tipranks() -> TipRanksAdapter:
    return TipRanksAdapter()


# ----------------------------------------------------------------------
# 13F commands
# ----------------------------------------------------------------------


@sec13f_app.command("recent")
def thirteen_f_recent_cmd(
    days: int = typer.Option(30, "--days"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Recent 13F-HR filings across all filers."""
    configure_logging()

    async def _run() -> int:
        adapter = _sec13f()
        try:
            rows = await adapter.list_recent_13f(days=days)
        except (MissingDataSourceError, ValueError) as exc:
            typer.echo(f"sec_13f error: {exc}", err=True)
            return 2
        return _emit_rows(rows, as_json=as_json, columns=(
            "filed_at", "fund_name", "cik", "period_of_report", "accession_number"
        ))

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


@sec13f_app.command("filer")
def thirteen_f_filer_cmd(
    cik: str = typer.Argument(..., help="SEC CIK; e.g. 0001067983 (Berkshire)."),
    quarters: int = typer.Option(4, "--quarters"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """13F-HR filings for one filer over time."""
    configure_logging()

    async def _run() -> int:
        adapter = _sec13f()
        try:
            rows = await adapter.get_filer_history(cik, quarters=quarters)
        except (MissingDataSourceError, ValueError) as exc:
            typer.echo(f"sec_13f error: {exc}", err=True)
            return 2
        return _emit_rows(rows, as_json=as_json, columns=(
            "period_of_report", "filed_at", "fund_name", "accession_number"
        ))

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


# ----------------------------------------------------------------------
# Form 4 commands
# ----------------------------------------------------------------------


@form4_app.command("ticker")
def form4_ticker_cmd(
    ticker: str = typer.Argument(..., help="Issuer ticker; e.g. NVDA."),
    days: int = typer.Option(30, "--days"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Form 4 insider transactions on one ticker."""
    configure_logging()

    async def _run() -> int:
        adapter = _form4()
        try:
            rows = await adapter.get_recent_form4_for_ticker(ticker, days=days)
        except (MissingDataSourceError, ValueError) as exc:
            typer.echo(f"sec_form4 error: {exc}", err=True)
            return 2
        return _emit_rows(rows, as_json=as_json, columns=(
            "transaction_date", "filer_name", "role", "transaction_code",
            "shares", "price_per_share", "value_usd"
        ))

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


@form4_app.command("filer")
def form4_filer_cmd(
    cik: str = typer.Argument(..., help="Filer CIK."),
    days: int = typer.Option(90, "--days"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Form 4 transactions filed by one CIK across all issuers."""
    configure_logging()

    async def _run() -> int:
        adapter = _form4()
        try:
            rows = await adapter.get_recent_form4_for_filer(cik, days=days)
        except (MissingDataSourceError, ValueError) as exc:
            typer.echo(f"sec_form4 error: {exc}", err=True)
            return 2
        return _emit_rows(rows, as_json=as_json, columns=(
            "transaction_date", "ticker", "transaction_code",
            "shares", "price_per_share", "value_usd"
        ))

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


# ----------------------------------------------------------------------
# Politicians (capitoltrades) command
# ----------------------------------------------------------------------


@app.command("politicians")
def politicians_cmd(
    days: int = typer.Option(30, "--days"),
    ticker: str | None = typer.Option(None, "--ticker"),
    politician: str | None = typer.Option(None, "--politician",
                                          help="Politician slug, e.g. nancy-pelosi."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """STOCK Act trades from capitoltrades.com.

    Mutually-exclusive filters: --ticker / --politician / (default: recent
    across everyone within --days).
    """
    configure_logging()

    async def _run() -> int:
        adapter = _capitoltrades()
        try:
            if ticker:
                rows = await adapter.list_trades_for_ticker(ticker, days=days)
            elif politician:
                rows = await adapter.list_trades_for_politician(politician)
            else:
                rows = await adapter.list_recent_trades(days=days)
        except (MissingDataSourceError, ValueError) as exc:
            typer.echo(f"capitoltrades error: {exc}", err=True)
            return 2
        return _emit_rows(rows, as_json=as_json, columns=(
            "transaction_date", "politician_name", "party", "state",
            "ticker", "transaction_type", "amount_range",
        ))

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


# ----------------------------------------------------------------------
# Analyst (tipranks) command
# ----------------------------------------------------------------------


@app.command("analyst")
def analyst_cmd(
    ticker: str = typer.Argument(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """TipRanks analyst consensus + blogger + hedge-fund signal for one ticker."""
    configure_logging()

    async def _run() -> int:
        adapter = _tipranks()
        out: dict[str, Any] = {"ticker": ticker.upper()}
        try:
            out["analyst"] = await adapter.get_analyst_consensus(ticker)
        except (MissingDataSourceError, ValueError) as exc:
            typer.echo(f"tipranks consensus error: {exc}", err=True)
            return 2
        try:
            out["blogger"] = await adapter.get_blogger_sentiment(ticker)
        except MissingDataSourceError as exc:
            out["blogger"] = {"error": str(exc)}
        try:
            out["hedge_fund"] = await adapter.get_hedge_fund_signal(ticker)
        except MissingDataSourceError as exc:
            out["hedge_fund"] = {"error": str(exc)}

        if as_json:
            typer.echo(json.dumps(out, indent=2, default=str))
            return 0
        a = out["analyst"]
        typer.echo(f"{ticker.upper()} — analyst consensus")
        typer.echo(f"  consensus      : {a.get('consensus_label')}")
        typer.echo(f"  avg PT         : {a.get('average_price_target')}")
        typer.echo(
            f"  buy/hold/sell  : "
            f"{a.get('num_buy')} / {a.get('num_hold')} / {a.get('num_sell')}"
        )
        b = out.get("blogger") or {}
        typer.echo(
            f"  blogger        : bullish={b.get('bullish_pct')}  "
            f"bearish={b.get('bearish_pct')}"
        )
        h = out.get("hedge_fund") or {}
        typer.echo(
            f"  hedge funds    : holding={h.get('hedge_funds_holding')}  "
            f"change={h.get('recent_change')}"
        )
        return 0

    rc = asyncio.run(_run())
    if rc != 0:
        raise typer.Exit(code=rc)


# ----------------------------------------------------------------------
# Tiny shared helper
# ----------------------------------------------------------------------


def _emit_rows(
    rows: list[dict[str, Any]],
    *,
    as_json: bool,
    columns: tuple[str, ...],
) -> int:
    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        typer.echo("(no rows)")
        return 0
    # Two-pass column-width calculation, then plain text.
    widths = {c: max(len(c), max((len(str(r.get(c) or "")) for r in rows), default=0))
              for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    typer.echo(header)
    typer.echo("  ".join("-" * widths[c] for c in columns))
    for r in rows:
        typer.echo("  ".join(str(r.get(c) or "").ljust(widths[c]) for c in columns))
    typer.echo(f"({len(rows)} row(s))")
    return 0


__all__ = ["app"]
