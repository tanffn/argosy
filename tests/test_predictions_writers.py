"""Tests for ``argosy/services/predictions/writers.py`` (Spec C commit #3).

Coverage per writer:

  * Each writer inserts cleanly with the right column values.
  * Per-source idempotency — re-running with the same source-stable id
    returns the existing row, no duplicate INSERT, no exception.
  * source enum on the inserted row matches the expected value.
  * ``evaluation_due_at`` correctly capped at min(event_at +
    timeframe_days, event_at + 30d) per spec §5.5.
  * ``evaluation_method`` selected per spec §3.1.
  * per_position_thesis HOLD → ``direction='neutral'``, NOT excluded
    (codex BLOCKER #3 anti-hide-behind-HOLD).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_predictions_writers.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.predictions.parsers import extract_alpha_call_from_text
from argosy.services.predictions.writers import (
    DEFAULT_TIMEFRAME_DAYS_DISCORD,
    DEFAULT_TIMEFRAME_DAYS_MONITOR,
    DEFAULT_TIMEFRAME_DAYS_NEWS_HIGH,
    DEFAULT_TIMEFRAME_DAYS_NEWS_MEDIUM,
    DEFAULT_TIMEFRAME_DAYS_OBSERVER,
    DEFAULT_TIMEFRAME_DAYS_THESIS,
    LONG_HORIZON_CAP_DAYS,
    write_discord_prediction,
    write_monitor_flag_prediction,
    write_news_signal_prediction,
    write_per_position_thesis_prediction,
    write_state_observer_prediction,
)
from argosy.state.models import Base, Prediction, User


USER = "ariel"
EVENT_AT = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """File-backed SQLite session with the predictions schema in place.

    ``Base.metadata.create_all`` installs all ORM-declared tables
    including ``predictions`` + ``prediction_outcomes`` +
    ``evaluation_method_registry``. The partial-unique index
    ``ix_predictions_source_messageid`` from migration 0050 is NOT
    auto-created by ``create_all`` (it's a partial-index with a WHERE
    clause that the ORM model doesn't declare); we install it manually
    here so the writers' idempotency contract exercises the same DB
    constraint it would in production.

    We also seed ``evaluation_method_registry`` with the five v1 methods
    so the FK on ``predictions.evaluation_method`` accepts the writer's
    chosen method name (the registry seed normally ships in migration
    0051).
    """
    db_path = tmp_path / "predictions_writers.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    # Enable FK enforcement per-connection — SQLite defaults to OFF.
    @sa.event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover — connect hook
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        # Mirror migration 0050's partial-unique index — Base.metadata
        # only creates the unique constraint without the partial WHERE,
        # so we drop+recreate to match production exactly.
        try:
            conn.execute(sa.text("DROP INDEX IF EXISTS ix_predictions_source_messageid"))
        except Exception:  # pragma: no cover — defensive
            pass
        conn.execute(sa.text(
            "CREATE UNIQUE INDEX ix_predictions_source_messageid "
            "ON predictions (source, message_id) "
            "WHERE message_id IS NOT NULL"
        ))

        # Seed evaluation_method_registry — five v1 methods per spec §5.
        for method_name, family in (
            ("target_stop", "target_stop"),
            ("fixed_lookahead_7d", "fixed_lookahead"),
            ("fixed_lookahead_30d", "fixed_lookahead"),
            ("multi_basket_weighted", "multi_basket"),
            ("unparseable", "unparseable"),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO evaluation_method_registry "
                    "(method_name, family, method_version, is_active) "
                    "VALUES (:m, :f, 1, 1)"
                ),
                {"m": method_name, "f": family},
            )

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _due_at_aware(row: Prediction) -> datetime:
    """Normalise ``evaluation_due_at`` to tz-aware UTC for arithmetic
    comparison in assertions (SQLite roundtrip strips tzinfo)."""
    dt = row.evaluation_due_at
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# write_discord_prediction
# ---------------------------------------------------------------------------


def test_write_discord_prediction_inserts_clean_row(sync_session):
    """Writer inserts a Prediction with the expected column values."""
    row = write_discord_prediction(
        sync_session,
        USER,
        message_id="1234567890",
        channel_id=999,
        ticker="NVDA",
        direction="long",
        entry_price=Decimal("145.00"),
        target_price=Decimal("180.00"),
        stop_price=Decimal("135.00"),
        timeframe_days=7,
        event_at=EVENT_AT,
        raw_text_ref="news_signals.id:42",
    )
    sync_session.commit()

    assert row.id is not None
    assert row.source == "discord"
    assert row.ticker == "NVDA"
    assert row.direction == "long"
    assert row.entry_price == Decimal("145.00")
    assert row.target_price == Decimal("180.00")
    assert row.stop_price == Decimal("135.00")
    assert row.timeframe_days == 7
    assert row.raw_text_ref == "news_signals.id:42"
    # target_stop chosen because both target + stop set.
    assert row.evaluation_method == "target_stop"
    # window = timeframe_days = 7.
    assert _due_at_aware(row) == EVENT_AT + timedelta(days=7)
    # message_id is the deterministic dedup key.
    assert row.message_id == "v1|predictions|discord|999.1234567890"


def test_write_discord_prediction_idempotent(sync_session):
    """Re-running with the same message_id returns the existing row."""
    first = write_discord_prediction(
        sync_session,
        USER,
        message_id="msg-abc",
        channel_id=42,
        ticker="AMD",
        direction="long",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    first_id = first.id

    second = write_discord_prediction(
        sync_session,
        USER,
        message_id="msg-abc",
        channel_id=42,
        ticker="AMD",
        direction="long",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert second.id == first_id
    # Exactly one row in DB.
    count = sync_session.scalar(
        sa.select(sa.func.count(Prediction.id)).where(Prediction.source == "discord")
    )
    assert count == 1


def test_write_discord_prediction_no_target_uses_fixed_lookahead_7d(sync_session):
    """No target_price → fixed_lookahead_7d, window=7."""
    row = write_discord_prediction(
        sync_session,
        USER,
        message_id="x",
        ticker="NVDA",
        direction="long",
        event_at=EVENT_AT,
        timeframe_days=7,
    )
    sync_session.commit()
    assert row.evaluation_method == "fixed_lookahead_7d"
    assert _due_at_aware(row) == EVENT_AT + timedelta(days=7)


def test_write_discord_prediction_dedup_key_stable_when_channel_id_missing(sync_session):
    """Codex BLOCKER 1 fix — channel_id=None produces the SAME message_id
    shape (``0.<message_id>``) as channel_id=0 / channel_id="0", so the
    same logical Discord event never produces duplicate ledger rows
    across heterogeneous re-ingest paths.
    """
    a = write_discord_prediction(
        sync_session,
        USER,
        message_id="same-msg",
        channel_id=None,  # backfill path may not carry channel_id
        ticker="X",
        direction="long",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert a.message_id == "v1|predictions|discord|0.same-msg"

    # Re-write with explicit channel_id=0 — must dedup against `a`.
    b = write_discord_prediction(
        sync_session,
        USER,
        message_id="same-msg",
        channel_id=0,
        ticker="X",
        direction="long",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert b.id == a.id


def test_write_discord_prediction_ensure_aware_normalizes_non_utc(sync_session):
    """Codex IMPORTANT 2 fix — non-UTC aware datetimes get astimezone(UTC)
    so SQLite's tzinfo-stripping roundtrip doesn't drift the
    evaluation_due_at math by the original tz offset.
    """
    from datetime import timezone as _tz
    ist = _tz(timedelta(hours=3))  # IST = UTC+3
    event_at_ist = datetime(2026, 5, 29, 15, 0, 0, tzinfo=ist)  # = 12:00 UTC
    row = write_discord_prediction(
        sync_session,
        USER,
        message_id="tz-test",
        ticker="X",
        direction="long",
        event_at=event_at_ist,
        timeframe_days=7,
    )
    sync_session.commit()
    # event_at_ist normalised to UTC = 2026-05-29 12:00:00 UTC.
    # evaluation_due_at = that + 7d = 2026-06-05 12:00:00 UTC.
    expected_due_utc = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert _due_at_aware(row) == expected_due_utc


# ---------------------------------------------------------------------------
# write_news_signal_prediction
# ---------------------------------------------------------------------------


def test_write_news_signal_prediction_high_materiality(sync_session):
    """HIGH → 14d timeframe → fixed_lookahead_30d (capped)."""
    row = write_news_signal_prediction(
        sync_session,
        USER,
        news_signal_id=423,
        ticker="NVDA",
        direction="long",
        materiality_tier="high",
        event_at=EVENT_AT,
        raw_text_ref="news_signals.id:423",
    )
    sync_session.commit()
    assert row is not None
    assert row.source == "internal_news_signal_analyst"
    assert row.timeframe_days == DEFAULT_TIMEFRAME_DAYS_NEWS_HIGH
    # 14d > 7d so falls into fixed_lookahead_30d with the 30d cap.
    assert row.evaluation_method == "fixed_lookahead_30d"
    assert _due_at_aware(row) == EVENT_AT + timedelta(days=LONG_HORIZON_CAP_DAYS)
    assert row.message_id == "v1|predictions|nsa|423.NVDA"


def test_write_news_signal_prediction_medium_materiality(sync_session):
    """MEDIUM → 30d → fixed_lookahead_30d."""
    row = write_news_signal_prediction(
        sync_session,
        USER,
        news_signal_id=99,
        ticker="amd",  # lowercase — writer must upper.
        direction="short",
        materiality_tier="medium",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert row is not None
    assert row.ticker == "AMD"
    assert row.timeframe_days == DEFAULT_TIMEFRAME_DAYS_NEWS_MEDIUM
    assert row.evaluation_method == "fixed_lookahead_30d"


def test_write_news_signal_prediction_low_materiality_skips(sync_session):
    """LOW → returns None, no row inserted (defensive gate)."""
    out = write_news_signal_prediction(
        sync_session,
        USER,
        news_signal_id=1,
        ticker="X",
        direction="neutral",
        materiality_tier="low",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert out is None
    count = sync_session.scalar(sa.select(sa.func.count(Prediction.id)))
    assert count == 0


def test_write_news_signal_prediction_idempotent_per_signal_ticker(sync_session):
    """Same (news_signal_id, ticker) → idempotent. Different ticker → new row."""
    a = write_news_signal_prediction(
        sync_session, USER,
        news_signal_id=1, ticker="NVDA", direction="long",
        materiality_tier="high", event_at=EVENT_AT,
    )
    sync_session.commit()
    b = write_news_signal_prediction(
        sync_session, USER,
        news_signal_id=1, ticker="NVDA", direction="long",
        materiality_tier="high", event_at=EVENT_AT,
    )
    sync_session.commit()
    assert a.id == b.id

    # Different ticker on the same signal → distinct prediction.
    c = write_news_signal_prediction(
        sync_session, USER,
        news_signal_id=1, ticker="AMD", direction="long",
        materiality_tier="high", event_at=EVENT_AT,
    )
    sync_session.commit()
    assert c.id != a.id


# ---------------------------------------------------------------------------
# write_per_position_thesis_prediction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected_direction",
    [
        ("BUY", "long"),
        ("ADD", "long"),
        ("TRIM", "short"),
        ("SELL", "short"),
        ("HOLD", "neutral"),  # codex BLOCKER #3 — HOLD is logged
    ],
)
def test_write_per_position_thesis_action_mapping(sync_session, action, expected_direction):
    """Each verdict maps to the expected direction; HOLD logs as neutral."""
    row = write_per_position_thesis_prediction(
        sync_session,
        USER,
        thesis_id=7,
        ticker="NVDA",
        action=action,
        conviction="HIGH",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert row.source == "internal_per_position_thesis"
    assert row.direction == expected_direction
    assert row.ticker == "NVDA"
    assert row.timeframe_days == DEFAULT_TIMEFRAME_DAYS_THESIS
    # 30d timeframe → fixed_lookahead_30d.
    assert row.evaluation_method == "fixed_lookahead_30d"
    assert _due_at_aware(row) == EVENT_AT + timedelta(days=30)


def test_write_per_position_thesis_hold_not_excluded(sync_session):
    """Codex BLOCKER #3 — HOLD verdicts MUST be inserted, not excluded."""
    row = write_per_position_thesis_prediction(
        sync_session,
        USER,
        thesis_id="draft-1",
        ticker="SCHG",
        action="HOLD",
        conviction="MEDIUM",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert row.id is not None
    assert row.direction == "neutral"
    # Round-trip via DB to confirm it landed.
    fetched = sync_session.execute(
        sa.select(Prediction).where(Prediction.id == row.id)
    ).scalar_one()
    assert fetched.direction == "neutral"
    assert fetched.source == "internal_per_position_thesis"


def test_write_per_position_thesis_idempotent(sync_session):
    """Same (thesis_id, ticker) → same row on re-run."""
    a = write_per_position_thesis_prediction(
        sync_session, USER,
        thesis_id=12, ticker="NVDA", action="TRIM", conviction="HIGH",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    b = write_per_position_thesis_prediction(
        sync_session, USER,
        thesis_id=12, ticker="NVDA", action="TRIM", conviction="HIGH",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert a.id == b.id


def test_write_per_position_thesis_unknown_action_raises(sync_session):
    """Unrecognised action raises ValueError (no silent neutral-fallback)."""
    with pytest.raises(ValueError, match="unrecognised action"):
        write_per_position_thesis_prediction(
            sync_session, USER,
            thesis_id=1, ticker="X", action="WAIT",  # type: ignore[arg-type]
            conviction="HIGH", event_at=EVENT_AT,
        )


# ---------------------------------------------------------------------------
# write_state_observer_prediction
# ---------------------------------------------------------------------------


def test_write_state_observer_prediction_warning(sync_session):
    """severity=warning → row inserted with ticker=None + neutral."""
    row = write_state_observer_prediction(
        sync_session,
        USER,
        observer_flag_id=55,
        primary_field="macro.fx_usd_nis_spot",
        severity="warning",
        deviation_bucket="large",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert row is not None
    assert row.source == "internal_state_observer"
    assert row.ticker is None
    assert row.direction == "neutral"
    assert row.timeframe_days == DEFAULT_TIMEFRAME_DAYS_OBSERVER
    assert row.evaluation_method == "fixed_lookahead_30d"
    assert _due_at_aware(row) == EVENT_AT + timedelta(days=30)
    assert row.message_id == "v1|predictions|so|55"


def test_write_state_observer_prediction_info_skips(sync_session):
    """severity=info → returns None, no row inserted (defensive gate)."""
    out = write_state_observer_prediction(
        sync_session,
        USER,
        observer_flag_id=10,
        primary_field="macro.fx_usd_nis_spot",
        severity="info",
        deviation_bucket="small",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert out is None
    count = sync_session.scalar(sa.select(sa.func.count(Prediction.id)))
    assert count == 0


def test_write_state_observer_prediction_idempotent(sync_session):
    """Same observer_flag_id → same row on re-run."""
    a = write_state_observer_prediction(
        sync_session, USER,
        observer_flag_id="flag-uuid-abc",
        primary_field="portfolio.top_concentration_pct",
        severity="critical", deviation_bucket="extreme",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    b = write_state_observer_prediction(
        sync_session, USER,
        observer_flag_id="flag-uuid-abc",
        primary_field="portfolio.top_concentration_pct",
        severity="critical", deviation_bucket="extreme",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert a.id == b.id


# ---------------------------------------------------------------------------
# write_monitor_flag_prediction
# ---------------------------------------------------------------------------


def test_write_monitor_flag_prediction_allocation_drift(sync_session):
    """allocation_drift → ticker=None + neutral + fixed_lookahead_30d."""
    row = write_monitor_flag_prediction(
        sync_session,
        USER,
        monitor_flag_id=88,
        kind="allocation_drift",
        severity="warning",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert row.source == "internal_monitor_flags"
    assert row.ticker is None
    assert row.direction == "neutral"
    assert row.timeframe_days == DEFAULT_TIMEFRAME_DAYS_MONITOR
    assert row.evaluation_method == "fixed_lookahead_30d"
    assert _due_at_aware(row) == EVENT_AT + timedelta(days=30)
    assert row.message_id == "v1|predictions|mf|88"


def test_write_monitor_flag_prediction_idempotent(sync_session):
    """Same monitor_flag_id → same row on re-run."""
    a = write_monitor_flag_prediction(
        sync_session, USER,
        monitor_flag_id=99, kind="mc_regression", severity="critical",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    b = write_monitor_flag_prediction(
        sync_session, USER,
        monitor_flag_id=99, kind="mc_regression", severity="critical",
        event_at=EVENT_AT,
    )
    sync_session.commit()
    assert a.id == b.id


# ---------------------------------------------------------------------------
# evaluation_due_at 30d cap (spec §5.5)
# ---------------------------------------------------------------------------


def test_evaluation_due_at_capped_at_30d_for_long_timeframe(sync_session):
    """A prediction with timeframe_days=90 (e.g. 13F-style) is still due
    at 30 days — the §5.5 cap applied at write time per codex BLOCKER 2.

    Exercised via the Discord writer with an explicit 90d override since
    Discord is the only writer that surfaces a timeframe_days parameter
    today; the cap logic lives in ``_choose_method_and_window`` and is
    shared by every writer.
    """
    row = write_discord_prediction(
        sync_session,
        USER,
        message_id="long-horizon",
        ticker="AAPL",
        direction="long",
        event_at=EVENT_AT,
        timeframe_days=90,
    )
    sync_session.commit()
    assert row.evaluation_method == "fixed_lookahead_30d"
    # Capped to 30d even though timeframe_days=90.
    assert _due_at_aware(row) == EVENT_AT + timedelta(days=30)
    # The original timeframe_days IS preserved on the row for audit.
    assert row.timeframe_days == 90


# ---------------------------------------------------------------------------
# parsers.extract_alpha_call_from_text — gating helper for discord
# ---------------------------------------------------------------------------


def test_extract_alpha_call_picks_long_with_target_and_stop():
    """`BUY NVDA target 180 stop 135` parses to long + levels."""
    call = extract_alpha_call_from_text("BUY $NVDA target 180 stop 135 by Fri")
    assert call is not None
    assert call.ticker == "NVDA"
    assert call.direction == "long"
    assert call.target_price == 180.0
    assert call.stop_price == 135.0


def test_extract_alpha_call_short_with_arrow_target():
    """`SHORT AMD -> 80` parses to short with target only."""
    call = extract_alpha_call_from_text("SHORT AMD -> 80")
    assert call is not None
    assert call.ticker == "AMD"
    assert call.direction == "short"
    assert call.target_price == 80.0
    assert call.stop_price is None


def test_extract_alpha_call_returns_none_for_chatter():
    """Chatter without a direction+ticker pair returns None."""
    assert extract_alpha_call_from_text("hey what do you think") is None
    assert extract_alpha_call_from_text("") is None
    assert extract_alpha_call_from_text(None) is None  # type: ignore[arg-type]


def test_extract_alpha_call_ignores_isolated_direction_keyword():
    """`BUY` without a following ticker doesn't match."""
    assert extract_alpha_call_from_text("might BUY tomorrow") is None


# ---------------------------------------------------------------------------
# Cross-writer source-enum sanity
# ---------------------------------------------------------------------------


def test_source_enums_match_check_constraint(sync_session):
    """Each writer's source value matches the migration 0050 CHECK enum.

    A failed CHECK would surface as IntegrityError at flush; this test
    pins the writer-to-enum binding by inserting one row per writer +
    fetching it back to confirm the source survived round-trip.
    """
    write_discord_prediction(
        sync_session, USER, message_id="m1", ticker="X",
        direction="long", event_at=EVENT_AT,
    )
    write_news_signal_prediction(
        sync_session, USER, news_signal_id=1, ticker="Y",
        direction="long", materiality_tier="medium", event_at=EVENT_AT,
    )
    write_per_position_thesis_prediction(
        sync_session, USER, thesis_id=1, ticker="Z",
        action="BUY", conviction="HIGH", event_at=EVENT_AT,
    )
    write_state_observer_prediction(
        sync_session, USER, observer_flag_id=1,
        primary_field="macro.fx_usd_nis_spot", severity="warning",
        deviation_bucket="large", event_at=EVENT_AT,
    )
    write_monitor_flag_prediction(
        sync_session, USER, monitor_flag_id=1, kind="allocation_drift",
        severity="warning", event_at=EVENT_AT,
    )
    sync_session.commit()

    sources = sorted(sync_session.scalars(sa.select(Prediction.source)).all())
    assert sources == [
        "discord",
        "internal_monitor_flags",
        "internal_news_signal_analyst",
        "internal_per_position_thesis",
        "internal_state_observer",
    ]
