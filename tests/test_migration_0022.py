"""Migration 0022 — expense_transactions.amount_nis nullable."""

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


def test_amount_nis_is_nullable_after_0022(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0022_expense_amount_nis_nullable")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("expense_transactions")}
    assert cols["amount_nis"]["nullable"] is True


def test_amount_nis_is_not_null_at_0021(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0021_household_expenses")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("expense_transactions")}
    assert cols["amount_nis"]["nullable"] is False


def test_downgrade_makes_amount_nis_not_null_again(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0022_expense_amount_nis_nullable")
    command.downgrade(cfg, "0021_household_expenses")
    engine = sa.create_engine(sync_url)
    insp = sa.inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("expense_transactions")}
    assert cols["amount_nis"]["nullable"] is False
