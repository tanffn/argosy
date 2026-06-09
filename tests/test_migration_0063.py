"""Migration 0063 — plan_versions.target_allocation_json.

Adds the nullable Text column that persists the canonical instrument-level
TargetAllocationDoc on each plan version (roadmap T1.4). Mirrors 0062's
single-column add. Smoke tests: column present after upgrade, gone after
downgrade.
"""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """Fresh per-test DB rooted at ``tmp_path`` (mirrors 0055/0054)."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    return cfg, sync_url


def test_0063_adds_target_allocation_json(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0063_plan_target_allocation")
    engine = sa.create_engine(sync_url)
    cols = {c["name"] for c in sa.inspect(engine).get_columns("plan_versions")}
    assert "target_allocation_json" in cols


def test_0063_downgrade_drops_column(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0063_plan_target_allocation")
    command.downgrade(cfg, "0062_plan_narrative_persistence")
    engine = sa.create_engine(sync_url)
    cols = {c["name"] for c in sa.inspect(engine).get_columns("plan_versions")}
    assert "target_allocation_json" not in cols
