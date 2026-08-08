#!/usr/bin/env python
"""Operator action: restore the truncated multi-account holdings book.

Rebuilds the current book from each account's last covering snapshot
(same mechanism as ingest merge). Explicit, idempotent, self-verifying.

NEVER point this at the live ``db/argosy.db`` casually — always work on a
COPY. The script refuses paths named ``argosy.db`` unless
``--i-really-mean-the-live-db`` is passed, and always writes a sibling
``.bak_pre_restore`` backup before any write.

Usage (PowerShell)::

    $env:PYTHONIOENCODING = "utf-8"
    D:/Projects/financial-advisor/.venv/Scripts/python.exe `
      scripts/backfill_restored_holdings_book.py `
      --db path/to/argosy.copy.db
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Allow `python scripts/...` without installing the package editable in this
# worktree — put the worktree root on sys.path.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        required=True,
        help="SQLite DB path to repair (use a COPY of live, not live itself)",
    )
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument(
        "--i-really-mean-the-live-db",
        action="store_true",
        help="Required when --db basename is argosy.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve + verify only; do not write",
    )
    parser.add_argument(
        "--skip-expected-check",
        action="store_true",
        help="Do not assert the Ariel 46 / $4047.6k reconciliation targets",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 2
    if db_path.name == "argosy.db" and not args.i_really_mean_the_live_db:
        print(
            "REFUSING to touch a file named argosy.db without "
            "--i-really-mean-the-live-db. Copy the DB first.",
            file=sys.stderr,
        )
        return 2

    from argosy.services.holding_books import (
        EXPECTED_RESTORED_POSITION_COUNT,
        EXPECTED_RESTORED_USD_K,
        backfill_restored_holdings_book,
        resolve_prior_positions_by_account_coverage,
    )

    expected_n = None if args.skip_expected_check else EXPECTED_RESTORED_POSITION_COUNT
    expected_usd = None if args.skip_expected_check else EXPECTED_RESTORED_USD_K

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        if args.dry_run:
            positions = resolve_prior_positions_by_account_coverage(
                session, args.user_id,
            )
            n = len(positions)
            total = sum(float(p.get("usd_value_k") or 0.0) for p in positions)
            print(f"dry-run reconstruction: {n} positions / ${total:.1f}k")
            if expected_n is not None and n != expected_n:
                print(f"FAIL: expected {expected_n} positions", file=sys.stderr)
                return 1
            if expected_usd is not None and abs(total - expected_usd) > 0.5:
                print(f"FAIL: expected ${expected_usd:.1f}k", file=sys.stderr)
                return 1
            print("dry-run OK")
            return 0

        backup = db_path.with_suffix(db_path.suffix + ".bak_pre_restore")
        shutil.copy2(db_path, backup)
        print(f"backup written: {backup}")

        result = backfill_restored_holdings_book(
            session,
            user_id=args.user_id,
            expected_position_count=expected_n,
            expected_usd_k=expected_usd,
            actor="scripts/backfill_restored_holdings_book.py",
        )
        print(
            f"{result['status']}: {result['position_count']} positions / "
            f"${result['total_usd_k']:.1f}k accounts={result['accounts']}"
        )
        if result["status"] == "restored":
            print(f"new snapshot id={result.get('snapshot_id')}")
        return 0
    except Exception as exc:  # noqa: BLE001 — operator surface
        print(f"ERROR: {exc}", file=sys.stderr)
        session.rollback()
        return 1
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
