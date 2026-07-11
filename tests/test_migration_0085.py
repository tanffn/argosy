"""Migration 0085 — tenant-scoped raw signal event ledger."""
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


def _seed_user(engine: sa.Engine, user_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES (:user_id, 'free', '2026-07-11 09:00:00+00:00')"
            ),
            {"user_id": user_id},
        )


def _insert_event(
    engine: sa.Engine,
    *,
    user_id: str = "ariel",
    event_key: str = "group-a:non_derivative:0",
    payload_json: str = '{"accession":"a"}',
    source_urls_json: str = '["https://www.sec.gov/a"]',
    evaluation_pending: int | None = None,
) -> None:
    pending_column = (
        ", evaluation_pending" if evaluation_pending is not None else ""
    )
    pending_value = (
        ", :evaluation_pending" if evaluation_pending is not None else ""
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO signal_stream_events "
                "(user_id, stream, event_key, event_group_key, ticker, "
                "event_at, available_at, payload_json, source_urls_json, active, "
                f"first_seen_at, last_seen_at{pending_column}) VALUES "
                "(:user_id, 'insider_cluster', :event_key, 'group-a', 'ACME', "
                "'2026-07-01', '2026-07-02', :payload_json, :source_urls_json, 1, "
                "'2026-07-02 12:00:00+00:00', "
                f"'2026-07-02 12:00:00+00:00'{pending_value})"
            ),
            {
                "user_id": user_id,
                "event_key": event_key,
                "payload_json": payload_json,
                "source_urls_json": source_urls_json,
                "evaluation_pending": evaluation_pending,
            },
        )


def test_signal_event_ledger_roundtrip_constraints_and_indexes(alembic_cfg) -> None:
    config, sync_url = alembic_cfg
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_url)
    _seed_user(engine, "ariel")
    _seed_user(engine, "noga")

    inspector = sa.inspect(engine)
    assert inspector.has_table("signal_stream_events")
    columns = {
        column["name"]: column
        for column in inspector.get_columns("signal_stream_events")
    }
    assert columns["evaluation_pending"]["nullable"] is False
    assert str(columns["evaluation_pending"]["default"]) in {"3", "'3'"}
    index_columns = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_indexes("signal_stream_events")
    }
    assert index_columns["ix_signal_events_user_stream_available"] == (
        "user_id",
        "stream",
        "available_at",
    )
    assert index_columns["ix_signal_events_user_stream_ticker_event"] == (
        "user_id",
        "stream",
        "ticker",
        "event_at",
    )
    assert index_columns["ix_signal_events_user_stream_group"] == (
        "user_id",
        "stream",
        "event_group_key",
    )
    foreign_keys = inspector.get_foreign_keys("signal_stream_events")
    assert any(
        foreign_key["constrained_columns"] == ["user_id"]
        and foreign_key["referred_table"] == "users"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in foreign_keys
    )

    _insert_event(engine)
    _insert_event(engine, user_id="noga")
    with engine.connect() as connection:
        assert connection.execute(
            sa.text(
                "SELECT evaluation_pending FROM signal_stream_events "
                "WHERE user_id = 'ariel'"
            )
        ).scalar_one() == 3
    with pytest.raises(sa.exc.IntegrityError):
        _insert_event(engine)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_event(engine, event_key="bad-payload", payload_json="{")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_event(engine, event_key="bad-urls", source_urls_json="{")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_event(
            engine,
            event_key="bad-pending",
            evaluation_pending=4,
        )

    command.downgrade(config, "0084_signal_stream_warning_kind")
    assert not sa.inspect(engine).has_table("signal_stream_events")
    engine.dispose()
