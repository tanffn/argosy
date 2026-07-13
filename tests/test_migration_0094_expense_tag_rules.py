"""Migration 0094 expense_tag_rules — up / down / up probe."""

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
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    return Config("alembic.ini"), sync_url


def test_expense_tag_rules_up_down_up(alembic_cfg):
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    eng = sa.create_engine(sync_url)
    insp = sa.inspect(eng)
    assert "expense_tag_rules" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("expense_tag_rules")}
    assert {
        "id",
        "user_id",
        "match_merchant_normalized",
        "match_category_slug",
        "tag",
        "created_at",
    } <= cols
    eng.dispose()

    command.downgrade(cfg, "0093_instrument_plan_classes")
    eng = sa.create_engine(sync_url)
    assert "expense_tag_rules" not in sa.inspect(eng).get_table_names()
    eng.dispose()

    command.upgrade(cfg, "head")
    eng = sa.create_engine(sync_url)
    assert "expense_tag_rules" in sa.inspect(eng).get_table_names()
    eng.dispose()
