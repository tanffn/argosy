"""Tests for argosy.services.anomaly.bucket_d.

Covers (sprint #2 commit #9) — Bucket D: cross-card duplicate detector.

D1 — cross-card duplicate:
  1. Same merchant + amount within 7d on SAME card → no fire (legit dup).
  2. Same merchant + amount within 7d on DIFFERENT cards → fires warning.
  3. As above but amount >= ₪1000 → fires critical.
  4. is_card_payment=TRUE txns are excluded from pairing.
  5. Idempotency: re-running doesn't double-write.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \
        tests/test_bucket_d_detector.py -v
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.anomaly.bucket_d import detect_cross_card_duplicates
from argosy.state.models import (
    Base,
    ExpenseReviewQueue,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    User,
    UserFile,
)


USER = "ariel"
AS_OF = date(2026, 5, 29)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite session bound to a file in tmp_path."""
    db_path = tmp_path / "bucket_d.db"
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


def _make_card(
    db, *, last4: str, sha_seed: str,
) -> tuple[int, int]:
    """Build one (source, statement) pair representing one card."""
    f = UserFile(
        user_id=USER, sha256=sha_seed * 64,
        original_name=f"{last4}.csv",
        sanitized_name=f"{last4}.csv",
        mime_type="text/csv", kind="other",
        size_bytes=1, storage_path=f"/tmp/{last4}",
        source="expense_statement",
    )
    db.add(f)
    db.flush()
    src = ExpenseSource(
        user_id=USER, kind="card", issuer="visa",
        external_id=last4, display_name=f"Visa {last4}",
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
    occurred_on: date,
    is_card_payment: bool = False,
) -> ExpenseTransaction:
    tx = ExpenseTransaction(
        user_id=USER, source_id=source_id, statement_id=statement_id,
        occurred_on=occurred_on,
        merchant_raw=merchant, merchant_normalized=merchant,
        amount_nis=Decimal(str(amount)),
        direction="debit", tx_type="regular",
        is_card_payment=is_card_payment,
        raw_row_json="{}",
    )
    db.add(tx)
    db.flush()
    return tx


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_d1_same_card_does_not_fire(sync_session):
    """Two txns on the SAME card same merchant same amount within 7d
    → no fire (legit duplicate purchase)."""
    src_id, stmt_id = _make_card(sync_session, last4="1111", sha_seed="a")
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="dual_purchase_shop", amount=200,
        occurred_on=AS_OF - timedelta(days=5),
    )
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="dual_purchase_shop", amount=200,
        occurred_on=AS_OF - timedelta(days=3),
    )
    sync_session.commit()

    fired = detect_cross_card_duplicates(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert fired == []
    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert queue_rows == []


def test_d1_different_cards_fires_warning(sync_session):
    """Two txns on DIFFERENT cards same merchant same amount within 7d
    → fires warning."""
    src_a, stmt_a = _make_card(sync_session, last4="1111", sha_seed="a")
    src_b, stmt_b = _make_card(sync_session, last4="2222", sha_seed="b")
    tx1 = _add_tx(
        sync_session, src_a, stmt_a,
        merchant="suspect_vendor", amount=250,
        occurred_on=AS_OF - timedelta(days=5),
    )
    tx2 = _add_tx(
        sync_session, src_b, stmt_b,
        merchant="suspect_vendor", amount=250,
        occurred_on=AS_OF - timedelta(days=3),
    )
    sync_session.commit()

    fired = detect_cross_card_duplicates(sync_session, USER, as_of=AS_OF)
    sync_session.commit()

    assert len(fired) == 1
    flag = fired[0]
    assert flag.detector == "d1_cross_card_duplicate"
    assert flag.severity == "warning"
    assert flag.merchant_normalized == "suspect_vendor"
    assert flag.min_tx_id == min(tx1.id, tx2.id)
    assert flag.max_tx_id == max(tx1.id, tx2.id)

    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert len(queue_rows) == 1
    row = queue_rows[0]
    assert row.bucket == "duplicate"
    assert row.materiality == "warning"
    assert row.related_tx_id == min(tx1.id, tx2.id)
    assert row.dedup_key is not None
    assert row.dedup_key.startswith("v1|d1|")
    assert f"pair:{min(tx1.id, tx2.id)}-{max(tx1.id, tx2.id)}" in row.dedup_key


def test_d1_large_amount_fires_critical(sync_session):
    """Same as D1 warning but amount >= ₪1000 → fires critical."""
    src_a, stmt_a = _make_card(sync_session, last4="1111", sha_seed="a")
    src_b, stmt_b = _make_card(sync_session, last4="2222", sha_seed="b")
    _add_tx(
        sync_session, src_a, stmt_a,
        merchant="big_ticket_shop", amount=1500,
        occurred_on=AS_OF - timedelta(days=4),
    )
    _add_tx(
        sync_session, src_b, stmt_b,
        merchant="big_ticket_shop", amount=1500,
        occurred_on=AS_OF - timedelta(days=2),
    )
    sync_session.commit()

    fired = detect_cross_card_duplicates(sync_session, USER, as_of=AS_OF)
    sync_session.commit()

    assert len(fired) == 1
    assert fired[0].severity == "critical"
    row = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalar_one()
    assert row.materiality == "critical"


def test_d1_card_payment_txns_excluded(sync_session):
    """``is_card_payment=TRUE`` txns are never paired."""
    src_a, stmt_a = _make_card(sync_session, last4="1111", sha_seed="a")
    src_b, stmt_b = _make_card(sync_session, last4="2222", sha_seed="b")
    _add_tx(
        sync_session, src_a, stmt_a,
        merchant="bank_transfer", amount=250,
        occurred_on=AS_OF - timedelta(days=4),
        is_card_payment=True,
    )
    _add_tx(
        sync_session, src_b, stmt_b,
        merchant="bank_transfer", amount=250,
        occurred_on=AS_OF - timedelta(days=2),
        is_card_payment=True,
    )
    sync_session.commit()

    fired = detect_cross_card_duplicates(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert fired == []


def test_d1_idempotency_no_double_write(sync_session):
    """Re-running the detector does not insert duplicate queue rows."""
    src_a, stmt_a = _make_card(sync_session, last4="1111", sha_seed="a")
    src_b, stmt_b = _make_card(sync_session, last4="2222", sha_seed="b")
    _add_tx(
        sync_session, src_a, stmt_a,
        merchant="rerun_vendor", amount=300,
        occurred_on=AS_OF - timedelta(days=4),
    )
    _add_tx(
        sync_session, src_b, stmt_b,
        merchant="rerun_vendor", amount=300,
        occurred_on=AS_OF - timedelta(days=2),
    )
    sync_session.commit()

    fired_first = detect_cross_card_duplicates(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert len(fired_first) == 1

    fired_again = detect_cross_card_duplicates(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert fired_again == []
    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert len(queue_rows) == 1
