"""Migration 0072 — monitor_flags.status lifecycle column.

Smoke tests:
* upgrade to 0072 adds the ``status`` column with server_default 'active';
* the backfill stamps acknowledged rows 'acknowledged' and the rest 'active';
* downgrade drops the column cleanly.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_migration_0072.py -v
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    sync_url = get_settings().database_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    return cfg, sync_url


def _seed_user(engine):
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, plan, created_at) "
            "VALUES ('ariel', 'free', '2026-05-29 10:00:00+00:00')"
        ))


def _cols(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_0072_adds_status_column_with_default(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0071_derivation_graph_persistence")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    _seed_user(engine)
    # Pre-0072 there is no status column.
    assert "status" not in _cols(engine, "monitor_flags")

    # Seed one acknowledged + one live flag BEFORE the upgrade.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO monitor_flags "
            "(user_id, kind, severity, payload, surfaced_at, acknowledged_at, created_at) "
            "VALUES "
            "('ariel','state_observer_fx_observation','warning','{}','2026-06-01 00:00:00', "
            "  '2026-06-02 00:00:00','2026-06-01 00:00:00'),"
            "('ariel','state_observer_fx_observation','warning','{}','2026-06-01 00:00:00', "
            "  NULL,'2026-06-01 00:00:00')"
        ))
    engine.dispose()

    command.upgrade(cfg, "0072_monitor_flag_status")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    assert "status" in _cols(engine, "monitor_flags")
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT acknowledged_at, status FROM monitor_flags ORDER BY id"
        )).fetchall()
    # Backfill: acknowledged row -> 'acknowledged'; live row -> 'active'.
    assert rows[0][1] == "acknowledged"
    assert rows[1][1] == "active"
    engine.dispose()


def test_0072_downgrade_drops_status(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0072_monitor_flag_status")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    assert "status" in _cols(engine, "monitor_flags")
    engine.dispose()

    command.downgrade(cfg, "0071_derivation_graph_persistence")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    assert "status" not in _cols(engine, "monitor_flags")
    engine.dispose()
