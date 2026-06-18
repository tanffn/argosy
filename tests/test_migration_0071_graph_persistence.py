# tests/test_migration_0071_graph_persistence.py
"""Run alembic upgrade head on a throwaway SQLite DB (via the repo's
ARGOSY_HOME + settings.database_url convention, matching tests/test_migration_0067.py)
and assert the five Phase-1c tables exist with the spec columns; then
downgrade to 0070 drops them."""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    # Mirror tests/test_migration_0067.py: alembic/env.py resolves the URL
    # from settings.database_url, which keys off ARGOSY_HOME — so point
    # ARGOSY_HOME at tmp_path rather than set_main_option (env.py overrides it).
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    sync_url = get_settings().database_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return Config("alembic.ini"), sync_url


def test_upgrade_head_creates_graph_tables(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    eng = sa.create_engine(sync_url)
    insp = sa.inspect(eng)
    names = set(insp.get_table_names())
    assert {
        "plan_nodes", "plan_edges", "change_requests",
        "dialogue_turns", "propagation_events",
    } <= names

    node_cols = {c["name"] for c in insp.get_columns("plan_nodes")}
    assert {"plan_id", "node_key", "kind", "value_json", "input_hash",
            "status_validity", "status_flag", "compute_version"} <= node_cols

    prop_cols = {c["name"] for c in insp.get_columns("propagation_events")}
    assert {"cycle_id", "trigger_node_key", "invalidated_node_keys_json",
            "recomputed_json", "rerendered_surfaces_json",
            "verification_verdicts_json"} <= prop_cols
    eng.dispose()


def test_downgrade_drops_graph_tables(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0070_tax_simulation_lots")

    eng = sa.create_engine(sync_url)
    names = set(sa.inspect(eng).get_table_names())
    assert "plan_nodes" not in names
    assert "propagation_events" not in names
    eng.dispose()
