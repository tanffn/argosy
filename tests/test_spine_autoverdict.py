"""Auto-record hook + backfill for the Phase-1 integrity verdict producer.

Wiring-level tests (NOT the assessment logic — that lives in
``test_spine_integrity``): every durably-persisted snapshot gets exactly one
verdict + head; a verdict-recording failure is swallowed and NEVER breaks the
persist; and ``backfill_integrity_verdicts`` heads pre-existing verdict-less
snapshots idempotently.

In-memory SQLite with FOREIGN KEYS ENFORCED — NEVER the live DB.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
from argosy.services import portfolio_snapshot_store as store
from argosy.services.spine import integrity as integrity_mod
from argosy.services.spine.integrity import (
    backfill_integrity_verdicts,
    record_integrity_verdict,
    record_integrity_verdict_if_absent,
)
from argosy.state.models import (
    Base,
    IntegrityVerdict,
    IntegrityVerdictHead,
    PortfolioSnapshotRow,
    User,
)

USER = "u-test"


@pytest.fixture()
def session():
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # enforce the composite head FK
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    sess.add(User(id=USER, plan="free", created_at=datetime.now(timezone.utc)))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _pos(symbol: str, location: str, shares: float, value_k: float,
         price: float) -> PortfolioPosition:
    return PortfolioPosition(
        symbol=symbol, location=location, shares=shares,
        usd_value_k=value_k, current_price=price,
    )


def _snap(positions, *, when: date, source: str = "feed.tsv") -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_date=when, source_path=source, positions=positions,
        allocations=[], nvda_sales=[], real_estate=[], pensions=[],
        fx_usd_nis=3.35, fx_usd_eur=0.92,
    )


def _book(nvda_shares: float, nvda_value_k: float):
    return [
        _pos("NVDA", "schwab", nvda_shares, nvda_value_k, 210.9),
        _pos("CSPX", "leumi", 100.0, 60.0, 600.0),
    ]


def _verdicts_for(session, snapshot_id: int):
    return session.execute(
        sa.select(IntegrityVerdict).where(
            IntegrityVerdict.snapshot_id == snapshot_id
        )
    ).scalars().all()


# ---------------------------------------------------------------------------
# 1. the persist hook records exactly one verdict + head per snapshot
# ---------------------------------------------------------------------------
def test_persist_records_one_verdict_and_head(session):
    row = store.persist_snapshot(
        session, user_id=USER, snapshot=_snap(_book(10940.0, 2307.9), when=date(2026, 8, 8))
    )
    verdicts = _verdicts_for(session, row.id)
    assert len(verdicts) == 1
    head = session.get(IntegrityVerdictHead, row.id)
    assert head is not None
    assert head.current_verdict_id == verdicts[0].id
    assert head.seq == 1


def test_persist_content_hash_matches_row(session):
    from argosy.services.spine.integrity import compute_snapshot_content_hash

    row = store.persist_snapshot(
        session, user_id=USER, snapshot=_snap(_book(10940.0, 2307.9), when=date(2026, 8, 8))
    )
    v = _verdicts_for(session, row.id)[0]
    assert v.snapshot_content_hash == compute_snapshot_content_hash(row)


def test_second_changed_persist_heads_its_own_snapshot(session):
    row1 = store.persist_snapshot(
        session, user_id=USER, snapshot=_snap(_book(10940.0, 2307.9), when=date(2026, 8, 8))
    )
    # A later, richer book (no stale-date / catastrophic-drop guard trip).
    row2 = store.persist_snapshot(
        session, user_id=USER,
        snapshot=_snap(_book(11000.0, 2320.0), when=date(2026, 8, 9)),
    )
    assert row1.id != row2.id
    assert len(_verdicts_for(session, row1.id)) == 1
    assert len(_verdicts_for(session, row2.id)) == 1
    h1 = session.get(IntegrityVerdictHead, row1.id)
    h2 = session.get(IntegrityVerdictHead, row2.id)
    # Each head points at ITS OWN snapshot's verdict.
    assert h1.current_verdict_id == _verdicts_for(session, row1.id)[0].id
    assert h2.current_verdict_id == _verdicts_for(session, row2.id)[0].id
    assert h1.current_verdict_id != h2.current_verdict_id


def test_write_through_if_changed_records_verdict(session):
    row = store.write_through_if_changed(
        session, user_id=USER, snapshot=_snap(_book(10940.0, 2307.9), when=date(2026, 8, 8))
    )
    assert row is not None
    assert len(_verdicts_for(session, row.id)) == 1
    assert session.get(IntegrityVerdictHead, row.id) is not None


# ---------------------------------------------------------------------------
# 2. a verdict-recording failure NEVER breaks the persist
# ---------------------------------------------------------------------------
def test_verdict_failure_does_not_break_persist(session, monkeypatch):
    import argosy.services.spine.integrity as integrity

    def _boom(*_a, **_k):
        raise RuntimeError("verdict recorder exploded")

    monkeypatch.setattr(integrity, "record_integrity_verdict_if_absent", _boom)

    row = store.persist_snapshot(
        session, user_id=USER, snapshot=_snap(_book(10940.0, 2307.9), when=date(2026, 8, 8))
    )
    # The snapshot still landed durably...
    assert row.id is not None
    persisted = session.get(PortfolioSnapshotRow, row.id)
    assert persisted is not None
    # ...but no verdict/head was recorded (the failure was swallowed).
    assert _verdicts_for(session, row.id) == []
    assert session.get(IntegrityVerdictHead, row.id) is None


# ---------------------------------------------------------------------------
# 3. backfill heads pre-existing verdict-less snapshots, idempotently
# ---------------------------------------------------------------------------
def _insert_raw_snapshot(session, positions, *, when: date) -> PortfolioSnapshotRow:
    """Insert a snapshot row DIRECTLY (bypasses the persist hook → no verdict)."""
    row = PortfolioSnapshotRow(
        user_id=USER,
        snapshot_date=when,
        imported_at=datetime.now(timezone.utc),
        source_path="raw.tsv",
        positions_json=json.dumps(positions),
        totals_json="{}",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_backfill_records_missing_and_is_idempotent(session):
    a = _insert_raw_snapshot(
        session,
        [{"symbol": "AAPL", "location": "schwab", "shares": 100,
          "current_price": 200.0, "current_value_local": 20000.0,
          "usd_value_k": 20.0, "currency": "USD"}],
        when=date(2026, 7, 1),
    )
    b = _insert_raw_snapshot(
        session,
        [{"symbol": "MSFT", "location": "schwab", "shares": 50,
          "current_price": 400.0, "current_value_local": 20000.0,
          "usd_value_k": 20.0, "currency": "USD"}],
        when=date(2026, 7, 2),
    )
    assert session.get(IntegrityVerdictHead, a.id) is None
    assert session.get(IntegrityVerdictHead, b.id) is None

    tally = backfill_integrity_verdicts(session)
    assert tally["recorded"] == 2
    assert tally["skipped"] == 0
    assert tally["failed"] == 0
    assert session.get(IntegrityVerdictHead, a.id) is not None
    assert session.get(IntegrityVerdictHead, b.id) is not None
    assert len(_verdicts_for(session, a.id)) == 1
    assert len(_verdicts_for(session, b.id)) == 1

    # Second run is a pure no-op (every snapshot already headed).
    tally2 = backfill_integrity_verdicts(session)
    assert tally2["recorded"] == 0
    assert tally2["skipped"] == 2
    assert len(_verdicts_for(session, a.id)) == 1  # not re-appended
    assert len(_verdicts_for(session, b.id)) == 1


def test_backfill_skips_hook_recorded_snapshot(session):
    row = store.persist_snapshot(
        session, user_id=USER, snapshot=_snap(_book(10940.0, 2307.9), when=date(2026, 8, 8))
    )
    assert session.get(IntegrityVerdictHead, row.id) is not None  # hook already headed
    tally = backfill_integrity_verdicts(session)
    assert tally["recorded"] == 0
    assert tally["skipped"] == 1
    assert len(_verdicts_for(session, row.id)) == 1  # untouched


# ---------------------------------------------------------------------------
# defect 1 — record-if-absent: exactly-once semantics
# ---------------------------------------------------------------------------
def test_record_if_absent_noop_when_head_exists(session):
    row = _insert_raw_snapshot(
        session,
        [{"symbol": "AAPL", "location": "schwab", "shares": 100,
          "current_price": 200.0, "current_value_local": 20000.0,
          "usd_value_k": 20.0, "currency": "USD"}],
        when=date(2026, 7, 1),
    )
    # First absent-record writes; second is a pure no-op (head already present).
    v1 = record_integrity_verdict_if_absent(session, USER, row)
    assert v1 is not None
    v2 = record_integrity_verdict_if_absent(session, USER, row)
    assert v2 is None
    assert len(_verdicts_for(session, row.id)) == 1  # NOT appended a 2nd time


def test_record_if_absent_real_race_constraint_arbitrates(session, monkeypatch):
    # Reproduce the EXACT uncovered timing: a concurrent writer COMMITS seq=1 +
    # head in the window AFTER our absence check but BEFORE our first verdict
    # insert. A check-then-max+1 seam would read prior_max=1 and silently append
    # seq=2 (duplicate [1,2]); the constraint-arbitrated claim must instead COLLIDE
    # on UNIQUE(snapshot_id, verdict_seq=1), roll back, and SKIP (return None),
    # leaving exactly ONE verdict (seq=1) and head.seq=1.
    from argosy.services.spine.integrity import RESULT_PASS, THRESHOLD_POLICY_VERSION

    row = _insert_raw_snapshot(
        session,
        [{"symbol": "AAPL", "location": "schwab", "shares": 100,
          "current_price": 200.0, "current_value_local": 20000.0,
          "usd_value_k": 20.0, "currency": "USD"}],
        when=date(2026, 7, 1),
    )

    real_assess = integrity_mod.assess_snapshot_integrity
    state = {"raced": False}

    def _assess_then_race(sess, uid, snap):
        # Fires once, during if-absent's work — i.e. after its absence check
        # passed but before its own seq=1 INSERT.
        if not state["raced"]:
            state["raced"] = True
            comp = IntegrityVerdict(
                user_id=uid, snapshot_id=snap.id, result=RESULT_PASS,
                snapshot_content_hash="c" * 64, verdict_seq=1,
                threshold_policy_version=THRESHOLD_POLICY_VERSION,
                detail_json="{}", authored_at=datetime.now(timezone.utc),
            )
            sess.add(comp)
            sess.flush()
            sess.add(IntegrityVerdictHead(
                snapshot_id=snap.id, current_verdict_id=comp.id, seq=1,
            ))
            sess.commit()  # concurrent writer's seq=1 is now committed
        return real_assess(sess, uid, snap)

    monkeypatch.setattr(integrity_mod, "assess_snapshot_integrity", _assess_then_race)

    result = record_integrity_verdict_if_absent(session, USER, row)
    assert result is None  # our seq=1 claim collided -> SKIP, not append seq=2
    verdicts = _verdicts_for(session, row.id)
    assert [v.verdict_seq for v in verdicts] == [1]  # exactly one, NOT [1, 2]
    head = session.get(IntegrityVerdictHead, row.id)
    assert head is not None and head.seq == 1


def test_record_if_absent_collides_on_preexisting_seq1(session):
    # A prior verdict already claimed seq=1 (via the explicit recorder). Even if
    # the fast-path head check is bypassed, the seq=1 claim must collide -> skip.
    row = _insert_raw_snapshot(
        session,
        [{"symbol": "AAPL", "location": "schwab", "shares": 100,
          "current_price": 200.0, "current_value_local": 20000.0,
          "usd_value_k": 20.0, "currency": "USD"}],
        when=date(2026, 7, 1),
    )
    record_integrity_verdict(session, USER, row)  # seq=1 + head land
    assert record_integrity_verdict_if_absent(session, USER, row) is None
    assert [v.verdict_seq for v in _verdicts_for(session, row.id)] == [1]


# ---------------------------------------------------------------------------
# defect 2 — the restore path records a verdict
# ---------------------------------------------------------------------------
def test_restore_path_records_verdict(session):
    from argosy.services.holding_books import backfill_restored_holdings_book

    # Two account-disjoint snapshots: the latest (schwab only) dropped leumi, so
    # the reconstruction (schwab from latest + leumi from the older) does NOT
    # match the latest -> a restored row is written (not a noop).
    _insert_raw_snapshot(
        session,
        [{"symbol": "CSPX", "location": "leumi", "shares": 10,
          "current_price": 600.0, "current_value_local": 6000.0,
          "usd_value_k": 60.0, "currency": "ILS", "asset_type": "ETF"}],
        when=date(2026, 7, 1),
    )
    _insert_raw_snapshot(
        session,
        [{"symbol": "NVDA", "location": "schwab", "shares": 100,
          "current_price": 210.0, "current_value_local": 21000.0,
          "usd_value_k": 21.0, "currency": "USD", "asset_type": "Stock"}],
        when=date(2026, 8, 1),
    )
    result = backfill_restored_holdings_book(
        session, user_id=USER,
        expected_position_count=None, expected_usd_k=None,
    )
    assert result["status"] == "restored", result
    snap_id = result["snapshot_id"]
    assert session.get(IntegrityVerdictHead, snap_id) is not None
    assert len(_verdicts_for(session, snap_id)) == 1


# ---------------------------------------------------------------------------
# defect 3 — a recorder DB error leaves the caller's session usable
# ---------------------------------------------------------------------------
def test_recorder_db_error_leaves_session_usable(session, monkeypatch):
    # Simulate a transaction-invalidating DB error raised INSIDE the recorder,
    # BEFORE any internal rollback-try. The best-effort wrapper must roll the
    # caller's session back so subsequent caller ops still work.
    def _poison(sess, *_a, **_k):
        sess.execute(sa.text("SELECT * FROM __no_such_table__"))  # invalidates txn

    monkeypatch.setattr(integrity_mod, "record_integrity_verdict_if_absent", _poison)

    row = store.persist_snapshot(
        session, user_id=USER, snapshot=_snap(_book(10940.0, 2307.9), when=date(2026, 8, 8))
    )
    # Snapshot still landed durably; no verdict recorded.
    assert row.id is not None
    assert session.get(IntegrityVerdictHead, row.id) is None

    # A follow-up query on the SAME session succeeds (session not left dirty).
    rows = session.execute(sa.select(PortfolioSnapshotRow)).scalars().all()
    assert any(r.id == row.id for r in rows)


# ---------------------------------------------------------------------------
# defect 4 — backfill continues past a per-row failure (incl. head lookup)
# ---------------------------------------------------------------------------
def test_backfill_continues_past_failing_row(session, monkeypatch):
    a = _insert_raw_snapshot(
        session,
        [{"symbol": "AAPL", "location": "schwab", "shares": 100,
          "current_price": 200.0, "current_value_local": 20000.0,
          "usd_value_k": 20.0, "currency": "USD"}],
        when=date(2026, 7, 1),
    )
    b = _insert_raw_snapshot(
        session,
        [{"symbol": "MSFT", "location": "schwab", "shares": 50,
          "current_price": 400.0, "current_value_local": 20000.0,
          "usd_value_k": 20.0, "currency": "USD"}],
        when=date(2026, 7, 2),
    )

    real = record_integrity_verdict_if_absent

    def _flaky(sess, user_id, snapshot_row):
        # Row `a` fails during processing (stands in for a head-lookup DB error);
        # row `b` proceeds normally — the run must NOT abort on `a`.
        if snapshot_row.id == a.id:
            raise RuntimeError("simulated head-lookup / record failure")
        return real(sess, user_id, snapshot_row)

    monkeypatch.setattr(integrity_mod, "record_integrity_verdict_if_absent", _flaky)

    tally = backfill_integrity_verdicts(session)
    assert tally["failed"] == 1
    assert tally["recorded"] == 1
    assert session.get(IntegrityVerdictHead, a.id) is None  # the failed row
    assert session.get(IntegrityVerdictHead, b.id) is not None  # the loop continued
