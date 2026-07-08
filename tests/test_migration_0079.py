"""Migration 0079 — holding_reviews audit table.

Smoke tests:
* upgrade to 0079 creates ``holding_reviews`` with the expected columns +
  index and accepts an insert;
* downgrade drops the table cleanly.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_migration_0079.py -v
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


def _tables(engine):
    return set(sa.inspect(engine).get_table_names())


def _cols(engine, table):
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_0079_creates_holding_reviews(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0078_action_proposal_status_executed")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    assert "holding_reviews" not in _tables(engine)
    engine.dispose()

    command.upgrade(cfg, "0079_holding_reviews")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    assert "holding_reviews" in _tables(engine)
    assert _cols(engine, "holding_reviews") == {
        "id", "user_id", "symbol", "reviewed_at", "verdict", "confidence",
        "reason", "evidence_json", "position_usd", "elevated_by_flag", "outcome",
    }
    idx_names = {i["name"] for i in sa.inspect(engine).get_indexes("holding_reviews")}
    assert "ix_holding_reviews_user_symbol" in idx_names

    # A representative insert lands (defaults for reviewed_at / elevated_by_flag).
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, plan, created_at) "
            "VALUES ('ariel', 'free', '2026-07-08 10:00:00+00:00')"
        ))
        conn.execute(sa.text(
            "INSERT INTO holding_reviews "
            "(user_id, symbol, verdict, confidence, reason, evidence_json, "
            " position_usd, outcome) "
            "VALUES ('ariel', 'TEM', 'TRIM', 'MED', 'read-out risk', "
            "'{\"evidence\": []}', 4800.0, 'held_unverified')"
        ))
        row = conn.execute(sa.text(
            "SELECT symbol, outcome, elevated_by_flag, reviewed_at "
            "FROM holding_reviews"
        )).fetchone()
    assert row[0] == "TEM"
    assert row[1] == "held_unverified"
    assert row[2] == 0            # server_default
    assert row[3] is not None     # CURRENT_TIMESTAMP default
    engine.dispose()


def test_0079_downgrade_drops_table(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0079_holding_reviews")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    assert "holding_reviews" in _tables(engine)
    engine.dispose()

    command.downgrade(cfg, "0078_action_proposal_status_executed")
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    assert "holding_reviews" not in _tables(engine)
    engine.dispose()
