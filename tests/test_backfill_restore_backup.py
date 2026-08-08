"""D-1 — WAL-safe, identity-guarded, timestamped pre-restore backups.

Every test fails for the right reason when its fix is reverted. Never
touches live ``db/argosy.db``.
"""
from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "backfill_restored_holdings_book.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "backfill_restored_holdings_book_d1", _SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_script()


def test_d1_sqlite_backup_includes_committed_wal_pages(tmp_path, mod):
    """Defect 1 — backup API must see rows committed into WAL.

    Revert detector: replace ``sqlite_consistent_backup`` with
    ``shutil.copy2(src, dst)`` → this test fails (backup lacks the row,
    or integrity diverges from source).
    """
    src = tmp_path / "source.db"
    conn = sqlite3.connect(str(src))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t(v) VALUES ('committed-in-wal')")
    conn.commit()
    # Second committed row while WAL mode is active — the classic copy2 miss.
    conn.execute("INSERT INTO t(v) VALUES ('second-committed')")
    conn.commit()

    bak = tmp_path / "source.db.bak"
    mod.sqlite_consistent_backup(src, bak)
    conn.close()

    check = sqlite3.connect(str(bak))
    rows = [r[0] for r in check.execute("SELECT v FROM t ORDER BY id").fetchall()]
    integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    check.close()
    assert integrity == "ok"
    assert rows == ["committed-in-wal", "second-committed"]
    # Standalone: no WAL sibling required to read the backup.
    assert not Path(str(bak) + "-wal").exists()


def test_d1_shutil_copy2_misses_wal_committed_row(tmp_path):
    """Control probe — documents why copy2 is unsafe (OLD behaviour fails)."""
    src = tmp_path / "source.db"
    conn = sqlite3.connect(str(src))
    conn.execute("PRAGMA journal_mode=WAL")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t(v) VALUES ('visible')")
    conn.commit()
    # Keep the writer connection open so the WAL is the live store for
    # subsequent commits (main file may lag).
    conn.execute("INSERT INTO t(v) VALUES ('only-in-wal-until-checkpoint')")
    conn.commit()

    bak = tmp_path / "copy2.bak"
    shutil.copy2(src, bak)
    # Source still sees both rows.
    src_rows = [r[0] for r in conn.execute("SELECT v FROM t ORDER BY id").fetchall()]
    assert src_rows == ["visible", "only-in-wal-until-checkpoint"]
    conn.close()

    # copy2 of the main file alone is not guaranteed to include WAL pages.
    # On some platforms/checkpoints it may luckily contain them; force the
    # known-unsafe shape by copying ONLY the main file while a -wal exists
    # that is newer / non-empty when possible.
    wal = Path(str(src) + "-wal")
    # Re-open and ensure WAL has content relative to a fresh copy2.
    conn2 = sqlite3.connect(str(src))
    conn2.execute("PRAGMA journal_mode=WAL")
    conn2.execute("INSERT INTO t(v) VALUES ('post-copy2-commit')")
    conn2.commit()
    bak2 = tmp_path / "copy2_after_commit.bak"
    shutil.copy2(src, bak2)
    src_n = conn2.execute("SELECT count(*) FROM t").fetchone()[0]
    try:
        bak_n = sqlite3.connect(str(bak2)).execute("SELECT count(*) FROM t").fetchone()[0]
    except sqlite3.DatabaseError:
        bak_n = -1  # corrupt / incomplete copy also proves the defect
    conn2.close()
    # The defect: source has a committed row the copy2 backup lacks OR the
    # backup is unreadable. (If a checkpoint folded WAL into main before
    # copy2, this control is inconclusive — skip rather than false-green.)
    if wal.exists() and wal.stat().st_size > 0 and bak_n >= 0 and bak_n == src_n:
        pytest.skip(
            "WAL was checkpointed into main before copy2; cannot demonstrate "
            "the miss on this platform/run"
        )
    assert bak_n != src_n or bak_n < 0, (
        f"expected copy2 to diverge from source (src={src_n} bak={bak_n})"
    )


def test_d1_live_db_guard_refuses_hardlink_alias(tmp_path, mod, monkeypatch):
    """Defect 2 — refuse hardlink/alias to live DB, not basename alone.

    Revert detector: restore ``if db_path.name == "argosy.db"`` only →
    hardlink named ``not_live.sqlite`` pointing at live is accepted.
    """
    live = tmp_path / "argosy.db"
    live.write_bytes(b"x")
    monkeypatch.setattr(mod, "live_db_candidates", lambda: [live])

    alias = tmp_path / "not_named_argosy.sqlite"
    try:
        alias.hardlink_to(live)
    except OSError:
        pytest.skip("hardlinks unavailable on this volume")
    assert mod.is_live_db_path(alias) is True
    assert mod.is_live_db_path(live) is True

    other = tmp_path / "other" / "portfolio.copy.db"
    other.parent.mkdir()
    other.write_bytes(b"y")
    assert mod.is_live_db_path(other) is False


def test_d1_timestamped_backups_are_distinct(mod):
    """Defect 3 — two applies must not share one backup path.

    Revert detector: restore
    ``db_path.with_suffix(db_path.suffix + ".bak_pre_restore")`` →
    both timestamps collide on the same path.
    """
    db = Path("C:/tmp/argosy.copy.db")
    b1 = mod.timestamped_backup_path(
        db, now=datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc),
    )
    b2 = mod.timestamped_backup_path(
        db, now=datetime(2026, 8, 8, 12, 0, 1, tzinfo=timezone.utc),
    )
    assert b1 != b2
    assert b1.name == "argosy.copy.db.bak_pre_restore.20260808T120000Z"
    assert b2.name == "argosy.copy.db.bak_pre_restore.20260808T120001Z"
    assert ".bak_pre_restore." in b1.name


def test_d1_backup_integrity_matches_source_rowcount(tmp_path, mod):
    """Integrity + row-count comparison of backup vs source."""
    src = tmp_path / "book.db"
    conn = sqlite3.connect(str(src))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE holdings (symbol TEXT, usd_k REAL)")
    for i in range(20):
        conn.execute("INSERT INTO holdings VALUES (?, ?)", (f"S{i}", float(i)))
    conn.commit()
    conn.close()

    bak = tmp_path / "book.db.bak_pre_restore.probe"
    mod.sqlite_consistent_backup(src, bak)

    s = sqlite3.connect(str(src))
    b = sqlite3.connect(str(bak))
    assert b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (
        s.execute("SELECT count(*) FROM holdings").fetchone()[0]
        == b.execute("SELECT count(*) FROM holdings").fetchone()[0]
        == 20
    )
    assert (
        s.execute("SELECT round(sum(usd_k),1) FROM holdings").fetchone()[0]
        == b.execute("SELECT round(sum(usd_k),1) FROM holdings").fetchone()[0]
    )
    s.close()
    b.close()
