"""Schema-shape assertions after migration 0015.

Mirrors the pattern of tests/test_migration_0013.py: spin up a temp DB,
run alembic upgrade to head, assert columns + indexes + constraints
exist with the expected types.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect


def _columns(engine, table: str) -> dict[str, dict]:
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def _indexes(engine, table: str) -> list[dict]:
    insp = inspect(engine)
    return insp.get_indexes(table)


def test_0015_adds_lifecycle_columns(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    assert "role" in cols
    assert "accepted_at" in cols
    assert "accepted_by_user_id" in cols
    assert "superseded_at" in cols
    assert "derived_from_id" in cols
    assert "decision_run_id" in cols


def test_0015_adds_decision_kind_to_decision_runs(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "decision_runs")
    assert "decision_kind" in cols


def test_0015_adds_partial_unique_indexes(alembic_engine_at_head):
    """One baseline / current / draft per user — partial unique indexes."""
    idxs = {i["name"]: i for i in _indexes(alembic_engine_at_head, "plan_versions")}
    expected = {
        "uq_plan_versions_baseline_per_user",
        "uq_plan_versions_current_per_user",
        "uq_plan_versions_draft_per_user",
    }
    assert expected.issubset(idxs.keys()), f"missing partial unique indexes: {expected - idxs.keys()}"


def test_0015_role_default_is_baseline_for_existing_rows(
    alembic_engine_with_existing_plan_row,
):
    """Pre-existing plan_versions rows must be backfilled to role=baseline.

    Pre-0015 the table had implicit "all rows are baseline-ish" semantics.
    The migration must backfill role='baseline' so existing data still
    resolves to a usable plan.
    """
    eng = alembic_engine_with_existing_plan_row
    with eng.connect() as conn:
        rows = conn.execute(sa.text("SELECT role FROM plan_versions")).fetchall()
    assert all(r[0] == "baseline" for r in rows), rows
