"""Tests for argosy.services.anomaly.bucket_c.

Covers (sprint #2 commit #8) — Bucket C: merchant cache anomalies.

C1 — novel merchant:
  1. New account (<100 historical txns) → no fire (rate-limited).
  2. Established account (>=100 historical txns) → fires info on novel
     merchant.
  3. Previously-seen merchant → no fire.
  4. Idempotency: re-running doesn't double-write.

C2 — category drift:
  5. Stale cache (200d ago) + recent tx same category → no fire.
  6. Stale cache (200d ago) + recent tx DIFFERENT category → fires warning.
  7. Fresh cache (50d ago) + drift → no fire (not stale yet).
  8. Idempotency: re-running doesn't double-write.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \
        tests/test_bucket_c_detectors.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.anomaly.bucket_c import (
    C1_MIN_HISTORICAL_TXNS,
    detect_category_drift,
    detect_novel_merchants,
)
from argosy.state.models import (
    Base,
    ExpenseCategory,
    ExpenseReviewQueue,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    MerchantCategoryCache,
    User,
    UserFile,
)


USER = "ariel"
AS_OF = date(2026, 5, 29)
# Cache-staleness seeds must be anchored to AS_OF (the injected clock the
# detector uses for its stale-days threshold), NOT real datetime.now() — else as
# real time drifts the now()-N-days offset crosses the as_of-based threshold and
# the fire/no-fire flips (a date-drift time-bomb).
AS_OF_DT = datetime.combine(AS_OF, datetime.min.time(), tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite session bound to a file in tmp_path."""
    db_path = tmp_path / "bucket_c.db"
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
    """Create a minimal ExpenseSource + ExpenseStatement."""
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
        period_start=date(2025, 1, 1), period_end=date(2026, 12, 31),
        parsed_total_nis=Decimal("1000"),
        declared_total_nis=Decimal("1000"),
        parser_name="visa", parser_version="0.1.0",
        status="parsed",
    )
    db.add(stmt)
    db.flush()
    return src.id, stmt.id


def _make_categories(db) -> tuple[int, int]:
    """Two categories so drift tests can swap between them."""
    a = ExpenseCategory(
        user_id=USER, slug="food.restaurants",
        label_en="Restaurants", label_he="מסעדות",
    )
    b = ExpenseCategory(
        user_id=USER, slug="transport.taxi",
        label_en="Taxi", label_he="מונית",
    )
    db.add_all([a, b])
    db.flush()
    return a.id, b.id


def _add_tx(
    db, source_id: int, statement_id: int, *,
    merchant: str, amount: Decimal | float | int,
    occurred_on: date, category_id: int | None = None,
    direction: str = "debit",
) -> ExpenseTransaction:
    tx = ExpenseTransaction(
        user_id=USER, source_id=source_id, statement_id=statement_id,
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


def _seed_historical_txns(
    db, source_id: int, statement_id: int, *,
    count: int, base_day: date | None = None,
) -> None:
    """Seed N historical transactions at well-known merchants so the C1
    rate-limit gate can be toggled by varying ``count``."""
    base_day = base_day or (AS_OF - timedelta(days=365))
    for i in range(count):
        _add_tx(
            db, source_id, statement_id,
            merchant=f"seed_merchant_{i % 5}",
            amount=Decimal("12.34"),
            occurred_on=base_day + timedelta(days=i % 300),
        )


# ---------------------------------------------------------------------------
# C1 — novel merchant.
# ---------------------------------------------------------------------------


def test_c1_rate_limited_on_new_account(sync_session):
    """User with <100 historical txns → no fire even with a 'novel' merchant."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    # 50 < 100; below the rate-limit gate.
    _seed_historical_txns(sync_session, src_id, stmt_id, count=50)
    # A novel merchant just landed.
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="brand_new_taqueria",
        amount=42,
        occurred_on=AS_OF - timedelta(days=2),
    )
    sync_session.commit()

    fired = detect_novel_merchants(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert fired == []
    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert queue_rows == []


def test_c1_fires_info_on_established_account(sync_session):
    """User with >=100 historical txns → novel merchant fires info."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    _seed_historical_txns(
        sync_session, src_id, stmt_id, count=C1_MIN_HISTORICAL_TXNS,
    )
    novel_tx = _add_tx(
        sync_session, src_id, stmt_id,
        merchant="brand_new_taqueria",
        amount=42,
        occurred_on=AS_OF - timedelta(days=2),
    )
    sync_session.commit()

    fired = detect_novel_merchants(sync_session, USER, as_of=AS_OF)
    sync_session.commit()

    assert len(fired) == 1
    flag = fired[0]
    assert flag.detector == "c1_novel_merchant"
    assert flag.severity == "info"
    assert flag.merchant_normalized == "brand_new_taqueria"
    assert flag.transaction_id == novel_tx.id

    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert len(queue_rows) == 1
    row = queue_rows[0]
    assert row.bucket == "cache"
    assert row.materiality == "info"
    assert row.related_tx_id == novel_tx.id
    assert row.dedup_key is not None and row.dedup_key.startswith("v1|c1|")


def test_c1_does_not_fire_for_previously_seen_merchant(sync_session):
    """A merchant that already has prior history should not fire."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    _seed_historical_txns(
        sync_session, src_id, stmt_id, count=C1_MIN_HISTORICAL_TXNS,
    )
    # Add the merchant a year ago AND today — today's tx is not novel.
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="repeated_merchant",
        amount=10, occurred_on=AS_OF - timedelta(days=300),
    )
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="repeated_merchant",
        amount=12, occurred_on=AS_OF - timedelta(days=2),
    )
    sync_session.commit()

    fired = detect_novel_merchants(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    # The only candidate is the recent txn whose merchant has a prior
    # occurrence → not novel.
    assert fired == []


def test_c1_idempotency_no_double_write(sync_session):
    """Re-running detect_novel_merchants does not insert duplicates."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    _seed_historical_txns(
        sync_session, src_id, stmt_id, count=C1_MIN_HISTORICAL_TXNS,
    )
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="brand_new_taqueria", amount=42,
        occurred_on=AS_OF - timedelta(days=2),
    )
    sync_session.commit()

    fired_first = detect_novel_merchants(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert len(fired_first) == 1

    fired_again = detect_novel_merchants(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    # The partial unique index on dedup_key suppresses re-insertion.
    assert fired_again == []
    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert len(queue_rows) == 1


# ---------------------------------------------------------------------------
# C2 — category drift.
# ---------------------------------------------------------------------------


def test_c2_fresh_cache_with_same_category_does_not_fire(sync_session):
    """Cache confirmed 200d ago + recent tx uses SAME category → no fire."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    cat_a, _ = _make_categories(sync_session)

    cache = MerchantCategoryCache(
        user_id=USER, merchant_pattern="shawarma_palace",
        is_regex=False, category_id=cat_a,
        source="user", confidence=Decimal("1.0"),
        hit_count=10,
        last_hit_at=AS_OF_DT - timedelta(days=200),
    )
    sync_session.add(cache)
    sync_session.flush()
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="shawarma_palace", amount=55,
        occurred_on=AS_OF - timedelta(days=3),
        category_id=cat_a,  # SAME as cache.
    )
    sync_session.commit()

    fired = detect_category_drift(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert fired == []


def test_c2_stale_cache_with_drift_fires_warning(sync_session):
    """Cache 200d stale + recent tx DIFFERENT category → fires warning."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    cat_a, cat_b = _make_categories(sync_session)

    cache = MerchantCategoryCache(
        user_id=USER, merchant_pattern="shawarma_palace",
        is_regex=False, category_id=cat_a,
        source="user", confidence=Decimal("1.0"),
        hit_count=10,
        last_hit_at=AS_OF_DT - timedelta(days=200),
    )
    sync_session.add(cache)
    sync_session.flush()
    drifted_tx = _add_tx(
        sync_session, src_id, stmt_id,
        merchant="shawarma_palace", amount=55,
        occurred_on=AS_OF - timedelta(days=3),
        category_id=cat_b,  # DIFFERENT from cache.
    )
    sync_session.commit()

    fired = detect_category_drift(sync_session, USER, as_of=AS_OF)
    sync_session.commit()

    assert len(fired) == 1
    flag = fired[0]
    assert flag.detector == "c2_category_drift"
    assert flag.severity == "warning"
    assert flag.merchant_normalized == "shawarma_palace"
    assert flag.transaction_id == drifted_tx.id

    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert len(queue_rows) == 1
    row = queue_rows[0]
    assert row.bucket == "cache"
    assert row.materiality == "warning"
    assert row.dedup_key is not None and row.dedup_key.startswith("v1|c2|")
    # dedup_key encodes (cache_cat, obs_month).
    assert f"cache_cat:{cat_a}" in row.dedup_key
    assert f"obs_month:{drifted_tx.occurred_on.strftime('%Y-%m')}" in row.dedup_key


def test_c2_fresh_cache_does_not_fire_even_with_drift(sync_session):
    """Cache confirmed 50d ago is NOT stale → no fire even on drift."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    cat_a, cat_b = _make_categories(sync_session)

    cache = MerchantCategoryCache(
        user_id=USER, merchant_pattern="shawarma_palace",
        is_regex=False, category_id=cat_a,
        source="user", confidence=Decimal("1.0"),
        hit_count=10,
        last_hit_at=AS_OF_DT - timedelta(days=50),
    )
    sync_session.add(cache)
    sync_session.flush()
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="shawarma_palace", amount=55,
        occurred_on=AS_OF - timedelta(days=3),
        category_id=cat_b,
    )
    sync_session.commit()

    fired = detect_category_drift(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert fired == []
    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert queue_rows == []


def test_c2_idempotency_no_double_write(sync_session):
    """Re-running detect_category_drift does not insert duplicates."""
    src_id, stmt_id = _make_source_and_statement(sync_session)
    cat_a, cat_b = _make_categories(sync_session)

    cache = MerchantCategoryCache(
        user_id=USER, merchant_pattern="shawarma_palace",
        is_regex=False, category_id=cat_a,
        source="user", confidence=Decimal("1.0"),
        hit_count=10,
        last_hit_at=AS_OF_DT - timedelta(days=200),
    )
    sync_session.add(cache)
    sync_session.flush()
    _add_tx(
        sync_session, src_id, stmt_id,
        merchant="shawarma_palace", amount=55,
        occurred_on=AS_OF - timedelta(days=3),
        category_id=cat_b,
    )
    sync_session.commit()

    fired_first = detect_category_drift(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert len(fired_first) == 1

    fired_again = detect_category_drift(sync_session, USER, as_of=AS_OF)
    sync_session.commit()
    assert fired_again == []
    queue_rows = sync_session.execute(
        sa.select(ExpenseReviewQueue)
    ).scalars().all()
    assert len(queue_rows) == 1
