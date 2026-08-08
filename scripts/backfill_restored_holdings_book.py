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
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow `python scripts/...` without installing the package editable in this
# worktree — put the worktree root on sys.path.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine, inspect as sa_inspect, text as sa_text
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

    Identity ONLY — never the basename. A hardlink named ``portfolio.copy.db``
    pointing at the live file must be refused, and equally a legitimate staged
    copy at ``<stage>/db/argosy.db`` must NOT be. Refusing safe copies by name
    forces operators to pass ``--i-really-mean-the-live-db`` during rehearsals,
    which makes a rehearsal indistinguishable from the production run and
    builds exactly the habit that loses money.
    """
    for live in live_db_candidates():
        if paths_refer_to_same_file(db_path, live):
            return True
    return False


def refuse_reason(
    db_path: Path, *, override: bool, staged_copy: bool = False
) -> str | None:
    """The single decision for whether this tool may touch ``db_path``.

    Kept as one function so the refusal is testable at the layer that actually
    decides it — a test asserting on ``is_live_db_path`` alone would still pass
    if a basename check were reintroduced here.

    Every target needs a positive designation; there is no path where the tool
    guesses. Exactly one of two flags applies:

    * The target IS a database we can identify as live ⇒
      ``--i-really-mean-the-live-db``.
    * Anything else ⇒ ``--staged-copy``, affirming it is a throwaway.

    Both weaker designs failed. Identity alone cannot protect a production
    database at a path we do not enumerate. Adding "and refuse anything named
    ``argosy.db``" only moved the hole: a review pointed the tool at an
    unenumerated ``production.sqlite`` and it was accepted with no flag at all,
    because the guard was guessing from a filename. Guessing is now gone — the
    operator states which situation this is, and the two situations never share
    a flag, so a rehearsal cannot train the fingers that run the real repair.
    """
    if is_live_db_path(db_path) and not override:
        return (
            f"REFUSING: {db_path} IS the live database (resolved path / "
            "samefile identity match). Pass --i-really-mean-the-live-db only "
            "for the scheduled production repair; rehearse on a copy instead."
        )
    if not override and not staged_copy:
        return (
            f"REFUSING: {db_path} is not a location this tool can identify, so "
            "it may be a production database we cannot see (ARGOSY_HOME unset, "
            "mapped drive, alternate install, a file simply named something "
            "else). Pass --staged-copy to affirm it is a throwaway copy, or "
            "--i-really-mean-the-live-db for the real repair."
        )
    return None


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


def book_fingerprint(db_file: Path, user_id: str) -> dict:
    """Content fingerprint of the latest snapshot: counts, money, row-hash.

    ``PRAGMA integrity_check`` is NOT a backup check. A ``shutil.copy2`` of a
    WAL-mode database can be a 4 KB empty-header file whose schema still lives
    in the ``-wal`` sibling, and it reports ``integrity_check = ok``. A
    structural check blesses garbage, so a backup is only trustworthy when its
    CONTENT reconciles against the source.
    """
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT id, positions_json FROM portfolio_snapshots "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return {"snapshot_id": None, "positions": 0, "total_usd_k": 0.0,
                    "accounts": {}, "row_hash": "empty"}
        snapshot_id, positions_json = row
        positions = json.loads(positions_json or "[]")
        per_account: dict[str, float] = {}
        for p in positions:
            key = str(p.get("location") or "?").lower()
            per_account[key] = per_account.get(key, 0.0) + float(
                p.get("usd_value_k") or 0.0
            )
        parts = sorted(
            f"{str(p.get('symbol') or '').strip()}|"
            f"{str(p.get('location') or '').lower()}|"
            f"{p.get('shares')}|{p.get('usd_value_k')}"
            for p in positions
        )
        digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
        return {
            "snapshot_id": snapshot_id,
            "positions": len(positions),
            "total_usd_k": round(sum(per_account.values()), 1),
            "accounts": {k: round(v, 1) for k, v in sorted(per_account.items())},
            "row_hash": digest,
        }
    finally:
        con.close()


def database_fingerprint(db_file: Path) -> dict:
    """Content hash of EVERY row of EVERY table, plus the structural check.

    Two weaker versions of this were not enough. Reconciling only the latest
    book blessed a backup that had lost the ``proposals`` table entirely.
    Reconciling table names and ROW COUNTS then blessed a backup in which a
    ``proposals.note`` had been altered while the count stayed the same. A
    rollback point has to match on content, so every row is hashed.

    Per-row digests are combined by summation rather than concatenation, so the
    fingerprint does not depend on the order rows come back in (a copy made by
    some other mechanism may lay pages out differently). Summation rather than
    XOR, so that two identical rows do not cancel each other out.
    """
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    # text_factory is consulted ONLY for TEXT values, so tagging them here keeps
    # a TEXT 'abc' distinguishable from a BLOB x'616263'. Without the tag both
    # arrive as identical bytes and a type change fingerprints as no change.
    con.text_factory = lambda b: b"\x00T\x00" + b
    try:
        tables = [
            r[0].decode("utf-8", "replace").replace("\x00T\x00", "")
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        # Views, indexes, triggers and table DDL are part of what a rollback
        # point has to restore; comparing only table rows let a changed view
        # definition through.
        def _clean(v: Any) -> str:
            if isinstance(v, bytes):
                return v.decode("utf-8", "replace").replace("\x00T\x00", "")
            return str(v)

        schema_objs: dict[str, str] = {}
        for typ, name, tbl_name, sql in con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ):
            key = f"{_clean(typ)}:{_clean(name)}"
            schema_objs[key] = hashlib.sha256(
                repr((_clean(tbl_name), _clean(sql))).encode("utf-8", "replace")
            ).hexdigest()[:16]

        digests: dict[str, Any] = {}
        unreadable: list[str] = []
        for t in tables:
            try:
                acc = 0
                count = 0
                for row in con.execute(f'SELECT * FROM "{t}"'):
                    count += 1
                    acc = (
                        acc
                        + int.from_bytes(
                            hashlib.sha256(
                                repr(row).encode("utf-8", "replace")
                            ).digest(),
                            "big",
                        )
                    ) % (1 << 256)
                digests[t] = f"{count}:{acc:064x}"
            except sqlite3.Error as exc:
                # Must not become a comparable string: two unreadable databases
                # would then reconcile against each other.
                unreadable.append(f"{t} ({exc})")
                digests[t] = f"UNREADABLE {t}"
        if unreadable:
            raise sqlite3.DatabaseError(
                f"{len(unreadable)} unreadable table(s): {unreadable[:5]}"
            )
        integrity = con.execute("PRAGMA quick_check(1)").fetchone()[0]
        if isinstance(integrity, bytes):
            integrity = integrity.decode("utf-8", "replace").replace("\x00T\x00", "")
        return {
            "integrity": integrity,
            "table_count": len(tables),
            "schema": schema_objs,
            "row_counts": digests,
        }
    finally:
        con.close()


def verify_backup_against_source(
    src: Path, backup: Path, user_id: str
) -> tuple[bool, str]:
    """Reconcile a backup's CONTENT against its source. Also assert standalone."""
    orphan_siblings = [
        str(p) for p in (Path(str(backup) + "-wal"), Path(str(backup) + "-shm"))
        if p.exists()
    ]
    if orphan_siblings:
        return False, f"backup is not standalone; needs {orphan_siblings}"
    try:
        src_fp = book_fingerprint(src, user_id)
    except (sqlite3.Error, ValueError) as exc:
        return False, f"cannot fingerprint SOURCE ({exc}) — aborting"
    # An unreadable backup is a FAILED backup, not a crash: a copy2 artefact can
    # be missing the schema entirely, which raises rather than mismatching.
    try:
        bak_fp = book_fingerprint(backup, user_id)
    except (sqlite3.Error, ValueError) as exc:
        return False, f"backup is unreadable ({exc}) — no rollback point"
    if src_fp != bak_fp:
        diffs = [
            f"{k}: source={src_fp.get(k)!r} backup={bak_fp.get(k)!r}"
            for k in sorted(set(src_fp) | set(bak_fp))
            if src_fp.get(k) != bak_fp.get(k)
        ]
        return False, "content mismatch -> " + "; ".join(diffs)

    # The book matching is necessary but not sufficient — reconcile the whole
    # database, so selective loss outside the latest snapshot cannot pass.
    try:
        src_db = database_fingerprint(src)
        bak_db = database_fingerprint(backup)
    except (sqlite3.Error, ValueError) as exc:
        return False, f"cannot fingerprint the whole database ({exc})"
    if bak_db["integrity"] != "ok":
        return False, f"backup integrity={bak_db['integrity']!r}"
    if src_db["schema"] != bak_db["schema"]:
        src_s, bak_s = src_db["schema"], bak_db["schema"]
        gone = sorted(set(src_s) - set(bak_s))
        added = sorted(set(bak_s) - set(src_s))
        changed = sorted(
            k for k in set(src_s) & set(bak_s) if src_s[k] != bak_s[k]
        )
        return False, (
            "schema differs — the backup is not an equivalent database: "
            f"missing={gone[:5]} added={added[:5]} changed={changed[:5]}"
        )
    missing = sorted(set(src_db["row_counts"]) - set(bak_db["row_counts"]))
    if missing:
        return False, f"backup is MISSING {len(missing)} table(s): {missing[:8]}"
    differing = [
        f"{t}: source={src_db['row_counts'][t][:24]}… "
        f"backup={bak_db['row_counts'][t][:24]}…"
        for t in sorted(src_db["row_counts"])
        if src_db["row_counts"][t] != bak_db["row_counts"][t]
    ]
    if differing:
        return False, (
            f"{len(differing)} table(s) differ in CONTENT -> "
            + "; ".join(differing[:6])
        )
    return True, (
        f"reconciled: snapshot {src_fp['snapshot_id']}, {src_fp['positions']} "
        f"positions / ${src_fp['total_usd_k']}k, accounts={src_fp['accounts']}, "
        f"row_hash={src_fp['row_hash']}; whole-DB: {src_db['table_count']} "
        "tables, every row hashed and matching"
    )


def wal_sidecar_size(db_path: Path) -> int:
    sidecar = Path(str(db_path) + "-wal")
    return sidecar.stat().st_size if sidecar.exists() else 0


def quiesce_check(db_path: Path, settle_seconds: float) -> tuple[bool, str]:
    """Confirm no OTHER connection is writing, and that we can lock the file.

    An earlier version of this compared the ``-wal`` sidecar SIZE at two
    instants. That is not a liveness test: with ``wal_autocheckpoint`` active a
    steady writer holds the WAL at a constant size, and a measured run saw 143
    transactions commit across the window while the sidecar sat at exactly 4152
    bytes, so the gate reported "quiesced" against a fully live database.

    Two authoritative signals replace it:

    * ``PRAGMA data_version`` — SQLite guarantees this value changes, as seen
      from ONE held-open connection, when any OTHER connection commits. It is a
      change detector rather than a heuristic, so we hold a single connection
      across the window.
    * ``BEGIN EXCLUSIVE`` — mutual exclusion rather than observation. If any
      other connection holds the database we cannot acquire it, which also
      proves no writer can slip in at this instant.
    """
    if settle_seconds <= 0:
        return False, (
            "REFUSED: --settle-seconds 0 disables the only liveness check. "
            "A zero-length window cannot observe a writer."
        )
    con = sqlite3.connect(str(db_path), timeout=1.0)
    try:
        try:
            first = con.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error as exc:
            return False, f"cannot read data_version ({exc}) — refusing"
        time.sleep(settle_seconds)
        second = con.execute("PRAGMA data_version").fetchone()[0]
        if first != second:
            return False, (
                f"data_version moved {first} -> {second} during a "
                f"{settle_seconds:g}s window: ANOTHER connection committed. "
                "Stop the backend and scheduler first."
            )
        # Observation says idle; now prove exclusivity.
        try:
            con.execute("BEGIN EXCLUSIVE")
            con.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            return False, (
                f"cannot acquire an EXCLUSIVE lock ({exc}): another connection "
                "holds the database. Stop the backend and scheduler first."
            )
        return True, (
            f"data_version stable at {second} over {settle_seconds:g}s and "
            "EXCLUSIVE lock acquired — no other connection is active"
        )
    finally:
        con.close()


def checkpoint_wal(db_path: Path) -> tuple[bool, str]:
    """Fold the WAL into the main database, and report whether it COMPLETED.

    ``wal_checkpoint(TRUNCATE)`` reports ``busy=1`` with ``checkpointed=0`` when
    a reader pins the WAL. The previous version returned only a log string, so
    the repair proceeded against a database whose WAL had not been folded in at
    all. The result is now a pass/fail the caller must honour.
    """
    con = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        busy, log_pages, checkpointed = con.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        con.commit()
        remaining = wal_sidecar_size(db_path)
        detail = (
            f"wal_checkpoint(TRUNCATE) busy={busy} log_pages={log_pages} "
            f"checkpointed={checkpointed}; wal now {remaining}B"
        )
        if busy != 0 or remaining != 0:
            return False, (
                detail
                + " — INCOMPLETE: the WAL was not folded into the main file "
                "(a reader is holding it). Stop every other connection."
            )
        return True, detail
    finally:
        con.close()


class InterloperWatch:
    """Detects commits by ANY other connection, across a window we do not write.

    ``PRAGMA data_version`` only carries meaning when compared across samples
    taken on the SAME connection: SQLite documents it as changing when another
    connection commits, as observed by one held connection. An earlier version
    opened a fresh connection per sample, which made the comparison inert — a
    review reproduced an intervening commit reading 2 -> 2, undetected. The
    connection is therefore held open for the life of the watch.

    What this DOES guarantee: if anything else commits between ``start()`` and
    a ``moved()`` call, we find out and can refuse.

    What it does NOT guarantee: mutual exclusion. This is only the check that
    covers the BACKUP, which cannot be taken under a lock — ``sqlite3``'s backup
    API hangs when the source connection is inside a write transaction
    (measured). The RESTORE does not share that constraint and is performed
    under ``BEGIN IMMEDIATE``, so the window this watch covers ends the moment
    that lock is taken; from there the exclusion is structural, not observed.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._con: sqlite3.Connection | None = None
        self._baseline: int | None = None

    def start(self) -> int | None:
        self._con = sqlite3.connect(str(self._db_path), timeout=5.0)
        self._baseline = self._read()
        return self._baseline

    def _read(self) -> int | None:
        if self._con is None:
            return None
        try:
            return int(self._con.execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.Error:
            return None

    def moved(self) -> tuple[bool, str]:
        """True if someone else committed since ``start()``."""
        if self._baseline is None:
            return False, "data_version unavailable — no interloper check"
        current = self._read()
        if current is None:
            return False, "data_version unavailable — no interloper check"
        if current != self._baseline:
            return True, (
                f"another connection committed (data_version "
                f"{self._baseline} -> {current})"
            )
        return False, f"no other connection committed (data_version {current})"

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None


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
        "--staged-copy",
        action="store_true",
        help=(
            "Affirm that a target named argosy.db is a throwaway copy. Use this "
            "for rehearsals so they never share a flag with the live repair."
        ),
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
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=5.0,
        help="Watch the WAL this long to confirm no writer is active (0 skips)",
    )
    parser.add_argument(
        "--skip-quiesce-check",
        action="store_true",
        help="Proceed even if a writer appears active (not advised)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 2
    # Refuse live DB by identity (resolve/samefile), not basename alone.
    # Basename remains a belt-and-braces refusal for casual copies named
    # argosy.db that are not the live inode.
    refusal = refuse_reason(
        db_path,
        override=args.i_really_mean_the_live_db,
        staged_copy=args.staged_copy,
    )
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2

    from argosy.services.holding_books import (
        EXPECTED_RESTORED_POSITION_COUNT,
        EXPECTED_RESTORED_USD_K,
        accounts_covered_from_positions,
        backfill_restored_holdings_book,
        resolve_prior_positions_by_account_coverage,
    )
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row

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

        # On the live database the safety overrides are not available at all.
        # An escape hatch that exists is an escape hatch that gets used at 2am.
        if args.i_really_mean_the_live_db and (
            args.skip_quiesce_check or args.settle_seconds <= 0
        ):
            print(
                "ERROR: --skip-quiesce-check / --settle-seconds 0 are refused "
                "for the live database. Stop the backend and scheduler.",
                file=sys.stderr,
            )
            return 2

        # Release the read locks our own reconnaissance SELECTs are holding,
        # otherwise the EXCLUSIVE probe below would refuse OUR connection and
        # the repair would deadlock against itself.
        session.rollback()

        # Gate step 1 — the database must be quiesced. A correct backup does
        # not help if the scheduler writes a new snapshot right after us.
        ok, detail = quiesce_check(db_path, args.settle_seconds)
        print(f"quiesce: {detail}")
        if not ok:
            if not args.skip_quiesce_check:
                print(
                    "ERROR: refusing to restore against a live writer. Stop the "
                    "backend and scheduler first.",
                    file=sys.stderr,
                )
                return 1
            print("WARNING: proceeding despite an active writer (override)")

        # Gate step 2 — fold the WAL into the main file before copying it.
        checkpointed, detail = checkpoint_wal(db_path)
        print(f"checkpoint: {detail}")
        if not checkpointed and not args.skip_quiesce_check:
            print(
                "ERROR: the WAL was not fully folded in, so the backup would "
                "not capture the database as it stands. Refusing.",
                file=sys.stderr,
            )
            return 1

        # Watch the whole no-write window: we do not write between here and the
        # start of the restore, so ANY commit in it belongs to someone else and
        # means the backup no longer describes what we are about to overwrite.
        watch = InterloperWatch(db_path)
        watch.start()

        # Gate step 3 — content-verified backup.
        backup = timestamped_backup_path(db_path)
        sqlite_consistent_backup(db_path, backup)
        verified, detail = verify_backup_against_source(
            db_path, backup, args.user_id
        )
        print(f"backup written: {backup}")
        print(f"backup verification: {detail}")
        if not verified:
            print(
                "ERROR: backup failed content reconciliation — refusing to "
                "restore. There is no trustworthy rollback point.",
                file=sys.stderr,
            )
            return 1

        moved, detail = watch.moved()
        print(f"interloper check (post-backup): {detail}")
        if moved:
            watch.close()
            print(
                f"ERROR: {detail} during the backup. The backup does not "
                "describe what we are about to overwrite. Refusing — stop the "
                "backend and scheduler, then re-run.",
                file=sys.stderr,
            )
            return 1

        # Close the window by construction rather than by observation. The
        # BACKUP cannot hold a lock (sqlite3's backup API hangs when the source
        # connection is inside a write transaction — measured), but the RESTORE
        # can: taking IMMEDIATE here means no other connection can write from
        # this point until we commit. An earlier version only re-checked and
        # hoped, which left a real commit window between the check and the
        # write.
        try:
            session.execute(sa_text("BEGIN IMMEDIATE"))
        except Exception as exc:  # noqa: BLE001 — operator surface
            watch.close()
            print(
                f"ERROR: could not take the write lock for the restore ({exc}). "
                "Another connection holds the database. Stop the backend and "
                "scheduler, then re-run.",
                file=sys.stderr,
            )
            return 1

        # With the lock held, one last look back over the unlocked stretch
        # (backup + verification). Nothing can move it from here on.
        moved, detail = watch.moved()
        watch.close()
        print(f"interloper check (pre-restore, lock held): {detail}")
        if moved:
            session.rollback()
            print(
                f"ERROR: {detail} after the backup was verified. Refusing to "
                "restore over a database that changed under us.",
                file=sys.stderr,
            )
            return 1

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
