"""Refund matcher: links credit rows to prior debits and inherits category."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.refund_matcher import match_refunds_for_user
from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    User, UserFile,
)


def _seed(s: Session, with_prior: bool = True, prior_categorized: bool = True):
    s.add(User(id="ariel", plan="free")); s.flush()
    f = UserFile(user_id="ariel", sha256="a"*64, original_name="x",
                 sanitized_name="x", mime_type="x", kind="other",
                 size_bytes=1, storage_path="/tmp/x",
                 source="chat_attachment")
    s.add(f); s.flush()
    cat = ExpenseCategory(slug="travel.flights", label_en="Flights",
                          label_he="טיסות")
    s.add(cat); s.flush()
    src = ExpenseSource(user_id="ariel", kind="card", issuer="max",
                        external_id="6225", display_name="Max 6225")
    s.add(src); s.flush()
    stmt = ExpenseStatement(
        user_id="ariel", source_id=src.id, file_id=f.id,
        period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
        charge_date=date(2026, 4, 15), parsed_total_nis=Decimal("0"),
        parser_name="max", parser_version="0.1.0", status="parsed",
    )
    s.add(stmt); s.flush()
    refund = ExpenseTransaction(
        user_id="ariel", statement_id=stmt.id, source_id=src.id,
        occurred_on=date(2026, 3, 21), merchant_raw="WIZZ AIRGR73FH",
        merchant_normalized="wizz airgr73fh",
        amount_nis=Decimal("2097.83"), direction="credit",
        tx_type="refund", raw_row_json="{}",
    )
    s.add(refund); s.flush()
    prior = None
    if with_prior:
        prior = ExpenseTransaction(
            user_id="ariel", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 2, 12), merchant_raw="WIZZ AIR123",
            merchant_normalized="wizz airgr73fh",
            amount_nis=Decimal("2097.83"), direction="debit",
            tx_type="regular",
            category_id=(cat.id if prior_categorized else None),
            category_source=("user" if prior_categorized else None),
            category_confidence=(Decimal("1.0") if prior_categorized else None),
            raw_row_json="{}",
        )
        s.add(prior); s.flush()
    return {"refund": refund, "prior": prior, "cat": cat}


def test_refund_matcher_inherits_category(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed(s); s.commit()
        n = match_refunds_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["refund"])
        assert n == 1
        assert ctx["refund"].refund_of_id == ctx["prior"].id
        assert ctx["refund"].category_id == ctx["cat"].id
        assert ctx["refund"].category_source == "inherited_from_refund"


def test_refund_matcher_skips_when_no_prior(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed(s, with_prior=False); s.commit()
        n = match_refunds_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["refund"])
        assert n == 0
        assert ctx["refund"].refund_of_id is None
        assert ctx["refund"].category_id is None


def test_refund_matcher_skips_when_prior_uncategorized(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        ctx = _seed(s, prior_categorized=False); s.commit()
        n = match_refunds_for_user(s, "ariel")
        s.commit()
        s.refresh(ctx["refund"])
        assert n == 0
        assert ctx["refund"].category_id is None


def test_refund_matcher_is_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed(s); s.commit()
        n1 = match_refunds_for_user(s, "ariel"); s.commit()
        n2 = match_refunds_for_user(s, "ariel"); s.commit()
        assert n1 == 1
        assert n2 == 0
