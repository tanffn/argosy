"""Schema assertions after migration 0016 (distillate columns)."""

from sqlalchemy import inspect


def _columns(engine, table):
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table)}


def test_0016_adds_distillate_columns(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    for name in ("distillate_json", "distillate_rendered", "source_hash", "distilled_at"):
        assert name in cols, f"expected column {name} on plan_versions, got {sorted(cols)}"


def test_0016_columns_are_nullable(alembic_engine_at_head):
    cols = _columns(alembic_engine_at_head, "plan_versions")
    # All distillate columns are populated only on role=baseline rows;
    # synthesized rows leave them NULL.
    for name in ("distillate_json", "distillate_rendered", "source_hash", "distilled_at"):
        assert cols[name]["nullable"] is True
