"""Tests for argosy.services.anomaly.bucket_a.detect_bucket_a.

Covers (sprint #2 commit #5):
  1. Empty transactions → 0 flags, 0 queue rows.
  2. Tx in a category with no rolling_stats baseline → no fire.
  3. A1: median=100 MAD=10, tx amount=200 → robust z ≈ 6.7 → critical.
  4. A1: amount below ₪200 absolute threshold → no fire even if robust z is huge.
  5. A1: baseline txn_count <6 → no fire even if robust z is huge.
  6. A2: mean=100, amount=400 → fires warning (≥3x).
  7. A2: mean=100, amount=600 → fires critical (≥5x).
  8. A2: mean=20 (< ₪50 min) → no fire even if amount is 10x mean.
  9. Idempotency: re-running on the same tx doesn't create duplicate
     ExpenseReviewQueue rows (partial unique index drops the second
     insert).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \
        tests/test_bucket_a_detector.py -v
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.anomaly.bucket_a import detect_bucket_a
from argosy.state.models import (
    Base,
    ExpenseReviewQueue,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    MerchantRollingStats,
    User,
    UserFile,
)


USER = "ariel"
AS_OF = date(2026, 6, 30)


# ---------------------------------------------------------------------------
# Fixtures + helpers.
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Self-contained SQLite session with the User seeded."""
    db_path = tmp_path / "bucket_a.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id=USER, plan="free"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _ensure_stmt(db) -> tuple[int, int]:
    """Idempotent: create a Source + Statement if one isn't already
    cached on the session, return (source_id, statement_id)."""
    cached = getattr(db, "_stmt_cache", None)
    if cached is not None:
        return cached
    f = UserFile(
        user_id=USER, sha256="d" * 64,
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
    db._stmt_cache = (src.id, stmt.id)
    return db._stmt_cache


def _add_tx(
    db,
    *,
    merchant: str,
    amount: Decimal | float | int,
    occurred_on: date,
    category_id: int | None = None,
    direction: str = "debit",
) -> ExpenseTransaction:
    """Create + persist an ExpenseTransaction. Returns the row with id set."""
    src_id, stmt_id = _ensure_stmt(db)
    tx = ExpenseTransaction(
        user_id=USER, source_id=src_id, statement_id=stmt_id,
        occurred_on=occurred_on,
        merchant_raw=merchant, merchant_normalized=merchant,
        amount_nis=Decimal(str(amount)),
        direction=direction, tx_type="regular",
        category_id=category_id,
        raw_row_json="{}",
    )
    db.add(tx)
    db.flush()
    return tx


def _add_baseline(
    db,
    *,
    merchant: str,
    category_id: int | None,
    median: Decimal | float | int,
    mad: Decimal | float | int | None,
    mean: Decimal | float | int,
    txn_count: int,
    window_end: date = AS_OF,
    window_days: int = 180,
) -> MerchantRollingStats:
    """Seed one MerchantRollingStats row directly (faster than calling
    recompute_merchant_stats for each test scenario — gives us exact
    control over the baseline numbers)."""
    median_d = Decimal(str(median))
    row = MerchantRollingStats(
        user_id=USER,
        merchant_normalized=merchant,
        category_id=category_id,
        window_start=window_end - timedelta(days=window_days - 1),
        window_end=window_end,
        txn_count=txn_count,
        median_nis=median_d,
        mad_nis=Decimal(str(mad)) if mad is not None else None,
        mean_nis=Decimal(str(mean)),
        stdev_nis=None,
        min_nis=median_d,
        max_nis=median_d,
        first_seen_at=window_end - timedelta(days=30),
        last_seen_at=window_end - timedelta(days=1),
    )
    db.add(row)
    db.flush()
    return row


def _queue_rows(db) -> list[ExpenseReviewQueue]:
    return list(
        db.execute(sa.select(ExpenseReviewQueue)).scalars().all()
    )


# ---------------------------------------------------------------------------
# 1. Empty transactions.
# ---------------------------------------------------------------------------


def test_empty_transactions_no_flags(db):
    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    assert flags == []
    assert _queue_rows(db) == []


# ---------------------------------------------------------------------------
# 2. No baseline → no fire.
# ---------------------------------------------------------------------------


def test_no_baseline_no_fire(db):
    """Single tx in a category with no rolling_stats yet → no fire (no baseline)."""
    _add_tx(
        db, merchant="brand_new_merchant", amount=500,
        occurred_on=AS_OF - timedelta(days=2),
        category_id=42,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    assert flags == []
    assert _queue_rows(db) == []


# ---------------------------------------------------------------------------
# 3. A1 critical via robust z ≈ 6.7.
# ---------------------------------------------------------------------------


def test_a1_robust_z_critical(db):
    """median=100 MAD=10, tx amount=200 → r = (200-100)/(1.4826*10) ≈ 6.745 → critical."""
    # Seed 6 baseline merchants in category 7 so the txn_count gate (>=6)
    # passes when aggregated up to the category. All anchored at median 100
    # with merchant-median spread of 10 to give category-MAD of 10.
    cat = 7
    for i, med in enumerate([90, 95, 100, 100, 105, 110]):
        _add_baseline(
            db, merchant=f"baseline_m_{i}", category_id=cat,
            median=med, mad=Decimal("5"),
            mean=med, txn_count=4,
        )
    # The aggregate of [90,95,100,100,105,110] has median ≈ 100, MAD=5,
    # but we need MAD=10 for the asserted z. Re-seed with a spread of
    # ±10 around 100.
    db.execute(sa.delete(MerchantRollingStats))
    for i, med in enumerate([80, 90, 100, 100, 110, 120]):
        _add_baseline(
            db, merchant=f"baseline_m_{i}", category_id=cat,
            median=med, mad=Decimal("5"),
            mean=med, txn_count=4,
        )
    db.commit()

    tx = _add_tx(
        db, merchant="outlier_shop", amount=200,
        occurred_on=AS_OF - timedelta(days=1),
        category_id=cat,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a1 = [f for f in flags if f.detector == "a1_category_outlier"]
    assert len(a1) == 1, f"expected 1 A1 flag, got {flags}"
    assert a1[0].transaction_id == tx.id
    assert a1[0].severity == "critical"  # z ≈ 6.7 -> critical band (>=6).
    assert "v1|a1|" in a1[0].dedup_key
    assert f"tx:{tx.id}" in a1[0].dedup_key
    # Queue row landed.
    rows = [r for r in _queue_rows(db) if r.kind == "a1_category_outlier"]
    assert len(rows) == 1
    assert rows[0].bucket == "amount"
    assert rows[0].materiality == "critical"


# ---------------------------------------------------------------------------
# 4. A1 below absolute-amount threshold → no fire.
# ---------------------------------------------------------------------------


def test_a1_below_min_abs_amount_no_fire(db):
    """Even a wild robust z fails if abs(amount) < ₪200."""
    cat = 8
    # Tight cluster around 10 with MAD=1 so a 150 NIS tx has z ≈ 94.
    for i, med in enumerate([8, 9, 10, 10, 11, 12]):
        _add_baseline(
            db, merchant=f"b_{i}", category_id=cat,
            median=med, mad=Decimal("1"),
            mean=med, txn_count=4,
        )
    db.commit()

    _add_tx(
        db, merchant="cheap_outlier", amount=150,
        occurred_on=AS_OF - timedelta(days=1),
        category_id=cat,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    assert [f for f in flags if f.detector == "a1_category_outlier"] == []


# ---------------------------------------------------------------------------
# 5. A1 baseline txn_count < 6 → no fire.
# ---------------------------------------------------------------------------


def test_a1_baseline_too_small_no_fire(db):
    """Robust z is enormous but only 2 prior txns in the category."""
    cat = 9
    # Only ONE baseline row contributing txn_count=2 — far below the
    # 6-txn gate.
    _add_baseline(
        db, merchant="small_baseline", category_id=cat,
        median=100, mad=Decimal("5"),
        mean=100, txn_count=2,
    )
    db.commit()

    _add_tx(
        db, merchant="huge_spend", amount=500,
        occurred_on=AS_OF - timedelta(days=1),
        category_id=cat,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    assert [f for f in flags if f.detector == "a1_category_outlier"] == []


# ---------------------------------------------------------------------------
# 6. A2 warning at 3x mean.
# ---------------------------------------------------------------------------


def test_a2_warning_at_3x_mean(db):
    """mean=100, amount=400 → 4x → warning (≥3x, <5x)."""
    _add_baseline(
        db, merchant="netflix", category_id=None,
        median=100, mad=Decimal("0"),
        mean=100, txn_count=6,
    )
    db.commit()

    tx = _add_tx(
        db, merchant="netflix", amount=400,
        occurred_on=AS_OF - timedelta(days=3),
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a2 = [f for f in flags if f.detector == "a2_merchant_spike"]
    assert len(a2) == 1
    assert a2[0].transaction_id == tx.id
    assert a2[0].severity == "warning"
    assert "v1|a2|" in a2[0].dedup_key


# ---------------------------------------------------------------------------
# 7. A2 critical at 5x+ mean.
# ---------------------------------------------------------------------------


def test_a2_critical_at_5x_mean(db):
    """mean=100, amount=600 → 6x → critical (≥5x)."""
    _add_baseline(
        db, merchant="spotify", category_id=None,
        median=100, mad=Decimal("0"),
        mean=100, txn_count=6,
    )
    db.commit()

    _add_tx(
        db, merchant="spotify", amount=600,
        occurred_on=AS_OF - timedelta(days=2),
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a2 = [f for f in flags if f.detector == "a2_merchant_spike"]
    assert len(a2) == 1
    assert a2[0].severity == "critical"


# ---------------------------------------------------------------------------
# 8. A2 min-mean gate (<₪50) → no fire.
# ---------------------------------------------------------------------------


def test_a2_below_min_mean_no_fire(db):
    """mean=20 < ₪50 min → no fire even at 10x."""
    _add_baseline(
        db, merchant="tiny_buys", category_id=None,
        median=20, mad=Decimal("0"),
        mean=20, txn_count=6,
    )
    db.commit()

    _add_tx(
        db, merchant="tiny_buys", amount=200,
        occurred_on=AS_OF - timedelta(days=1),
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    assert [f for f in flags if f.detector == "a2_merchant_spike"] == []


# ---------------------------------------------------------------------------
# 9. Idempotency — partial unique index suppresses dup inserts on rerun.
# ---------------------------------------------------------------------------


def test_idempotent_rerun_no_duplicate_rows(db):
    """Two runs over the same baseline + tx should yield ONE queue row."""
    _add_baseline(
        db, merchant="rerun_shop", category_id=None,
        median=100, mad=Decimal("0"),
        mean=100, txn_count=6,
    )
    db.commit()
    _add_tx(
        db, merchant="rerun_shop", amount=500,
        occurred_on=AS_OF - timedelta(days=1),
    )
    db.commit()

    # First run — fires.
    flags1 = detect_bucket_a(db, USER, as_of=AS_OF)
    db.commit()
    assert len([f for f in flags1 if f.detector == "a2_merchant_spike"]) == 1
    rows_after_first = _queue_rows(db)
    assert len(rows_after_first) == 1

    # Second run — partial unique index drops the duplicate insert.
    flags2 = detect_bucket_a(db, USER, as_of=AS_OF)
    db.commit()
    # The second run returns [] because the SAVEPOINT-protected insert
    # raised an IntegrityError and was suppressed.
    assert flags2 == []
    rows_after_second = _queue_rows(db)
    assert len(rows_after_second) == 1
    assert rows_after_second[0].id == rows_after_first[0].id
