"""Append-only successful recovery of previously unscorable backfilled outcomes."""
import sqlalchemy as sa

from alembic import op

revision = "0121_prediction_data_recovery"
down_revision = "0120_prediction_scoring_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prediction_recovery_state",
        sa.Column("prediction_id", sa.Integer(), sa.ForeignKey("predictions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
    )
    for days in (7, 30, 180, 365):
        base = f"fixed_lookahead_{days}d"
        op.get_bind().execute(sa.text(
            "INSERT INTO evaluation_method_registry "
            "(method_name,family,method_version,is_active,scoring_contract,description) "
            "VALUES (:name,'fixed_lookahead',3,1,:base,:description)"
        ), {"name": base + "_entry_backfilled_recovered", "base": base,
            "description": "Successful data-availability retry; preserves prior unscorable outcomes"})


def downgrade() -> None:
    for days in (7, 30, 180, 365):
        name = f"fixed_lookahead_{days}d_entry_backfilled_recovered"
        if op.get_bind().execute(sa.text(
            "SELECT 1 FROM prediction_outcomes WHERE evaluation_method=:name LIMIT 1"
        ), {"name": name}).first():
            raise RuntimeError("Cannot remove recovery methods with historical outcomes")
    for days in (7, 30, 180, 365):
        op.get_bind().execute(sa.text(
            "DELETE FROM evaluation_method_registry WHERE method_name=:name"
        ), {"name": f"fixed_lookahead_{days}d_entry_backfilled_recovered"})
    op.drop_table("prediction_recovery_state")
