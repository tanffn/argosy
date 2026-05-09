"""Migration 0023 — fx_rates cache table."""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """Alembic Config + sync sqlite URL isolated via ARGOSY_HOME.

    alembic/env.py overwrites sqlalchemy.url from argosy.config.get_settings(),
    so we must steer it via ARGOSY_HOME (per the project's existing migration-test
    pattern in tests/conftest.py).
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    cfg = Config("alembic.ini")
    return cfg, sync_url


def test_fx_rates_table_exists_at_head(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert "fx_rates" in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns("fx_rates")}
    assert cols["date"]["nullable"] is False
    assert cols["currency"]["nullable"] is False
    assert cols["rate"]["nullable"] is False
    assert cols["source"]["nullable"] is False
    assert cols["fetched_at"]["nullable"] is False
    pks = insp.get_pk_constraint("fx_rates")
    assert set(pks["constrained_columns"]) == {"date", "currency"}
    idx_names = {ix["name"] for ix in insp.get_indexes("fx_rates")}
    assert "idx_fx_rates_currency" in idx_names


def test_downgrade_removes_fx_rates(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0022_expense_amount_nis_nullable")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    assert "fx_rates" not in insp.get_table_names()
