"""Tests for the monitor agent's macro-shift trigger.

Sprint commit #15 of the plan/execute/monitor reorg (spec §5.1.3).
Reads `news_signals` rows that Stage 2 (commit #14) classified as
materiality='high' + recommended_flag='macro_shift', fires monitor_flags
rows of kind='macro_shift' for any not yet flagged.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.plan_monitor import (
    MacroShiftCheckResult,
    MacroShiftFlag,
    check_macro_shift,
    get_active_macro_shift_flags,
)
from argosy.state.models import Base, MonitorFlag, NewsSignal, User


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "macro_shift.db"
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


def _seed_signal(
    session,
    *,
    materiality: str = "high",
    recommended_flag: str | None = "macro_shift",
    event_keywords: list[str] | None = None,
    parsed_tickers: list[str] | None = None,
    received_at: datetime | None = None,
    rationale: str = "Rate-cycle inflection",
    analyzed: bool = True,
) -> int:
    """Seed a news_signals row and return its id."""
    sig = NewsSignal(
        source="rss",
        source_ref=f"test-{id(event_keywords)}-{id(parsed_tickers)}-{materiality}-{recommended_flag}-{rationale[:20]}",
        received_at=received_at or datetime.now(timezone.utc),
        parsed_tickers=json.dumps(parsed_tickers or []),
        event_keywords=json.dumps(event_keywords or []),
        sentiment="negative",
        source_trust="high",
        evidence_excerpt="Fed signaled rate cut path delay",
        raw_text="<<<RAW TEXT — must never reach LLM prompt>>>",
        materiality=materiality,
        recommended_flag=recommended_flag,
        rationale=rationale,
        analyzed_at=datetime.now(timezone.utc) if analyzed else None,
    )
    session.add(sig)
    session.commit()
    return sig.id


class TestFiring:
    def test_no_signals_no_flags(self, db_session):
        """Empty news_signals → no flags fired."""
        result = check_macro_shift(db_session, "ariel")
        assert isinstance(result, MacroShiftCheckResult)
        assert result.flags_fired == []
        assert result.signals_evaluated == 0

    def test_high_materiality_macro_shift_fires(self, db_session):
        """A signal with materiality=high + recommended_flag=macro_shift
        fires a macro_shift monitor flag."""
        sig_id = _seed_signal(
            db_session,
            event_keywords=["rate", "Fed"],
            parsed_tickers=["NVDA"],
        )
        result = check_macro_shift(db_session, "ariel")
        assert len(result.flags_fired) == 1
        assert result.flags_fired[0].news_signal_id == sig_id
        # Default severity for non-high-impact-keyword: warning
        assert result.flags_fired[0].severity == "warning"

        # Persisted to monitor_flags
        rows = db_session.query(MonitorFlag).filter_by(
            user_id="ariel", kind="macro_shift",
        ).all()
        assert len(rows) == 1
        payload = json.loads(rows[0].payload)
        assert payload["news_signal_id"] == sig_id
        assert payload["parsed_tickers"] == ["NVDA"]

    def test_high_impact_keyword_escalates_to_critical(self, db_session):
        """Event keywords like 'war', 'sanction', 'Taiwan' escalate
        severity to critical."""
        _seed_signal(
            db_session,
            event_keywords=["war", "geopolitical"],
            parsed_tickers=["NVDA"],
        )
        result = check_macro_shift(db_session, "ariel")
        assert len(result.flags_fired) == 1
        assert result.flags_fired[0].severity == "critical"

    def test_low_materiality_does_not_fire(self, db_session):
        """Stage 2 classified as materiality=medium → ignored by this trigger."""
        _seed_signal(db_session, materiality="medium")
        result = check_macro_shift(db_session, "ariel")
        assert result.flags_fired == []
        # Still counted as evaluated? No — query filters on materiality=high.
        assert result.signals_evaluated == 0

    def test_no_recommended_flag_does_not_fire(self, db_session):
        """Stage 2 recommended_flag=None (analyst decided not to flag) → no fire."""
        _seed_signal(db_session, recommended_flag=None)
        result = check_macro_shift(db_session, "ariel")
        assert result.flags_fired == []

    def test_not_yet_analyzed_does_not_fire(self, db_session):
        """analyzed_at IS NULL → ignored (Stage 2 hasn't classified yet)."""
        _seed_signal(db_session, analyzed=False)
        result = check_macro_shift(db_session, "ariel")
        assert result.flags_fired == []


class TestIdempotency:
    def test_same_signal_fires_at_most_once(self, db_session):
        """Two consecutive check_macro_shift calls don't re-fire the same signal."""
        _seed_signal(
            db_session,
            event_keywords=["rate"],
            parsed_tickers=["NVDA"],
        )
        r1 = check_macro_shift(db_session, "ariel")
        r2 = check_macro_shift(db_session, "ariel")
        assert len(r1.flags_fired) == 1
        assert r2.flags_fired == []
        # DB only has one row
        rows = db_session.query(MonitorFlag).filter_by(
            user_id="ariel", kind="macro_shift",
        ).all()
        assert len(rows) == 1

    def test_acknowledged_signal_does_not_re_fire(self, db_session):
        """Even after acknowledge, a signal that already produced a
        macro_shift row isn't re-flagged."""
        sig_id = _seed_signal(
            db_session,
            event_keywords=["rate"],
            parsed_tickers=["NVDA"],
        )
        check_macro_shift(db_session, "ariel")
        # Acknowledge
        row = db_session.query(MonitorFlag).filter_by(
            user_id="ariel", kind="macro_shift",
        ).one()
        row.acknowledged_at = datetime.now(timezone.utc)
        db_session.commit()
        # Re-run — shouldn't fire again for the same news_signal_id
        r2 = check_macro_shift(db_session, "ariel")
        assert r2.flags_fired == []


class TestActiveFilter:
    def test_get_active_returns_unacknowledged_unexpired(self, db_session):
        _seed_signal(
            db_session,
            event_keywords=["sanction"],
            parsed_tickers=["NVDA"],
        )
        check_macro_shift(db_session, "ariel")
        active = get_active_macro_shift_flags(db_session, "ariel")
        assert len(active) == 1
        assert isinstance(active[0], MacroShiftFlag)
        assert active[0].severity == "critical"

    def test_get_active_excludes_acknowledged(self, db_session):
        _seed_signal(db_session, event_keywords=["rate"])
        check_macro_shift(db_session, "ariel")
        row = db_session.query(MonitorFlag).filter_by(
            user_id="ariel", kind="macro_shift",
        ).one()
        row.acknowledged_at = datetime.now(timezone.utc)
        db_session.commit()
        active = get_active_macro_shift_flags(db_session, "ariel")
        assert active == []


class TestLookbackWindow:
    def test_old_signal_outside_lookback_ignored(self, db_session):
        """A signal received 30 days ago (outside the 7d default lookback)
        is not flagged even if it would otherwise qualify."""
        old = datetime.now(timezone.utc) - timedelta(days=30)
        _seed_signal(
            db_session,
            received_at=old,
            event_keywords=["rate"],
        )
        result = check_macro_shift(db_session, "ariel")
        assert result.flags_fired == []
