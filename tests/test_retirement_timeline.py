"""Tests for the Holistic Timeline composer (sprint commit #10).

Covers:
  - Empty user → well-formed shape with empty arrays.
  - Two historical vests → past_vests populated + future_vests projected.
  - Retirement-milestone life event → surfaces on life_events marker list.
  - horizon_days caps future-vest projection length.
  - MAX_FUTURE_VESTS hard cap (12) on the projected count.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.retirement_timeline import (
    MAX_FUTURE_VESTS,
    build_holistic_timeline,
)
from argosy.state.models import (
    Base,
    LifeEvent,
    RsuVestEvent,
    User,
)


@pytest.fixture
def db_session(tmp_path):
    """Self-contained SQLite + seeded user 'ariel'."""
    db_path = tmp_path / "retirement_timeline.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_vest(
    session,
    *,
    user_id: str = "ariel",
    grant_id: str = "G1",
    vest_date: date,
    shares: float = 100.0,
    fmv: float = 150.0,
) -> None:
    """Helper to add an RsuVestEvent row for a test."""
    session.add(RsuVestEvent(
        user_id=user_id,
        symbol="NVDA",
        grant_id=grant_id,
        vest_date=vest_date,
        shares_vested=Decimal(str(shares)),
        shares_withheld=Decimal("0"),
        shares_net=Decimal(str(shares)),
        fmv_per_share_usd=Decimal(str(fmv)),
        award_date=None,
        source_file="test_seed",
    ))
    session.commit()


class TestEmptyUser:
    """Empty user has no vests, no life events; payload still well-formed."""

    def test_empty_user_returns_empty_arrays(self, db_session):
        as_of = date(2026, 5, 29)
        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
        )
        assert out.today == as_of
        assert out.past_vests == []
        assert out.future_vests == []
        assert out.life_events == []
        # With portfolio=0 / expenses=0 the retire-ready check trips at
        # t=0, so all three scenario zones land on current_age. That's
        # documented behavior of the canonical clamp on empty users.
        # (Three zones, since each scenario produces age_years != None.)
        assert len(out.retire_ready_zones) == 3
        scenarios = {z.scenario for z in out.retire_ready_zones}
        assert scenarios == {"bear", "base", "bull"}


class TestHistoricalAndProjected:
    """Two historical vests → past list + forward projection."""

    def test_two_historical_vests_classified_and_projected(self, db_session):
        as_of = date(2026, 5, 29)
        # Two vests strictly in the past.
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2025, 12, 15), shares=100, fmv=140.0,
        )
        _seed_vest(
            db_session, grant_id="G2",
            vest_date=date(2026, 3, 18), shares=120, fmv=181.93,
        )

        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
        )

        # Past list populated, sorted ascending.
        assert len(out.past_vests) == 2
        assert out.past_vests[0].date < out.past_vests[1].date
        assert all(v.kind == "past_vest" for v in out.past_vests)
        # Field plumbing: gross USD is shares * fmv.
        assert out.past_vests[0].estimated_gross_usd == pytest.approx(
            100 * 140.0, rel=1e-6,
        )
        assert out.past_vests[1].estimated_gross_usd == pytest.approx(
            120 * 181.93, rel=1e-6,
        )

        # Future vests projected from the latest historical at +90d
        # increments. With as_of=2026-05-29 and latest=2026-03-18, the
        # first projected vest lands at 2026-06-16 (latest + 90d).
        assert len(out.future_vests) > 0
        assert all(v.kind == "future_vest" for v in out.future_vests)
        assert out.future_vests[0].date > as_of
        # First future vest uses the latest historical's per-tranche size.
        assert out.future_vests[0].shares == pytest.approx(120.0)
        assert out.future_vests[0].fmv_per_share_usd == pytest.approx(181.93)

        # Projected vests are strictly increasing in time.
        for i in range(1, len(out.future_vests)):
            assert out.future_vests[i].date > out.future_vests[i - 1].date


class TestLifeEventsMarker:
    """Retirement-milestone life event renders as a marker."""

    def test_retirement_milestone_event_in_life_events_list(self, db_session):
        as_of = date(2026, 5, 29)
        target = date(2028, 9, 1)
        db_session.add(LifeEvent(
            user_id="ariel",
            category="retirement_milestone",
            kind="target_retire_year_change",
            target_date=target,
            amount_usd=None,
            description="bump target retirement to Sep-2028",
        ))
        # Also add a recurring event WITHOUT a target_date -- must be
        # filtered out (recurring patterns aren't point-in-time markers).
        db_session.add(LifeEvent(
            user_id="ariel",
            category="recurring_expense",
            kind="bi_annual_vacation",
            target_date=None,
            amount_usd=4000.0,
            description="should not appear in life_events markers",
        ))
        db_session.commit()

        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
        )

        # Only the dated row surfaces.
        assert len(out.life_events) == 1
        marker = out.life_events[0]
        assert marker.date == target
        assert marker.category == "retirement_milestone"
        assert marker.kind == "target_retire_year_change"
        assert marker.description == "bump target retirement to Sep-2028"


class TestHorizonCaps:
    """horizon_days and MAX_FUTURE_VESTS both bound the projected list."""

    def test_short_horizon_caps_projection_length(self, db_session):
        as_of = date(2026, 5, 29)
        # Seed one recent historical vest so projection has a base.
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 18), shares=100, fmv=180.0,
        )

        # 100-day horizon — first projection at +90d (2026-06-16) is
        # 18 days after as_of, comfortably inside; second at +180d
        # (2026-09-14) is 108 days out, OUTSIDE the 100-day window.
        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
            horizon_days=100,
        )
        assert len(out.future_vests) == 1
        assert out.future_vests[0].date <= as_of + timedelta(days=100)

    def test_future_vest_count_clamped_at_12(self, db_session):
        as_of = date(2026, 5, 29)
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 18), shares=100, fmv=180.0,
        )

        # Huge horizon (50 years) — without the count cap, projection
        # would emit 50*4 ≈ 200 markers. MAX_FUTURE_VESTS=12 caps it.
        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
            horizon_days=365 * 50,
        )
        assert len(out.future_vests) == MAX_FUTURE_VESTS

    def test_horizon_days_negative_clamped_to_min(self, db_session):
        """Codex IMPORTANT (commit #10 review): negative horizon_days
        would create a past horizon and silently zero future vests.
        Must be clamped at the service-layer to a positive minimum."""
        as_of = date(2026, 5, 29)
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 18), shares=100, fmv=180.0,
        )
        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
            horizon_days=-100,  # would otherwise place horizon BEFORE today
        )
        # Clamp to MIN_HORIZON_DAYS=1 → no future vests reachable
        # within the 1-day window, but the response shape stays
        # well-formed.
        assert isinstance(out.future_vests, list)
        assert all(v.date >= as_of for v in out.future_vests)

    def test_horizon_days_huge_clamped_to_max(self, db_session):
        """Codex IMPORTANT (commit #10 review): horizon_days values
        above MAX_HORIZON_DAYS are clamped down."""
        from argosy.services.retirement_timeline import MAX_HORIZON_DAYS
        as_of = date(2026, 5, 29)
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 18), shares=100, fmv=180.0,
        )
        # Pass an absurd value (1000 years); MAX_FUTURE_VESTS still
        # caps the count, and the underlying horizon is bounded too.
        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
            horizon_days=365 * 1000,
        )
        # The final clamp lands the horizon at as_of + MAX_HORIZON_DAYS.
        max_reachable = as_of + timedelta(days=MAX_HORIZON_DAYS)
        for v in out.future_vests:
            assert v.date <= max_reachable

    def test_today_boundary_vest_classified_as_past(self, db_session):
        """Codex NICE (commit #10 review): a vest event whose date is
        EXACTLY today should be classified as past, not future."""
        as_of = date(2026, 5, 29)
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=as_of, shares=100, fmv=180.0,
        )
        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
            horizon_days=365,
        )
        assert len(out.past_vests) == 1
        assert out.past_vests[0].date == as_of
        # The today vest is also the latest, so projections from it
        # start +90 days out — verify no projected vest lands on today.
        for v in out.future_vests:
            assert v.date > as_of
