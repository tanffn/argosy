"""Durable settlement and exactly-once receipt application journal."""
import sqlalchemy as sa

from alembic import op

revision = "0122_fill_book_application"
down_revision = "0121_prediction_data_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("fills", sa.Column("execution_time_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("fills", sa.Column("commission_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("fills", sa.Column("native_account_id", sa.String(64), nullable=True))
    op.add_column("fills", sa.Column("price_currency", sa.String(8), nullable=True))
    op.add_column("fills", sa.Column("commission_currency", sa.String(8), nullable=True))
    op.add_column("pending_orders", sa.Column("receipt_sync_error", sa.Text(), nullable=True))
    op.create_table(
        "fill_book_applications",
        sa.Column("fill_id", sa.Integer(), sa.ForeignKey("fills.id"), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("settlement_json", sa.Text(), nullable=True),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("base_snapshot_id", sa.Integer(), sa.ForeignKey("portfolio_snapshots.id"), nullable=True),
        sa.Column("applied_snapshot_id", sa.Integer(), sa.ForeignKey("portfolio_snapshots.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_fill_book_applications_user_id", "fill_book_applications", ["user_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM fill_book_applications LIMIT 1")).first():
        raise RuntimeError("Cannot discard fill application audit history")
    op.drop_table("fill_book_applications")
    op.drop_column("fills", "execution_time_confirmed")
    op.drop_column("fills", "commission_confirmed")
    op.drop_column("fills", "native_account_id")
    op.drop_column("fills", "price_currency")
    op.drop_column("fills", "commission_currency")
    op.drop_column("pending_orders", "receipt_sync_error")
