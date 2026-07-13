"""Kind-aware expense source Status (card↔bank / bank continuity / n/a)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.expense_ingest.source_status import (
    bank_statement_status,
    card_statement_status,
    declared_gap_status,
    statement_status,
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
        f"sqlite:///{tmp_path / 'status.db'}",
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


def _file(db) -> int:
    f = UserFile(
        user_id=USER, sha256="s" * 64, original_name="f.xls",
        sanitized_name="f.xls", mime_type="application/vnd.ms-excel",
        kind="other", size_bytes=1, storage_path="/tmp/f",
        source="expense_statement",
    )
    db.add(f)
    db.flush()
    return f.id


def _card(db, *, external_id="1266") -> ExpenseSource:
    src = ExpenseSource(
        user_id=USER, kind="card", issuer="isracard",
        external_id=external_id, display_name=f"Card {external_id}",
    )
    db.add(src)
    db.flush()
    return src


def _bank(db) -> ExpenseSource:
    src = ExpenseSource(
        user_id=USER, kind="bank", issuer="leumi",
        external_id="osh", display_name="Leumi Osh",
    )
    db.add(src)
    db.flush()
    return src


def _stmt(db, source_id, file_id, *, start, end, declared=None, parsed=None,
          charge_date=None, parser="isracard") -> ExpenseStatement:
    st = ExpenseStatement(
        user_id=USER, source_id=source_id, file_id=file_id,
        period_start=start, period_end=end,
        charge_date=charge_date,
        # Column is NOT NULL; bank fixtures often omit parsed — default 0.
        parsed_total_nis=Decimal(str(parsed if parsed is not None else 0)),
        declared_total_nis=Decimal(str(declared)) if declared is not None else None,
        parser_name=parser, parser_version="0.1.0", status="parsed",
    )
    db.add(st)
    db.flush()
    return st


def _tx(db, source_id, statement_id, *, occurred_on, amount, merchant="X",
        direction="debit", reference=None, posted_on=None, balance=None,
        is_card_payment=False, matched_statement_id=None):
    raw = {}
    if balance is not None:
        raw["balance"] = str(balance)
    db.add(ExpenseTransaction(
        user_id=USER, source_id=source_id, statement_id=statement_id,
        occurred_on=occurred_on, posted_on=posted_on,
        merchant_raw=merchant, merchant_normalized=merchant.lower(),
        amount_nis=Decimal(str(amount)), direction=direction,
        tx_type="regular", reference=reference,
        is_card_payment=is_card_payment,
        matched_statement_id=matched_statement_id,
        raw_row_json=json.dumps(raw),
    ))
    db.flush()


def test_declared_gap_status_na_not_unknown():
    assert declared_gap_status(None) == "n/a"
    assert declared_gap_status(0.1) == "green"
    assert declared_gap_status(2.0) == "yellow"
    assert declared_gap_status(10.0) == "red"


def test_card_falls_back_to_declared_gap_when_no_buckets(sync_session):
    db = sync_session
    fid = _file(db)
    card = _card(db)
    st = _stmt(
        db, card.id, fid,
        start=date(2026, 5, 1), end=date(2026, 5, 31),
        declared=250, parsed=250,
    )
    _tx(db, card.id, st.id, occurred_on=date(2026, 5, 10), amount=250)
    db.commit()
    assert card_statement_status(
        db, user_id=USER, source=card, statement=st,
    ) == "green"


def test_card_na_without_declared_or_bank(sync_session):
    db = sync_session
    fid = _file(db)
    card = _card(db)
    st = _stmt(
        db, card.id, fid,
        start=date(2026, 5, 1), end=date(2026, 5, 31),
        declared=None, parsed=100,
    )
    _tx(db, card.id, st.id, occurred_on=date(2026, 5, 10), amount=100)
    db.commit()
    assert card_statement_status(
        db, user_id=USER, source=card, statement=st,
    ) == "n/a"


def test_card_green_when_bank_matches_charge_bucket(sync_session):
    db = sync_session
    fid = _file(db)
    card = _card(db, external_id="1266")
    bank = _bank(db)
    st = _stmt(
        db, card.id, fid,
        start=date(2026, 5, 1), end=date(2026, 5, 31),
        charge_date=date(2026, 5, 15), declared=None, parsed=1078.31,
    )
    _tx(db, card.id, st.id, occurred_on=date(2026, 5, 3), amount=1078.31,
        posted_on=date(2026, 5, 15))
    bst = _stmt(
        db, bank.id, fid,
        start=date(2026, 5, 1), end=date(2026, 5, 31),
        parser="leumi_osh",
    )
    _tx(
        db, bank.id, bst.id, occurred_on=date(2026, 5, 15), amount=1078.31,
        merchant="ל.מאסטרקרד(יש)", reference="1266",
    )
    db.commit()
    assert card_statement_status(
        db, user_id=USER, source=card, statement=st,
        today=date(2026, 6, 1),
    ) == "green"


def test_card_red_when_bank_charge_exceeds_bucket(sync_session):
    db = sync_session
    fid = _file(db)
    card = _card(db, external_id="6225")
    bank = _bank(db)
    st = _stmt(
        db, card.id, fid,
        start=date(2026, 4, 18), end=date(2026, 7, 9),
        declared=None, parsed=918.30, parser="max",
    )
    _tx(db, card.id, st.id, occurred_on=date(2026, 4, 20), amount=918.30,
        posted_on=date(2026, 5, 15), merchant="BBB")
    bst = _stmt(
        db, bank.id, fid,
        start=date(2026, 5, 1), end=date(2026, 5, 31),
        parser="leumi_osh",
    )
    _tx(
        db, bank.id, bst.id, occurred_on=date(2026, 5, 15), amount=1051.73,
        merchant="כרטיסי אשראי-י", reference="8547",
    )
    db.commit()
    assert card_statement_status(
        db, user_id=USER, source=card, statement=st,
        today=date(2026, 6, 1),
    ) == "red"


def test_card_green_via_matched_statement_id(sync_session):
    db = sync_session
    fid = _file(db)
    card = _card(db)
    bank = _bank(db)
    st = _stmt(
        db, card.id, fid,
        start=date(2026, 5, 1), end=date(2026, 5, 31),
        declared=None, parsed=100,
    )
    _tx(db, card.id, st.id, occurred_on=date(2026, 5, 10), amount=100)
    bst = _stmt(
        db, bank.id, fid,
        start=date(2026, 5, 1), end=date(2026, 5, 31),
        parser="leumi_osh",
    )
    _tx(
        db, bank.id, bst.id, occurred_on=date(2026, 5, 15), amount=100,
        merchant="ל.מאסטרקרד(יש)", matched_statement_id=st.id,
        is_card_payment=True,
    )
    db.commit()
    assert card_statement_status(
        db, user_id=USER, source=card, statement=st,
    ) == "green"


def test_bank_overlap_identity_green(sync_session):
    db = sync_session
    fid = _file(db)
    bank = _bank(db)
    a = _stmt(
        db, bank.id, fid,
        start=date(2026, 1, 1), end=date(2026, 5, 10),
        parser="leumi_osh",
    )
    b = _stmt(
        db, bank.id, fid,
        start=date(2026, 3, 10), end=date(2026, 6, 10),
        parser="leumi_osh",
    )
    _tx(
        db, bank.id, a.id, occurred_on=date(2026, 4, 15), amount=654.88,
        merchant="כרטיסי אשראי-י", reference="8547", balance="57287.40",
    )
    _tx(
        db, bank.id, b.id, occurred_on=date(2026, 4, 15), amount=654.88,
        merchant="כרטיסי אשראי-י", reference="8547", balance="57287.40",
    )
    db.commit()
    assert bank_statement_status(
        db, user_id=USER, source=bank, statement=b,
    ) == "green"


def test_bank_overlap_identity_red_on_balance_mismatch(sync_session):
    db = sync_session
    fid = _file(db)
    bank = _bank(db)
    a = _stmt(
        db, bank.id, fid,
        start=date(2026, 1, 1), end=date(2026, 5, 10),
        parser="leumi_osh",
    )
    b = _stmt(
        db, bank.id, fid,
        start=date(2026, 3, 10), end=date(2026, 6, 10),
        parser="leumi_osh",
    )
    _tx(
        db, bank.id, a.id, occurred_on=date(2026, 4, 15), amount=654.88,
        merchant="כרטיסי אשראי-י", reference="8547", balance="57287.40",
    )
    _tx(
        db, bank.id, b.id, occurred_on=date(2026, 4, 15), amount=654.88,
        merchant="כרטיסי אשראי-י", reference="8547", balance="100.00",
    )
    db.commit()
    assert bank_statement_status(
        db, user_id=USER, source=bank, statement=b,
    ) == "red"


def test_bank_single_statement_na(sync_session):
    db = sync_session
    fid = _file(db)
    bank = _bank(db)
    a = _stmt(
        db, bank.id, fid,
        start=date(2026, 1, 1), end=date(2026, 5, 10),
        parser="leumi_osh",
    )
    _tx(
        db, bank.id, a.id, occurred_on=date(2026, 4, 15), amount=10,
        merchant="X", balance="100",
    )
    db.commit()
    assert bank_statement_status(
        db, user_id=USER, source=bank, statement=a,
    ) == "n/a"


def test_statement_status_dispatches_by_kind(sync_session):
    db = sync_session
    fid = _file(db)
    card = _card(db)
    st = _stmt(
        db, card.id, fid,
        start=date(2026, 5, 1), end=date(2026, 5, 31),
        declared=None, parsed=1,
    )
    db.commit()
    assert statement_status(
        db, user_id=USER, source=card, statement=st,
    ) == "n/a"
