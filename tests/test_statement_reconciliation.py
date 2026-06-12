"""Statement-merge reconciliation — overlap dedup + gap/balance-continuity.

Two bank-statement dumps can overlap or leave a gap:
  * OVERLAP (e.g. 1-8 + 5-16): the days 5-8 transactions appear in BOTH dumps.
    The per-transaction content hash includes ``statement_id``, so cross-statement
    duplicates are NOT caught by the normal dedup — they double-count. This module
    removes them.
  * GAP (e.g. 1-8 + 14-16): days 9-13 are absent. That's only a problem if money
    moved in the gap — validated by BALANCE CONTINUITY: prior closing == new
    opening ⇒ quiet days (OK); mismatch ⇒ loud warning with the Δ.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.expense_ingest.statement_reconciliation import (
    reconcile_statement,
)
from argosy.state.models import (
    Base,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    User,
    UserFile,
)

USER = "ariel"


@pytest.fixture
def sync_session(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'recon.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _source(db) -> int:
    f = UserFile(
        user_id=USER, sha256="c" * 64, original_name="o.xls",
        sanitized_name="o.xls", mime_type="application/vnd.ms-excel",
        kind="other", size_bytes=1, storage_path="/tmp/o",
        source="expense_statement",
    )
    db.add(f)
    db.flush()
    src = ExpenseSource(
        user_id=USER, kind="bank", issuer="leumi",
        external_id="osh", display_name="Leumi Osh",
    )
    db.add(src)
    db.flush()
    # file_id reused across statements is fine for the test.
    db._file_id = f.id  # type: ignore[attr-defined]
    return src.id


def _statement(db, source_id, *, start, end) -> int:
    stmt = ExpenseStatement(
        user_id=USER, source_id=source_id, file_id=db._file_id,
        period_start=start, period_end=end,
        parsed_total_nis=Decimal("0"), declared_total_nis=Decimal("0"),
        parser_name="leumi_osh", parser_version="0.1.0", status="parsed",
    )
    db.add(stmt)
    db.flush()
    return stmt.id


def _tx(db, source_id, statement_id, *, merchant, occurred_on, direction="debit",
        amount=None, amount_orig=None, currency_orig=None, reference=None,
        balance=None, balance_usd=None):
    raw: dict = {}
    if balance is not None:
        raw["balance"] = balance
    if balance_usd is not None:
        raw["balance_usd"] = balance_usd
    db.add(ExpenseTransaction(
        user_id=USER, source_id=source_id, statement_id=statement_id,
        occurred_on=occurred_on, merchant_raw=merchant,
        merchant_normalized=merchant,
        amount_nis=Decimal(str(amount)) if amount is not None else None,
        amount_orig=Decimal(str(amount_orig)) if amount_orig is not None else None,
        currency_orig=currency_orig, reference=reference,
        direction=direction, tx_type="regular",
        raw_row_json=json.dumps(raw),
    ))
    db.flush()


def _count_txns(db, statement_id) -> int:
    return db.query(ExpenseTransaction).filter_by(statement_id=statement_id).count()


# ---------------------------------------------------------------------------
# Overlap dedup
# ---------------------------------------------------------------------------

def test_overlap_dedup_removes_cross_statement_duplicates(sync_session):
    db = sync_session
    src = _source(db)
    # Statement A: days 1-8.
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    for d in (3, 6, 7):
        _tx(db, src, a, merchant="SUPER", amount=10 * d, occurred_on=date(2026, 1, d))
    # Statement B: days 5-16, repeats the days 6 & 7 lines (overlap) + new ones.
    b = _statement(db, src, start=date(2026, 1, 5), end=date(2026, 1, 16))
    for d in (6, 7):  # duplicates of A
        _tx(db, src, b, merchant="SUPER", amount=10 * d, occurred_on=date(2026, 1, d))
    for d in (10, 14):  # genuinely new
        _tx(db, src, b, merchant="SUPER", amount=10 * d, occurred_on=date(2026, 1, d))
    db.commit()

    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)

    assert receipt.overlap_duplicates_removed == 2
    assert a in receipt.overlapping_statement_ids
    # B keeps only its genuinely-new lines (days 10, 14); A is untouched.
    assert _count_txns(db, b) == 2
    assert _count_txns(db, a) == 3


def test_non_overlapping_statements_no_dedup(sync_session):
    db = sync_session
    src = _source(db)
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    _tx(db, src, a, merchant="X", amount=10, occurred_on=date(2026, 1, 3))
    b = _statement(db, src, start=date(2026, 1, 14), end=date(2026, 1, 16))
    _tx(db, src, b, merchant="X", amount=10, occurred_on=date(2026, 1, 15))
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)
    assert receipt.overlap_duplicates_removed == 0
    assert _count_txns(db, b) == 1


# ---------------------------------------------------------------------------
# Gap + balance continuity
# ---------------------------------------------------------------------------

def test_gap_with_balance_continuity_is_ok(sync_session):
    db = sync_session
    src = _source(db)
    # Prior 1-8 closes at 1000 (last txn balance).
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    _tx(db, src, a, merchant="X", amount=50, occurred_on=date(2026, 1, 8),
        direction="debit", balance="1000")
    # New 14-16: first txn balance 950 after a 50 debit ⇒ opening = 1000 (== prior close).
    b = _statement(db, src, start=date(2026, 1, 14), end=date(2026, 1, 16))
    _tx(db, src, b, merchant="Y", amount=50, occurred_on=date(2026, 1, 14),
        direction="debit", balance="950")
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)
    assert len(receipt.continuities) == 1
    c = receipt.continuities[0]
    assert c.has_gap is True
    assert c.balance_continuous is True
    assert c.warning is None
    assert not receipt.warnings


def test_gap_with_balance_mismatch_warns(sync_session):
    db = sync_session
    src = _source(db)
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    _tx(db, src, a, merchant="X", amount=50, occurred_on=date(2026, 1, 8),
        direction="debit", balance="1000")
    # New opening = 800 - (-50) = 850 ≠ 1000 ⇒ Δ -150 ⇒ warn (money moved in the gap).
    b = _statement(db, src, start=date(2026, 1, 14), end=date(2026, 1, 16))
    _tx(db, src, b, merchant="Y", amount=50, occurred_on=date(2026, 1, 14),
        direction="debit", balance="800")
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)
    c = receipt.continuities[0]
    assert c.has_gap is True
    assert c.balance_continuous is False
    assert c.delta == Decimal("-150")
    assert c.warning is not None and "missing transactions" in c.warning
    assert receipt.warnings  # surfaced


def test_adjacent_statements_no_gap(sync_session):
    db = sync_session
    src = _source(db)
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    _tx(db, src, a, merchant="X", amount=50, occurred_on=date(2026, 1, 8), balance="1000")
    # Starts the very next day ⇒ no gap, even if balances differ.
    b = _statement(db, src, start=date(2026, 1, 9), end=date(2026, 1, 16))
    _tx(db, src, b, merchant="Y", amount=50, occurred_on=date(2026, 1, 9), balance="500")
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)
    c = receipt.continuities[0]
    assert c.has_gap is False
    assert c.warning is None


# ---------------------------------------------------------------------------
# Codex-blocker regressions (round 1)
# ---------------------------------------------------------------------------

def test_distinct_usd_rows_not_deduped(sync_session):
    """B1: two DISTINCT USD rows (same date/merchant/direction, amount_nis=None,
    different amount_orig + reference) must NOT collapse — that was silent money
    loss. The dedup key includes reference + amount_orig + currency_orig."""
    db = sync_session
    src = _source(db)
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    _tx(db, src, a, merchant="SEC BUY", occurred_on=date(2026, 1, 6),
        direction="debit", amount_orig=100, currency_orig="USD", reference="A")
    b = _statement(db, src, start=date(2026, 1, 5), end=date(2026, 1, 16))
    _tx(db, src, b, merchant="SEC BUY", occurred_on=date(2026, 1, 6),
        direction="debit", amount_orig=200, currency_orig="USD", reference="B")
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)
    assert receipt.overlap_duplicates_removed == 0
    assert _count_txns(db, b) == 1


def test_identical_usd_rows_still_deduped(sync_session):
    """...but genuinely identical USD overlap rows DO dedup."""
    db = sync_session
    src = _source(db)
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    _tx(db, src, a, merchant="SEC BUY", occurred_on=date(2026, 1, 6),
        direction="debit", amount_orig=100, currency_orig="USD", reference="A")
    b = _statement(db, src, start=date(2026, 1, 5), end=date(2026, 1, 16))
    _tx(db, src, b, merchant="SEC BUY", occurred_on=date(2026, 1, 6),
        direction="debit", amount_orig=100, currency_orig="USD", reference="A")
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)
    assert receipt.overlap_duplicates_removed == 1


def test_overlap_across_two_existing_statements_counts_once(sync_session):
    """B2 (r2): a new row overlapping BOTH an earlier and a later existing
    statement must be removed/counted exactly ONCE, not twice."""
    db = sync_session
    src = _source(db)
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 10))
    _tx(db, src, a, merchant="X", amount=10, occurred_on=date(2026, 1, 7),
        direction="debit", reference="R")
    c = _statement(db, src, start=date(2026, 1, 5), end=date(2026, 1, 16))
    _tx(db, src, c, merchant="X", amount=10, occurred_on=date(2026, 1, 7),
        direction="debit", reference="R")
    b = _statement(db, src, start=date(2026, 1, 6), end=date(2026, 1, 8))
    _tx(db, src, b, merchant="X", amount=10, occurred_on=date(2026, 1, 7),
        direction="debit", reference="R")
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)
    assert receipt.overlap_duplicates_removed == 1   # not 2
    assert _count_txns(db, b) == 0


def test_usd_continuity_uses_amount_orig(sync_session):
    """B2: USD continuity must use amount_orig + balance_usd. A credit of 50 USD
    in the gap (amount_nis=None) makes opening 950 ≠ prior close 1000 ⇒ warn."""
    db = sync_session
    src = _source(db)
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    _tx(db, src, a, merchant="X", occurred_on=date(2026, 1, 8),
        direction="debit", amount_orig=10, currency_orig="USD", balance_usd="1000")
    b = _statement(db, src, start=date(2026, 1, 14), end=date(2026, 1, 16))
    # credit 50 USD, balance_usd 1000 ⇒ opening = 1000 - (+50) = 950 ≠ 1000.
    _tx(db, src, b, merchant="Y", occurred_on=date(2026, 1, 14),
        direction="credit", amount_orig=50, currency_orig="USD", balance_usd="1000")
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)
    c = receipt.continuities[0]
    assert c.currency == "USD"
    assert c.balance_continuous is False
    assert c.delta == Decimal("-50")
    assert receipt.warnings


def test_out_of_order_upload_catches_gap_via_next_neighbour(sync_session):
    """B4: upload the LATER statement first, then the earlier one; reconciling the
    earlier must check the NEXT neighbour and still catch the gap."""
    db = sync_session
    src = _source(db)
    # Later statement already present (opening 850 after a 50 debit @ balance 800).
    later = _statement(db, src, start=date(2026, 1, 14), end=date(2026, 1, 16))
    _tx(db, src, later, merchant="Y", amount=50, occurred_on=date(2026, 1, 14),
        direction="debit", balance="800")
    # Now the EARLIER statement arrives, closing at 1000 ≠ later opening 850.
    earlier = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    _tx(db, src, earlier, merchant="X", amount=50, occurred_on=date(2026, 1, 8),
        direction="debit", balance="1000")
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=earlier)
    assert any(c.has_gap and c.balance_continuous is False for c in receipt.continuities)
    assert receipt.warnings


def test_sub_shekel_movement_warns(sync_session):
    """B5: a ₪0.50 movement in the gap must warn (tolerance is ₪0.01, not ₪1.00)."""
    db = sync_session
    src = _source(db)
    a = _statement(db, src, start=date(2026, 1, 1), end=date(2026, 1, 8))
    _tx(db, src, a, merchant="X", amount=10, occurred_on=date(2026, 1, 8),
        direction="debit", balance="1000.00")
    # opening = 989.50 - (-10) = 999.50 ≠ 1000.00 ⇒ Δ -0.50 ⇒ warn.
    b = _statement(db, src, start=date(2026, 1, 14), end=date(2026, 1, 16))
    _tx(db, src, b, merchant="Y", amount=10, occurred_on=date(2026, 1, 14),
        direction="debit", balance="989.50")
    db.commit()
    receipt = reconcile_statement(db, user_id=USER, source_id=src, statement_id=b)
    c = receipt.continuities[0]
    assert c.balance_continuous is False
    assert c.delta == Decimal("-0.50")
    assert receipt.warnings
