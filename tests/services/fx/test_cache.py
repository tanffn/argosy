"""fx.cache — DB read/write of daily rates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from argosy.services.fx.cache import (
    find_walkback,
    get_rate,
    put_rates,
)
from argosy.services.fx.errors import FXRateUnavailable


def _seed(session, rows):
    from argosy.state.models import FxRate
    for d, ccy, r in rows:
        session.add(FxRate(date=d, currency=ccy, rate=Decimal(str(r)), source="test"))
    session.flush()


def test_get_rate_returns_seeded_value(alembic_engine_at_head):
    from sqlalchemy.orm import Session
    with Session(alembic_engine_at_head) as s:
        _seed(s, [(date(2026, 4, 8), "USD", 3.6512)])
        s.commit()
        assert get_rate(s, date(2026, 4, 8), "USD") == Decimal("3.6512")


def test_get_rate_returns_none_when_missing(alembic_engine_at_head):
    from sqlalchemy.orm import Session
    with Session(alembic_engine_at_head) as s:
        assert get_rate(s, date(2026, 4, 8), "USD") is None


def test_put_rates_inserts_and_is_idempotent(alembic_engine_at_head):
    from sqlalchemy.orm import Session
    rows = [(date(2026, 4, 8), "USD", Decimal("3.6512"))]
    with Session(alembic_engine_at_head) as s:
        n1 = put_rates(s, rows)
        s.commit()
        n2 = put_rates(s, rows)  # idempotent on (date, currency) PK
        s.commit()
    assert n1 == 1
    assert n2 == 0


def test_find_walkback_finds_previous_friday_for_saturday(alembic_engine_at_head):
    from sqlalchemy.orm import Session
    with Session(alembic_engine_at_head) as s:
        _seed(s, [(date(2026, 4, 3), "USD", 3.65)])  # Friday
        s.commit()
        # Saturday = no rate; walkback returns Friday's
        assert find_walkback(s, date(2026, 4, 4), "USD") == Decimal("3.65")


def test_find_walkback_raises_when_more_than_7_days_missing(alembic_engine_at_head):
    from sqlalchemy.orm import Session
    with Session(alembic_engine_at_head) as s:
        with pytest.raises(FXRateUnavailable):
            find_walkback(s, date(2026, 4, 8), "USD")
