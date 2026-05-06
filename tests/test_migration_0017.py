"""Schema assertions after migration 0017 (synthesis columns)."""

from sqlalchemy import inspect


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def test_0017_adds_horizon_columns(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    for name in (
        "horizon_long_json", "horizon_medium_json", "horizon_short_json",
        "horizon_long_md", "horizon_medium_md", "horizon_short_md",
        "synthesis_inputs_json",
    ):
        assert name in cols, f"missing {name}"


def test_0017_columns_are_nullable(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    for name in (
        "horizon_long_json", "horizon_medium_json", "horizon_short_json",
        "horizon_long_md", "horizon_medium_md", "horizon_short_md",
        "synthesis_inputs_json",
    ):
        assert cols[name]["nullable"] is True
