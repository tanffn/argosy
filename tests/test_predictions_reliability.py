"""Spec C commit #5 — source_reliability view + reliability service tests.

Covers:

* SQL view shape — column counts, hit-rate denominator (excludes
  unparseable), abstain_rate counts neutral direction.
* Method-family dedup — when two ``is_active=1`` method versions both
  scored the same prediction, the view picks ONE outcome per
  ``(prediction_id, family)`` via ROW_NUMBER (codex BLOCKER 1 fix in
  spec §3.4).
* Rolling 30d window — outcomes evaluated >30d ago are excluded from
  ``rolling_30d_hit_rate``; recent outcomes count.
* Sample-size warning fires when scored < 10.
* Multi-tenant correctness — predictions for user A never leak into
  user B's reliability view.
* ``get_source_reliability`` — basic accessor + per-source filter +
  cache hit/miss + ``invalidate_reliability_cache`` busts the cache.
* ``get_weight_for_source`` — default 1.0 for unknown source;
  attenuation floor of 0.10 prevents feedback-loop collapse
  (spec §6.6); cap of 1.50; participation_penalty multiplies in.

Pattern (mirrors ``tests/test_predictions_evaluator.py``): per-test
in-memory SQLite at alembic head; raw INSERT seeds via SQLAlchemy core
so the view query reads exactly what the migration set up.

Run:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_predictions_reliability.py -v
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from argosy.services.predictions.reliability import (
    CACHE_TTL_SECONDS,
    FULL_SAMPLE_SIZE,
    MIN_SAMPLE_SIZE,
    WEIGHT_CEIL,
    WEIGHT_FLOOR,
    SourceReliability,
    _cache_key,
    get_source_reliability,
    get_weight_for_source,
    invalidate_reliability_cache,
)
from argosy.services.predictions import reliability as reliability_mod
from argosy.state.models import Prediction, PredictionOutcome


# ---------------------------------------------------------------------------
# Fixtures — fresh DB per test; FKs enforced.
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path, monkeypatch) -> "tuple[Session, sessionmaker]":
    """Per-test SQLite at alembic head; yields (Session, factory).

    Identical idiom to ``tests/test_predictions_evaluator.py`` so the
    view's underlying tables (predictions + prediction_outcomes +
    evaluation_method_registry) are present and the FK chain is wired
    via the same migration set.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES ('ariel', 'free', '2026-01-01 00:00:00+00:00')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES ('noga', 'free', '2026-01-01 00:00:00+00:00')"
            )
        )

    session = factory()
    # Clear the module-level cache at the start of every test so cache
    # hits from a prior test never leak in.
    invalidate_reliability_cache()
    try:
        yield session, factory
    finally:
        session.close()
        engine.dispose()
        invalidate_reliability_cache()


_INSERT_COUNTER = [0]


def _insert_prediction(
    session: Session,
    *,
    user_id: str = "ariel",
    source: str = "discord",
    ticker: str | None = "NVDA",
    direction: str = "long",
    entry_price: float | None = 100.0,
    target_price: float | None = None,
    stop_price: float | None = None,
    timeframe_days: int | None = 7,
    event_at: datetime = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
    evaluation_due_at: datetime | None = None,
    evaluation_method: str = "fixed_lookahead_7d",
    archived: int = 0,
) -> Prediction:
    """Seed a predictions row + return the ORM object."""
    if evaluation_due_at is None:
        evaluation_due_at = event_at + timedelta(days=timeframe_days or 7)
    _INSERT_COUNTER[0] += 1
    row = Prediction(
        user_id=user_id,
        source=source,
        source_ref="{}",
        ticker=ticker,
        direction=direction,
        entry_price=(
            Decimal(str(entry_price)) if entry_price is not None else None
        ),
        target_price=(
            Decimal(str(target_price)) if target_price is not None else None
        ),
        stop_price=(
            Decimal(str(stop_price)) if stop_price is not None else None
        ),
        timeframe_days=timeframe_days,
        message_id=f"v1|predictions|{source}|{_INSERT_COUNTER[0]}",
        event_at=event_at,
        evaluation_due_at=evaluation_due_at,
        evaluation_method=evaluation_method,
        archived=archived,
    )
    session.add(row)
    session.flush()
    return row


def _insert_outcome(
    session: Session,
    prediction: Prediction,
    *,
    outcome_kind: str,
    pnl_pct: float | None,
    evaluation_method: str | None = None,
    evaluated_at: datetime | None = None,
) -> PredictionOutcome:
    """Seed an outcome row tied to the given prediction."""
    method = evaluation_method or prediction.evaluation_method
    row = PredictionOutcome(
        prediction_id=prediction.id,
        evaluation_method=method,
        outcome_kind=outcome_kind,
        pnl_pct=Decimal(str(pnl_pct)) if pnl_pct is not None else None,
        evaluated_at=evaluated_at or datetime.now(timezone.utc),
        entry_price_used=Decimal("100.0"),
        exit_price_used=Decimal("100.0"),
        exit_trigger_date=date(2026, 5, 8),
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# View shape + correctness
# ---------------------------------------------------------------------------


def test_view_counts_basic(sync_session) -> None:
    """6 hit_target + 4 hit_stop → hit_rate = 6/10 = 0.6; scored=10."""
    session, _ = sync_session
    for _ in range(6):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.15)
    for _ in range(4):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_stop", pnl_pct=-0.10)
    session.flush()

    rows = get_source_reliability(session, "ariel", source="discord")
    assert len(rows) == 1
    r = rows[0]
    assert r.total_predictions == 10
    assert r.scored_predictions == 10
    assert r.hit_target_count == 6
    assert r.hit_stop_count == 4
    assert r.unparseable_count == 0
    assert r.hit_rate == pytest.approx(0.6, abs=1e-9)


def test_hit_rate_excludes_unparseable(sync_session) -> None:
    """6 hit_target + 4 unparseable → hit_rate = 6/6 = 1.0; coverage shows
    the 4 unparseable rows so the user can see the source's coverage
    is only 60%."""
    session, _ = sync_session
    for _ in range(6):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    for _ in range(4):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="unparseable", pnl_pct=None)
    session.flush()

    rows = get_source_reliability(session, "ariel", source="discord")
    r = rows[0]
    assert r.total_predictions == 10
    assert r.scored_predictions == 6
    assert r.unparseable_count == 4
    assert r.hit_rate == pytest.approx(1.0, abs=1e-9)


def test_abstain_rate_counts_neutral_predictions(sync_session) -> None:
    """3 neutral (HOLD) + 7 long predictions, all scored → abstain_rate=0.3.

    Codex BLOCKER 3 (spec §2.4) — HOLD verdicts are written as
    ``direction='neutral'`` so the agent can be measured for hiding
    behind HOLD. The view's abstain_rate exposes this; consumers apply
    participation_penalty = 1 - abstain_rate.
    """
    session, _ = sync_session
    for _ in range(3):
        p = _insert_prediction(
            session,
            source="internal_per_position_thesis",
            direction="neutral",
        )
        _insert_outcome(
            session, p, outcome_kind="expired_neutral", pnl_pct=0.001
        )
    for _ in range(7):
        p = _insert_prediction(
            session,
            source="internal_per_position_thesis",
            direction="long",
        )
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    rows = get_source_reliability(
        session, "ariel", source="internal_per_position_thesis"
    )
    r = rows[0]
    assert r.total_predictions == 10
    assert r.abstain_rate == pytest.approx(0.3, abs=1e-9)
    assert r.participation_penalty == pytest.approx(0.7, abs=1e-9)


def test_method_family_dedup_picks_one_outcome(sync_session) -> None:
    """Codex BLOCKER 1 (spec §3.4) — when two active method versions
    both score the same prediction, the view picks ONE per
    ``(prediction_id, family)`` so sample_size doesn't double-count.

    Setup: insert a SECOND method version
    ``fixed_lookahead_7d_v2`` (family=fixed_lookahead, version=2,
    is_active=1). Score one prediction under v1 (hit_stop, -0.10) AND
    under v2 (hit_target, +0.15). The view should report
    total_predictions=1 (NOT 2) and hit_rate=1.0 (v2 wins on
    method_version DESC).
    """
    session, _ = sync_session

    # Register the new method version.
    session.execute(
        sa.text(
            "INSERT INTO evaluation_method_registry "
            "(method_name, family, method_version, is_active) "
            "VALUES ('fixed_lookahead_7d_v2', 'fixed_lookahead', 2, 1)"
        )
    )
    session.flush()

    p = _insert_prediction(session, evaluation_method="fixed_lookahead_7d")
    # v1 outcome: hit_stop
    _insert_outcome(
        session,
        p,
        outcome_kind="hit_stop",
        pnl_pct=-0.10,
        evaluation_method="fixed_lookahead_7d",
        evaluated_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    # v2 outcome: hit_target (newer version wins)
    _insert_outcome(
        session,
        p,
        outcome_kind="hit_target",
        pnl_pct=0.15,
        evaluation_method="fixed_lookahead_7d_v2",
        evaluated_at=datetime.now(timezone.utc),
    )
    session.flush()

    rows = get_source_reliability(session, "ariel")
    # Exactly ONE row for (ariel, discord, fixed_lookahead) — dedup'd.
    fl_rows = [r for r in rows if r.method_family == "fixed_lookahead"]
    assert len(fl_rows) == 1
    r = fl_rows[0]
    assert r.total_predictions == 1, "single prediction counted once"
    assert r.scored_predictions == 1
    assert r.hit_target_count == 1, "v2 (newest version) wins"
    assert r.hit_stop_count == 0
    assert r.hit_rate == pytest.approx(1.0, abs=1e-9)


def test_rolling_30d_excludes_old_evaluated_at(sync_session) -> None:
    """rolling_30d_hit_rate uses evaluated_at; outcomes evaluated >30d
    ago are excluded. Spec §5.5 — decision latency requires the
    rolling window key off evaluation time, not event_at, so a
    backfill of stale predictions appears immediately."""
    session, _ = sync_session
    now = datetime.now(timezone.utc)

    # 4 hits evaluated TODAY — counted in 30d window.
    for _ in range(4):
        p = _insert_prediction(session)
        _insert_outcome(
            session,
            p,
            outcome_kind="hit_target",
            pnl_pct=0.15,
            evaluated_at=now,
        )

    # 6 misses evaluated 60 DAYS AGO — excluded from 30d window.
    for _ in range(6):
        p = _insert_prediction(session)
        _insert_outcome(
            session,
            p,
            outcome_kind="hit_stop",
            pnl_pct=-0.10,
            evaluated_at=now - timedelta(days=60),
        )
    session.flush()

    rows = get_source_reliability(session, "ariel", source="discord")
    r = rows[0]
    # All-time hit_rate = 4/10 = 0.4
    assert r.hit_rate == pytest.approx(0.4, abs=1e-9)
    # Rolling 30d hit_rate = 4/4 = 1.0 (only recent hits visible)
    assert r.rolling_30d_hit_rate == pytest.approx(1.0, abs=1e-9)


def test_rolling_30d_excludes_future_dated_evaluated_at(sync_session) -> None:
    """Codex review 2026-05-29 IMPORTANT #2 — a future-dated
    evaluated_at (clock skew on a writer, or a bug in a backfill that
    stamps evaluated_at past now) must NOT inflate the rolling 30d
    metrics.

    The view bounds the rolling window BOTH sides
    (``evaluated_at >= now-30d AND evaluated_at <= now``); without the
    upper bound a future-dated row would be visible as "recent".

    Setup: 4 hits TODAY (counted), 2 hits dated 10 DAYS IN THE
    FUTURE (must be excluded from rolling 30d). All counted in
    all-time metrics.
    """
    session, _ = sync_session
    now = datetime.now(timezone.utc)

    # 4 hits today.
    for _ in range(4):
        p = _insert_prediction(session)
        _insert_outcome(
            session,
            p,
            outcome_kind="hit_target",
            pnl_pct=0.15,
            evaluated_at=now,
        )

    # 2 misses dated 10 days in the FUTURE — must not enter the
    # rolling 30d window.
    for _ in range(2):
        p = _insert_prediction(session)
        _insert_outcome(
            session,
            p,
            outcome_kind="hit_stop",
            pnl_pct=-0.10,
            evaluated_at=now + timedelta(days=10),
        )
    session.flush()

    rows = get_source_reliability(session, "ariel", source="discord")
    r = rows[0]
    # All-time: 4 hits + 2 misses = 6 scored; hit_rate = 4/6 ≈ 0.667.
    assert r.scored_predictions == 6
    assert r.hit_rate == pytest.approx(4.0 / 6.0, abs=1e-9)
    # Rolling 30d: future rows excluded → only 4 hits visible → 4/4 = 1.0.
    assert r.rolling_30d_hit_rate == pytest.approx(1.0, abs=1e-9)


def test_sample_size_warning_fires_under_10(sync_session) -> None:
    """sample_size_warning = 1 when scored < 10; else 0."""
    session, _ = sync_session
    for _ in range(5):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    rows = get_source_reliability(session, "ariel", source="discord")
    assert rows[0].sample_size_warning == 1

    # Pump the count past 10 → flag flips off.
    for _ in range(6):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()
    invalidate_reliability_cache()

    rows2 = get_source_reliability(session, "ariel", source="discord")
    assert rows2[0].sample_size_warning == 0
    assert rows2[0].scored_predictions == 11


def test_multi_tenant_isolation(sync_session) -> None:
    """ariel's predictions never leak into noga's reliability view."""
    session, _ = sync_session
    # ariel: 5 hits
    for _ in range(5):
        p = _insert_prediction(session, user_id="ariel")
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    # noga: 3 misses
    for _ in range(3):
        p = _insert_prediction(session, user_id="noga")
        _insert_outcome(session, p, outcome_kind="hit_stop", pnl_pct=-0.10)
    session.flush()

    ariel = get_source_reliability(session, "ariel")
    noga = get_source_reliability(session, "noga")

    assert sum(r.total_predictions for r in ariel) == 5
    assert sum(r.total_predictions for r in noga) == 3
    # Mutual exclusion of tenants.
    assert all(r.user_id == "ariel" for r in ariel)
    assert all(r.user_id == "noga" for r in noga)


def test_archived_predictions_excluded(sync_session) -> None:
    """archived=1 predictions are excluded by the view. The retention
    job (spec §9.1) flips this flag at 2-year-old + 90d-inactive rows
    so they fall out of the live reliability calc but stay queryable
    via the archive view."""
    session, _ = sync_session
    # 5 fresh hits (counted).
    for _ in range(5):
        p = _insert_prediction(session, archived=0)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    # 5 archived hits (NOT counted).
    for _ in range(5):
        p = _insert_prediction(session, archived=1)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    rows = get_source_reliability(session, "ariel", source="discord")
    r = rows[0]
    assert r.total_predictions == 5


def test_median_pnl_computed_in_python(sync_session) -> None:
    """SQLite has no MEDIAN aggregate; ``reliability.py`` computes
    it client-side. Pin the contract: pnl values [0.05, 0.10, 0.15,
    0.20, 0.25] → median = 0.15."""
    session, _ = sync_session
    for pnl in (0.05, 0.10, 0.15, 0.20, 0.25):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=pnl)
    session.flush()

    rows = get_source_reliability(session, "ariel", source="discord")
    assert rows[0].median_pnl_pct == pytest.approx(0.15, abs=1e-9)
    assert rows[0].mean_pnl_pct == pytest.approx(0.15, abs=1e-9)


# ---------------------------------------------------------------------------
# Cache + invalidation
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_db_query(sync_session, monkeypatch) -> None:
    """A cache hit returns the prior list WITHOUT touching the DB.

    Approach: warm the cache, then monkey-patch
    ``Session.execute`` to raise; the second call must STILL return
    the cached list.
    """
    session, _ = sync_session
    for _ in range(3):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    first = get_source_reliability(session, "ariel", source="discord")
    assert len(first) == 1

    # Cache key should be present.
    key = _cache_key("ariel", "discord", None)
    assert key in reliability_mod._CACHE

    # If we patch execute to blow up, the cached result still returns
    # — proving the DB wasn't hit.
    original_execute = session.execute

    def _exploding_execute(*_a, **_kw):
        raise AssertionError("cache MISS — DB should not have been queried")

    monkeypatch.setattr(session, "execute", _exploding_execute)
    second = get_source_reliability(session, "ariel", source="discord")
    assert second == first
    # Restore so the fixture teardown doesn't trip on the bomb.
    monkeypatch.setattr(session, "execute", original_execute)


def test_invalidate_busts_cache(sync_session) -> None:
    """``invalidate_reliability_cache`` empties the cache; the next
    call repopulates from the DB."""
    session, _ = sync_session
    p = _insert_prediction(session)
    _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    get_source_reliability(session, "ariel", source="discord")
    assert len(reliability_mod._CACHE) > 0

    invalidate_reliability_cache()
    assert len(reliability_mod._CACHE) == 0


def test_cache_ttl_constant_is_5_minutes() -> None:
    """Spec §4.2 — 5-minute TTL pinned in code. Tests would silently
    break if a future edit changed this number; pin via assertion."""
    assert CACHE_TTL_SECONDS == 300.0


# ---------------------------------------------------------------------------
# get_weight_for_source
# ---------------------------------------------------------------------------


def test_weight_unknown_source_returns_default(sync_session) -> None:
    """No predictions yet for (ariel, discord, fixed_lookahead) →
    default 1.0 weight."""
    session, _ = sync_session
    weight = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead"
    )
    assert weight == 1.0


def test_weight_small_sample_returns_dimmed_factor(sync_session) -> None:
    """3 perfect hits → scored=3 < MIN_SAMPLE_SIZE=10 → sample_size_factor
    = 0.5 → weight = 1.0 * 1.0 * 0.5 = 0.5 (not 1.0 — even a perfect
    small sample is dimmed). Clipped to [0.10, 1.50]."""
    session, _ = sync_session
    for _ in range(3):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    weight = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead"
    )
    assert weight == pytest.approx(0.5, abs=1e-9)


def test_weight_full_sample_perfect_hit_rate(sync_session) -> None:
    """50 perfect hits, no neutrals → hit_rate=1.0, penalty=1.0,
    sample_size_factor=1.0 → weight = 1.0 (capped at 1.50 but doesn't
    reach the cap because hit_rate caps at 1.0)."""
    session, _ = sync_session
    for _ in range(FULL_SAMPLE_SIZE):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    weight = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead"
    )
    assert weight == pytest.approx(1.0, abs=1e-9)


def test_weight_floor_prevents_collapse(sync_session) -> None:
    """20 misses + 80 HOLDs → hit_rate≈0, penalty=0.2 (80/100 abstain),
    sample_size_factor would be 1.0 but raw weight = 0 * 0.2 * 1.0 = 0
    → clipped UP to WEIGHT_FLOOR=0.10.

    This is the codex IMPORTANT 3 / spec §6.6 anti-feedback-loop
    contract — a degenerate source never collapses to zero weight;
    some signal always flows so the source can recover within the
    30d rolling window.
    """
    session, _ = sync_session
    # 20 long predictions, all misses.
    for _ in range(20):
        p = _insert_prediction(
            session,
            source="internal_per_position_thesis",
            direction="long",
        )
        _insert_outcome(session, p, outcome_kind="hit_stop", pnl_pct=-0.10)
    # 80 HOLD predictions, all expired_neutral.
    for _ in range(80):
        p = _insert_prediction(
            session,
            source="internal_per_position_thesis",
            direction="neutral",
            evaluation_method="fixed_lookahead_30d",
        )
        _insert_outcome(
            session,
            p,
            outcome_kind="expired_neutral",
            pnl_pct=0.001,
            evaluation_method="fixed_lookahead_30d",
        )
    session.flush()

    weight = get_weight_for_source(
        session,
        "ariel",
        "internal_per_position_thesis",
        "fixed_lookahead",
    )
    # raw = 0 * (1 - 0.8) * 1.0 = 0; floor applies.
    assert weight == pytest.approx(WEIGHT_FLOOR, abs=1e-9)


def test_weight_participation_penalty_multiplies_in(sync_session) -> None:
    """30 long hits + 30 HOLD neutral → abstain=0.5, penalty=0.5,
    hit_rate=30/60=0.5, sample_size_factor=1.0 (>=50 scored) →
    raw weight = 0.5 * 0.5 * 1.0 = 0.25."""
    session, _ = sync_session
    for _ in range(30):
        p = _insert_prediction(
            session,
            source="internal_per_position_thesis",
            direction="long",
        )
        _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    for _ in range(30):
        p = _insert_prediction(
            session,
            source="internal_per_position_thesis",
            direction="neutral",
            evaluation_method="fixed_lookahead_30d",
        )
        _insert_outcome(
            session,
            p,
            outcome_kind="expired_neutral",
            pnl_pct=0.001,
            evaluation_method="fixed_lookahead_30d",
        )
    session.flush()

    weight = get_weight_for_source(
        session,
        "ariel",
        "internal_per_position_thesis",
        "fixed_lookahead",
    )
    # 30 hit_target (long) + 30 expired_neutral (HOLD) = 60 scored.
    # hit_rate = 30 / 60 = 0.5. abstain = 30 / 60 = 0.5. penalty = 0.5.
    # sample_size_factor (60 >= 50) = 1.0.
    # raw = 0.5 * 0.5 * 1.0 = 0.25 → within [0.10, 1.50].
    assert weight == pytest.approx(0.25, abs=1e-9)


def test_weight_returns_default_for_only_unparseable(sync_session) -> None:
    """A source with only unparseable outcomes → scored_predictions=0
    → defaults to 1.0 weight (can't measure reliability)."""
    session, _ = sync_session
    for _ in range(10):
        p = _insert_prediction(session)
        _insert_outcome(session, p, outcome_kind="unparseable", pnl_pct=None)
    session.flush()

    weight = get_weight_for_source(
        session, "ariel", "discord", "fixed_lookahead"
    )
    assert weight == 1.0


def test_weight_bounds_constants() -> None:
    """Pin the floor/ceiling so future edits surface as test failure."""
    assert WEIGHT_FLOOR == 0.10
    assert WEIGHT_CEIL == 1.50
    assert MIN_SAMPLE_SIZE == 10
    assert FULL_SAMPLE_SIZE == 50


# ---------------------------------------------------------------------------
# Filter behaviour
# ---------------------------------------------------------------------------


def test_filter_by_source_returns_only_matching(sync_session) -> None:
    """Two sources, filter to one → only that source's row returned."""
    session, _ = sync_session
    p1 = _insert_prediction(session, source="discord")
    _insert_outcome(session, p1, outcome_kind="hit_target", pnl_pct=0.12)
    p2 = _insert_prediction(session, source="news")
    _insert_outcome(session, p2, outcome_kind="hit_stop", pnl_pct=-0.10)
    session.flush()

    only_discord = get_source_reliability(
        session, "ariel", source="discord"
    )
    assert len(only_discord) == 1
    assert only_discord[0].source == "discord"

    all_sources = get_source_reliability(session, "ariel")
    sources_returned = {r.source for r in all_sources}
    assert sources_returned == {"discord", "news"}


def test_returns_sourcereliability_instances(sync_session) -> None:
    """Verify the accessor returns the public dataclass, not raw Row
    objects (consumers rely on attribute access + immutability)."""
    session, _ = sync_session
    p = _insert_prediction(session)
    _insert_outcome(session, p, outcome_kind="hit_target", pnl_pct=0.12)
    session.flush()

    rows = get_source_reliability(session, "ariel")
    assert all(isinstance(r, SourceReliability) for r in rows)
    # Frozen dataclass — direct mutation should raise.
    with pytest.raises((AttributeError, TypeError)):
        rows[0].hit_rate = 0.0  # type: ignore[misc]
