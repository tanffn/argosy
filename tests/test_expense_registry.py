"""Tests for ExpenseSource registry / auto-registration."""

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.registry import (
    register_or_get_source, list_active_sources,
)
from argosy.services.expense_ingest.types import SourceHint
from argosy.state.models import ExpenseSource, User


def _seed_user(s: Session) -> None:
    s.add(User(id="ariel", plan="free"))
    s.flush()


def test_register_creates_new_source(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_user(s)
        hint = SourceHint(kind="card", issuer="isracard", external_id="1266",
                          cardholder_name="Ariel")
        src = register_or_get_source(s, "ariel", hint)
        s.commit()
        assert src.id is not None
        assert src.display_name  # auto-derived
        assert src.cardholder_name == "Ariel"


def test_register_reuses_existing_source(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_user(s)
        hint = SourceHint(kind="card", issuer="isracard", external_id="1266")
        src1 = register_or_get_source(s, "ariel", hint)
        s.commit()
        src2 = register_or_get_source(s, "ariel", hint)
        s.commit()
        assert src1.id == src2.id
        assert s.query(ExpenseSource).count() == 1


def test_register_does_not_overwrite_cardholder(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_user(s)
        hint1 = SourceHint(kind="card", issuer="isracard", external_id="1266",
                           cardholder_name="Ariel")
        src1 = register_or_get_source(s, "ariel", hint1)
        s.commit()
        # Second call without cardholder shouldn't blank it out
        hint2 = SourceHint(kind="card", issuer="isracard", external_id="1266")
        src2 = register_or_get_source(s, "ariel", hint2)
        s.commit()
        assert src2.cardholder_name == "Ariel"


def test_list_active_sources_filters_inactive(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed_user(s)
        hint = SourceHint(kind="card", issuer="max", external_id="6225")
        src = register_or_get_source(s, "ariel", hint)
        src.active = False
        s.commit()
        assert len(list_active_sources(s, "ariel")) == 0
        src.active = True
        s.commit()
        assert len(list_active_sources(s, "ariel")) == 1
