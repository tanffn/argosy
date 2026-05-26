"""Persist Schwab cost-basis CSV rows into the `lots` table.

The CSV format is produced by the Schwab Equity Awards Center "Export
Cost Basis" function. Parser already exists in
``argosy.adapters.brokers.schwab_csv._parse_csv``; this module adds the
write half so the TaxAnalyst's ``lots_summary`` payload can read real
data from ``lots`` instead of the empty-sentinel fallback.

Idempotent on ``(user_id, account_id, lot_id_external)``: re-ingesting
the same CSV does NOT duplicate rows. The CSV parser synthesises a
fallback ``lot_id_external = f"{symbol}-{line_no}"`` when the CSV
doesn't ship one (Schwab CSVs vary).

Public entry points:

* ``ingest_schwab_lots(session, *, user_id, csv_path, account_id) -> int``
  — write rows; return the number INSERTED (skipping conflicts).
* CLI: ``argosy ingest schwab-lots <path> --user-id ariel
  [--account-id schwab]`` (mounted from ``argosy.cli.ingest``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.adapters.brokers.schwab_csv import _parse_csv
from argosy.state.models import Lot


def ingest_schwab_lots(
    session: Session,
    *,
    user_id: str,
    csv_path: Path | str,
    account_id: str = "schwab",
) -> int:
    """Parse a Schwab cost-basis CSV and upsert into the ``lots`` table.

    Returns the number of rows newly inserted. Existing rows are matched
    on ``(user_id, account_id, lot_id_external)`` and updated in place
    (quantity / cost basis can shift slightly between exports as Schwab
    reconciles wash-sale adjustments). Rows that fall out of the CSV are
    NOT deleted — partial Schwab exports shouldn't nuke history.
    """
    csv_path = Path(csv_path)
    parsed = _parse_csv(csv_path)
    if not parsed:
        return 0

    # Build a lookup of existing rows for this user + account so we can
    # in-place update rather than insert-then-fail-then-load.
    existing = {
        r.lot_id_external: r
        for r in session.execute(
            select(Lot).where(
                Lot.user_id == user_id,
                Lot.account_id == account_id,
            )
        ).scalars().all()
    }

    inserted = 0
    now = datetime.now(timezone.utc)
    for row in parsed:
        eff_account = (row.account_id or account_id).strip() or account_id
        key = row.lot_id_external
        existing_row = existing.get(key)
        if existing_row is not None:
            existing_row.quantity = Decimal(str(row.quantity))
            existing_row.cost_basis_usd = Decimal(str(row.cost_basis_total))
            existing_row.ticker = row.symbol
            existing_row.account_id = eff_account
            if row.acquired_at is not None:
                existing_row.acquired_at = row.acquired_at
            existing_row.source = "schwab_csv"
            existing_row.imported_at = now
            continue
        session.add(
            Lot(
                user_id=user_id,
                account_id=eff_account,
                ticker=row.symbol,
                lot_id_external=key,
                quantity=Decimal(str(row.quantity)),
                cost_basis_usd=Decimal(str(row.cost_basis_total)),
                acquired_at=row.acquired_at,
                source="schwab_csv",
                imported_at=now,
            )
        )
        inserted += 1
    session.commit()
    return inserted


__all__ = ["ingest_schwab_lots"]
