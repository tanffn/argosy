"""Admin CLI for the expenses subsystem.

Subcommands:
  verify-file <path>     — print oracle vs parser side-by-side
  backfill <dir>         — bulk-ingest a directory tree (Task 24)
  issuer-coverage        — list unmapped Max ענף values seen in DB (Task 24)
  audit-corpus <dir>     — deterministic count comparison: oracle vs parser
                           vs DB. NEVER calls the LLM. Read-only diagnostic.
  verify-rsu             — cross-validate Schwab Equity Awards Center
                           disbursements against Leumi USD account credits.
                           Read-only — never writes to the DB.
"""

from __future__ import annotations

from pathlib import Path

import typer

# Module-level import so tests can monkeypatch
# `argosy.cli.expenses_admin.ingest_user_file` directly.
from argosy.services.expense_ingest.orchestrator import ingest_user_file

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
                # Infer card last-4 from parent folder name when present
                # (corpus convention: <root>/Cards/<Issuer>/<last4>/<file>).
                parent = p.parent.name
                last4_hint = parent if parent.isdigit() and len(parent) == 4 else None
                user_file = _maybe_async_catalog_upload(
                    s, user_id=user_id, original_name=p.name,
                    contents=contents,
                )
                s.commit()
                ingest_user_file(s, user_id, user_file.id, last4_hint=last4_hint)
                s.commit()
                successes += 1
                typer.echo(f"  OK {p.name}" + (f" (last4={last4_hint})" if last4_hint else ""))
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


# ---------------------------------------------------------------------------
# audit-corpus
# ---------------------------------------------------------------------------

@app.command("audit-corpus")
def audit_corpus(
    user_id: str = typer.Option(..., "--user-id"),
    dir: Path = typer.Option(..., "--dir", exists=True),
) -> None:
    """Deterministic per-file audit: oracle vs parser vs DB row counts.

    NEVER calls the LLM. Read-only — does not write to the DB. For each
    .xls/.xlsx file under <dir>:

      1. detect_format → parser_name (or UnknownFormatError → '?')
      2. parser.parse(file) → row count + debit/credit sums (or '✗' on raise)
      3. oracle from tests/expense_ground_truth.py → ground-truth counts/sums
      4. DB query: count expense_transactions in expense_statements joined
         on (user_id, parser_name, period overlapping parsed period)

    Pass criteria (✓):
      - oracle.row_count == parsed row count
      - |oracle.sum_debits - parsed debits| <= 1.0
      - |oracle.sum_credits - parsed credits| <= 1.0
      - if oracle.declared_total_nis is not None:
          |parsed_total - declared| <= 50.0

    Footer prints a per-source totals table (Files, Files OK, rows
    oracle/parsed/DB, discrepancy notes).
    """
    # Imports kept inside the function so the module loads without a DB on
    # disk (the issuer-coverage / backfill commands also do this).
    from argosy.services.expense_ingest.sniff import (
        detect_format, UnknownFormatError,
    )
    from argosy.services.expense_ingest.types import ParserName
    from argosy.services.expense_ingest.parsers import (
        leumi_osh as p_leumi, leumi_usd as p_leumi_usd,
        isracard as p_isra, max as p_max,
        discount as p_discount,
    )
    # tests/expense_ground_truth.py is the deterministic oracle; add the repo
    # root to sys.path so imports work whether the user invokes via the
    # installed `argosy` script or `python -m argosy.cli.expenses_admin`.
    import sys as _sys
    from pathlib import Path as _P
    _repo_root = _P(__file__).resolve().parents[2]
    if str(_repo_root) not in _sys.path:
        _sys.path.insert(0, str(_repo_root))
    try:
        from tests.expense_ground_truth import (  # type: ignore
            leumi_oracle, leumi_usd_oracle,
            isracard_oracle, max_oracle, discount_oracle,
        )
    except ImportError as e:
        typer.echo(
            "BLOCKED: cannot import tests.expense_ground_truth "
            f"({e}). Ensure repo root is on PYTHONPATH (e.g. run from the "
            "project directory or set PYTHONPATH=./)."
        )
        raise typer.Exit(code=2)

    parser_for: dict[ParserName, callable] = {
        ParserName.LEUMI_OSH: p_leumi.parse,
        ParserName.LEUMI_USD: p_leumi_usd.parse,
        ParserName.ISRACARD:  p_isra.parse,
        ParserName.MAX:       p_max.parse,
        ParserName.DISCOUNT:  p_discount.parse,
    }
    oracle_for: dict[ParserName, callable] = {
        ParserName.LEUMI_OSH: leumi_oracle,
        ParserName.LEUMI_USD: leumi_usd_oracle,
        ParserName.ISRACARD:  isracard_oracle,
        ParserName.MAX:       max_oracle,
        ParserName.DISCOUNT:  discount_oracle,
    }

    files = sorted(
        p for p in dir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".xls", ".xlsx"}
    )
    typer.echo(f"Found {len(files)} files (.xls/.xlsx) under {dir}")
    typer.echo("")

    # Optional DB session — soft-fail if no DB exists. We still run oracle vs
    # parser comparisons; DB rows column is rendered as 'n/a'.
    SessionLocal = None
    db_open_error: str | None = None
    try:
        from argosy.config import reload_settings, get_settings
        import sqlalchemy as sa
        from sqlalchemy.orm import sessionmaker
        from argosy.state.models import Base as _Base  # noqa: F401
        reload_settings()
        settings = get_settings()
        if settings.db_file.exists():
            sync_url = f"sqlite:///{settings.db_file}"
            engine = sa.create_engine(
                sync_url, connect_args={"check_same_thread": False},
            )
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        else:
            db_open_error = "no DB file at " + str(settings.db_file)
    except Exception as e:                       # pragma: no cover
        db_open_error = f"DB unavailable: {e}"

    # Aggregations keyed by source label, e.g. 'isracard 0235'. The label is
    # inferred from the parser_name + the parent folder when the corpus
    # follows the curated layout (`<root>/<issuer>_<last4>/...`); otherwise
    # we fall back to just the parser_name.
    class _Bucket:
        __slots__ = ("files", "files_ok", "rows_oracle", "rows_parsed",
                     "rows_db", "notes")

        def __init__(self) -> None:
            self.files = 0
            self.files_ok = 0
            self.rows_oracle = 0
            self.rows_parsed = 0
            self.rows_db = 0
            self.notes: list[str] = []

    buckets: dict[str, _Bucket] = {}
    total = _Bucket()

    def _bucket(label: str) -> _Bucket:
        b = buckets.get(label)
        if b is None:
            b = _Bucket()
            buckets[label] = b
        return b

    def _label_for(path: Path, parser_name: str, external_id: str | None = None) -> str:
        # Prefer the parser's parsed external_id (e.g. Leumi account
        # '44745280') when available — that's the canonical identifier.
        # Otherwise fall back to folder-name digits (curated corpus
        # convention: <root>/<issuer>_<last4>/<file>; cards backfill
        # convention: <root>/Cards/<Issuer>/<last4>/<file>).
        if external_id and any(ch.isdigit() for ch in external_id):
            return f"{parser_name} {external_id}"
        parent = path.parent.name
        grand = path.parent.parent.name if path.parent.parent else ""
        cand = parent if any(ch.isdigit() for ch in parent) else grand
        if not cand or not any(ch.isdigit() for ch in cand):
            return parser_name
        digits = "".join(ch for ch in cand if ch.isdigit())
        return f"{parser_name} {digits}"

    n_unknown = 0
    n_parser_error = 0
    n_ok = 0
    n_files = len(files)

    for p in files:
        # ----- 1. sniff -----
        try:
            fmt = detect_format(p)
        except UnknownFormatError as e:
            typer.echo(f"??  {p.name} — UnknownFormatError: {e}")
            n_unknown += 1
            # Bucket label is just 'unrecognized' — files don't have a parser.
            b = _bucket("unrecognized")
            b.files += 1
            b.notes.append(p.name)
            continue
        except Exception as e:
            typer.echo(f"??  {p.name} — sniff failed: {e}")
            n_unknown += 1
            b = _bucket("unrecognized")
            b.files += 1
            b.notes.append(p.name)
            continue

        parser = parser_for.get(fmt)
        oracle = oracle_for.get(fmt)
        if parser is None or oracle is None:
            typer.echo(f"??  {p.name} — no implementation for {fmt.value}")
            n_unknown += 1
            b = _bucket(f"{fmt.value}")
            b.files += 1
            b.notes.append(f"{p.name} (no parser/oracle wired)")
            continue

        # ----- 2. parse -----
        try:
            result = parser(p)
        except Exception as e:
            typer.echo(f"✗  {p.name} — {type(e).__name__}: {e}")
            n_parser_error += 1
            b = _bucket(_label_for(p, fmt.value))
            b.files += 1
            b.notes.append(f"{p.name}: parser raised {type(e).__name__}")
            continue

        # ----- 3. oracle -----
        try:
            truth = oracle(p)
        except Exception as e:
            typer.echo(f"✗  {p.name} — oracle raised {type(e).__name__}: {e}")
            b = _bucket(_label_for(p, fmt.value))
            b.files += 1
            b.notes.append(f"{p.name}: oracle raised {type(e).__name__}")
            continue

        parsed_rows = len(result.transactions)
        # For foreign-currency parsers (e.g. LEUMI_USD) every row has
        # amount_nis=None and the comparable scalar is amount_orig (USD).
        # We fall back to amount_orig when no row carries amount_nis;
        # mixed-currency parsers (Isracard) keep their NIS-only sums by
        # design — foreign rows are excluded from the oracle, too.
        all_amount_nis_none = all(
            t.amount_nis is None for t in result.transactions
        )
        if all_amount_nis_none and result.transactions:
            parsed_debits = sum(
                (t.amount_orig or 0.0) for t in result.transactions
                if t.direction == "debit"
            )
            parsed_credits = sum(
                (t.amount_orig or 0.0) for t in result.transactions
                if t.direction == "credit"
            )
        else:
            parsed_debits = sum(
                t.amount_nis for t in result.transactions
                if t.direction == "debit" and t.amount_nis is not None
            )
            parsed_credits = sum(
                t.amount_nis for t in result.transactions
                if t.direction == "credit" and t.amount_nis is not None
            )
        parsed_total = float(result.statement.parsed_total_nis)
        gap_str = (
            "n/a" if truth.declared_total_nis is None
            else f"{round(parsed_total - truth.declared_total_nis, 2)}"
        )

        # ----- 4. DB count -----
        rows_db: int | str = "n/a"
        if SessionLocal is not None:
            try:
                from argosy.state.models import (
                    ExpenseStatement, ExpenseTransaction,
                )
                with SessionLocal() as s:
                    rows_db = s.query(ExpenseTransaction).join(
                        ExpenseStatement,
                        ExpenseTransaction.statement_id == ExpenseStatement.id,
                    ).filter(
                        ExpenseTransaction.user_id == user_id,
                        ExpenseStatement.parser_name == fmt.value,
                        # period overlap: stmt.period_end >= file.period_start
                        # AND stmt.period_start <= file.period_end
                        ExpenseStatement.period_end >= result.statement.period_start,
                        ExpenseStatement.period_start <= result.statement.period_end,
                    ).count()
            except Exception as e:                # pragma: no cover
                rows_db = f"err({type(e).__name__})"

        # ----- pass/fail -----
        rows_ok = parsed_rows == truth.row_count
        debit_ok = abs(parsed_debits - truth.sum_debits_nis) <= 1.0
        credit_ok = abs(parsed_credits - truth.sum_credits_nis) <= 1.0
        declared_ok = (
            truth.declared_total_nis is None
            or abs(parsed_total - truth.declared_total_nis) <= 50.0
        )
        ok = rows_ok and debit_ok and credit_ok and declared_ok
        mark = "OK " if ok else "FAIL"
        if ok:
            n_ok += 1

        # Per-file row
        typer.echo(
            f"{mark}  {p.name}  parser={fmt.value}  "
            f"rows={truth.row_count}/{parsed_rows}/{rows_db}  "
            f"sum_debit={round(truth.sum_debits_nis, 2)}/"
            f"{round(parsed_debits, 2)}  "
            f"gap={gap_str}"
        )

        # Bucket aggregation
        ext_id = (
            result.source_hint.external_id
            if result.source_hint is not None else None
        )
        label = _label_for(p, fmt.value, external_id=ext_id)
        b = _bucket(label)
        b.files += 1
        if ok:
            b.files_ok += 1
        b.rows_oracle += truth.row_count
        b.rows_parsed += parsed_rows
        if isinstance(rows_db, int):
            b.rows_db += rows_db
        if not rows_ok:
            b.notes.append(f"{p.name}: rows oracle={truth.row_count} parsed={parsed_rows}")
        if not debit_ok:
            b.notes.append(
                f"{p.name}: debit oracle={round(truth.sum_debits_nis, 2)} "
                f"parsed={round(parsed_debits, 2)}"
            )
        if not declared_ok:
            b.notes.append(
                f"{p.name}: declared {truth.declared_total_nis} vs "
                f"parsed {round(parsed_total, 2)}"
            )

    # ----- Footer table -----
    typer.echo("")
    typer.echo("Source                | Files | Files OK | Rows oracle | Rows parsed | Rows in DB | Discrepancy")
    typer.echo("-" * 120)

    def _fmt_row(label: str, b: "_Bucket") -> str:
        disc = "; ".join(b.notes[:3]) if b.notes else "none"
        if b.notes and len(b.notes) > 3:
            disc += f" (+{len(b.notes) - 3} more)"
        return (
            f"{label:21s} | {b.files:5d} | {b.files_ok:8d} | "
            f"{b.rows_oracle:11d} | {b.rows_parsed:11d} | {b.rows_db:10d} | {disc}"
        )

    grand_files = 0
    grand_files_ok = 0
    grand_rows_oracle = 0
    grand_rows_parsed = 0
    grand_rows_db = 0
    grand_notes_count = 0
    for label in sorted(buckets):
        b = buckets[label]
        typer.echo(_fmt_row(label, b))
        grand_files += b.files
        grand_files_ok += b.files_ok
        grand_rows_oracle += b.rows_oracle
        grand_rows_parsed += b.rows_parsed
        grand_rows_db += b.rows_db
        grand_notes_count += len(b.notes)
    total.files = grand_files
    total.files_ok = grand_files_ok
    total.rows_oracle = grand_rows_oracle
    total.rows_parsed = grand_rows_parsed
    total.rows_db = grand_rows_db
    if grand_notes_count:
        total.notes = [f"{grand_notes_count} per-file discrepancies"]
    else:
        total.notes = []
    typer.echo("-" * 120)
    typer.echo(_fmt_row("TOTAL", total))
    if db_open_error:
        typer.echo(f"\n(note: DB rows shown as n/a — {db_open_error})")
    typer.echo(
        f"\nSummary: {n_ok}/{n_files} files passed; "
        f"{n_unknown} unrecognized; {n_parser_error} parser errors."
    )


# ---------------------------------------------------------------------------
# verify-rsu
# ---------------------------------------------------------------------------

@app.command("verify-rsu")
def verify_rsu(
    user_id: str = typer.Option(..., "--user-id"),
    schwab: list[Path] = typer.Option(
        ..., "--schwab",
        help="Path(s) to Schwab Equity Awards CSV(s). Pass multiple times.",
    ),
    tolerance_usd: float = typer.Option(1.0, "--tolerance-usd"),
    tolerance_days: int = typer.Option(7, "--tolerance-days"),
) -> None:
    """Cross-validate Schwab RSU disbursements against Leumi USD account credits.

    Read-only: parses one or more Schwab Equity Awards Center CSVs, queries
    the existing ``expense_transactions`` rows for the Leumi USD account
    (44745200) for the user, then greedy-pairs each Schwab disbursement
    with the closest unmatched Leumi USD credit inside the
    ``[date, date+tolerance_days]`` window with USD amount within
    ``tolerance_usd``. Prints a sales summary, a disbursements-vs-Leumi
    table, and the unmatched-credits residual.

    Console output is ASCII-safe (``OK``/``FAIL``/``??`` markers) — the
    Hebrew merchant strings still go out as Unicode, but no fancy box-
    drawing or check marks (cp1252 console choke risk).
    """
    from argosy.config import reload_settings, get_settings
    from argosy.services.rsu_reconciliation import (
        LeumiCredit, parse_csv, reconcile,
    )

    # ---- 1. Parse all Schwab CSVs and merge ----
    merged = _MergedReport()
    for p in schwab:
        if not p.exists():
            typer.echo(f"FAIL  Schwab CSV not found: {p}")
            raise typer.Exit(code=2)
        try:
            r = parse_csv(p)
        except Exception as e:
            typer.echo(f"FAIL  parse error in {p.name}: {type(e).__name__}: {e}")
            raise typer.Exit(code=2)
        merged.add(p, r)

    typer.echo(
        f"Parsed {len(schwab)} Schwab CSV(s): "
        f"{len(merged.report.sales)} sales, "
        f"{len(merged.report.disbursements)} disbursements."
    )
    if merged.report.unparsed_actions:
        unparsed = ", ".join(
            f"{a}={n}" for a, n in sorted(
                merged.report.unparsed_actions.items(),
                key=lambda kv: -kv[1],
            )
        )
        typer.echo(f"  (skipped non-modelled actions: {unparsed})")
    typer.echo("")

    # ---- 2. Pull Leumi USD credits from DB ----
    reload_settings()
    settings = get_settings()
    if not settings.db_file.exists():
        typer.echo(f"FAIL  no DB file at {settings.db_file} — run an ingest first.")
        raise typer.Exit(code=2)

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker
    from argosy.state.models import (
        ExpenseSource, ExpenseTransaction,
    )

    sync_url = f"sqlite:///{settings.db_file}"
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    leumi_credits: list[LeumiCredit] = []
    with SessionLocal() as s:
        rows = (
            s.query(ExpenseTransaction)
            .join(ExpenseSource, ExpenseTransaction.source_id == ExpenseSource.id)
            .filter(
                ExpenseTransaction.user_id == user_id,
                ExpenseSource.issuer == "leumi",
                ExpenseSource.external_id == "44745200",
                ExpenseTransaction.direction == "credit",
                ExpenseTransaction.currency_orig == "USD",
            )
            .order_by(ExpenseTransaction.occurred_on)
            .all()
        )
        for tx in rows:
            if tx.amount_orig is None:
                continue
            leumi_credits.append(LeumiCredit(
                date=tx.occurred_on,
                amount_usd=float(tx.amount_orig),
                merchant_raw=tx.merchant_raw,
                reference=tx.reference,
                tx_id=tx.id,
            ))

    typer.echo(f"Loaded {len(leumi_credits)} Leumi USD credit(s) from DB "
               f"(user_id={user_id}, account=44745200).")
    typer.echo("")

    # ---- 3. Reconcile ----
    rec = reconcile(
        merged.report,
        leumi_credits,
        tolerance_usd=tolerance_usd,
        tolerance_days=tolerance_days,
    )

    # ---- 4. Print disbursements table ----
    n_d = len(merged.report.disbursements)
    typer.echo(f"Schwab disbursements ({n_d} found):")
    typer.echo(f"  {'Date':12s} {'Amount':>14s}   Status")
    matched_by_disb = {id(m.disbursement): m for m in rec.matches}
    for disb in sorted(merged.report.disbursements, key=lambda d: d.date):
        amt = f"${disb.amount_usd:,.2f}"
        m = matched_by_disb.get(id(disb))
        if m is not None:
            status = (
                f"OK    matched to Leumi {m.credit.date.isoformat()} "
                f"(delta {m.days_diff:+d} days, "
                f"{m.amount_diff_usd:+.2f} USD"
            )
            if m.credit.reference:
                status += f", ref {m.credit.reference}"
            status += ")"
        else:
            status = (
                f"FAIL  NO MATCH — no Leumi USD credit within "
                f"${tolerance_usd:.2f} / {tolerance_days} days"
            )
        typer.echo(f"  {disb.date.isoformat():12s} {amt:>14s}   {status}")
    typer.echo("")

    # ---- 5. Sales detail (Q&A pane) ----
    typer.echo("Schwab sales (per-share Q&A):")
    for sale in sorted(merged.report.sales, key=lambda s: s.date):
        typer.echo(
            f"  {sale.date.isoformat()}  {sale.quantity_shares:>5d} shares "
            f"gross=${sale.gross_usd:,.2f}  fees=${sale.fees_usd:,.2f}  "
            f"taxes=${sale.total_taxes_usd:,.2f}  net=${sale.net_usd:,.2f}  "
            f"({len(sale.lots)} lot{'s' if len(sale.lots) != 1 else ''})"
        )
    typer.echo("")

    # ---- 6. Unmatched Leumi credits residual ----
    n_u = len(rec.unmatched_leumi_credits)
    u_total = sum(c.amount_usd for c in rec.unmatched_leumi_credits)
    typer.echo(
        f"Leumi USD credits with no Schwab counterpart "
        f"({n_u} credits, ${u_total:,.2f} total):"
    )
    for c in sorted(rec.unmatched_leumi_credits, key=lambda x: x.date):
        ref_part = f"  ref {c.reference}" if c.reference else ""
        typer.echo(
            f"  {c.date.isoformat()}  ${c.amount_usd:>12,.2f}  "
            f"{c.merchant_raw}{ref_part}"
        )
    typer.echo("")
    typer.echo("(Unmatched credits are likely non-RSU income — interest, "
               "securities-account transfers, etc.)")
    typer.echo("")

    # ---- 7. Summary line ----
    typer.echo("Summary:")
    typer.echo(f"  {rec.summary}")
    if rec.unmatched_disbursements:
        typer.echo(
            f"  WARNING: {len(rec.unmatched_disbursements)} disbursement(s) "
            "did not reconcile — investigate."
        )

    # Exit non-zero if any disbursement is unmatched (operator signal).
    if rec.unmatched_disbursements:
        raise typer.Exit(code=1)


class _MergedReport:
    """Helper: merge multiple SchwabReports while de-duping disbursements
    and sales by (date, action, amount) — the same CSV is sometimes
    exported twice (e.g. one snapshot per calendar year), and re-running
    against overlapping snapshots should not double-count.
    """

    def __init__(self) -> None:
        from argosy.services.rsu_reconciliation import SchwabReport
        self.report = SchwabReport()
        self._seen_disb: set[tuple] = set()
        self._seen_sale: set[tuple] = set()

    def add(self, path: Path, r) -> None:
        for sale in r.sales:
            key = (sale.date, sale.symbol, sale.quantity_shares,
                   round(sale.gross_usd, 2), round(sale.fees_usd, 2))
            if key in self._seen_sale:
                continue
            self._seen_sale.add(key)
            self.report.sales.append(sale)
        for disb in r.disbursements:
            key = (disb.date, disb.action, round(disb.amount_usd, 2))
            if key in self._seen_disb:
                continue
            self._seen_disb.add(key)
            self.report.disbursements.append(disb)
        for action, n in r.unparsed_actions.items():
            self.report.unparsed_actions[action] = (
                self.report.unparsed_actions.get(action, 0) + n
            )

