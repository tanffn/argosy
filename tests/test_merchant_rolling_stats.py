"""Tests for argosy.services.anomaly.rolling_stats.recompute_merchant_stats.

Covers (sprint #2 commit #4):
  1. Empty transactions → 0 rows written.
  2. 5 txns at the same merchant → 1 row with correct median + MAD.
  3. Merchant with 1 txn → skipped (need ≥2 for MAD to be meaningful).
  4. Re-running recompute upserts (no duplicates on same window_end).
  5. Stale window (window_end ≠ today) doesn't block today's recompute.
  6. Multiple merchants × categories → one row per (merchant, category).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \
        tests/test_merchant_rolling_stats.py -v
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.anomaly.rolling_stats import recompute_merchant_stats
from argosy.state.models import (
    Base,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    MerchantRollingStats,
    User,
    UserFile,
)


USER = "ariel"


# ---------------------------------------------------------------------------
# Fixture.
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite session bound to a file in tmp_path.

    Mirrors the pattern used by tests/test_anomaly_runner.py.
    """
    db_path = tmp_path / "rolling.db"
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
    """Create a minimal ExpenseSource + ExpenseStatement so transactions
    can be FK-attached. Returns (source_id, statement_id)."""
    f = UserFile(
        user_id=USER, sha256="b" * 64,
        original_name="test.csv", sanitized_name="test.csv",
        mime_type="text/csv", kind="other",
        size_bytes=1, storage_path="/tmp/test",
        source="expense_statement",
    )
    db.add(f)
    db.flush()
    src = ExpenseSource(
        user_id=USER, kind="card", issuer="visa",
        external_id="1234", display_name="Visa 1234",
    )
    db.add(src)
    db.flush()
    stmt = ExpenseStatement(
        user_id=USER, source_id=src.id, file_id=f.id,
        period_start=date(2026, 1, 1), period_end=date(2026, 6, 30),
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
    occurred_on: date, category_id: int | None = None,
    direction: str = "debit",
) -> None:
    db.add(ExpenseTransaction(
        user_id=USER, source_id=source_id, statement_id=statement_id,
        occurred_on=occurred_on,
        merchant_raw=merchant, merchant_normalized=merchant,
        amount_nis=Decimal(str(amount)),
        direction=direction, tx_type="regular",
        category_id=category_id,
        raw_row_json="{}",
    ))


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_empty_transactions_writes_zero_rows(sync_session):
    written = recompute_merchant_stats(
        sync_session, USER, as_of=date(2026, 6, 30),
    )
    assert written == 0
    rows = sync_session.execute(
        sa.select(MerchantRollingStats)
    ).scalars().all()
    assert rows == []


def test_five_txns_one_row_with_correct_median_mad(sync_session):
    src_id, stmt_id = _make_source_and_statement(sync_session)
    # Amounts: 80, 90, 100, 110, 120 → median = 100, MAD = 20.
    amounts = [80, 90, 100, 110, 120]
    base_day = date(2026, 5, 1)
    for i, amt in enumerate(amounts):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="spar", amount=amt,
            occurred_on=base_day + timedelta(days=i * 3),
        )
    sync_session.commit()

    written = recompute_merchant_stats(
        sync_session, USER, as_of=date(2026, 6, 30),
    )
    assert written == 1
    sync_session.commit()

    row = sync_session.execute(
        sa.select(MerchantRollingStats)
    ).scalar_one()
    assert row.merchant_normalized == "spar"
    assert row.txn_count == 5
    assert row.median_nis == Decimal("100.00")
    # MAD of [80, 90, 100, 110, 120] vs median 100 = median([20, 10, 0, 10, 20]) = 10.
    assert row.mad_nis == Decimal("10.00")
    assert row.mean_nis == Decimal("100.00")
    assert row.min_nis == Decimal("80.00")
    assert row.max_nis == Decimal("120.00")
    assert row.first_seen_at == base_day
    assert row.last_seen_at == base_day + timedelta(days=4 * 3)
    assert row.window_end == date(2026, 6, 30)


def test_single_txn_merchant_is_skipped(sync_session):
    src_id, stmt_id = _make_source_and_statement(sync_session)
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="rare_merchant", amount=50,
        occurred_on=date(2026, 5, 15),
    )
    # Plus a multi-txn merchant so we know the loop ran.
    for i in range(3):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="regular", amount=100 + i,
            occurred_on=date(2026, 5, 10 + i),
        )
    sync_session.commit()

    written = recompute_merchant_stats(
        sync_session, USER, as_of=date(2026, 6, 30),
    )
    # Only "regular" should be written; rare_merchant skipped.
    assert written == 1
    rows = sync_session.execute(
        sa.select(MerchantRollingStats)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].merchant_normalized == "regular"


def test_rerun_upserts_no_duplicates_on_same_window_end(sync_session):
    src_id, stmt_id = _make_source_and_statement(sync_session)
    for i, amt in enumerate([50, 60, 70]):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="cafe", amount=amt,
            occurred_on=date(2026, 5, 1) + timedelta(days=i),
        )
    sync_session.commit()

    # First run.
    first = recompute_merchant_stats(
        sync_session, USER, as_of=date(2026, 6, 30),
    )
    sync_session.commit()
    assert first == 1

    # Add another txn, re-run with the SAME window_end.
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="cafe", amount=200,
        occurred_on=date(2026, 6, 1),
    )
    sync_session.commit()

    second = recompute_merchant_stats(
        sync_session, USER, as_of=date(2026, 6, 30),
    )
    sync_session.commit()
    assert second == 1

    rows = sync_session.execute(
        sa.select(MerchantRollingStats).where(
            MerchantRollingStats.merchant_normalized == "cafe"
        )
    ).scalars().all()
    # UPSERT: still exactly 1 row, but txn_count updated.
    assert len(rows) == 1
    assert rows[0].txn_count == 4
    assert rows[0].max_nis == Decimal("200.00")


def test_stale_window_end_does_not_block_today_recompute(sync_session):
    """A row with an OLD window_end coexists with a freshly-computed row
    for today (separate rows on different window_ends — that's how
    history retention works)."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    for i, amt in enumerate([100, 110, 120]):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="butcher", amount=amt,
            occurred_on=date(2026, 4, 1) + timedelta(days=i),
        )
    sync_session.commit()

    # Earlier recompute (stale window_end).
    recompute_merchant_stats(
        sync_session, USER, as_of=date(2026, 4, 30),
    )
    sync_session.commit()

    # Fresh recompute today — should NOT collide with the stale row.
    written = recompute_merchant_stats(
        sync_session, USER, as_of=date(2026, 6, 30),
    )
    sync_session.commit()
    assert written == 1

    rows = sync_session.execute(
        sa.select(MerchantRollingStats).where(
            MerchantRollingStats.merchant_normalized == "butcher"
        )
    ).scalars().all()
    assert len(rows) == 2
    window_ends = sorted(r.window_end for r in rows)
    assert window_ends == [date(2026, 4, 30), date(2026, 6, 30)]


def test_multiple_merchants_each_get_own_row(sync_session):
    src_id, stmt_id = _make_source_and_statement(sync_session)
    for merchant in ("spar", "rami_levy", "shufersal"):
        for i in range(3):
            _add_tx(
                sync_session, src_id, stmt_id,
                merchant=merchant, amount=50 + i * 10,
                occurred_on=date(2026, 5, 1 + i),
            )
    sync_session.commit()

    written = recompute_merchant_stats(
        sync_session, USER, as_of=date(2026, 6, 30),
    )
    sync_session.commit()
    assert written == 3
    merchants = {
        r.merchant_normalized for r in sync_session.execute(
            sa.select(MerchantRollingStats)
        ).scalars()
    }
    assert merchants == {"spar", "rami_levy", "shufersal"}


def test_amount_null_rows_are_skipped(sync_session):
    """Rows with NULL amount_nis must not break the computation."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    # Two valid rows for "cafe".
    for i, amt in enumerate([10, 20]):
        _add_tx(
            sync_session, src_id, stmt_id,
            merchant="cafe", amount=amt,
            occurred_on=date(2026, 5, 1 + i),
        )
    # One NULL-amount row for the SAME merchant.
    sync_session.add(ExpenseTransaction(
        user_id=USER, source_id=src_id, statement_id=stmt_id,
        occurred_on=date(2026, 5, 5),
        merchant_raw="cafe", merchant_normalized="cafe",
        amount_nis=None,
        direction="debit", tx_type="regular",
        raw_row_json="{}",
    ))
    sync_session.commit()

    written = recompute_merchant_stats(
        sync_session, USER, as_of=date(2026, 6, 30),
    )
    sync_session.commit()
    assert written == 1
    row = sync_session.execute(
        sa.select(MerchantRollingStats)
    ).scalar_one()
    assert row.txn_count == 2  # the NULL row excluded
