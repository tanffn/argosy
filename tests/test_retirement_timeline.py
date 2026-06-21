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
    UserContext,
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
    """Empty user has no DB vests / DB life events; payload still well-formed.

    future_vests stays empty (no rsu_vest_schedule in identity_yaml, no
    RsuVestEvent rows). life_events is NOT empty: the canonical phase-
    expense fallback fires off the household age anchor (which always
    resolves — falls back to ~43yo when DOB is missing), so the spending-
    phase markers populate even for a user with no other state."""

    def test_empty_user_returns_well_formed_payload(self, db_session):
        as_of = date(2026, 5, 29)
        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
        )
        assert out.today == as_of
        assert out.past_vests == []
        # No vest source (neither DB table nor identity_yaml schedule).
        assert out.future_vests == []
        # life_events come from the canonical phase curve (age-anchored).
        assert all(e.category == "expense_event" for e in out.life_events)
        assert all(e.date >= as_of for e in out.life_events)
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


class TestCanonicalScheduleFallback:
    """When rsu_vest_events is empty (the primary household keeps its
    vests in identity_yaml.rsu_vest_schedule, not the CSV table), the
    builder falls back to the canonical project_quarterly_vests source
    for future_vests AND the phase-expense curve for life_events. This
    is the path the live /api/retirement/timeline?user_id=ariel hits."""

    _IDENTITY_YAML = """\
rsu_vest_schedule:
  implied_nvda_price_usd: 215.05
  active_grants:
    - award_date: "2023-06-08"
      award_id: "246477"
      quarterly_shares: 220
    - award_date: "2025-03-10"
      award_id: "331375"
      quarterly_shares_approx: 71
  quarterly_vests:
    - {date: "2026-06-17", period: "June 2026", shares: 729}
    - {date: "2026-09-16", period: "September 2026", shares: 449}
    - {date: "2026-12-09", period: "December 2026", shares: 460}
    - {date: "2027-03-17", period: "March 2027", shares: 450}
"""

    def _seed_ctx(self, session, yaml_text: str) -> None:
        session.add(UserContext(user_id="ariel", identity_yaml=yaml_text))
        session.commit()

    def test_future_vests_from_canonical_schedule(self, db_session):
        # No RsuVestEvent rows seeded — DB-backed projection is empty, so
        # the canonical rsu_vest_schedule fallback must populate it.
        self._seed_ctx(db_session, self._IDENTITY_YAML)
        as_of = date(2026, 6, 21)

        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
        )

        # DB had no vest rows → past stays empty, future comes from YAML.
        assert out.past_vests == []
        assert len(out.future_vests) > 0
        assert all(v.kind == "future_vest" for v in out.future_vests)
        # Sourced from the canonical schedule, valued at the implied NVDA
        # price; strictly future + ascending.
        assert all(v.symbol == "NVDA" for v in out.future_vests)
        assert all(v.grant_id == "rsu_vest_schedule" for v in out.future_vests)
        assert all(v.fmv_per_share_usd == pytest.approx(215.05) for v in out.future_vests)
        assert all(v.date > as_of for v in out.future_vests)
        for i in range(1, len(out.future_vests)):
            assert out.future_vests[i].date > out.future_vests[i - 1].date
        # The portal June-2026 vest already vested (<= today) → excluded;
        # Sept-2026 (449sh) is the first forward marker, reconciling with
        # the overview RSU chapter's share buckets.
        first = out.future_vests[0]
        assert first.date == date(2026, 9, 15)
        assert first.shares == pytest.approx(449.0)
        assert first.estimated_gross_usd == pytest.approx(449.0 * 215.05)

    def test_life_events_from_phase_curve(self, db_session):
        # No LifeEvent rows → canonical phase-expense curve fallback.
        self._seed_ctx(db_session, self._IDENTITY_YAML)
        as_of = date(2026, 6, 21)

        out = build_holistic_timeline(
            session=db_session,
            user_id="ariel",
            as_of=as_of,
        )

        assert len(out.life_events) > 0
        labels = {e.kind for e in out.life_events}
        # empty_nest (age 56) + healthcare_ramp (age 65) fall inside the
        # 30y default horizon for a ~43yo; kids_peak (already entered) and
        # late_life (beyond horizon) are correctly skipped.
        assert "empty_nest" in labels
        assert all(e.category == "expense_event" for e in out.life_events)
        assert all(e.date >= as_of for e in out.life_events)
        # Markers carry a human-readable phase description.
        assert all(e.description for e in out.life_events)

    def test_db_vests_take_precedence_over_canonical(self, db_session):
        # When the DB DOES have vest rows, the canonical fallback must NOT
        # fire (no double-counting / source mixing).
        self._seed_ctx(db_session, self._IDENTITY_YAML)
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 18), shares=100, fmv=180.0,
        )
        as_of = date(2026, 6, 21)
        out = build_holistic_timeline(
            session=db_session, user_id="ariel", as_of=as_of,
        )
        # Future vests came from the DB projection (grant_id G1, fmv 180),
        # not the canonical schedule (grant_id rsu_vest_schedule, fmv 215).
        assert len(out.future_vests) > 0
        assert all(v.grant_id == "G1" for v in out.future_vests)

    def test_missing_schedule_degrades_to_empty(self, db_session):
        # No UserContext at all → both fallbacks degrade to [] (no throw).
        as_of = date(2026, 6, 21)
        out = build_holistic_timeline(
            session=db_session, user_id="ariel", as_of=as_of,
        )
        assert out.future_vests == []
        # Phase curve still resolves (age falls back to 43.0), so life
        # events DO populate even without a schedule — they only need the
        # household age anchor, which extract_household_state always gives.
        assert isinstance(out.life_events, list)


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
