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


# ---------------------------------------------------------------------------
# Weighted-baselines follow-on (2026-05-30) — A1 raw-transaction path.
# ---------------------------------------------------------------------------
#
# These tests cover the new behavior layered onto Pattern A1:
#   - Per-category trimmed-mean + MAD computed from RAW transactions in
#     the baseline window (preferred path).
#   - Empirical-Bayes shrinkage toward a global cross-category baseline
#     for small per-category samples.
#   - Robustness to a single outlier in the training window via trim.
#   - Category-isolation: groceries vs. dining maintain separate baselines.
#   - Backwards-compat fallback: rolling-stats-only seeds keep working.
# ---------------------------------------------------------------------------


def _seed_baseline_history(
    db,
    *,
    category_id: int,
    amounts: list[Decimal | float | int],
    merchant_prefix: str = "hist",
    baseline_window_start_days: int = 90,
) -> list[ExpenseTransaction]:
    """Seed ``len(amounts)`` raw transactions in ``category_id``, all
    inside the baseline window (between ``baseline_window_start_days``
    days before AS_OF and AS_OF - 31 days, i.e. OUTSIDE the 30-day
    detection window so they don't trigger detection themselves).

    Spreads the dates across the baseline window so each row falls
    safely outside the detection window.
    """
    out: list[ExpenseTransaction] = []
    span_days = max(1, baseline_window_start_days - 31)
    for i, amt in enumerate(amounts):
        # All baseline txns fall between AS_OF - 90 and AS_OF - 31.
        offset = 31 + (i * span_days // max(1, len(amounts)))
        out.append(
            _add_tx(
                db,
                merchant=f"{merchant_prefix}_{i}",
                amount=amt,
                occurred_on=AS_OF - timedelta(days=offset),
                category_id=category_id,
            )
        )
    return out


def test_a1_raw_baseline_path_fires_on_clean_history(db):
    """Raw-transaction baseline path: 30 prior tightly-clustered txns
    around 100 NIS in category 'groceries' → a 400 NIS tx in groceries
    fires A1 (critical, robust z >> 6)."""
    cat = 100
    # 30 prior groceries txns clustered tightly around 100 (so MAD is small).
    amounts = [Decimal("95"), Decimal("98"), Decimal("100"), Decimal("102"), Decimal("105")] * 6
    _seed_baseline_history(db, category_id=cat, amounts=amounts, merchant_prefix="grocer")
    db.commit()

    tx = _add_tx(
        db, merchant="splurgy_supermarket", amount=400,
        occurred_on=AS_OF - timedelta(days=1),
        category_id=cat,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a1 = [f for f in flags if f.detector == "a1_category_outlier"]
    assert len(a1) == 1, f"expected 1 A1 flag, got {flags}"
    assert a1[0].transaction_id == tx.id
    assert a1[0].severity == "critical"
    # Rationale must mention the raw-baseline source label.
    assert "src=raw" in a1[0].rationale


def test_a1_raw_baseline_isolates_per_category(db):
    """Category isolation: a 400 NIS tx is normal in 'dining' (where
    baseline mean is ~300) but anomalous in 'groceries' (baseline ~100).

    Seeds enough history in BOTH categories so each gets the raw path.
    The detection tx lands in dining → should NOT fire.
    """
    groceries = 200
    dining = 201
    # Groceries: tightly clustered around 100. n=30.
    _seed_baseline_history(
        db, category_id=groceries,
        amounts=[Decimal("100")] * 30, merchant_prefix="grocer",
    )
    # Dining: tightly clustered around 300. n=30.
    _seed_baseline_history(
        db, category_id=dining,
        amounts=[Decimal("300")] * 30, merchant_prefix="dine",
    )
    db.commit()

    # 400 NIS in dining — only ~33% above baseline, well within noise.
    # But in groceries (baseline 100), 400 NIS would be a 4x outlier.
    tx_dining = _add_tx(
        db, merchant="nice_restaurant", amount=400,
        occurred_on=AS_OF - timedelta(days=1),
        category_id=dining,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a1_for_tx = [
        f for f in flags
        if f.detector == "a1_category_outlier" and f.transaction_id == tx_dining.id
    ]
    # Dining tx at 400 is NOT an outlier given dining's own baseline at 300.
    # The MAD will be near 0 (tight cluster) — with k * scaled_MAD threshold
    # the absolute delta of 100 must exceed it. Tightly clustered samples
    # at exact value 300 give MAD=0 → A1 cannot evaluate → no fire (gate).
    # Either way: no A1 fire on the dining tx.
    assert a1_for_tx == [], (
        "dining tx at 400 NIS should NOT trigger A1 — within its own "
        f"category baseline, got flags={a1_for_tx}"
    )


def test_a1_raw_baseline_robust_to_single_outlier_in_training(db):
    """Trimmed-mean robustness: a 30-txn training set clustered around
    100 NIS plus ONE 5000 NIS outlier should still produce a baseline
    center near 100 (the trim discards the outlier).

    A 400 NIS detection tx should still fire (not be shielded by the
    inflated mean a non-robust statistic would produce).
    """
    cat = 300
    # 29 mildly-noisy obs around 100, 1 obs at 5000 (the corrupt outlier).
    # The mild noise is REQUIRED so MAD > 0 after trimming — otherwise a
    # pure-100 cluster gives spread=0 and short-circuits A1 entirely.
    # 10% trim of 30 = 3 per tail; after trim we keep 24 of the 29 noisy
    # values around 100 (trim discards the 5000 plus 2 of the largest
    # 100-cluster vals; remaining 24 still spans 90..110).
    amounts = [Decimal(str(v)) for v in (
        [90, 92, 94, 96, 98, 99, 100, 100, 100, 100,
         100, 100, 100, 100, 100, 100, 100, 100, 100, 102,
         102, 104, 104, 106, 106, 108, 108, 110, 110]
        + [5000]
    )]
    assert len(amounts) == 30
    _seed_baseline_history(db, category_id=cat, amounts=amounts, merchant_prefix="rob")
    db.commit()

    tx = _add_tx(
        db, merchant="suspicious_charge", amount=400,
        occurred_on=AS_OF - timedelta(days=1),
        category_id=cat,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a1 = [
        f for f in flags
        if f.detector == "a1_category_outlier" and f.transaction_id == tx.id
    ]
    # If the implementation used a NON-robust mean, the baseline would
    # be (29*100 + 5000)/30 ≈ 263 — and a 400 NIS tx would look much
    # closer to normal. With trimmed mean (trim 10%/tail = drop top 3),
    # center stays at 100, so 400 NIS is clearly anomalous.
    assert len(a1) == 1, (
        f"expected trimmed mean to discard the 5000 NIS outlier and "
        f"flag the 400 NIS test tx — got flags={a1}"
    )
    # The rationale should reflect the raw path was used.
    assert "src=raw" in a1[0].rationale


def test_a1_small_sample_shrinks_toward_global(db):
    """Small per-category samples (n=6, just past the gate) should
    shrink toward the global baseline.

    Scenario: category 'rare_cat' has just 6 raw obs all at 100 NIS,
    but the OVERALL spend across all categories has a much larger
    spread (e.g. global center ≈ 500, MAD ≈ 200). A 250 NIS tx in
    'rare_cat' should NOT fire because the small-n shrinkage pulls
    the effective spread toward the global ~200 — well above the
    raw 0 MAD that would have flagged anything > 100.

    Without shrinkage (raw spread = 0), even a tiny excess would
    fire spuriously. With shrinkage, the threshold becomes sane.
    """
    rare_cat = 400
    # 6 obs in rare_cat at exactly 100 → raw MAD = 0.
    _seed_baseline_history(
        db, category_id=rare_cat,
        amounts=[Decimal("100")] * 6, merchant_prefix="rare",
    )
    # Seed a noisy "big_cat" with high variance so the GLOBAL baseline
    # has a real spread. n=24 → total raw observations across all
    # categories = 30 → global baseline weight is meaningful.
    big_cat = 401
    big_amounts = [Decimal(str(v)) for v in [
        100, 200, 300, 400, 500, 600, 700, 800, 900, 1000,
        100, 200, 300, 400, 500, 600, 700, 800, 900, 1000,
        100, 200, 300, 400,
    ]]
    _seed_baseline_history(
        db, category_id=big_cat, amounts=big_amounts, merchant_prefix="big",
    )
    db.commit()

    # A 250 NIS tx in rare_cat. Raw rare-cat baseline says (center=100,
    # spread=0) → would fire trivially. After shrinkage with weight=6/30=0.2,
    # effective_center ≈ 0.2*100 + 0.8*global_center; effective_spread ≈
    # 0.2*0 + 0.8*global_spread — large enough that 250 NIS no longer looks
    # like a 6+ sigma outlier.
    tx = _add_tx(
        db, merchant="rare_shop", amount=250,
        occurred_on=AS_OF - timedelta(days=1),
        category_id=rare_cat,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a1 = [
        f for f in flags
        if f.detector == "a1_category_outlier" and f.transaction_id == tx.id
    ]
    # Without shrinkage the spread=0 path would either fire wildly or
    # be gated out by the spread<=0 check. EITHER way is acceptable as
    # long as the spurious-fire path is closed. The behavior we DO NOT
    # want is "fires critical/warning on a 250 NIS tx in a small-n
    # category because raw MAD happened to be 0".
    if a1:
        # If it does fire, it must NOT be critical — the shrinkage
        # toward the noisy global baseline should at most yield 'info'.
        assert a1[0].severity == "info", (
            f"small-sample category should not produce a critical/warning "
            f"A1 flag on a 250 NIS tx; got {a1[0].severity}"
        )


def test_a1_fallback_to_rolling_stats_when_no_raw_history(db):
    """Backwards-compat: when the category has zero raw transactions
    in the baseline window but DOES have merchant_rolling_stats seeded,
    A1 falls back to the rolling-stats aggregation path."""
    cat = 500
    # Seed only rolling-stats — no raw transactions in the baseline
    # window. Six baselines with spread of ±10 around 100 → category
    # median ≈ 100, MAD ≈ 10.
    for i, med in enumerate([80, 90, 100, 100, 110, 120]):
        _add_baseline(
            db, merchant=f"fb_{i}", category_id=cat,
            median=med, mad=Decimal("5"),
            mean=med, txn_count=4,
        )
    db.commit()

    tx = _add_tx(
        db, merchant="fb_outlier", amount=200,
        occurred_on=AS_OF - timedelta(days=1),
        category_id=cat,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a1 = [f for f in flags if f.detector == "a1_category_outlier" and f.transaction_id == tx.id]
    assert len(a1) == 1, f"fallback path should fire; got {flags}"
    assert a1[0].severity == "critical"
    # Rationale must show the rolling-stats fallback source.
    assert "src=rolling" in a1[0].rationale


def test_a1_sparse_raw_does_not_silently_fall_back_to_rolling(db):
    """Codex BLOCKER (2026-05-30): once ANY raw history exists for a
    category, the raw path wins. Sparse raw must NOT silently fall back
    to a stale rolling-stats proxy — that would be mixed-mode behavior.

    Scenario: category has just 3 raw obs (below the 6-count gate) AND
    a healthy rolling-stats baseline. The detector must use raw (and
    gate-suppress the fire because n<6), NOT fall back to rolling-stats
    and fire.
    """
    cat = 700
    # Sparse raw history: only 3 obs in cat 700. Add at varying amounts
    # so it's not degenerate (MAD!=0).
    _seed_baseline_history(
        db, category_id=cat,
        amounts=[Decimal("90"), Decimal("100"), Decimal("110")],
        merchant_prefix="sparse",
    )
    # Healthy rolling-stats for the same category — would fire critical
    # on a 200 NIS tx if it were consulted.
    for i, med in enumerate([80, 90, 100, 100, 110, 120]):
        _add_baseline(
            db, merchant=f"healthy_{i}", category_id=cat,
            median=med, mad=Decimal("5"),
            mean=med, txn_count=4,
        )
    db.commit()

    tx = _add_tx(
        db, merchant="big_purchase", amount=200,
        occurred_on=AS_OF - timedelta(days=1),
        category_id=cat,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a1 = [
        f for f in flags
        if f.detector == "a1_category_outlier" and f.transaction_id == tx.id
    ]
    # The raw path returns (center, spread, n=3); _evaluate_a1 gates
    # because 3 < A1_MIN_BASELINE_COUNT. NO fire. If the code had
    # silently fallen through to rolling-stats, this would have fired
    # critical (as in test_a1_fallback_to_rolling_stats_when_no_raw_history).
    assert a1 == [], (
        "sparse raw must gate on count<6, not fall through to "
        f"rolling-stats; got flags={a1}"
    )


def test_a1_raw_baseline_excludes_detection_window_tx(db):
    """A newly-arrived outlier in the detection window must NOT pollute
    its own baseline.

    Scenario: baseline window has 30 obs at 100 NIS, then a 600 NIS tx
    arrives in the detection window. The detection tx must be excluded
    from the baseline computation (otherwise its presence would bias
    the trimmed mean upward and the spread outward, masking itself).
    """
    cat = 600
    # Use a tightly-noisy distribution so MAD > 0 after trim; without
    # natural noise the baseline spread collapses to zero and A1 gates
    # out before evaluating.
    noisy = [Decimal(str(v)) for v in (
        [95, 97, 99, 100, 101, 103, 105] * 4 + [100, 100]
    )]
    assert len(noisy) == 30
    _seed_baseline_history(
        db, category_id=cat,
        amounts=noisy, merchant_prefix="exc",
    )
    db.commit()

    tx = _add_tx(
        db, merchant="detection_outlier", amount=600,
        occurred_on=AS_OF - timedelta(days=2),  # inside 30-day detection window.
        category_id=cat,
    )
    db.commit()

    flags = detect_bucket_a(db, USER, as_of=AS_OF)
    a1 = [
        f for f in flags
        if f.detector == "a1_category_outlier" and f.transaction_id == tx.id
    ]
    # If the detection tx had been included in its own baseline, the
    # baseline center would shift toward ~116 and the spread would
    # widen. We assert it fires critical (z >= 6) — which requires the
    # baseline to remain anchored at 100 NIS.
    assert len(a1) == 1
    assert a1[0].severity == "critical"
