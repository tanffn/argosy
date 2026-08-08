#!/usr/bin/env python
"""Operator action: restore the truncated multi-account holdings book.

Rebuilds the current book from each account's last covering snapshot
(same mechanism as ingest merge). Explicit, idempotent, self-verifying.

NEVER point this at the live ``db/argosy.db`` casually — always work on a
COPY. The script refuses paths named ``argosy.db`` unless
``--i-really-mean-the-live-db`` is passed, and always writes a sibling
``.bak_pre_restore`` backup before any write.

Default action is DRY-RUN (resolve + verify only). Pass ``--apply`` to write.

Usage (PowerShell)::

    $env:PYTHONIOENCODING = "utf-8"
    D:/Projects/financial-advisor/.venv/Scripts/python.exe `
      scripts/backfill_restored_holdings_book.py `
      --db path/to/argosy.copy.db
    # then, after inspecting the dry-run output:
    D:/Projects/financial-advisor/.venv/Scripts/python.exe `
      scripts/backfill_restored_holdings_book.py `
      --db path/to/argosy.copy.db --apply
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

from sqlalchemy import create_engine, inspect as sa_inspect
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
        "--apply",
        action="store_true",
        help="Actually write the restored snapshot (default is dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Deprecated alias: dry-run is the default; kept for compatibility",
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
        accounts_covered_from_positions,
        backfill_restored_holdings_book,
        resolve_prior_positions_by_account_coverage,
    )
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row
    import json

    expected_n = None if args.skip_expected_check else EXPECTED_RESTORED_POSITION_COUNT
    expected_usd = None if args.skip_expected_check else EXPECTED_RESTORED_USD_K

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        # Fail closed before any reconstruction claims success.
        insp = sa_inspect(engine)
        missing = [
            t for t in ("unmanaged_holdings", "unmanaged_symbol_policy")
            if not insp.has_table(t)
        ]
        if missing:
            print(
                "ERROR: prerequisite missing: "
                + ", ".join(missing)
                + " (need alembic 0097_unmanaged_holdings). "
                "Refusing — NVDA would restore as managed.",
                file=sys.stderr,
            )
            return 1

        positions = resolve_prior_positions_by_account_coverage(
            session, args.user_id,
        )
        n = len(positions)
        total = sum(float(p.get("usd_value_k") or 0.0) for p in positions)
        accounts = sorted(accounts_covered_from_positions(positions))
        latest = get_latest_snapshot_row(session, args.user_id)
        latest_accounts: set[str] = set()
        if latest is not None:
            try:
                cur = json.loads(latest.positions_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                cur = []
            latest_accounts = set(accounts_covered_from_positions(cur))
        accounts_carried = sorted(a for a in accounts if a not in latest_accounts)

        mode = "APPLY" if args.apply and not args.dry_run else "DRY-RUN"
        print(
            f"{mode} reconstruction: {n} positions / ${total:.1f}k "
            f"accounts_covered={accounts} accounts_carried={accounts_carried}"
        )
        if expected_n is not None and n != expected_n:
            print(f"FAIL: expected {expected_n} positions", file=sys.stderr)
            return 1
        if expected_usd is not None and abs(total - expected_usd) > 0.5:
            print(f"FAIL: expected ${expected_usd:.1f}k", file=sys.stderr)
            return 1

        if not args.apply or args.dry_run:
            print("dry-run OK (pass --apply to write)")
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
            f"${result['total_usd_k']:.1f}k accounts={result['accounts']} "
            f"carried={result.get('accounts_carried')}"
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
