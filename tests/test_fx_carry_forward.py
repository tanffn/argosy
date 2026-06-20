"""A missing daily BOI USD/NIS rate (a feed gap beyond the 10-day walkback) must NOT
pending-out every NIS figure in the plan. ``_apply_fx_boi`` carries forward the most-
recent cached rate (flagged MEDIUM, "as of") instead, and only pends when the cache
holds no USD rate at all."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.services.plan_numeric_resolver import _apply_fx_boi
from argosy.state.models import FxRate


def test_stale_rate_is_carried_forward_not_pended(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        # Only an OLD rate (100 days back) — outside the 10-day walkback.
        old = date.today() - timedelta(days=100)
        s.add(FxRate(date=old, currency="USD", rate=Decimal("2.944"), source="test"))
        s.commit()
        values: dict = {}
        _apply_fx_boi(s, values)
        rv = values["fx.usd_nis"]
        assert rv.status == "resolved"
        assert abs(float(rv.value) - 2.944) < 1e-9
        assert rv.confidence == "MEDIUM"           # downgraded — it's stale
        assert "carried forward" in rv.source_locator
        assert str(old) in rv.source_locator       # explicit "as of" provenance


def test_empty_cache_still_pends(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        values: dict = {}
        _apply_fx_boi(s, values)
        assert values["fx.usd_nis"].status == "pending"  # never the magic number


def test_fresh_rate_resolves_high_confidence(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        s.add(FxRate(date=date.today(), currency="USD", rate=Decimal("2.95"), source="test"))
        s.commit()
        values: dict = {}
        _apply_fx_boi(s, values)
        rv = values["fx.usd_nis"]
        assert rv.status == "resolved" and rv.confidence == "HIGH"
        assert "carried forward" not in (rv.source_locator or "")
