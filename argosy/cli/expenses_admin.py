"""Admin CLI for the expenses subsystem.

Subcommands:
  verify-file <path>     — print oracle vs parser side-by-side
  backfill <dir>         — bulk-ingest a directory tree (Task 24)
  issuer-coverage        — list unmapped Max ענף values seen in DB (Task 24)
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(help="Argosy expenses admin utilities.", no_args_is_help=True)


# ---------------------------------------------------------------------------
# Stubs for Task 24 — registered now so the Typer app stays in multi-command
# mode and dispatches subcommand names correctly.
# ---------------------------------------------------------------------------

@app.command("backfill")
def backfill(
    directory: Path = typer.Argument(..., help="Root directory to scan recursively."),
) -> None:
    """Bulk-ingest a directory tree of statement files (Task 24)."""
    typer.echo("backfill: not yet implemented (Task 24)", err=True)
    raise typer.Exit(code=1)


@app.command("issuer-coverage")
def issuer_coverage() -> None:
    """List unmapped Max ענף values seen in DB (Task 24)."""
    typer.echo("issuer-coverage: not yet implemented (Task 24)", err=True)
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# verify-file
# ---------------------------------------------------------------------------

@app.command("verify-file")
def verify_file(
    path: Path = typer.Argument(..., help="Path to the statement file to verify."),
) -> None:
    """Print oracle vs parser side-by-side for one statement file."""
    from argosy.services.expense_ingest.sniff import (
        detect_format, UnknownFormatError,
    )
    from argosy.services.expense_ingest.types import ParserName
    from argosy.services.expense_ingest.parsers import (
        leumi_osh as p_leumi, isracard as p_isra, max as p_max,
    )
    from tests.expense_ground_truth import (
        leumi_oracle, isracard_oracle, max_oracle,
    )

    try:
        fmt = detect_format(path)
    except UnknownFormatError as e:
        typer.echo(f"File: {path}")
        typer.echo(f"unrecognized format: {e}")
        raise typer.Exit(code=2)

    parser = {
        ParserName.LEUMI_OSH: p_leumi.parse,
        ParserName.ISRACARD:  p_isra.parse,
        ParserName.MAX:       p_max.parse,
    }.get(fmt)
    if parser is None:
        typer.echo(f"no implementation for parser {fmt.value}")
        raise typer.Exit(code=2)

    oracle = {
        ParserName.LEUMI_OSH: leumi_oracle,
        ParserName.ISRACARD:  isracard_oracle,
        ParserName.MAX:       max_oracle,
    }[fmt]

    truth = oracle(path)
    result = parser(path)
    debits = sum(t.amount_nis for t in result.transactions
                 if t.direction == "debit")
    credits = sum(t.amount_nis for t in result.transactions
                  if t.direction == "credit")

    typer.echo(f"File:    {path}")
    typer.echo(f"Format:  {fmt.value}")
    typer.echo("Oracle:")
    typer.echo(f"  rows           {truth.row_count}")
    typer.echo(f"  sum_debits     {truth.sum_debits_nis}")
    typer.echo(f"  sum_credits    {truth.sum_credits_nis}")
    typer.echo(f"  declared_total {truth.declared_total_nis}")
    typer.echo("Parser:")

    def mark(actual, expected, tol=1.0) -> str:
        return "✓" if abs(actual - expected) <= tol else "✗"

    typer.echo(f"  rows           {len(result.transactions)} "
               f"{chr(0x2713) if len(result.transactions) == truth.row_count else chr(0x2717)}")
    typer.echo(f"  sum_debits     {round(debits, 2)} "
               f"{mark(debits, truth.sum_debits_nis)}")
    typer.echo(f"  sum_credits    {round(credits, 2)} "
               f"{mark(credits, truth.sum_credits_nis)}")
    if truth.declared_total_nis is not None:
        typer.echo(f"  parsed_total   {round(float(result.statement.parsed_total_nis), 2)} "
                   f"{mark(float(result.statement.parsed_total_nis), truth.declared_total_nis, 50.0)}")

    rows_ok = len(result.transactions) == truth.row_count
    debit_ok = abs(debits - truth.sum_debits_nis) <= 1.0
    credit_ok = abs(credits - truth.sum_credits_nis) <= 1.0
    # declared_total comparison is informational only — the issuer footer may
    # exclude foreign-currency sub-totals or rounding adjustments that cause
    # the parsed NIS total to diverge legitimately from the declared figure.

    if rows_ok and debit_ok and credit_ok:
        typer.echo("Status: PASS")
        raise typer.Exit(code=0)
    else:
        typer.echo("Status: FAIL")
        raise typer.Exit(code=1)
