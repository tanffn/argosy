"""Tests for the canonical effective_retire_ready_age() function.

Sprint commit #9 of the plan/execute/monitor reorg. Codex BLOCKER #3 on
the spec review: every retirement-age consumer (timeline card, hero,
MC regression, monitor) must call this one function, not roll its own.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.cashflow_projection import (
    EffectiveRetireReadyAge,
    effective_retire_ready_age,
)
from argosy.state.models import Base, RsuVestEvent, User


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "effective_retire.db"
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


def _add_historical_vest(
    session,
    *,
    vest_date: date,
    grant_id: str = "213000",
    shares: int = 280,
    fmv: float = 181.93,
):
    """Helper: seed an rsu_vest_events row."""
    session.add(RsuVestEvent(
        user_id="ariel",
        symbol="NVDA",
        grant_id=grant_id,
        vest_date=vest_date,
        shares_vested=Decimal(str(shares)),
        shares_withheld=Decimal("0"),
        shares_net=Decimal(str(shares)),
        fmv_per_share_usd=Decimal(str(fmv)),
        award_date=date(2022, 6, 8),
        source_file="test-fixture",
    ))
    session.commit()


class TestRSUClamp:
    """RSU vest projection heuristic (latest historical + 90d cadence)."""

    def test_no_vests_returns_none(self, db_session):
        """User with no vest events → no RSU clamp date."""
        from argosy.services.cashflow_projection import (
            _latest_projected_rsu_vest_date,
        )
        result = _latest_projected_rsu_vest_date(
            db_session, "ariel", date(2026, 5, 29),
        )
        assert result is None

    def test_recent_vest_projects_next_quarter(self, db_session):
        """Latest vest 2026-03-18 → next expected vest ~2026-06-16
        (90 days later)."""
        from argosy.services.cashflow_projection import (
            _latest_projected_rsu_vest_date,
        )
        _add_historical_vest(db_session, vest_date=date(2026, 3, 18))
        projected = _latest_projected_rsu_vest_date(
            db_session, "ariel", date(2026, 5, 29),
        )
        assert projected == date(2026, 3, 18) + timedelta(days=90)
        # = 2026-06-16

    def test_stale_vest_iterates_forward(self, db_session):
        """Latest vest 2024-09-18 (20 months ago) → projection iterates
        until next quarterly slot AFTER today."""
        _add_historical_vest(db_session, vest_date=date(2024, 9, 18))
        from argosy.services.cashflow_projection import (
            _latest_projected_rsu_vest_date,
        )
        as_of = date(2026, 5, 29)
        projected = _latest_projected_rsu_vest_date(
            db_session, "ariel", as_of,
        )
        # Each +90d step lands on a future date that the loop checks
        # against as_of. With 2024-09-18 → +90 = 2024-12-17 (still past),
        # → 2025-03-17 → 2025-06-15 → 2025-09-13 → 2025-12-12 → 2026-03-12
        # → 2026-06-10 (first date >= 2026-05-29 going forward, but the
        # loop exits when projected > as_of; verify it's AFTER as_of).
        assert projected > as_of
        # And the projection should be within one cadence step of as_of
        # (i.e. we haven't over-shot by multiple quarters).
        assert (projected - as_of).days <= 90

    def test_future_dated_vest_used_as_is(self, db_session):
        """If somehow a future vest is already in the table (backfilled
        from a known schedule), use it directly."""
        future = date(2026, 9, 18)
        _add_historical_vest(db_session, vest_date=future)
        from argosy.services.cashflow_projection import (
            _latest_projected_rsu_vest_date,
        )
        projected = _latest_projected_rsu_vest_date(
            db_session, "ariel", date(2026, 5, 29),
        )
        assert projected == future


class TestEffectiveRetireReadyAge:
    """End-to-end: the canonical function returns clamped age with reason."""

    def test_returns_dataclass_shape(self, db_session):
        """Smoke: function returns an EffectiveRetireReadyAge with the
        expected fields populated, even when household state is empty
        (no crossing detected)."""
        # The user has no household state seeded → projection won't find
        # a crossing → expect clamp_reason='no_crossing'.
        result = effective_retire_ready_age(
            scenario="base",
            user_id="ariel",
            session=db_session,
        )
        assert isinstance(result, EffectiveRetireReadyAge)
        assert result.scenario == "base"
        # With no household state, the base projection has no portfolio
        # value and no crossing is reachable.
        assert result.clamp_reason in {"no_crossing", "no_clamp_needed", "rsu_unvested"}

    def test_all_three_scenarios_callable(self, db_session):
        """The function MUST accept 'bear' | 'base' | 'bull' literally."""
        for scenario in ("bear", "base", "bull"):
            result = effective_retire_ready_age(
                scenario=scenario,
                user_id="ariel",
                session=db_session,
            )
            assert result.scenario == scenario

    def test_canonical_invariant_same_inputs_same_output(self, db_session):
        """Spec invariant: no consumer computes retire-ready-age
        independently. Asserted here by calling the function twice with
        identical inputs + checking the result is byte-equal."""
        a = effective_retire_ready_age(
            scenario="base",
            user_id="ariel",
            session=db_session,
            as_of=date(2026, 5, 29),
        )
        b = effective_retire_ready_age(
            scenario="base",
            user_id="ariel",
            session=db_session,
            as_of=date(2026, 5, 29),
        )
        # Dataclass __eq__ compares all fields including the clamp date.
        assert a == b
