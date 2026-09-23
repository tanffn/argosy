"""Keep checkpoint horizons separate when selecting calibration outcomes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0120_prediction_scoring_contract"
down_revision = "0119_private_discord_advisor"
branch_labels = None
depends_on = None


def _previous_view_sql() -> str:
    path = Path(__file__).with_name("0052_source_reliability_view.py")
    spec = importlib.util.spec_from_file_location("reliability_view_0052", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._VIEW_SQL


def upgrade() -> None:
    old_sql = _previous_view_sql()
    current_sql = op.get_bind().execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='source_reliability'"
    )).scalar_one()
    if " ".join(current_sql.strip().rstrip(";").split()) != " ".join(old_sql.strip().rstrip(";").split()):
        raise RuntimeError("Unexpected reliability view definition; refusing to overwrite it")
    op.add_column("evaluation_method_registry", sa.Column("scoring_contract", sa.Text()))
    # Explicit compatibility metadata, not inferred from arbitrary method names.
    # New method versions must declare their contract to supersede another score.
    for days in (7, 30, 180, 365):
        base = f"fixed_lookahead_{days}d"
        op.get_bind().execute(sa.text(
            "UPDATE evaluation_method_registry SET scoring_contract=:contract "
            "WHERE method_name IN (:base, :backfilled)"
        ), {"contract": base, "base": base, "backfilled": base + "_entry_backfilled"})
    marker = "     AND r.is_active = 1"
    assert old_sql.count(marker) == 1
    new_sql = old_sql.replace("PARTITION BY o.prediction_id, r.family", "PARTITION BY o.prediction_id")
    new_sql = new_sql.replace(marker, marker + """
    JOIN predictions current_prediction ON current_prediction.id = o.prediction_id
    JOIN evaluation_method_registry current_method
      ON current_method.method_name = current_prediction.evaluation_method
    WHERE COALESCE(r.scoring_contract, r.method_name)
        = COALESCE(current_method.scoring_contract, current_method.method_name)""")
    op.execute("DROP VIEW source_reliability")
    op.execute(new_sql)


def downgrade() -> None:
    op.execute("DROP VIEW source_reliability")
    op.execute(_previous_view_sql())
    op.drop_column("evaluation_method_registry", "scoring_contract")
