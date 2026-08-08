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
import itertools
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


def test_gate_interloper_watch_detects_a_commit_a_fresh_connection_misses(
    tmp_path, mod,
):
    """`PRAGMA data_version` only means anything on a HELD connection.

    The first version of this guard opened a fresh connection per sample, which
    made the comparison inert: a review reproduced an intervening commit reading
    2 -> 2. This pins both halves — the fresh-connection approach missing it,
    and the held connection catching it.

    Revert detector: sample from a new connection each time → this fails.
    """
    src = tmp_path / "watched.db"
    _book_db(src, _POSITIONS)

    def fresh_sample() -> int:
        con = sqlite3.connect(str(src), timeout=5)
        try:
            return con.execute("PRAGMA data_version").fetchone()[0]
        finally:
            con.close()

    before_fresh = fresh_sample()

    watch = mod.InterloperWatch(src)
    watch.start()
    try:
        moved, detail = watch.moved()
        assert moved is False, f"nothing has happened yet: {detail}"

        intruder = sqlite3.connect(str(src), timeout=10)
        intruder.execute(
            "INSERT INTO portfolio_snapshots (user_id, positions_json) "
            "VALUES ('scheduler', '[]')"
        )
        intruder.commit()
        intruder.close()

        moved, detail = watch.moved()
    finally:
        watch.close()

    after_fresh = fresh_sample()
    assert before_fresh == after_fresh, (
        "precondition: a fresh connection per sample cannot see the commit "
        f"({before_fresh} -> {after_fresh}) — that was the original bug"
    )
    assert moved is True, f"the held watch must see the commit: {detail}"
    assert "data_version" in detail


def test_gate_backup_missing_an_unrelated_table_is_rejected(tmp_path, mod):
    """A rollback point is the WHOLE database, not just the latest book.

    A backup can carry a byte-identical latest snapshot while having lost the
    `proposals` table entirely. Reconciling only the book blessed it.

    Revert detector: drop the whole-database reconciliation → this fails.
    """
    src = tmp_path / "book.db"
    _book_db(src, _POSITIONS)
    con = sqlite3.connect(str(src))
    con.execute("CREATE TABLE proposals (id INTEGER PRIMARY KEY, note TEXT)")
    con.execute("INSERT INTO proposals (note) VALUES ('approve NVDA trim')")
    con.commit()
    con.close()

    # A "backup" with the same book but the other table missing.
    bad = tmp_path / "book.db.partial"
    mod.sqlite_consistent_backup(src, bad)
    con = sqlite3.connect(str(bad))
    con.execute("DROP TABLE proposals")
    con.commit()
    con.close()

    ok, detail = mod.verify_backup_against_source(src, bad, "ariel")
    assert ok is False, f"a backup missing a table must not verify: {detail}"
    assert "proposals" in detail

    # And a row-count difference in an unrelated table must also fail.
    thinned = tmp_path / "book.db.thinned"
    mod.sqlite_consistent_backup(src, thinned)
    con = sqlite3.connect(str(thinned))
    con.execute("DELETE FROM proposals")
    con.commit()
    con.close()
    ok2, detail2 = mod.verify_backup_against_source(src, thinned, "ariel")
    assert ok2 is False, f"row-count loss must not verify: {detail2}"

    # A row-count-preserving CONTENT change must also fail.
    # Revert detector: reconcile COUNT(*) instead of hashing rows → this fails.
    edited = tmp_path / "book.db.edited"
    mod.sqlite_consistent_backup(src, edited)
    con = sqlite3.connect(str(edited))
    con.execute("UPDATE proposals SET note = 'approve NVDA SELL ALL'")
    con.commit()
    con.close()
    ok_edit, detail_edit = mod.verify_backup_against_source(src, edited, "ariel")
    assert ok_edit is False, (
        f"an altered row with an unchanged count must not verify: {detail_edit}"
    )
    assert "CONTENT" in detail_edit

    # A TEXT value replaced by a BLOB of the SAME bytes is a type change, not a
    # no-op. Revert detector: use a plain bytes text_factory → this fails.
    retyped = tmp_path / "book.db.retyped"
    mod.sqlite_consistent_backup(src, retyped)
    con = sqlite3.connect(str(retyped))
    con.execute("UPDATE proposals SET note = CAST(note AS BLOB)")
    con.commit()
    con.close()
    ok_ty, detail_ty = mod.verify_backup_against_source(src, retyped, "ariel")
    assert ok_ty is False, f"TEXT -> BLOB must not verify: {detail_ty}"

    # A changed VIEW definition is a schema change the rollback point must keep.
    # Revert detector: fingerprint only type='table' rows → this fails.
    con = sqlite3.connect(str(src))
    con.execute("CREATE VIEW v_open AS SELECT id FROM proposals")
    con.commit()
    con.close()
    viewed = tmp_path / "book.db.viewed"
    mod.sqlite_consistent_backup(src, viewed)
    con = sqlite3.connect(str(viewed))
    con.execute("DROP VIEW v_open")
    con.execute("CREATE VIEW v_open AS SELECT note FROM proposals")
    con.commit()
    con.close()
    ok_v, detail_v = mod.verify_backup_against_source(src, viewed, "ariel")
    assert ok_v is False, f"a changed view must not verify: {detail_v}"
    assert "schema" in detail_v

    # Control: a faithful backup still passes.
    good = tmp_path / "book.db.good"
    mod.sqlite_consistent_backup(src, good)
    ok3, detail3 = mod.verify_backup_against_source(src, good, "ariel")
    assert ok3 is True, detail3
    assert "whole-DB" in detail3


def test_gate_value_encoding_is_injective_across_types(tmp_path, mod):
    """Distinct values must never share a fingerprint.

    Tagging TEXT by PREFIXING a marker onto the payload was in-band signalling,
    and a review collided it: TEXT "abc" and BLOB b"\\x00T\\x00abc" fingerprinted
    identically, so `verify_backup_against_source` accepted two unequal
    databases. The type is now carried out of band and every value is length
    framed.

    Revert detector: go back to a prefixing text_factory → the first pair
    collides and this fails.
    """
    src = tmp_path / "typed.db"
    _book_db(src, _POSITIONS)

    counter = itertools.count()

    def fingerprint_with(value_sql: str, params: tuple) -> str:
        db = tmp_path / f"probe{next(counter)}.db"
        mod.sqlite_consistent_backup(src, db)
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE probe (v)")
        con.execute(f"INSERT INTO probe (v) VALUES ({value_sql})", params)
        con.commit()
        con.close()
        return mod.database_fingerprint(db)["row_counts"]["probe"]

    # The exact collision the review reproduced.
    text_abc = fingerprint_with("?", ("abc",))
    blob_tagged = fingerprint_with("?", (sqlite3.Binary(b"\x00T\x00abc"),))
    assert text_abc != blob_tagged, (
        "TEXT 'abc' must not fingerprint as BLOB b'\\x00T\\x00abc'"
    )

    # The plain form of the same confusion, and numeric type confusion.
    blob_abc = fingerprint_with("?", (sqlite3.Binary(b"abc"),))
    assert text_abc != blob_abc, "TEXT must not fingerprint as an equal BLOB"
    assert fingerprint_with("?", (1,)) != fingerprint_with("?", (1.0,)), (
        "INTEGER 1 must not fingerprint as REAL 1.0"
    )
    assert fingerprint_with("NULL", ()) != fingerprint_with("?", ("",)), (
        "NULL must not fingerprint as an empty string"
    )

    # Length framing specifically: with type tags but NO lengths, a value that
    # contains a tag sequence can swallow the boundary with its neighbour, so
    # ("aT|b", "c") and ("a", "bT|c") both encode to T|aT|bT|c. The lengths are
    # what keep the columns apart.
    # Revert detector: drop the length from _encode_value → this fails.
    def fingerprint_pair(a: str, b: str) -> str:
        db = tmp_path / f"pair{next(counter)}.db"
        mod.sqlite_consistent_backup(src, db)
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE pair (a, b)")
        con.execute("INSERT INTO pair (a, b) VALUES (?, ?)", (a, b))
        con.commit()
        con.close()
        return mod.database_fingerprint(db)["row_counts"]["pair"]

    assert fingerprint_pair("aT|b", "c") != fingerprint_pair("a", "bT|c"), (
        "adjacent columns must not be able to shift the frame between them"
    )

    # Non-UTF8 text must survive rather than crash the fingerprint.
    weird = fingerprint_with("CAST(? AS TEXT)", (sqlite3.Binary(b"\xff\xfe"),))
    assert weird, "non-UTF8 TEXT must still fingerprint"


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


def test_gate_quiesce_check_detects_a_writer_that_keeps_the_wal_size_constant(
    tmp_path, mod,
):
    """Gate step 1 must detect a writer the SIZE heuristic could not see.

    The original check compared the ``-wal`` sidecar size at two instants. With
    ``wal_autocheckpoint`` active a steady writer holds that size constant: a
    measured run committed 143 transactions while the sidecar stayed at exactly
    4152 bytes and the gate reported "quiesced". This pins the replacement
    (``PRAGMA data_version`` from one held-open connection, plus an EXCLUSIVE
    probe).

    Revert detector: go back to comparing ``wal_sidecar_size`` twice → this
    fails, because the sizes are equal while the writer runs.
    """
    src = tmp_path / "busy.db"
    _book_db(src, _POSITIONS)
    con = sqlite3.connect(str(src))
    con.execute("PRAGMA wal_autocheckpoint=1")
    con.close()

    stop = threading.Event()
    commits = {"n": 0}

    def writer():
        con = sqlite3.connect(str(src), timeout=30)
        con.execute("PRAGMA wal_autocheckpoint=1")
        while not stop.is_set():
            con.execute(
                "INSERT OR REPLACE INTO portfolio_snapshots "
                "(id, user_id, positions_json) VALUES (9999, 'noise', '[]')"
            )
            con.commit()
            commits["n"] += 1
            time.sleep(0.005)
        con.close()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    time.sleep(0.2)
    sizes = []
    try:
        sizes = [mod.wal_sidecar_size(src) for _ in range(3)]
        before = commits["n"]
        ok, detail = mod.quiesce_check(src, 0.5)
        during = commits["n"] - before
    finally:
        stop.set()
        t.join(timeout=5)

    assert during > 0, "the writer must have committed during the window"
    assert ok is False, (
        f"a writer committing {during} txns must be detected; sizes={sizes} "
        f"detail={detail}"
    )
    assert "data_version" in detail


def test_gate_quiesce_check_refuses_a_zero_length_window(tmp_path, mod):
    """`--settle-seconds 0` silently reported "quiesced" — it must refuse.

    Revert detector: return True for a zero window → this fails.
    """
    src = tmp_path / "zero.db"
    _book_db(src, _POSITIONS)
    ok, detail = mod.quiesce_check(src, 0)
    assert ok is False
    assert "REFUSED" in detail


def test_gate_quiesce_check_passes_when_idle(tmp_path, mod):
    """Characterization: the gate must not refuse a genuinely idle database.

    This one does NOT bite on revert (a broken gate also passes an idle DB); it
    exists to catch over-tightening that would block the real repair.
    """
    src = tmp_path / "idle.db"
    _book_db(src, _POSITIONS)
    ok, detail = mod.quiesce_check(src, 0.2)
    assert ok is True, detail


def test_gate_checkpoint_reports_failure_when_a_reader_pins_the_wal(tmp_path, mod):
    """An incomplete checkpoint must be a FAILURE, not a log line.

    With a reader holding an open read transaction, TRUNCATE returns busy=1 /
    checkpointed=0 and leaves the WAL in place. The previous version returned
    only a string, so the repair proceeded and backed up a database whose WAL
    had not been folded in.

    Revert detector: return just the detail string (no pass/fail) → this fails.
    """
    src = tmp_path / "pinned.db"
    _book_db(src, _POSITIONS)

    reader = sqlite3.connect(str(src), timeout=30)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM portfolio_snapshots").fetchall()
    writer = sqlite3.connect(str(src), timeout=30)
    writer.execute(
        "INSERT INTO portfolio_snapshots (user_id, positions_json) "
        "VALUES ('pending', '[]')"
    )
    writer.commit()
    try:
        ok, detail = mod.checkpoint_wal(src)
    finally:
        reader.rollback()
        reader.close()
        writer.close()

    assert ok is False, f"a pinned WAL must fail the checkpoint gate: {detail}"
    assert "INCOMPLETE" in detail


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
        ok, detail = mod.checkpoint_wal(src)
        assert ok is True, detail
        assert mod.wal_sidecar_size(src) == 0, detail
    finally:
        writer.close()


def test_gate_unidentifiable_argosy_db_is_refused_without_a_designation(
    tmp_path, mod,
):
    """Identity alone cannot protect a deployment we do not enumerate.

    With ``ARGOSY_HOME`` unset, a real production database at an unknown path
    (a mapped drive, an alternate install) is not identity-matched, so
    identity-only refusal would let the repair write to it.

    Revert detector: refuse on identity ONLY → this fails.
    """
    unknown_prod = tmp_path / "some" / "deployment" / "db" / "argosy.db"
    unknown_prod.parent.mkdir(parents=True)
    _book_db(unknown_prod, _POSITIONS)

    assert mod.is_live_db_path(unknown_prod) is False, "not identity-matched"
    reason = mod.refuse_reason(unknown_prod, override=False)
    assert reason is not None, "an unidentifiable argosy.db must be refused"
    assert "--staged-copy" in reason

    # And the hole a basename heuristic leaves: a production database that is
    # simply named something else must ALSO be refused.
    # Revert detector: guard on `db_path.name == "argosy.db"` → this fails.
    oddly_named = tmp_path / "some" / "deployment" / "production.sqlite"
    _book_db(oddly_named, _POSITIONS)
    assert mod.refuse_reason(oddly_named, override=False) is not None, (
        "a production DB under a non-standard name must not be accepted"
    )


def test_gate_staged_copy_can_never_authorize_the_live_database(
    tmp_path, monkeypatch, mod,
):
    """The most dangerous possible regression: the harmless flag opening prod.

    `--staged-copy` is meant to be safe to type. If it were ever accepted for a
    database we can identify as live, the safe habit would become the loaded
    gun. Only `--i-really-mean-the-live-db` may pass the live database.

    Revert detector: check `staged_copy` before the identity branch, or accept
    either flag → this fails.
    """
    live = tmp_path / "home" / "db" / "argosy.db"
    live.parent.mkdir(parents=True)
    _book_db(live, _POSITIONS)
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path / "home"))

    assert mod.is_live_db_path(live) is True, "precondition: identity-matched"

    reason = mod.refuse_reason(live, override=False, staged_copy=True)
    assert reason is not None, (
        "--staged-copy must NOT authorize the live database"
    )
    assert "live database" in reason
    # Only the explicit live override gets through.
    assert mod.refuse_reason(live, override=True, staged_copy=False) is None


def test_gate_staged_copy_designation_is_distinct_from_the_live_override(
    tmp_path, mod,
):
    """A rehearsal must never need the flag the production repair needs.

    That was the original reason for dropping the blunt basename check: sharing
    one flag trains the operator to type the live override during rehearsals.
    """
    staged = tmp_path / "stage" / "db" / "argosy.db"
    staged.parent.mkdir(parents=True)
    _book_db(staged, _POSITIONS)

    # Rehearsal: the harmless affirmation is enough, and is NOT the live flag.
    assert mod.refuse_reason(staged, override=False, staged_copy=True) is None
    plain = tmp_path / "rehearsal_copy.db"
    _book_db(plain, _POSITIONS)
    assert mod.refuse_reason(plain, override=False, staged_copy=True) is None
    # But nothing is accepted without SOME designation — no guessing.
    assert mod.refuse_reason(plain, override=False) is not None


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
