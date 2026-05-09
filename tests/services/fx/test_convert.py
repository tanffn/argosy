"""fx public API — convert / rate / warm_cache."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from argosy.services.fx import convert, rate, warm_cache
from argosy.services.fx.errors import FXRateUnavailable


def _seed(session, rows):
    from argosy.state.models import FxRate
    for d, ccy, r in rows:
        session.add(FxRate(date=d, currency=ccy, rate=Decimal(str(r)), source="test"))
    session.flush()
    session.commit()


def test_convert_same_currency_passthrough(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        assert convert(s, 100.0, "USD", "USD", date(2026, 4, 8)) == Decimal("100.0")


def test_convert_nis_alias_normalizes_to_ils(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed(s, [(date(2026, 4, 8), "USD", "3.65")])
        result = convert(s, 100.0, "USD", "NIS", date(2026, 4, 8))
        assert result == Decimal("365.00")


def test_convert_to_ils_uses_cached_rate(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed(s, [(date(2026, 4, 8), "USD", "3.65")])
        result = convert(s, 100.0, "USD", "ILS", date(2026, 4, 8))
        assert result == Decimal("365.00")


def test_convert_cross_rate_via_ils(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed(s, [
            (date(2026, 4, 8), "USD", "3.65"),
            (date(2026, 4, 8), "EUR", "4.00"),
        ])
        result = convert(s, 100.0, "USD", "EUR", date(2026, 4, 8))
        # 100 USD -> 365 ILS -> 365/4.00 = 91.25 EUR
        assert result == Decimal("91.25")


def test_rate_uses_walkback_for_weekend(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        _seed(s, [(date(2026, 4, 3), "USD", "3.65")])  # Friday
        # Saturday -> walkback to Friday's rate
        assert rate(s, "USD", "ILS", date(2026, 4, 4)) == Decimal("3.65")


def test_convert_raises_when_rate_missing_and_offline(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.fx.boi_client.fetch_range",
               side_effect=FXRateUnavailable("offline")):
        with pytest.raises(FXRateUnavailable):
            convert(s, 100.0, "USD", "ILS", date(2026, 4, 8))


def test_warm_cache_inserts_fetched_rates(alembic_engine_at_head):
    fake_rates = [(date(2026, 4, 8), "USD", Decimal("3.65"))]
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.fx.boi_client.fetch_range",
               return_value=fake_rates):
        n = warm_cache(s, date(2026, 4, 8), date(2026, 4, 8), ["USD"])
        s.commit()
    assert n == 1
    with Session(alembic_engine_at_head) as s2:
        assert rate(s2, "USD", "ILS", date(2026, 4, 8)) == Decimal("3.65")
