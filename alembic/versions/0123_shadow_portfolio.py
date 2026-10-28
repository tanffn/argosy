"""Immutable whole-liquid-portfolio comparison ledger."""
import sqlalchemy as sa

from alembic import op

revision = "0123_shadow_portfolio"
down_revision = "0122_fill_book_application"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shadow_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("cohort_id", sa.String(64), nullable=False),
        sa.Column("event_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("user_id", "cohort_id", "event_key", name="uq_shadow_event_identity"))
    op.create_index("ix_shadow_events_user_id", "shadow_events", ["user_id"])
    op.create_index("ix_shadow_events_cohort_id", "shadow_events", ["cohort_id"])
    for operation in ("UPDATE", "DELETE"):
        op.execute(f"CREATE TRIGGER trg_shadow_events_no_{operation.lower()} BEFORE {operation} "
                   "ON shadow_events BEGIN SELECT RAISE(ABORT, 'append-only: shadow_events is immutable'); END")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM shadow_events LIMIT 1")).first():
        raise RuntimeError("Cannot discard shadow portfolio audit history")
    for operation in ("update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_shadow_events_no_{operation}")
    op.drop_table("shadow_events")
