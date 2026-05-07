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
