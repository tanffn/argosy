"""Migration 0067 — admit thesis_monitor_* kinds in monitor_flags.kind CHECK.

Smoke tests:
* upgrade to head: the CHECK admits 'thesis_monitor_weakened' /
  'thesis_monitor_broken' and still rejects a bogus kind;
* downgrade to 0066 (no thesis rows): the narrower CHECK rejects thesis kinds;
* downgrade PREFLIGHT refuses to narrow the CHECK if a thesis_monitor_* row
  would be silently dropped.
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
    return Config("alembic.ini"), sync_url


def _seed_user(engine: sa.Engine, uid: str = "ariel") -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES (:uid, 'free', '2026-06-14 10:00:00+00:00')"
            ),
            {"uid": uid},
        )


def _insert_flag(engine: sa.Engine, kind: str, dedup: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO monitor_flags (user_id, kind, severity, payload, "
                "surfaced_at, dedup_key) VALUES ('ariel', :k, 'critical', '{}', "
                "'2026-06-14 11:00:00+00:00', :d)"
            ),
            {"k": kind, "d": dedup},
        )


def test_upgrade_admits_thesis_kinds_and_rejects_bogus(alembic_cfg) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    # thesis kinds now allowed
    _insert_flag(engine, "thesis_monitor_weakened", "t|w")
    _insert_flag(engine, "thesis_monitor_broken", "t|b")
    with engine.connect() as conn:
        n = conn.execute(
            sa.text("SELECT COUNT(*) FROM monitor_flags WHERE kind LIKE 'thesis_monitor_%'")
        ).scalar()
    assert n == 2
    # a bogus kind is still rejected by the CHECK
    with pytest.raises(sa.exc.IntegrityError):
        _insert_flag(engine, "totally_bogus_kind", "t|x")


def test_downgrade_rejects_thesis_kinds(alembic_cfg) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0066_trend_scan_state")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_flag(engine, "thesis_monitor_broken", "t|b")


def test_downgrade_preflight_refuses_when_thesis_row_present(alembic_cfg) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = sa.create_engine(sync_url)
    _seed_user(engine)
    _insert_flag(engine, "thesis_monitor_broken", "t|b")  # a row that would be dropped
    with pytest.raises(RuntimeError, match="preflight failed"):
        command.downgrade(cfg, "0066_trend_scan_state")
