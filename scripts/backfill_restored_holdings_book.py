#!/usr/bin/env python
"""Operator action: restore the truncated multi-account holdings book.

Rebuilds the current book from each account's last covering snapshot
(same mechanism as ingest merge). Explicit, idempotent, self-verifying.

NEVER point this at the live ``db/argosy.db`` casually — always work on a
COPY. The script refuses any path that resolves to (or hardlinks to) the
live DB unless ``--i-really-mean-the-live-db`` is passed, and always writes
a timestamped sibling ``.bak_pre_restore.<utc>`` via the SQLite backup API
(WAL-safe; produces a complete standalone DB with no ``-wal``/``-shm``
dependency) before any write.

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
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python scripts/...` without installing the package editable in this
# worktree — put the worktree root on sys.path.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker


def _main_repo_root() -> Path:
    """Resolve the main checkout even when invoked from a worktree."""
    if _ROOT.parent.name == ".worktrees":
        return _ROOT.parents[1]
    return _ROOT


def live_db_candidates() -> list[Path]:
    """Known live DB locations (ARGOSY_HOME, main checkout, this tree)."""
    out: list[Path] = []
    home = os.environ.get("ARGOSY_HOME")
    if home:
        out.append(Path(home) / "db" / "argosy.db")
    out.append(_main_repo_root() / "db" / "argosy.db")
    out.append(_ROOT / "db" / "argosy.db")
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def paths_refer_to_same_file(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` are the same inode (resolve + samefile)."""
    try:
        ar = a.resolve()
        br = b.resolve()
    except OSError:
        return False
    if ar == br:
        return True
    try:
        if ar.exists() and br.exists():
            return ar.samefile(br)
    except OSError:
        return False
    return False


def is_live_db_path(db_path: Path) -> bool:
    """True if ``db_path`` is (or hardlinks/aliases to) a known live argosy.db.

    Filename alone is not enough — a hardlink named ``portfolio.copy.db``
    that points at the live file must also be refused.
    """
    for live in live_db_candidates():
        if paths_refer_to_same_file(db_path, live):
            return True
    return False


def sqlite_consistent_backup(src: Path, dst: Path) -> None:
    """WAL-safe consistent copy via the SQLite online backup API.

    Why not ``shutil.copy2``: under WAL mode, committed pages may live only
    in the ``-wal`` sibling. ``copy2`` of the main file alone can omit those
    pages, producing a backup that fails ``integrity_check`` or silently
    lacks committed rows. ``Connection.backup`` reads through the pager and
    writes a complete standalone database (no ``-wal``/``-shm`` siblings
    required to open the result).

    Why not ``VACUUM INTO`` alone: also consistent, but the backup API is
    the same mechanism ``BackupLoop`` already uses in-tree and does not
    require exclusive locks for the duration of a vacuum rebuild.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    # Remove stale sidecars if a previous failed attempt left them.
    for side in (Path(str(dst) + "-wal"), Path(str(dst) + "-shm")):
        if side.exists():
            side.unlink()
    with sqlite3.connect(str(src)) as src_conn:
        with sqlite3.connect(str(dst)) as dst_conn:
            src_conn.backup(dst_conn)


def timestamped_backup_path(db_path: Path, *, now: datetime | None = None) -> Path:
    """Distinct backup path so a later apply never clobbers an earlier one."""
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return db_path.with_name(f"{db_path.name}.bak_pre_restore.{stamp}")


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
        help="Required when --db resolves to (or hardlinks) the live argosy.db",
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
    # Refuse live DB by identity (resolve/samefile), not basename alone.
    # Basename remains a belt-and-braces refusal for casual copies named
    # argosy.db that are not the live inode.
    if (
        (is_live_db_path(db_path) or db_path.name == "argosy.db")
        and not args.i_really_mean_the_live_db
    ):
        print(
            "REFUSING to touch the live argosy.db (resolved path / samefile "
            "match or basename argosy.db) without --i-really-mean-the-live-db. "
            "Copy the DB first.",
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

        backup = timestamped_backup_path(db_path)
        sqlite_consistent_backup(db_path, backup)
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
