"""Migration 0048 — job_runs schema (Spec A commit #1).

Smoke tests:
  - upgrade to 0048 creates the ``job_runs`` table with all columns +
    CHECK constraints + UNIQUE constraint + the partial index;
  - CHECK constraints reject bad values (status enum, manual_trigger
    bool, output_summary non-JSON, duration_ms negative);
  - UNIQUE on ``idempotency_key`` prevents duplicates;
  - downgrade drops the table cleanly.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_migration_0048.py -v
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
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    return cfg, sync_url


def test_migration_0048_creates_job_runs_table(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048_job_runs")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)

    assert "job_runs" in insp.get_table_names()

    cols = {c["name"] for c in insp.get_columns("job_runs")}
    expected = {
        "id",
        "job_name",
        "started_at",
        "finished_at",
        "status",
        "skip_reason",
        "error_message",
        "manual_trigger",
        "triggered_by",
        "output_summary",
        "duration_ms",
        "idempotency_key",
        "created_at",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"

    # Both indexes present (one regular, one partial).
    idx_names = {i["name"] for i in insp.get_indexes("job_runs")}
    assert "ix_job_runs_job_started" in idx_names
    assert "ix_job_runs_status_started" in idx_names

    # Partial index condition is visible via sqlite_master.sql.
    with engine.connect() as conn:
        partial_sql = conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='ix_job_runs_status_started'"
            )
        ).scalar()
    assert partial_sql is not None
    assert "WHERE status IN" in partial_sql


def test_migration_0048_check_status_enum(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048_job_runs")
    engine = sa.create_engine(sync_url)

    with engine.begin() as conn:
        # Valid status accepted.
        conn.execute(
            sa.text(
                "INSERT INTO job_runs "
                "(job_name, started_at, status, idempotency_key) "
                "VALUES ('test', '2026-05-29 10:00:00+00:00', "
                "'running', 'k1')"
            )
        )

    # Invalid status rejected.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO job_runs "
                    "(job_name, started_at, status, idempotency_key) "
                    "VALUES ('test', '2026-05-29 10:00:00+00:00', "
                    "'bogus_status', 'k2')"
                )
            )


def test_migration_0048_check_manual_trigger_bool(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048_job_runs")
    engine = sa.create_engine(sync_url)

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO job_runs "
                    "(job_name, started_at, status, manual_trigger, "
                    " idempotency_key) "
                    "VALUES ('t', '2026-05-29 10:00:00+00:00', 'ok', 2, "
                    "'k_bool')"
                )
            )


def test_migration_0048_check_output_summary_json(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048_job_runs")
    engine = sa.create_engine(sync_url)

    # NULL summary accepted.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO job_runs "
                "(job_name, started_at, status, idempotency_key) "
                "VALUES ('t', '2026-05-29 10:00:00+00:00', 'ok', "
                "'k_null')"
            )
        )

    # Valid JSON accepted.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO job_runs "
                "(job_name, started_at, status, output_summary, "
                " idempotency_key) "
                "VALUES ('t', '2026-05-29 10:00:00+00:00', 'ok', "
                "'{\"signals\": 7}', 'k_json')"
            )
        )

    # Non-JSON rejected (json_valid CHECK).
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO job_runs "
                    "(job_name, started_at, status, output_summary, "
                    " idempotency_key) "
                    "VALUES ('t', '2026-05-29 10:00:00+00:00', 'ok', "
                    "'not json at all', 'k_bad')"
                )
            )


def test_migration_0048_check_duration_nonneg(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048_job_runs")
    engine = sa.create_engine(sync_url)

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO job_runs "
                    "(job_name, started_at, status, duration_ms, "
                    " idempotency_key) "
                    "VALUES ('t', '2026-05-29 10:00:00+00:00', 'ok', "
                    "-1, 'k_neg')"
                )
            )


def test_migration_0048_unique_idempotency_key(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048_job_runs")
    engine = sa.create_engine(sync_url)

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO job_runs "
                "(job_name, started_at, status, idempotency_key) "
                "VALUES ('t', '2026-05-29 10:00:00+00:00', 'ok', "
                "'dup_key_1')"
            )
        )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO job_runs "
                    "(job_name, started_at, status, idempotency_key) "
                    "VALUES ('t', '2026-05-29 11:00:00+00:00', 'ok', "
                    "'dup_key_1')"
                )
            )


def test_migration_0048_downgrade_drops_table(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0048_job_runs")
    engine = sa.create_engine(sync_url)
    assert "job_runs" in sa.inspect(engine).get_table_names()

    command.downgrade(cfg, "0047_expense_review_queue_extensions")
    engine_after = sa.create_engine(sync_url)
    assert "job_runs" not in sa.inspect(engine_after).get_table_names()
