"""Durable allocation disputes, independent of trade approval or PC uptime."""
import sqlalchemy as sa
from alembic import op

revision = "0117_allocation_research_tasks"
down_revision = "0116_youtube_influencer_intelligence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "allocation_research_tasks",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("issue_key", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("next_review_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.Column("result_json", sa.Text()),
        sa.Column("last_error", sa.Text()),
    )


def downgrade():
    op.drop_table("allocation_research_tasks")
