"""Migration 0066 + ScanState ORM — trend_scan_state round-trips a row.

Smart-refresh persistence for the discovery funnel (Phase 2). Per-(user, ticker)
memory of the last radar score + estimator/fleet verdicts + timestamps + a
status/rank/quarantine for diffing.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    sync_url = get_settings().database_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0066_trend_scan_state")
    return cfg, sync_url


def test_0066_creates_table_and_indexes(migrated_db):
    _, sync_url = migrated_db
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert insp.has_table("trend_scan_state")
    cols = {c["name"] for c in insp.get_columns("trend_scan_state")}
    assert {"user_id", "ticker", "last_score", "status", "rank",
            "quarantine_reason", "radar_fingerprint", "estimator_json",
            "fleet_json", "last_radar_at", "last_estimated_at", "last_fleet_at",
            "last_seen_at"} <= cols
    idx_names = {i["name"] for i in insp.get_indexes("trend_scan_state")}
    assert "ix_trend_scan_state_user_status" in idx_names


def test_0066_round_trips_a_row(migrated_db):
    _, sync_url = migrated_db
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import ScanState

    engine = sa.create_engine(sync_url)
    Session = sessionmaker(bind=engine)
    now = datetime(2026, 6, 12, tzinfo=timezone.utc)
    with Session() as db:
        db.add(ScanState(
            user_id="ariel", ticker="PLTR", last_score=0.82,
            status="active", rank=1, quarantine_reason="",
            radar_fingerprint="score=0.82|fam=ai|liq=high",
            estimator_json='{"go": true, "conviction": "HIGH"}',
            fleet_json=None,
            last_radar_at=now, last_estimated_at=now,
            last_fleet_at=None, last_seen_at=now,
        ))
        db.commit()
    with Session() as db:
        row = db.get(ScanState, {"user_id": "ariel", "ticker": "PLTR"})
        assert row is not None
        assert row.last_score == 0.82
        assert row.status == "active"
        assert row.rank == 1
        assert row.radar_fingerprint == "score=0.82|fam=ai|liq=high"
        assert row.estimator_json == '{"go": true, "conviction": "HIGH"}'
        assert row.fleet_json is None


def test_0066_downgrade_drops_table(migrated_db):
    cfg, sync_url = migrated_db
    command.downgrade(cfg, "0065_plan_sections_json")
    engine = sa.create_engine(sync_url)
    assert not sa.inspect(engine).has_table("trend_scan_state")
