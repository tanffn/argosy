"""Schema assertions after migration 0018 (decision_runs amendment columns).

Deviation from the Wave 4 plan: the plan called for ``add_column("tier", ...)``
on ``decision_runs``, but a ``tier`` column already exists from migration 0004
(``String(4)``, ``NOT NULL``, used for trade-proposal T-tier sentinels like
"T0"/"T3"). Migration 0018 therefore *widens* the existing column to
``String(8)`` and makes it nullable so it can also carry the amendment-tier
values "small"/"medium"/"large" used by the plan-amendment-chat flow. The
column-existence + nullability + type assertions below match either path.
"""

from sqlalchemy import inspect


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def test_0018_adds_tier_column(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "decision_runs")
    assert "tier" in cols
    assert cols["tier"]["nullable"] is True
    assert "VARCHAR" in str(cols["tier"]["type"]).upper() or "TEXT" in str(cols["tier"]["type"]).upper()


def test_0018_adds_notes_json_column(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "decision_runs")
    assert "notes_json" in cols
    assert cols["notes_json"]["nullable"] is True


def test_0018_creates_partial_unique_index(alembic_engine_at_head):
    insp = inspect(alembic_engine_at_head)
    idx_names = {i["name"] for i in insp.get_indexes("decision_runs")}
    assert "ix_decision_runs_one_amendment_running_per_user" in idx_names


def test_0018_preserves_existing_tier_values(tmp_path, monkeypatch):
    """M7: pre-existing decision_runs rows with tier='T3' (or other T-tier
    values) must survive the 0017→0018 widen-and-relax of the tier
    column. Without this, every existing trade-flow run would lose its
    tier on upgrade.
    """
    import os

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings
    reload_settings()
    db_url = get_settings().database_url
    sync_url = db_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    cfg = Config("alembic.ini")
    # Stop one migration short of head so we can insert a pre-0018 row.
    command.upgrade(cfg, "0017_plan_versions_synthesis")

    eng = create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO users (id, plan, created_at) VALUES "
                "('ariel', 'free', :now)"
            ), {"now": "2026-01-01"})
            conn.execute(sa.text(
                "INSERT INTO decision_runs "
                "(user_id, ticker, tier, status, decision_kind, started_at) "
                "VALUES ('ariel', 'NVDA', 'T3', 'completed', "
                "'plan_revision', :now)"
            ), {"now": "2026-02-01"})

        # Now upgrade to head (which runs 0018).
        command.upgrade(cfg, "head")

        with eng.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT tier FROM decision_runs WHERE user_id='ariel' "
                "AND ticker='NVDA'"
            )).first()
        assert row is not None, "row vanished during 0018 upgrade"
        assert row[0] == "T3", (
            f"tier value lost during 0018 widen; got {row[0]!r}"
        )
    finally:
        eng.dispose()
