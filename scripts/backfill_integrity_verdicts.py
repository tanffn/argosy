#!/usr/bin/env python
"""Operator action: backfill spine integrity verdicts for verdict-less snapshots.

Records an ``integrity_verdict`` (+ head) for every ``portfolio_snapshots`` row
that has NO current ``integrity_verdict_head``. Idempotent + re-runnable — a
second run is a no-op. Writes ONLY the spine tables; it never touches the money
tables (positions/totals/allocations are read-only inputs to the verdict).

This slice only RECORDS verdicts. It does NOT enforce anything: no surface
refuses a book because of a verdict.

Usage (PowerShell) — run DELIBERATELY against the live DB::

    $env:PYTHONIOENCODING = "utf-8"
    D:/Projects/financial-advisor/.venv/Scripts/python.exe `
      scripts/backfill_integrity_verdicts.py            # dry-run: report only
    D:/Projects/financial-advisor/.venv/Scripts/python.exe `
      scripts/backfill_integrity_verdicts.py --apply    # actually record

Options::

    --db PATH     Target a specific SQLite file (default: the configured DB).
    --user ID     Scope the backfill to one user_id (default: all users).
    --apply       Perform the writes. Without it, the script only reports how
                  many snapshots WOULD be backfilled and exits without writing.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow `python scripts/...` without an editable install in this worktree.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _count_pending(session, user_id: str | None) -> tuple[int, int]:
    """Return ``(pending, total)`` snapshot counts for the scope."""
    from argosy.state.models import IntegrityVerdictHead, PortfolioSnapshotRow

    total_q = select(func.count(PortfolioSnapshotRow.id))
    if user_id is not None:
        total_q = total_q.where(PortfolioSnapshotRow.user_id == user_id)
    total = int(session.execute(total_q).scalar_one())

    pending_q = (
        select(func.count(PortfolioSnapshotRow.id))
        .outerjoin(
            IntegrityVerdictHead,
            IntegrityVerdictHead.snapshot_id == PortfolioSnapshotRow.id,
        )
        .where(IntegrityVerdictHead.snapshot_id.is_(None))
    )
    if user_id is not None:
        pending_q = pending_q.where(PortfolioSnapshotRow.user_id == user_id)
    pending = int(session.execute(pending_q).scalar_one())
    return pending, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="Path to the SQLite DB file.")
    parser.add_argument("--user", default=None, help="Scope to one user_id.")
    parser.add_argument(
        "--apply", action="store_true", help="Perform writes (else dry-run)."
    )
    args = parser.parse_args(argv)

    from argosy.services.spine.integrity import backfill_integrity_verdicts
    from argosy.state.db import create_sync_engine

    url = None
    if args.db is not None:
        url = f"sqlite:///{Path(args.db).resolve()}"
    engine = create_sync_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        pending, total = _count_pending(session, args.user)
        scope = f"user={args.user}" if args.user else "all users"
        print(
            f"[backfill] {scope}: {pending} of {total} snapshot(s) have no "
            f"integrity verdict head."
        )
        if not args.apply:
            print("[backfill] DRY-RUN — pass --apply to record verdicts.")
            return 0
        tally = backfill_integrity_verdicts(session, user_id=args.user)
        print(
            "[backfill] done: recorded={recorded} skipped={skipped} "
            "failed={failed} total={total}".format(**tally)
        )
        return 1 if tally["failed"] else 0
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
