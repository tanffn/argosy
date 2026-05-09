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
# backfill
# ---------------------------------------------------------------------------

@app.command("backfill")
def backfill(
    user_id: str = typer.Option(..., "--user-id"),
    dir: Path = typer.Option(..., "--dir", exists=True),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Bulk-ingest every recognized statement file under <dir> for <user_id>.

    Idempotent — re-running on the same tree produces zero new rows.
    """
    files = [p for p in dir.rglob("*") if p.is_file()
             and p.suffix.lower() in {".xls", ".xlsx"}]
    typer.echo(f"Found {len(files)} files (.xls/.xlsx) under {dir}")
    if dry_run:
        for p in files:
            typer.echo(f"  would ingest: {p}")
        return

    # Real ingest path. Build a sync session pointing at the configured DB.
    from argosy.config import reload_settings, get_settings
    reload_settings()
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    from argosy.state.models import Base
    sync_url = f"sqlite:///{settings.db_file}"
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    from argosy.services.expense_ingest.orchestrator import ingest_user_file

    successes = 0
    failures = 0
    with SessionLocal() as s:
        # Ensure the user row exists (FK)
        from argosy.state.models import User
        if s.get(User, user_id) is None:
            s.add(User(id=user_id, plan="free"))
            s.commit()
        for p in files:
            try:
                contents = p.read_bytes()
                user_file = _maybe_async_catalog_upload(
                    s, user_id=user_id, original_name=p.name,
                    contents=contents,
                )
                s.commit()
                ingest_user_file(s, user_id, user_file.id)
                s.commit()
                successes += 1
                typer.echo(f"  OK {p.name}")
            except Exception as e:
                s.rollback()
                failures += 1
                typer.echo(f"  FAIL {p.name}: {e}")

    typer.echo(f"\nDone. successes={successes} failures={failures}")


def _maybe_async_catalog_upload(s, *, user_id, original_name, contents):
    """Adapter for ``catalog_upload`` whether sync or async."""
    import inspect
    from argosy.services.file_catalog import catalog_upload
    if inspect.iscoroutinefunction(catalog_upload):
        import asyncio
        # catalog_upload is async (T19) — manages its own DB session via
        # db_mod.get_session(). Does NOT take a SQLAlchemy session argument.
        return asyncio.run(catalog_upload(
            user_id=user_id, original_name=original_name,
            raw_bytes=contents, mime_type="application/octet-stream",
            kind="other", source="chat_attachment",
        ))
    return catalog_upload(
        s, user_id=user_id, original_name=original_name,
        contents=contents, mime_type="application/octet-stream",
        kind="other", source="chat_attachment",
    )


# ---------------------------------------------------------------------------
# issuer-coverage
# ---------------------------------------------------------------------------

@app.command("issuer-coverage")
def issuer_coverage() -> None:
    """List Max-card ענף values seen in DB but not in the unambiguous map."""
    from argosy.services.expense_ingest.issuer_seed import (
        _UNAMBIGUOUS, _AMBIGUOUS,
    )
    import json as _json
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    from argosy.config import reload_settings, get_settings
    from argosy.state.models import ExpenseTransaction

    reload_settings()
    settings = get_settings()
    if not settings.db_file.exists():
        typer.echo("No DB found. Run an ingest first.")
        return
    sync_url = f"sqlite:///{settings.db_file}"
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    seen: dict[str, int] = {}
    with SessionLocal() as s:
        for tx in s.query(ExpenseTransaction).all():
            try:
                data = _json.loads(tx.raw_row_json)
            except Exception:
                continue
            anaf = data.get("anaf") if isinstance(data, dict) else None
            if not anaf:
                continue
            seen[anaf] = seen.get(anaf, 0) + 1

    unmapped = {a: n for a, n in seen.items()
                if a not in _UNAMBIGUOUS and a not in _AMBIGUOUS}
    if not unmapped:
        typer.echo("All ענף values are mapped.")
        return
    typer.echo("Unmapped ענף values (extend issuer_seed._UNAMBIGUOUS / _AMBIGUOUS):")
    for anaf, n in sorted(unmapped.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {anaf:30s}  {n} txs")


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
    # Foreign rows have amount_nis=None (Bug 2 fix); they are excluded from
    # the NIS-only debit/credit oracle reconciliation.
    debits = sum(t.amount_nis for t in result.transactions
                 if t.direction == "debit" and t.amount_nis is not None)
    credits = sum(t.amount_nis for t in result.transactions
                  if t.direction == "credit" and t.amount_nis is not None)

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
