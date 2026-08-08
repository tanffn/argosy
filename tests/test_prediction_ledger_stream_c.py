"""Stream C review-iteration-1 — prediction ledger measurement fixes.

Covers blockers 1–9. Item 10 deferred (no ORM/admin surface yet).

Run:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_prediction_ledger_stream_c.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from argosy.services.predictions.evaluator import (
    EvaluatorAdapterError,
    EvaluatorNoProgressError,
    run_evaluator_batch,
)
from argosy.services.predictions.fleet_verdict_backfill import (
    backfill_fleet_verdict_predictions,
)
from argosy.services.predictions.reliability import (
    invalidate_reliability_cache,
    ledger_scorecard_by_source,
)
from argosy.services.predictions.writers import (
    FLEET_VERDICT_SOURCE,
    classify_price_trigger_role,
    write_fleet_verdict_prediction,
)
from argosy.state.models import Prediction, PredictionOutcome, Proposal, Verdict


@pytest.fixture
def sync_session(tmp_path, monkeypatch) -> "tuple[Session, sessionmaker]":
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

    session = factory()
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
    source: str = "discord",
    ticker: str | None = "NVDA",
    direction: str = "long",
    entry_price: float | None = 100.0,
    timeframe_days: int | None = 7,
    event_at: datetime = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
    evaluation_due_at: datetime | None = None,
    evaluation_method: str = "fixed_lookahead_7d",
    archived: int = 0,
) -> Prediction:
    if evaluation_due_at is None:
        evaluation_due_at = event_at + timedelta(days=timeframe_days or 7)
    _INSERT_COUNTER[0] += 1
    row = Prediction(
        user_id="ariel",
        source=source,
        source_ref="{}",
        ticker=ticker,
        direction=direction,
        entry_price=(
            Decimal(str(entry_price)) if entry_price is not None else None
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
) -> PredictionOutcome:
    row = PredictionOutcome(
        prediction_id=prediction.id,
        evaluation_method=prediction.evaluation_method,
        outcome_kind=outcome_kind,
        pnl_pct=Decimal(str(pnl_pct)) if pnl_pct is not None else None,
        evaluated_at=datetime.now(timezone.utc),
        entry_price_used=Decimal("100.0"),
        exit_price_used=Decimal("110.0"),
        exit_trigger_date=date(2026, 5, 8),
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# B1 — unparseable must never count as progress
# ---------------------------------------------------------------------------


def test_evaluator_fails_when_due_backlog_ungraded(sync_session) -> None:
    session, _ = sync_session
    now = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    _insert_prediction(
        session,
        ticker="AMD",
        entry_price=100.0,
        event_at=now - timedelta(days=10),
        evaluation_due_at=now - timedelta(days=3),
    )
    session.flush()

    def _always_adapter_error(_t: str, _s: date, _e: date):
        raise EvaluatorAdapterError("simulated outage")

    with pytest.raises(EvaluatorNoProgressError) as ei:
        run_evaluator_batch(
            session, now=now, price_fetcher=_always_adapter_error
        )
    assert ei.value.summary.due_selected == 1
    assert ei.value.summary.evaluated == 0
    assert ei.value.summary.progress_ok is False


def test_unparseable_outcomes_do_not_count_as_progress(sync_session) -> None:
    """B1 regression: no-bars → unparseable ≠ progress_ok."""
    session, _ = sync_session
    now = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    # Has entry but fetcher returns no bars → unparseable audit, not progress.
    _insert_prediction(
        session,
        ticker="GARBAGE",
        entry_price=10.0,
        event_at=now - timedelta(days=10),
        evaluation_due_at=now - timedelta(days=3),
        evaluation_method="fixed_lookahead_7d",
    )
    session.flush()

    def _empty(_t: str, _s: date, _e: date):
        return []

    with pytest.raises(EvaluatorNoProgressError) as ei:
        run_evaluator_batch(session, now=now, price_fetcher=_empty)
    assert ei.value.summary.unparseable >= 1
    assert ei.value.summary.evaluated == 0
    assert ei.value.summary.progress_ok is False


def test_fleet_writer_pending_entry_is_durable(sync_session) -> None:
    """F2: missing quote still leaves a durable omission row."""
    session, _ = sync_session
    from argosy.services.predictions.writers import MISSING_ENTRY_REASON

    row = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=1,
        ticker="IOVA",
        verdict="BUY",
        event_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        entry_price=None,
        revisit_triggers=[{"kind": "price_below", "price": 4.5, "label": "stop"}],
    )
    assert row is not None
    assert row.entry_price is None
    assert row.unparseable_reason == MISSING_ENTRY_REASON
    n = session.execute(
        sa.select(sa.func.count()).select_from(Prediction).where(
            Prediction.source == FLEET_VERDICT_SOURCE
        )
    ).scalar_one()
    assert n == 1
    card = {
        r.source: r for r in ledger_scorecard_by_source(session, "ariel")
    }
    assert card[FLEET_VERDICT_SOURCE].excluded_pending_entry == 1
    assert card[FLEET_VERDICT_SOURCE].total_predictions == 1



def test_evaluator_ok_when_nothing_due(sync_session) -> None:
    session, _ = sync_session
    now = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    _insert_prediction(
        session,
        event_at=now,
        evaluation_due_at=now + timedelta(days=7),
    )
    session.flush()
    summary = run_evaluator_batch(
        session, now=now, price_fetcher=lambda *_a, **_k: []
    )
    assert summary.due_selected == 0
    assert summary.progress_ok is True


def test_loop_tick_fails_loudly_with_summary(sync_session) -> None:
    session, factory = sync_session
    now = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    _insert_prediction(
        session,
        event_at=now - timedelta(days=10),
        evaluation_due_at=now - timedelta(days=1),
    )
    session.commit()

    from argosy.orchestrator.loops.predictions_evaluator import (
        PredictionsEvaluatorLoop,
    )

    def _boom(_t: str, _s: date, _e: date):
        raise EvaluatorAdapterError("down")

    loop = PredictionsEvaluatorLoop(
        session_factory=factory,
        price_fetcher=_boom,
        now_fn=lambda: now,
    )
    with pytest.raises(EvaluatorNoProgressError):
        asyncio.run(loop.tick())
    assert loop.last_output_summary is not None
    assert loop.last_output_summary["progress_ok"] is False


# ---------------------------------------------------------------------------
# B2 — price_below add-on-weakness is NOT a stop
# ---------------------------------------------------------------------------


def test_add_on_weakness_price_below_is_not_a_stop(sync_session) -> None:
    session, _ = sync_session
    triggers = [
        {
            "kind": "price_below",
            "price": 4.5,
            "label": (
                "Re-check thesis / consider adding on weakness if the "
                "commercial ramp is still intact."
            ),
        },
        {
            "kind": "dated_event",
            "date": "2026-09-30",
            "label": "ESMO",
        },
    ]
    assert (
        classify_price_trigger_role(triggers[0], direction="long") is None
    )
    row = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=29,
        ticker="IOVA",
        verdict="BUY",
        event_at=datetime(2026, 8, 7, 15, 44, 0, tzinfo=timezone.utc),
        entry_price=5.20,
        revisit_triggers=triggers,
        conviction="MED",
        decision_run_id=271,
    )
    assert row is not None
    assert row.stop_price is None
    assert row.target_price is None
    notes = json.loads(row.source_ref)["grading_field_notes"]
    assert any("revisit-only" in n or "indeterminate" in n for n in notes)


def test_explicit_stop_label_becomes_stop(sync_session) -> None:
    session, _ = sync_session
    triggers = [
        {
            "kind": "price_below",
            "price": 90.0,
            "label": "Hard stop / exit if thesis broken below 90",
        },
    ]
    assert classify_price_trigger_role(triggers[0], direction="long") == "stop"
    row = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=50,
        ticker="NOW",
        verdict="BUY",
        event_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        entry_price=160.0,
        revisit_triggers=triggers,
    )
    assert float(row.stop_price) == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# B3 — migration downgrade fail-closed + round-trip
# ---------------------------------------------------------------------------


def test_migration_0096_round_trip_with_rows(tmp_path, monkeypatch) -> None:
    """Upgrade with real prediction+outcome rows, downgrade losslessly."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    os.makedirs(os.path.dirname(sync_url.replace("sqlite:///", "")), exist_ok=True)
    cfg = Config("alembic.ini")

    # Stop just before 0096, seed discord unparseable rows, then upgrade.
    command.upgrade(cfg, "0094_expense_tag_rules")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES ('ariel', 'free', '2026-01-01 00:00:00+00:00')"
            )
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO predictions (
                    user_id, source, source_ref, ticker, direction,
                    entry_price, timeframe_days, message_id, event_at,
                    evaluation_due_at, evaluation_method, archived
                ) VALUES (
                    'ariel', 'discord_alpha_report', '{}', 'AMD', 'long',
                    NULL, 7, 'v1|predictions|discord_alpha_report|seed1',
                    '2026-06-01 00:00:00+00:00',
                    '2026-06-08 00:00:00+00:00',
                    'fixed_lookahead_7d', 0
                )
                """
            )
        )
        pid = conn.execute(sa.text("SELECT id FROM predictions")).scalar_one()
        conn.execute(
            sa.text(
                """
                INSERT INTO prediction_outcomes (
                    prediction_id, outcome_kind, evaluation_method, pnl_pct
                ) VALUES (:pid, 'unparseable', 'fixed_lookahead_7d', NULL)
                """
            ),
            {"pid": pid},
        )
        # F1: unscored overdue discord row must NOT be archived by 0096.
        conn.execute(
            sa.text(
                """
                INSERT INTO predictions (
                    user_id, source, source_ref, ticker, direction,
                    entry_price, timeframe_days, message_id, event_at,
                    evaluation_due_at, evaluation_method, archived
                ) VALUES (
                    'ariel', 'discord_alpha_report', '{}', 'NVDA', 'long',
                    100.0, 7, 'v1|predictions|discord_alpha_report|seed_unscored',
                    '2026-06-01 00:00:00+00:00',
                    '2026-06-08 00:00:00+00:00',
                    'fixed_lookahead_7d', 0
                )
                """
            )
        )

    command.upgrade(cfg, "0096_prediction_ledger_scorecard")
    with engine.begin() as conn:
        archived = conn.execute(
            sa.text("SELECT archived FROM predictions WHERE id = :pid"),
            {"pid": pid},
        ).scalar_one()
        assert int(archived) == 1
        unscored_arch = conn.execute(
            sa.text(
                "SELECT archived FROM predictions "
                "WHERE message_id LIKE '%seed_unscored'"
            )
        ).scalar_one()
        assert int(unscored_arch) == 0  # F1: still scoreable
        n_ret = conn.execute(
            sa.text("SELECT COUNT(*) FROM prediction_source_retirements")
        ).scalar_one()
        assert int(n_ret) >= 1

        # Fleet row present → downgrade must refuse.
        conn.execute(
            sa.text(
                """
                INSERT INTO predictions (
                    user_id, source, source_ref, ticker, direction,
                    entry_price, timeframe_days, message_id, event_at,
                    evaluation_due_at, evaluation_method, archived
                ) VALUES (
                    'ariel', 'internal_fleet_verdict', '{}', 'IOVA', 'long',
                    5.2, 54, 'v1|predictions|internal_fleet_verdict|29.IOVA',
                    '2026-08-07 00:00:00+00:00',
                    '2026-09-30 00:00:00+00:00',
                    'fixed_lookahead_180d', 0
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="internal_fleet_verdict"):
        command.downgrade(cfg, "0094_expense_tag_rules")

    # Remove fleet row → downgrade restores archived discord ids.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM predictions WHERE source = 'internal_fleet_verdict'"
            )
        )
    command.downgrade(cfg, "0094_expense_tag_rules")
    with engine.begin() as conn:
        archived = conn.execute(
            sa.text("SELECT archived FROM predictions WHERE id = :pid"),
            {"pid": pid},
        ).scalar_one()
        assert int(archived) == 0
        # retirement table gone
        tables = {
            r[0]
            for r in conn.execute(
                sa.text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
        }
        assert "prediction_source_retirements" not in tables
    engine.dispose()


def test_migration_0096_downgrade_fails_on_corrupt_map(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    os.makedirs(os.path.dirname(sync_url.replace("sqlite:///", "")), exist_ok=True)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0096_prediction_ledger_scorecard")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        # Upgrade always inserts retirement rows (possibly with ids=[]).
        # Corrupt the map deliberately — downgrade must refuse rather than
        # drop the only restoration record.
        n = conn.execute(
            sa.text("SELECT COUNT(*) FROM prediction_source_retirements")
        ).scalar_one()
        assert int(n) >= 1
        conn.execute(
            sa.text(
                "UPDATE prediction_source_retirements "
                "SET prediction_ids_json = 'NOT_JSON'"
            )
        )
    with pytest.raises(RuntimeError, match="unreadable restoration map"):
        command.downgrade(cfg, "0094_expense_tag_rules")
    engine.dispose()


# ---------------------------------------------------------------------------
# B5/B6 — scorecard reconcile + HOLD measurable
# ---------------------------------------------------------------------------


def test_ledger_scorecard_reconciles_and_keeps_archived_scored(
    sync_session,
) -> None:
    session, _ = sync_session
    for kind, pnl, direction in (
        ("hit_target", 0.10, "long"),
        ("hit_target", 0.20, "long"),
        ("hit_stop", -0.10, "long"),
        ("unparseable", None, "long"),
        ("expired_neutral", 0.0, "neutral"),
    ):
        p = _insert_prediction(
            session, source="internal_news_signal_analyst", direction=direction
        )
        _insert_outcome(session, p, outcome_kind=kind, pnl_pct=pnl)
    # One unscored active prediction
    _insert_prediction(
        session, source="internal_news_signal_analyst", direction="long"
    )
    # Archived BUT scored (retention) — must stay in directional sample.
    archived = _insert_prediction(
        session,
        source="internal_news_signal_analyst",
        direction="long",
        archived=1,
    )
    _insert_outcome(
        session, archived, outcome_kind="hit_stop", pnl_pct=-0.15
    )
    session.flush()

    card = {
        row.source: row for row in ledger_scorecard_by_source(session, "ariel")
    }
    a = card["internal_news_signal_analyst"]
    # 3 active directional scored + 1 archived scored hit_stop = 4
    assert a.falsifiable_denominator == 4
    assert a.excluded_unparseable == 1
    assert a.excluded_neutral == 1
    assert a.excluded_unscored == 1
    assert a.excluded_archived == 0
    assert (
        a.falsifiable_denominator
        + a.excluded_unparseable
        + a.excluded_neutral
        + a.excluded_unscored
        + a.excluded_pending_entry
        + a.excluded_superseded
        + a.excluded_hold_self_benchmark
        + a.excluded_hold_non_equity
        + a.excluded_hold_incomplete_benchmark
        == a.total_predictions
    )
    assert a.hold_scored == 1
    assert a.hold_correct_count == 1


def test_hold_verdict_writes_neutral_prediction(sync_session) -> None:
    session, _ = sync_session
    row = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=99,
        ticker="ORCL",
        verdict="HOLD",
        event_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        entry_price=100.0,
    )
    assert row is not None
    assert row.direction == "neutral"
    assert float(row.entry_price) == pytest.approx(100.0)
    assert json.loads(row.source_ref)["hold_grading"]


# ---------------------------------------------------------------------------
# F6 — immutability (corrections append; original claim frozen)
# ---------------------------------------------------------------------------


def test_same_run_correction_appends_immutable_version(
    sync_session,
) -> None:
    session, _ = sync_session
    from argosy.services.predictions.writers import SUPERSEDED_REASON

    first = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=77,
        ticker="META",
        verdict="BUY",
        event_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        entry_price=500.0,
    )
    assert first is not None
    first_id = first.id
    first_direction = first.direction
    first_entry = float(first.entry_price)

    second = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=77,
        ticker="META",
        verdict="SELL",
        event_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        entry_price=500.0,
    )
    assert second is not None
    assert second.id != first_id
    assert second.direction == "short"
    # Original claim frozen
    session.refresh(first)
    assert first.direction == first_direction == "long"
    assert float(first.entry_price) == pytest.approx(first_entry)
    assert first.superseded_by_prediction_id == second.id
    assert first.unparseable_reason == SUPERSEDED_REASON
    n = session.execute(
        sa.select(sa.func.count()).select_from(Prediction).where(
            Prediction.source == FLEET_VERDICT_SOURCE,
            Prediction.ticker == "META",
        )
    ).scalar_one()
    assert n == 2


def test_write_verdict_with_entry_writes_prediction(sync_session) -> None:
    session, _ = sync_session
    from argosy.services import verdict_registry as vr

    v = vr.write_verdict(
        session,
        user_id="ariel",
        subject="NOW",
        verdict="BUY",
        conviction="MED",
        revisit_triggers=[
            {
                "kind": "price_below",
                "price": 100.0,
                "label": "Hard stop / exit if thesis broken",
            },
        ],
        entry_price=150.0,
        settled=True,
    )
    session.flush()
    pred = session.execute(
        sa.select(Prediction).where(
            Prediction.source == FLEET_VERDICT_SOURCE,
            Prediction.ticker == "NOW",
            Prediction.superseded_by_prediction_id.is_(None),
        )
    ).scalar_one()
    assert float(pred.entry_price) == pytest.approx(150.0)
    assert float(pred.stop_price) == pytest.approx(100.0)
    assert json.loads(pred.source_ref)["verdict_id"] == v.id


def test_write_verdict_without_entry_writes_pending(sync_session) -> None:
    """F2/F8: no yfinance on critical path — durable pending-entry row."""
    session, _ = sync_session
    from argosy.services import verdict_registry as vr
    from argosy.services.predictions.writers import MISSING_ENTRY_REASON

    vr.write_verdict(
        session,
        user_id="ariel",
        subject="COST",
        verdict="BUY",
        conviction="MED",
        entry_price=None,
        settled=True,
    )
    session.flush()
    pred = session.execute(
        sa.select(Prediction).where(
            Prediction.source == FLEET_VERDICT_SOURCE,
            Prediction.ticker == "COST",
        )
    ).scalar_one()
    assert pred.entry_price is None
    assert pred.unparseable_reason == MISSING_ENTRY_REASON


# ---------------------------------------------------------------------------
# F5 — backfill includes rejected + unsettled; event-time resolver
# ---------------------------------------------------------------------------


def _seed_aug7_verdicts(session: Session) -> None:
    """Seed realistic IOVA/TRLV/NOW verdicts + proposals (fixture DB only)."""
    fixtures = [
        (
            22,
            "NOW",
            264,
            18,
            "awaiting_human",
            160.0,
            [
                {
                    "kind": "price_below",
                    "price": 100.0,
                    "label": "Re-check thesis / consider adding on weakness",
                },
                {
                    "kind": "metric_condition",
                    "metric": "gross_margin",
                    "op": "<",
                    "value": 70.0,
                },
            ],
        ),
        (
            29,
            "IOVA",
            271,
            19,
            "approved",
            5.20,
            [
                {
                    "kind": "price_below",
                    "price": 4.5,
                    "label": (
                        "Re-check thesis / consider adding on weakness if "
                        "the commercial ramp is still intact."
                    ),
                },
                {
                    "kind": "dated_event",
                    "date": "2026-09-30",
                    "label": "ESMO",
                },
            ],
        ),
        (
            30,
            "TRLV",
            272,
            20,
            "rejected",
            1.10,
            [
                {
                    "kind": "dated_event",
                    "date": "2026-12-31",
                    "label": "Schedule III",
                },
            ],
        ),
    ]
    # Also a superseded (unsettled) prior for NOW.
    fixtures.append(
        (
            21,
            "NOW",
            263,
            None,
            None,
            155.0,
            [],
        )
    )

    for vid, subj, run_id, prop_id, prop_status, _px, triggers in fixtures:
        session.execute(
            sa.text(
                "INSERT OR IGNORE INTO decision_runs "
                "(id, user_id, ticker, decision_kind, started_at, status) "
                "VALUES (:id, 'ariel', :t, 'trade_proposal', "
                " '2026-08-07 00:00:00+00:00', 'completed')"
            ),
            {"id": run_id, "t": subj},
        )
        if prop_id is not None and prop_status is not None:
            session.add(
                Proposal(
                    id=prop_id,
                    user_id="ariel",
                    ticker=subj,
                    action="buy",
                    size_shares_or_currency=100,
                    size_units="shares",
                    instrument="stock",
                    order_type="market",
                    time_in_force="DAY",
                    tier="T2",
                    account_class="main",
                    status=prop_status,
                    rationale_summary="fixture",
                    expected_impact_json="{}",
                    decision_run_id=run_id,
                )
            )
        session.add(
            Verdict(
                id=vid,
                user_id="ariel",
                subject=subj,
                verdict="BUY",
                conviction="MED",
                falsifiers_json="[]",
                revisit_triggers_json=json.dumps(triggers),
                source_decision_run_id=run_id,
                settled=(vid != 21),
                reasoning_md="fixture",
                created_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
                updated_at=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
            )
        )
    session.flush()


def test_backfill_includes_rejected_and_unsettled(sync_session) -> None:
    """F5: no survivorship bias — rejected + superseded still recorded."""
    session, _ = sync_session
    _seed_aug7_verdicts(session)
    seen_as_of: list[tuple[str, datetime]] = []

    def _resolver(subject: str, as_of: datetime) -> float | None:
        seen_as_of.append((subject, as_of))
        return {"NOW": 160.0, "IOVA": 5.20, "TRLV": 1.10}.get(subject)

    summary = backfill_fleet_verdict_predictions(
        session,
        "ariel",
        price_resolver=_resolver,
    )
    assert summary.recorded_rejected_proposal >= 1
    assert summary.recorded_unsettled >= 1
    assert any(s == "NOW" for s, _ in seen_as_of)
    tickers = {
        r.ticker
        for r in session.execute(
            sa.select(Prediction).where(
                Prediction.source == FLEET_VERDICT_SOURCE,
                Prediction.superseded_by_prediction_id.is_(None),
            )
        ).scalars()
    }
    # Head rows per verdict id — TRLV rejected still present.
    all_tickers = {
        r.ticker
        for r in session.execute(
            sa.select(Prediction).where(
                Prediction.source == FLEET_VERDICT_SOURCE
            )
        ).scalars()
    }
    assert "TRLV" in all_tickers
    assert "IOVA" in all_tickers
    assert "NOW" in all_tickers


# ---------------------------------------------------------------------------
# F3 — timeout truly abandons; F4 — failure audit durable
# ---------------------------------------------------------------------------


def test_quote_timeout_does_not_wait_for_hung_worker(monkeypatch) -> None:
    import time

    from argosy.services import verdict_registry as vr

    def _slow_fetch() -> None:
        time.sleep(1.0)

    # Patch the inner fetch by making yfinance import path hang via Ticker.
    class _Hang:
        @property
        def fast_info(self):
            time.sleep(1.0)
            return {"last_price": 1.0}

        @property
        def info(self):
            time.sleep(1.0)
            return {}

    class _Yf:
        @staticmethod
        def Ticker(_s: str):
            return _Hang()

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _Yf)
    t0 = time.perf_counter()
    px = vr.resolve_entry_price_with_timeout("HANG", timeout_seconds=0.05)
    elapsed = time.perf_counter() - t0
    assert px is None
    assert elapsed < 0.5  # must not wait the full 1s worker


def test_evaluator_no_progress_persists_failure_audit(sync_session) -> None:
    session, factory = sync_session
    now = datetime(2026, 8, 7, 10, 0, 0, tzinfo=timezone.utc)
    _insert_prediction(
        session,
        ticker="GARBAGE",
        entry_price=10.0,
        event_at=now - timedelta(days=10),
        evaluation_due_at=now - timedelta(days=1),
    )
    session.commit()

    from argosy.orchestrator.loops.predictions_evaluator import (
        PredictionsEvaluatorLoop,
    )
    from argosy.state.models import PredictionEvaluatorBatchFailure

    def _empty(_t: str, _s: date, _e: date):
        return []

    loop = PredictionsEvaluatorLoop(
        session_factory=factory,
        price_fetcher=_empty,
        now_fn=lambda: now,
    )
    with pytest.raises(EvaluatorNoProgressError):
        asyncio.run(loop.tick())

    # Failure audit + unparseable outcomes both durable; job still failed.
    audit_session = factory()
    try:
        n = audit_session.execute(
            sa.select(sa.func.count()).select_from(
                PredictionEvaluatorBatchFailure
            )
        ).scalar_one()
        assert int(n) >= 1
        outcomes = audit_session.execute(
            sa.select(sa.func.count()).select_from(PredictionOutcome)
        ).scalar_one()
        assert int(outcomes) >= 1  # unparseable audit retained
        kinds = {
            r[0]
            for r in audit_session.execute(
                sa.select(PredictionOutcome.outcome_kind)
            )
        }
        assert "unparseable" in kinds
    finally:
        audit_session.close()


def test_scorecard_api_route_registered() -> None:
    from argosy.api.main import create_app

    app = create_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/predictions/scorecard" in paths


# ---------------------------------------------------------------------------
# F7 — HOLD vs CSPX (Option B)
# ---------------------------------------------------------------------------


def _bars(start: date, prices: list[float]) -> list:
    from argosy.services.predictions.evaluator import Bar

    out = []
    d = start
    for px in prices:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(
            Bar(bar_date=d, open=px, high=px, low=px, close=px)
        )
        d += timedelta(days=1)
    return out


def _dual_fetcher(name_map: dict[str, list], cspx_bars: list):
    def _fetch(ticker: str, start: date, end: date):
        sym = ticker.upper()
        if sym == "CSPX":
            return [b for b in cspx_bars if start <= b.bar_date <= end]
        series = name_map.get(sym)
        if series is None:
            return None
        return [b for b in series if start <= b.bar_date <= end]

    return _fetch


def _hold_window_fixtures():
    """Weekday-aligned bars: CSPX needs a mark on/before event_date.

    2026-07-18 is Saturday — bars starting that day skip to Mon 07-20 and
    leave entry uncovered (loud incomplete). Use Fri 2026-07-17 instead.
    """
    event = datetime(2026, 7, 17, tzinfo=timezone.utc)  # Friday
    due = event + timedelta(days=21)
    cspx_bars = _bars(date(2026, 7, 17), [100.0] * 15 + [103.39])
    return event, due, cspx_bars


def test_hold_beat_cspx_scores_correct(sync_session) -> None:
    """CRM-class: name +9.4%, CSPX +3.4% → beat → correct."""
    from argosy.services.predictions.evaluator import evaluate_prediction
    from argosy.services.predictions.hold_benchmark import (
        HOLD_BENCHMARK_BAND_PCT,
    )

    session, _ = sync_session
    assert HOLD_BENCHMARK_BAND_PCT == pytest.approx(0.03)
    event, due, cspx_bars = _hold_window_fixtures()
    # entry 100 → exit 109.37 (+9.37%); CSPX 100 → 103.39 (+3.39%)
    name_bars = _bars(date(2026, 7, 17), [100.0] * 15 + [109.37])
    pred = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=1001,
        ticker="CRM",
        verdict="HOLD",
        event_at=event,
        entry_price=100.0,
        asset_class="Equity",
    )
    assert pred is not None
    pred.evaluation_due_at = due
    pred.evaluation_method = "fixed_lookahead_30d"
    session.flush()
    outcome = evaluate_prediction(
        session,
        pred,
        price_fetcher=_dual_fetcher({"CRM": name_bars}, cspx_bars),
    )
    assert outcome.outcome_kind == "expired_positive", outcome.notes
    assert float(outcome.pnl_pct) > HOLD_BENCHMARK_BAND_PCT  # excess
    card = {
        r.source: r for r in ledger_scorecard_by_source(session, "ariel")
    }[FLEET_VERDICT_SOURCE]
    assert card.hold_scored == 1
    assert card.hold_correct_count == 1
    assert card.falsifiable_denominator == 0  # HOLD not in directional


def test_hold_lag_cspx_scores_incorrect(sync_session) -> None:
    """META-class: name −8.7%, CSPX +3.4% → lag beyond band → incorrect."""
    from argosy.services.predictions.evaluator import evaluate_prediction
    from argosy.services.predictions.hold_benchmark import (
        HOLD_BENCHMARK_BAND_PCT,
    )

    session, _ = sync_session
    event, due, cspx_bars = _hold_window_fixtures()
    name_bars = _bars(date(2026, 7, 17), [100.0] * 15 + [91.31])
    pred = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=1002,
        ticker="META",
        verdict="HOLD",
        event_at=event,
        entry_price=100.0,
        asset_class="Equity",
    )
    pred.evaluation_due_at = due
    pred.evaluation_method = "fixed_lookahead_30d"
    session.flush()
    outcome = evaluate_prediction(
        session,
        pred,
        price_fetcher=_dual_fetcher({"META": name_bars}, cspx_bars),
    )
    assert outcome.outcome_kind == "expired_negative", outcome.notes
    assert float(outcome.pnl_pct) < -HOLD_BENCHMARK_BAND_PCT
    card = {
        r.source: r for r in ledger_scorecard_by_source(session, "ariel")
    }[FLEET_VERDICT_SOURCE]
    assert card.hold_scored == 1
    assert card.hold_correct_count == 0


def test_hold_within_band_scores_correct(sync_session) -> None:
    """SCHD-class: excess −0.99% inside 3% band → expired_neutral correct."""
    from argosy.services.predictions.evaluator import evaluate_prediction
    from argosy.services.predictions.hold_benchmark import (
        HOLD_BENCHMARK_BAND_PCT,
    )

    session, _ = sync_session
    event, due, cspx_bars = _hold_window_fixtures()
    # name +2.40%, CSPX +3.39% → excess −0.99%
    name_bars = _bars(date(2026, 7, 17), [100.0] * 15 + [102.40])
    pred = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=1003,
        ticker="SCHD",
        verdict="HOLD",
        event_at=event,
        entry_price=100.0,
        asset_class="Equity",
    )
    pred.evaluation_due_at = due
    pred.evaluation_method = "fixed_lookahead_30d"
    session.flush()
    outcome = evaluate_prediction(
        session,
        pred,
        price_fetcher=_dual_fetcher({"SCHD": name_bars}, cspx_bars),
    )
    assert outcome.outcome_kind == "expired_neutral", outcome.notes
    assert abs(float(outcome.pnl_pct)) <= HOLD_BENCHMARK_BAND_PCT
    card = {
        r.source: r for r in ledger_scorecard_by_source(session, "ariel")
    }[FLEET_VERDICT_SOURCE]
    assert card.hold_correct_count == 1


def test_hold_self_benchmark_excluded(sync_session) -> None:
    from argosy.services.predictions.evaluator import evaluate_prediction

    session, _ = sync_session
    event, due, cspx_bars = _hold_window_fixtures()
    pred = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=1004,
        ticker="CSPX",
        verdict="HOLD",
        event_at=event,
        entry_price=100.0,
        asset_class="Equity",
    )
    pred.evaluation_due_at = due
    pred.evaluation_method = "fixed_lookahead_30d"
    session.flush()
    outcome = evaluate_prediction(
        session,
        pred,
        price_fetcher=_dual_fetcher({"CSPX": cspx_bars}, cspx_bars),
    )
    assert outcome.outcome_kind == "unparseable"
    assert "hold_self_benchmark" in (outcome.notes or "")
    card = {
        r.source: r for r in ledger_scorecard_by_source(session, "ariel")
    }[FLEET_VERDICT_SOURCE]
    assert card.excluded_hold_self_benchmark == 1
    assert card.hold_scored == 0
    assert (
        card.falsifiable_denominator
        + card.excluded_unparseable
        + card.excluded_neutral
        + card.excluded_unscored
        + card.excluded_pending_entry
        + card.excluded_superseded
        + card.excluded_hold_self_benchmark
        + card.excluded_hold_non_equity
        + card.excluded_hold_incomplete_benchmark
        == card.total_predictions
    )


def test_hold_non_equity_excluded(sync_session) -> None:
    from argosy.services.predictions.evaluator import evaluate_prediction

    session, _ = sync_session
    event, due, cspx_bars = _hold_window_fixtures()
    name_bars = _bars(date(2026, 7, 17), [100.0] * 16)
    pred = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=1005,
        ticker="CASHUSD",
        verdict="HOLD",
        event_at=event,
        entry_price=100.0,
        asset_class="Cash",
    )
    pred.evaluation_due_at = due
    pred.evaluation_method = "fixed_lookahead_30d"
    session.flush()
    outcome = evaluate_prediction(
        session,
        pred,
        price_fetcher=_dual_fetcher({"CASHUSD": name_bars}, cspx_bars),
    )
    assert outcome.outcome_kind == "unparseable"
    assert "hold_non_equity" in (outcome.notes or "")
    card = {
        r.source: r for r in ledger_scorecard_by_source(session, "ariel")
    }[FLEET_VERDICT_SOURCE]
    assert card.excluded_hold_non_equity == 1
    assert card.hold_scored == 0


def test_hold_incomplete_benchmark_excluded(sync_session) -> None:
    from argosy.services.predictions.evaluator import evaluate_prediction

    session, _ = sync_session
    event, due, _cspx = _hold_window_fixtures()
    name_bars = _bars(date(2026, 7, 17), [100.0] * 16)
    pred = write_fleet_verdict_prediction(
        session,
        "ariel",
        verdict_id=1006,
        ticker="SOFI",
        verdict="HOLD",
        event_at=event,
        entry_price=100.0,
        asset_class="Equity",
    )
    pred.evaluation_due_at = due
    pred.evaluation_method = "fixed_lookahead_30d"
    session.flush()

    def _no_cspx(ticker: str, start: date, end: date):
        if ticker.upper() == "CSPX":
            return None
        return [b for b in name_bars if start <= b.bar_date <= end]

    outcome = evaluate_prediction(session, pred, price_fetcher=_no_cspx)
    assert outcome.outcome_kind == "unparseable"
    assert "hold_incomplete_benchmark" in (outcome.notes or "")
    card = {
        r.source: r for r in ledger_scorecard_by_source(session, "ariel")
    }[FLEET_VERDICT_SOURCE]
    assert card.excluded_hold_incomplete_benchmark == 1


def test_hold_band_constant_and_classifier() -> None:
    from argosy.services.predictions.hold_benchmark import (
        HOLD_BENCHMARK_BAND_PCT,
        classify_hold_vs_benchmark,
        is_equity_class_for_hold,
    )

    assert HOLD_BENCHMARK_BAND_PCT == 0.03
    # Live sleeve labels must be equity-eligible (not silent non-equity).
    assert is_equity_class_for_hold("Individual Stocks") is True
    assert is_equity_class_for_hold("Core Equity") is True
    assert is_equity_class_for_hold("Dividend") is True
    assert is_equity_class_for_hold("Cash") is False
    assert is_equity_class_for_hold("Real Estate") is False
    assert is_equity_class_for_hold("Treasury 1-3yr") is False
    assert is_equity_class_for_hold(None) is None
    # SOFI-like −2.72% excess → within 3% → neutral/correct
    kind, excess, _ = classify_hold_vs_benchmark(
        ticker="SOFI",
        name_entry=100.0,
        name_exit=100.0 - 0.02,  # ~0
        name_exit_date=date(2026, 8, 7),
        cspx_entry=100.0,
        cspx_exit=102.70,
    )
    assert kind == "expired_neutral"
    assert excess == pytest.approx(-0.0272, abs=1e-3)
    # At 1% band would be a miss — document sensitivity in constant choice.
    kind_tight, _, _ = classify_hold_vs_benchmark(
        ticker="SOFI",
        name_entry=100.0,
        name_exit=99.98,
        name_exit_date=date(2026, 8, 7),
        cspx_entry=100.0,
        cspx_exit=102.70,
        band=0.01,
    )
    assert kind_tight == "expired_negative"
