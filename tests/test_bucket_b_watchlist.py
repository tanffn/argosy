"""Tests for argosy.services.anomaly.bucket_b_watchlist (sprint #2 commit #6).

Covers the 4-state Pattern B1 state machine:

  MATCHED → MISSING : FIRES   (the only firing transition per spec §1.2)
  MATCHED → MATCHED : no-fire
  MATCHED → PARTIAL : no-fire
  MATCHED → UNKNOWN : no-fire (statement late, not the discount line)
  UNKNOWN → MISSING : no-fire (no baseline)
  PARTIAL → MISSING : no-fire (charge missing prior; waiver moot)
  MISSING → MISSING : no-fire (duplicate noise)

Plus:
  * Severity is ``critical`` when missing amount > ₪50, else ``warning``.
  * Idempotent: a repeated firing of the same transition does NOT create
    a second open queue row (partial unique index on dedup_key).
  * ``track_watchlist_observation`` UPSERT (no duplicate rows).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_bucket_b_watchlist.py -v
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.anomaly.bucket_b_watchlist import (
    CRITICAL_AMOUNT_THRESHOLD_NIS,
    check_fee_waiver_transition,
    track_watchlist_observation,
)
from argosy.state.models import (
    Base,
    ExpenseReviewQueue,
    User,
    WatchlistObservation,
)


USER = "ariel"
ENTRY = "discount_bank_card_2923_fee_waiver"


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite session — mirrors test_merchant_rolling_stats.py setup."""
    db_path = tmp_path / "bucket_b_watchlist.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# track_watchlist_observation
# ---------------------------------------------------------------------------


def test_track_observation_inserts_first_row(sync_session):
    row = track_watchlist_observation(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        observation_period=date(2026, 4, 1),
        status="MATCHED",
        evidence_tx_ids=[101, 102],
    )
    sync_session.commit()

    assert row.id is not None
    assert row.status == "MATCHED"
    assert row.evidence_tx_ids == "[101, 102]"
    rows = sync_session.execute(sa.select(WatchlistObservation)).scalars().all()
    assert len(rows) == 1


def test_track_observation_upserts_on_same_period(sync_session):
    track_watchlist_observation(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        observation_period=date(2026, 4, 1),
        status="MATCHED",
    )
    sync_session.commit()

    # Re-track same (user, entry, period) with a different status — should
    # UPSERT, not insert a duplicate.
    track_watchlist_observation(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        observation_period=date(2026, 4, 1),
        status="MISSING",
        evidence_tx_ids=[202],
    )
    sync_session.commit()

    rows = sync_session.execute(sa.select(WatchlistObservation)).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "MISSING"
    assert rows[0].evidence_tx_ids == "[202]"


def test_track_observation_rejects_invalid_status(sync_session):
    with pytest.raises(ValueError):
        track_watchlist_observation(
            sync_session,
            user_id=USER,
            watchlist_entry_id=ENTRY,
            observation_period=date(2026, 4, 1),
            status="BOGUS",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# State-transition firing matrix
# ---------------------------------------------------------------------------


def _record(session, period: date, status: str) -> None:
    track_watchlist_observation(
        session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        observation_period=period,
        status=status,  # type: ignore[arg-type]
    )


def _queue_rows(session) -> list[ExpenseReviewQueue]:
    return (
        session.execute(
            sa.select(ExpenseReviewQueue).where(
                ExpenseReviewQueue.user_id == USER
            )
        )
        .scalars()
        .all()
    )


def test_transition_matched_to_missing_fires(sync_session):
    """The ONLY firing transition per spec §1.2."""
    _record(sync_session, date(2026, 3, 1), "MATCHED")
    _record(sync_session, date(2026, 4, 1), "MISSING")
    sync_session.commit()

    result = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),
        missing_amount_nis=Decimal("18.00"),
    )
    sync_session.commit()

    assert result.fired is True
    assert result.prior_status == "MATCHED"
    assert result.current_status == "MISSING"
    assert result.dedup_key is not None
    assert "v1|b1|" in result.dedup_key
    assert "transition:matched_missing" in result.dedup_key

    rows = _queue_rows(sync_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "bucket_b_fee_waiver_missing"
    assert row.bucket == "recurring"
    assert row.materiality == "warning"  # ₪18 < ₪50 threshold
    assert row.status == "open"


def test_transition_unknown_to_missing_does_not_fire(sync_session):
    """No baseline → don't fire (could be first observation)."""
    # No prior observation at all → prior_status is None.
    _record(sync_session, date(2026, 4, 1), "MISSING")
    sync_session.commit()

    result = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),
    )
    sync_session.commit()

    assert result.fired is False
    assert result.prior_status is None
    assert _queue_rows(sync_session) == []


def test_transition_matched_to_unknown_does_not_fire(sync_session):
    """Statement missing this period — surface separately, not B1."""
    _record(sync_session, date(2026, 3, 1), "MATCHED")
    _record(sync_session, date(2026, 4, 1), "UNKNOWN")
    sync_session.commit()

    result = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),
    )
    sync_session.commit()

    assert result.fired is False
    assert result.prior_status == "MATCHED"
    assert result.current_status == "UNKNOWN"
    assert _queue_rows(sync_session) == []


def test_transition_partial_to_missing_does_not_fire(sync_session):
    """Charge wasn't present last period → waiver-vs-no-waiver moot."""
    _record(sync_session, date(2026, 3, 1), "PARTIAL")
    _record(sync_session, date(2026, 4, 1), "MISSING")
    sync_session.commit()

    result = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),
    )
    sync_session.commit()

    assert result.fired is False
    assert result.prior_status == "PARTIAL"
    assert _queue_rows(sync_session) == []


def test_transition_missing_to_missing_does_not_fire(sync_session):
    """Duplicate noise: already alerted previously."""
    _record(sync_session, date(2026, 3, 1), "MISSING")
    _record(sync_session, date(2026, 4, 1), "MISSING")
    sync_session.commit()

    result = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),
    )
    sync_session.commit()

    assert result.fired is False
    assert result.prior_status == "MISSING"
    assert _queue_rows(sync_session) == []


def test_transition_matched_to_matched_does_not_fire(sync_session):
    """Steady state — explicit no-op."""
    _record(sync_session, date(2026, 3, 1), "MATCHED")
    _record(sync_session, date(2026, 4, 1), "MATCHED")
    sync_session.commit()

    result = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),
    )
    sync_session.commit()

    assert result.fired is False
    assert _queue_rows(sync_session) == []


def test_severity_bumps_to_critical_when_amount_exceeds_threshold(sync_session):
    """Missing waiver > ₪50 → critical (user silently losing real money)."""
    _record(sync_session, date(2026, 3, 1), "MATCHED")
    _record(sync_session, date(2026, 4, 1), "MISSING")
    sync_session.commit()

    high_amount = CRITICAL_AMOUNT_THRESHOLD_NIS + Decimal("10.00")
    result = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),
        missing_amount_nis=high_amount,
    )
    sync_session.commit()

    assert result.fired is True
    rows = _queue_rows(sync_session)
    assert len(rows) == 1
    assert rows[0].materiality == "critical"


def test_idempotent_rerun_does_not_create_duplicate_row(sync_session):
    """Re-running the detector over the same data must not create a
    second open queue row (the partial unique index is the contract)."""
    _record(sync_session, date(2026, 3, 1), "MATCHED")
    _record(sync_session, date(2026, 4, 1), "MISSING")
    sync_session.commit()

    first = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),
        missing_amount_nis=Decimal("18.00"),
    )
    sync_session.commit()
    assert first.fired is True

    second = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),
        missing_amount_nis=Decimal("18.00"),
    )
    sync_session.commit()
    assert second.fired is True  # treated as "alert is recorded"
    assert second.dedup_key == first.dedup_key

    rows = _queue_rows(sync_session)
    assert len(rows) == 1  # still only one open row


def test_current_period_without_observation_returns_no_fire(sync_session):
    """Caller must track_watchlist_observation() first — without it we
    can't evaluate the transition and silently no-op."""
    _record(sync_session, date(2026, 3, 1), "MATCHED")
    sync_session.commit()

    result = check_fee_waiver_transition(
        sync_session,
        user_id=USER,
        watchlist_entry_id=ENTRY,
        current_period=date(2026, 4, 1),  # never recorded
    )
    sync_session.commit()
    assert result.fired is False
    assert _queue_rows(sync_session) == []
