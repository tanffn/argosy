"""Tests for argosy.services.anomaly.bucket_b_recurring (sprint #2 commit #7).

Covers Pattern B2 (recurring-charge learner + missing detector):

  Learner:
    * 3 monthly Netflix-like txns at the same amount → pattern LEARNED.
    * 2 occurrences → NOT learned (need ≥3).
    * Irregular cadence (15d / 60d gaps) → NOT learned.
    * Amounts outside ±15% band → NOT learned.

  Missing detector:
    * Fires when last_seen + cadence + grace has elapsed without a fresh
      match.
    * Does NOT fire when within grace window.
    * Does NOT fire for status='user_dismissed' patterns.
    * Critical materiality when expected_amount ≥ ₪500; else warning.
    * Idempotent: re-run yields 0 new fires.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_bucket_b_recurring.py -v
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.anomaly.bucket_b_recurring import (
    CRITICAL_AMOUNT_THRESHOLD_NIS,
    detect_missing_recurring,
    learn_recurring_patterns,
)
from argosy.state.models import (
    Base,
    ExpenseReviewQueue,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    RecurringChargePattern,
    User,
    UserFile,
)


USER = "ariel"


# ---------------------------------------------------------------------------
# Fixture (mirrors test_merchant_rolling_stats.py).
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    db_path = tmp_path / "bucket_b_recurring.db"
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


def _make_source_and_statement(db) -> tuple[int, int]:
    f = UserFile(
        user_id=USER, sha256="c" * 64,
        original_name="test.csv", sanitized_name="test.csv",
        mime_type="text/csv", kind="other",
        size_bytes=1, storage_path="/tmp/test",
        source="expense_statement",
    )
    db.add(f)
    db.flush()
    src = ExpenseSource(
        user_id=USER, kind="card", issuer="visa",
        external_id="9999", display_name="Visa 9999",
    )
    db.add(src)
    db.flush()
    stmt = ExpenseStatement(
        user_id=USER, source_id=src.id, file_id=f.id,
        period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        parsed_total_nis=Decimal("1000"),
        declared_total_nis=Decimal("1000"),
        parser_name="visa", parser_version="0.1.0",
        status="parsed",
    )
    db.add(stmt)
    db.flush()
    return src.id, stmt.id


def _add_tx(
    db, source_id: int, statement_id: int, *,
    merchant: str, amount: Decimal | float | int,
    occurred_on: date, direction: str = "debit",
    is_card_payment: bool = False,
) -> None:
    db.add(ExpenseTransaction(
        user_id=USER, source_id=source_id, statement_id=statement_id,
        occurred_on=occurred_on,
        merchant_raw=merchant, merchant_normalized=merchant,
        amount_nis=Decimal(str(amount)),
        direction=direction, tx_type="regular",
        is_card_payment=is_card_payment,
        raw_row_json="{}",
    ))


# ---------------------------------------------------------------------------
# Learner tests.
# ---------------------------------------------------------------------------


def test_learn_pattern_from_three_monthly_netflix_like_txns(sync_session):
    """3 txns at the same merchant + amount + ~30d cadence → LEARNED."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    for i in range(3):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="netflix", amount=Decimal("55.00"),
            occurred_on=date(2026, 1, 5) + timedelta(days=30 * i),
        )
    sync_session.commit()

    learned = learn_recurring_patterns(
        sync_session, USER, as_of=date(2026, 4, 1),
    )
    sync_session.commit()
    assert learned == 1

    rows = sync_session.execute(
        sa.select(RecurringChargePattern)
    ).scalars().all()
    assert len(rows) == 1
    pat = rows[0]
    assert pat.merchant_normalized == "netflix"
    assert pat.expected_amount_nis == Decimal("55.00")
    assert pat.occurrence_count == 3
    assert pat.status == "active"
    assert 28 <= pat.cadence_days <= 32


def test_learner_skips_two_occurrence_merchants(sync_session):
    """Only 2 transactions → NO pattern (spec floor is ≥3)."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    for i in range(2):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="spotify", amount=Decimal("20.00"),
            occurred_on=date(2026, 2, 1) + timedelta(days=30 * i),
        )
    sync_session.commit()

    learned = learn_recurring_patterns(
        sync_session, USER, as_of=date(2026, 4, 1),
    )
    sync_session.commit()
    assert learned == 0
    assert sync_session.execute(
        sa.select(RecurringChargePattern)
    ).scalars().all() == []


def test_learner_skips_irregular_cadence(sync_session):
    """Gaps of 15 / 60 days → median ≠ monthly → NOT learned."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    # Day 0, day 15, day 75 → gaps [15, 60], median 37.5 → outside [28, 32].
    days = [0, 15, 75]
    for d in days:
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="random_shop", amount=Decimal("100.00"),
            occurred_on=date(2026, 1, 1) + timedelta(days=d),
        )
    sync_session.commit()

    learned = learn_recurring_patterns(
        sync_session, USER, as_of=date(2026, 4, 1),
    )
    sync_session.commit()
    assert learned == 0
    assert sync_session.execute(
        sa.select(RecurringChargePattern)
    ).scalars().all() == []


def test_learner_skips_when_amounts_drift_too_much(sync_session):
    """3 txns at very different amounts → in-band filter drops to <3 → not learned."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    # 100, 200, 300 → median 200, ±15% band is [170, 230]; only the 200
    # survives → in_band count = 1 < min_occurrences.
    for i, amt in enumerate([100, 200, 300]):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="bursty_merchant", amount=Decimal(str(amt)),
            occurred_on=date(2026, 1, 5) + timedelta(days=30 * i),
        )
    sync_session.commit()

    learned = learn_recurring_patterns(
        sync_session, USER, as_of=date(2026, 4, 1),
    )
    sync_session.commit()
    assert learned == 0


def test_learner_idempotent_on_rerun(sync_session):
    """Re-running with the same data UPSERTs in place — no duplicate rows."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    for i in range(3):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="netflix", amount=Decimal("55.00"),
            occurred_on=date(2026, 1, 5) + timedelta(days=30 * i),
        )
    sync_session.commit()

    first = learn_recurring_patterns(
        sync_session, USER, as_of=date(2026, 4, 1),
    )
    sync_session.commit()
    assert first == 1

    second = learn_recurring_patterns(
        sync_session, USER, as_of=date(2026, 4, 1),
    )
    sync_session.commit()
    # Nothing changed → 0 refreshed.
    assert second == 0
    rows = sync_session.execute(
        sa.select(RecurringChargePattern)
    ).scalars().all()
    assert len(rows) == 1  # no duplicate.


def test_learner_ignores_card_payments(sync_session):
    """Cross-statement card payments aren't subscriptions."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    for i in range(3):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="card_payment_visa", amount=Decimal("1500.00"),
            occurred_on=date(2026, 1, 5) + timedelta(days=30 * i),
            is_card_payment=True,
        )
    sync_session.commit()

    learned = learn_recurring_patterns(
        sync_session, USER, as_of=date(2026, 4, 1),
    )
    sync_session.commit()
    assert learned == 0


def test_learner_rejects_invalid_min_occurrences(sync_session):
    with pytest.raises(ValueError):
        learn_recurring_patterns(
            sync_session, USER, as_of=date(2026, 4, 1), min_occurrences=2,
        )


# ---------------------------------------------------------------------------
# Missing-detector tests.
# ---------------------------------------------------------------------------


def _seed_pattern(
    db, *,
    merchant: str = "netflix",
    expected_amount: Decimal = Decimal("55.00"),
    cadence_days: int = 30,
    last_seen: date = date(2026, 3, 5),
    status: str = "active",
    occurrence_count: int = 3,
) -> RecurringChargePattern:
    pat = RecurringChargePattern(
        user_id=USER,
        merchant_normalized=merchant,
        expected_amount_nis=expected_amount,
        amount_tolerance=Decimal("0.150"),
        cadence_days=cadence_days,
        cadence_tolerance_days=7,
        first_seen=last_seen - timedelta(days=cadence_days * 2),
        last_seen=last_seen,
        occurrence_count=occurrence_count,
        status=status,
    )
    db.add(pat)
    db.commit()
    return pat


def test_missing_detector_fires_when_cadence_plus_grace_elapsed(sync_session):
    _make_source_and_statement(sync_session)
    _seed_pattern(sync_session, last_seen=date(2026, 3, 5), cadence_days=30)

    # last_seen=Mar 5 + cadence 30 = expected Apr 4 + 7 day grace = Apr 11.
    # as_of=Apr 20 is past the deadline → fires.
    fires = detect_missing_recurring(
        sync_session, USER, as_of=date(2026, 4, 20),
    )
    sync_session.commit()
    assert len(fires) == 1
    f = fires[0]
    assert f.merchant_normalized == "netflix"
    assert f.expected_on == date(2026, 4, 4)
    assert f.days_overdue == 16
    assert f.materiality == "warning"  # ₪55 < ₪500 threshold
    assert "v1|b2|" in f.dedup_key

    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue).where(
            ExpenseReviewQueue.user_id == USER
        )
    ).scalars().all()
    assert len(queue_rows) == 1
    assert queue_rows[0].kind == "bucket_b_recurring_missing"
    assert queue_rows[0].bucket == "recurring"


def test_missing_detector_silent_within_grace_window(sync_session):
    """Inside the grace window → no fire (the charge may still post)."""
    _make_source_and_statement(sync_session)
    _seed_pattern(sync_session, last_seen=date(2026, 3, 5), cadence_days=30)

    # expected=Apr 4, deadline=Apr 11. as_of=Apr 8 is within grace.
    fires = detect_missing_recurring(
        sync_session, USER, as_of=date(2026, 4, 8),
    )
    sync_session.commit()
    assert fires == []
    assert sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all() == []


def test_missing_detector_ignores_user_dismissed_patterns(sync_session):
    """User said stop monitoring this — don't fire."""
    _make_source_and_statement(sync_session)
    _seed_pattern(
        sync_session, last_seen=date(2026, 3, 5),
        cadence_days=30, status="user_dismissed",
    )

    fires = detect_missing_recurring(
        sync_session, USER, as_of=date(2026, 4, 20),
    )
    sync_session.commit()
    assert fires == []


def test_missing_detector_does_not_fire_when_fresh_match_exists(sync_session):
    """A fresh in-band charge after last_seen means the pattern is healthy."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    _seed_pattern(sync_session, last_seen=date(2026, 3, 5), cadence_days=30)

    # Add a fresh Netflix charge on Apr 6.
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="netflix", amount=Decimal("55.00"),
        occurred_on=date(2026, 4, 6),
    )
    sync_session.commit()

    fires = detect_missing_recurring(
        sync_session, USER, as_of=date(2026, 4, 20),
    )
    sync_session.commit()
    assert fires == []


def test_missing_detector_severity_critical_for_high_amount(sync_session):
    """Pattern with expected_amount ≥ ₪500 → critical (likely auto-pay failure)."""
    _make_source_and_statement(sync_session)
    _seed_pattern(
        sync_session, merchant="rent",
        expected_amount=CRITICAL_AMOUNT_THRESHOLD_NIS + Decimal("100.00"),
        last_seen=date(2026, 3, 5), cadence_days=30,
    )

    fires = detect_missing_recurring(
        sync_session, USER, as_of=date(2026, 4, 20),
    )
    sync_session.commit()
    assert len(fires) == 1
    assert fires[0].materiality == "critical"
    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert queue_rows[0].materiality == "critical"


def test_missing_detector_idempotent_on_rerun(sync_session):
    """Re-running over the same state writes 0 new queue rows."""
    _make_source_and_statement(sync_session)
    _seed_pattern(sync_session, last_seen=date(2026, 3, 5), cadence_days=30)

    first = detect_missing_recurring(
        sync_session, USER, as_of=date(2026, 4, 20),
    )
    sync_session.commit()
    assert len(first) == 1

    second = detect_missing_recurring(
        sync_session, USER, as_of=date(2026, 4, 20),
    )
    sync_session.commit()
    assert second == []  # no new fires

    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert len(queue_rows) == 1  # still just one open row


def test_missing_detector_independent_of_learner(sync_session):
    """Detector + learner can be called in either order (no cross-fn
    dependency) — seed a pattern directly and detect missing without
    running the learner."""
    _make_source_and_statement(sync_session)
    pat = _seed_pattern(
        sync_session, last_seen=date(2026, 3, 5), cadence_days=30,
    )

    fires = detect_missing_recurring(
        sync_session, USER, as_of=date(2026, 4, 20),
    )
    sync_session.commit()
    assert len(fires) == 1
    assert fires[0].pattern_id == pat.id
