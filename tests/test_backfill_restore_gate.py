"""Repair gate — quiesce, WAL checkpoint, and CONTENT-verified backups.

The owner's repair gate (2026-08-08) requires, in order: a quiesced database,
``wal_checkpoint(TRUNCATE)``, and a backup verified by CONTENT reconciliation
rather than ``PRAGMA integrity_check`` — which is disqualified because a
``shutil.copy2`` of a WAL-mode database can be a 4 KB empty-header file that
still reports ``ok``.

Never touches live ``db/argosy.db``.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "backfill_restored_holdings_book.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "backfill_restored_holdings_book_gate", _SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_script()


def _book_db(
    path: Path,
    positions: list[dict],
    snapshot_id: int = 49,
    *,
    keep_open: bool = False,
) -> sqlite3.Connection | None:
    """Minimal portfolio_snapshots shape that book_fingerprint reads.

    ``keep_open=True`` leaves the writer connected, which is what makes the WAL
    hazard real: SQLite checkpoints on last-connection close, so a cleanly
    closed database copies fine with ``shutil.copy2``. The data loss happens
    only while a connection still holds un-checkpointed WAL content — i.e.
    exactly the production case with the backend running.
    """
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE portfolio_snapshots ("
        "id INTEGER PRIMARY KEY, user_id TEXT, positions_json TEXT)"
    )
    con.execute(
        "INSERT INTO portfolio_snapshots (id, user_id, positions_json) "
        "VALUES (?, 'ariel', ?)",
        (snapshot_id, json.dumps(positions)),
    )
    con.commit()
    if keep_open:
        return con
    con.close()
    return None


_POSITIONS = [
    {"symbol": "NVDA", "location": "schwab", "shares": 10940.0, "usd_value_k": 2307.9},
    {"symbol": "CSPX", "location": "leumi", "shares": 10.0, "usd_value_k": 8.0},
]


def test_gate_backup_verification_rejects_a_copy2_backup(tmp_path, mod):
    """A copy2 'backup' of a WAL database must FAIL content reconciliation.

    Revert detector: make ``verify_backup_against_source`` return True on any
    readable file (or check only ``PRAGMA integrity_check``) → this fails,
    because the copy2 artefact passes integrity while lacking the content.
    """
    src = tmp_path / "book.db"
    writer = _book_db(src, _POSITIONS, keep_open=True)
    assert writer is not None
    try:
        assert mod.wal_sidecar_size(src) > 0, "expected content held in the WAL"

        bad = tmp_path / "book.db.copy2"
        shutil.copy2(src, bad)  # main file only — content is still in -wal

        ok, detail = mod.verify_backup_against_source(src, bad, "ariel")
        assert ok is False, f"copy2 backup must not verify, got: {detail}"

        good = tmp_path / "book.db.backupapi"
        mod.sqlite_consistent_backup(src, good)
        ok, detail = mod.verify_backup_against_source(src, good, "ariel")
        assert ok is True, detail
        assert "row_hash=" in detail
    finally:
        writer.close()


def test_gate_integrity_check_alone_would_bless_garbage(tmp_path, mod):
    """Documents WHY content reconciliation replaced the structural check."""
    src = tmp_path / "book.db"
    writer = _book_db(src, _POSITIONS, keep_open=True)
    assert writer is not None
    try:
        bad = tmp_path / "book.db.copy2"
        shutil.copy2(src, bad)

        con = sqlite3.connect(f"file:{bad}?mode=ro", uri=True)
        structural = con.execute("PRAGMA integrity_check").fetchone()[0]
        try:
            con.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()
            readable = True
        except sqlite3.OperationalError:
            readable = False
        con.close()
    finally:
        writer.close()

    # The trap: structurally "ok" yet missing the table entirely.
    assert structural == "ok"
    assert readable is False


def test_gate_fingerprint_detects_a_changed_value(tmp_path, mod):
    """Row-hash must move when money moves, else reconciliation is cosmetic."""
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    _book_db(a, _POSITIONS)
    changed = [dict(p) for p in _POSITIONS]
    changed[0]["usd_value_k"] = 2307.8  # one cent-scale change
    _book_db(b, changed)

    fa = mod.book_fingerprint(a, "ariel")
    fb = mod.book_fingerprint(b, "ariel")
    assert fa["positions"] == fb["positions"]
    assert fa["row_hash"] != fb["row_hash"]


def test_gate_quiesce_check_detects_an_active_writer(tmp_path, mod):
    """Gate step 1 — an active writer must be refused, not merely noted.

    Revert detector: drop the settle-window comparison (always return True) →
    this fails.
    """
    src = tmp_path / "busy.db"
    _book_db(src, _POSITIONS)

    stop = threading.Event()

    def writer():
        con = sqlite3.connect(str(src), timeout=30)
        i = 0
        while not stop.is_set():
            con.execute(
                "INSERT INTO portfolio_snapshots (user_id, positions_json) "
                "VALUES ('noise', '[]')"
            )
            con.commit()
            i += 1
            time.sleep(0.01)
        con.close()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        ok, detail = mod.quiesce_check(src, 0.5)
    finally:
        stop.set()
        t.join(timeout=5)

    assert ok is False, f"an active writer must be detected, got: {detail}"
    assert "ACTIVE" in detail


def test_gate_quiesce_check_passes_when_idle(tmp_path, mod):
    src = tmp_path / "idle.db"
    _book_db(src, _POSITIONS)
    ok, detail = mod.quiesce_check(src, 0.2)
    assert ok is True, detail


def test_gate_checkpoint_folds_wal_into_main(tmp_path, mod):
    """Gate step 2 — after checkpointing, the WAL must be truncated."""
    src = tmp_path / "wal.db"
    writer = _book_db(src, _POSITIONS, keep_open=True)
    assert writer is not None
    try:
        for i in range(50):
            writer.execute(
                "INSERT INTO portfolio_snapshots (user_id, positions_json) "
                "VALUES (?, '[]')",
                (f"u{i}",),
            )
        writer.commit()

        assert mod.wal_sidecar_size(src) > 0, "expected an un-checkpointed WAL"
        detail = mod.checkpoint_wal(src)
        assert mod.wal_sidecar_size(src) == 0, detail
    finally:
        writer.close()


def test_gate_guard_accepts_a_staged_copy_named_argosy_db(tmp_path, mod):
    """The guard must key on identity, never the basename.

    Refusing a legitimately-named staged copy forces
    ``--i-really-mean-the-live-db`` in rehearsal, making rehearsal
    indistinguishable from production.

    Revert detector: re-add ``db_path.name == "argosy.db"`` to the refusal →
    this fails.
    """
    staged = tmp_path / "stage" / "db" / "argosy.db"
    staged.parent.mkdir(parents=True)
    _book_db(staged, _POSITIONS)

    # Asserted at the layer that DECIDES the refusal, not on is_live_db_path —
    # otherwise a basename check reintroduced in the caller would slip through.
    assert mod.refuse_reason(staged, override=False) is None
    assert mod.is_live_db_path(staged) is False


def test_gate_guard_refuses_a_hardlink_alias_of_the_live_db(tmp_path, mod, monkeypatch):
    """Identity, not name: an aliased path pointing at live must be refused."""
    fake_home = tmp_path / "home"
    (fake_home / "db").mkdir(parents=True)
    live = fake_home / "db" / "argosy.db"
    _book_db(live, _POSITIONS)
    monkeypatch.setenv("ARGOSY_HOME", str(fake_home))

    alias = tmp_path / "totally_safe_copy.db"
    try:
        Path(alias).hardlink_to(live)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks unavailable on this filesystem")

    assert mod.refuse_reason(alias, override=False) is not None
    assert mod.refuse_reason(alias, override=True) is None
