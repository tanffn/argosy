"""Migration 0084 — admit the generic signal-stream warning flag kind."""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    sync_url = get_settings().database_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return Config("alembic.ini"), sync_url


def _seed_user(engine: sa.Engine, user_id: str = "ariel") -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES (:user_id, 'free', '2026-07-11 09:00:00+00:00')"
            ),
            {"user_id": user_id},
        )


def _insert_flag(engine: sa.Engine, kind: str, dedup_key: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO monitor_flags "
                "(user_id, kind, severity, payload, surfaced_at, dedup_key) "
                "VALUES ('ariel', :kind, 'warning', '{}', "
                "'2026-07-11 09:30:00+00:00', :dedup_key)"
            ),
            {"kind": kind, "dedup_key": dedup_key},
        )


def _prediction_index_sql(engine: sa.Engine) -> str:
    with engine.connect() as connection:
        sql = connection.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' "
                "AND name = 'ix_predictions_source_messageid'"
            )
        ).scalar_one()
    return " ".join(str(sql).lower().split())


def _insert_prediction(
    engine: sa.Engine,
    *,
    user_id: str,
    message_id: str,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO predictions "
                "(user_id, source, source_ref, ticker, direction, "
                "entry_price, timeframe_days, message_id, event_at, "
                "evaluation_due_at, evaluation_method) VALUES "
                "(:user_id, 'signal_stream:insider_cluster', '{}', "
                "'SCHW', 'short', 25, 30, :message_id, "
                "'2026-07-11 09:30:00+00:00', "
                "'2026-08-10 09:30:00+00:00', 'fixed_lookahead_30d')"
            ),
            {"user_id": user_id, "message_id": message_id},
        )


def test_head_accepts_generic_warning_and_round_trips(alembic_cfg) -> None:
    config, sync_url = alembic_cfg
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    head_index_sql = _prediction_index_sql(engine)
    assert "on predictions (user_id, source, message_id)" in head_index_sql
    assert "where message_id is not null" in head_index_sql
    _insert_flag(engine, "signal_stream_warning", "warning|1")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_flag(engine, "insider_cluster_warning", "warning|bogus")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "DELETE FROM monitor_flags "
                "WHERE kind = 'signal_stream_warning'"
            )
        )
    command.downgrade(config, "0083_signal_stream_cursors")
    old_index_sql = _prediction_index_sql(engine)
    assert "on predictions (source, message_id)" in old_index_sql
    assert "user_id" not in old_index_sql
    assert "where message_id is not null" in old_index_sql
    with pytest.raises(sa.exc.IntegrityError):
        _insert_flag(engine, "signal_stream_warning", "warning|2")
    command.upgrade(config, "head")
    _insert_flag(engine, "signal_stream_warning", "warning|3")
    engine.dispose()


def test_prediction_dedup_is_per_user_with_independent_scorecards(
    alembic_cfg,
) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from sqlalchemy.orm import sessionmaker

    from argosy.services.predictions.reliability import (
        signal_source_scorecard,
    )
    from argosy.services.predictions.writers import (
        write_signal_stream_predictions,
    )
    from argosy.state.models import Prediction, PredictionOutcome

    config, sync_url = alembic_cfg
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_url)
    _seed_user(engine, "ariel")
    _seed_user(engine, "noga")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event_at = datetime(2026, 7, 11, 9, 30, tzinfo=UTC)

    with factory() as session:
        ariel_first = write_signal_stream_predictions(
            session,
            "ariel",
            stream="insider_cluster",
            dedup_key="shared-nomination",
            ticker="SCHW",
            direction="short",
            event_at=event_at,
            entry_price=25,
            evidence={"price": 25},
        )
        ariel_rerun = write_signal_stream_predictions(
            session,
            "ariel",
            stream="insider_cluster",
            dedup_key="shared-nomination",
            ticker="SCHW",
            direction="short",
            event_at=event_at,
            entry_price=25,
            evidence={"price": 25},
        )
        noga_rows = write_signal_stream_predictions(
            session,
            "noga",
            stream="insider_cluster",
            dedup_key="shared-nomination",
            ticker="SCHW",
            direction="short",
            event_at=event_at,
            entry_price=25,
            evidence={"price": 25},
        )
        session.commit()

        assert [row.id for row in ariel_rerun] == [
            row.id for row in ariel_first
        ]
        assert set(row.id for row in noga_rows).isdisjoint(
            row.id for row in ariel_first
        )
        assert session.query(Prediction).filter_by(user_id="ariel").count() == 2
        assert session.query(Prediction).filter_by(user_id="noga").count() == 2

        session.add(
            PredictionOutcome(
                prediction_id=ariel_first[0].id,
                evaluation_method="fixed_lookahead_30d",
                outcome_kind="hit_target",
                pnl_pct=Decimal("0.12"),
                evaluated_at=datetime(2026, 8, 10, tzinfo=UTC),
                entry_price_used=Decimal("25"),
                exit_price_used=Decimal("22"),
            )
        )
        session.commit()
        ariel_scorecard = signal_source_scorecard(
            session,
            "ariel",
            "insider_cluster",
            now=datetime(2026, 8, 11, tzinfo=UTC),
        )
        noga_scorecard = signal_source_scorecard(
            session,
            "noga",
            "insider_cluster",
            now=datetime(2026, 8, 11, tzinfo=UTC),
        )
        assert ariel_scorecard["scored_outcomes"] == 1
        assert noga_scorecard["scored_outcomes"] == 0
    engine.dispose()


def test_downgrade_refuses_cross_user_prediction_collision(
    alembic_cfg,
) -> None:
    config, sync_url = alembic_cfg
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_url)
    _seed_user(engine, "ariel")
    _seed_user(engine, "noga")
    _insert_prediction(engine, user_id="ariel", message_id="shared")
    _insert_prediction(engine, user_id="noga", message_id="shared")

    with pytest.raises(RuntimeError, match="prediction.*preflight"):
        command.downgrade(config, "0083_signal_stream_cursors")
    engine.dispose()


def test_downgrade_refuses_while_generic_warning_rows_exist(
    alembic_cfg,
) -> None:
    config, sync_url = alembic_cfg
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    _insert_flag(engine, "signal_stream_warning", "warning|1")

    with pytest.raises(RuntimeError, match="preflight failed"):
        command.downgrade(config, "0083_signal_stream_cursors")
    engine.dispose()
