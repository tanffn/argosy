"""Real migration round-trip: preserve a legacy run's execution policy."""
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command


@pytest.mark.real_seam
def test_private_chat_migration_roundtrip(tmp_path, monkeypatch):
    target = tmp_path / "migration.db"
    monkeypatch.setenv("ARGOSY_ALEMBIC_URL", f"sqlite+aiosqlite:///{target.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "0118_shared_research_inputs")
    with sqlite3.connect(target) as connection:
        connection.execute("INSERT INTO users (id, created_at) VALUES ('migration-owner', CURRENT_TIMESTAMP)")
        connection.execute("INSERT INTO decision_runs (user_id, ticker, tier, started_at, status, decision_kind) VALUES ('migration-owner', 'TEST', 'T2', CURRENT_TIMESTAMP, 'completed', 'ticker')")
    command.upgrade(config, "0119_private_discord_advisor")
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT execution_policy FROM decision_runs").fetchone()[0] == "normal"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"chat_bindings", "chat_threads", "chat_turns", "chat_analysis_requests", "chat_agent_progress", "notification_outbox", "chat_cursors"} <= tables
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    command.downgrade(config, "0118_shared_research_inputs")
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT count(*) FROM decision_runs").fetchone()[0] == 1
        assert "execution_policy" not in {row[1] for row in connection.execute("PRAGMA table_info(decision_runs)")}
    command.upgrade(config, "0119_private_discord_advisor")
