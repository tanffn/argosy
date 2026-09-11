"""Add YouTube input sources, videos, claims, and claim evaluations.

Revision ID: 0116_youtube_influencer_intelligence
Revises: 0115_prediction_benchmark_outcomes
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0116_youtube_influencer_intelligence"
down_revision: str | None = "0115_prediction_benchmark_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "youtube_channels",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("youtube_channel_id", sa.String(128), nullable=False),
        sa.Column("channel_name", sa.String(255), nullable=False),
        sa.Column("channel_url", sa.String(1024), nullable=False),
        sa.Column("enabled", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("last_seen_video_id", sa.String(32)),
        sa.Column("last_seen_published_at", sa.DateTime(timezone=True)),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("user_id", "youtube_channel_id", name="uq_youtube_channel_user"),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_youtube_channels_enabled"),
    )
    op.create_index("ix_youtube_channels_user_enabled", "youtube_channels", ["user_id", "enabled"])
    op.create_table(
        "youtube_videos",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "channel_id", sa.Integer, sa.ForeignKey("youtube_channels.id", ondelete="SET NULL")
        ),
        sa.Column("youtube_video_id", sa.String(32), nullable=False),
        sa.Column("url", sa.String(1024), nullable=False),
        sa.Column("title", sa.String(512)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "transcript_file_id", sa.Integer, sa.ForeignKey("user_files.id", ondelete="SET NULL")
        ),
        sa.Column("analysis_run_id", sa.String(128)),
        sa.Column("artifact_path", sa.String(1024)),
        sa.Column("tickers_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("recommendations_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column(
            "analyzed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "youtube_video_id", name="uq_youtube_video_user"),
        sa.CheckConstraint("json_valid(tickers_json)", name="ck_youtube_videos_tickers_json"),
        sa.CheckConstraint(
            "json_valid(recommendations_json)", name="ck_youtube_videos_recommendations_json"
        ),
    )
    op.create_index(
        "ix_youtube_videos_channel_analyzed", "youtube_videos", ["channel_id", "analyzed_at"]
    )
    op.create_table(
        "youtube_claims",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "video_id",
            sa.Integer,
            sa.ForeignKey("youtube_videos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("claim_key", sa.String(64), nullable=False),
        sa.Column("timestamp", sa.String(16)),
        sa.Column("claim_type", sa.String(32), nullable=False),
        sa.Column("statement", sa.Text, nullable=False),
        sa.Column("evidence_excerpt", sa.Text, nullable=False, server_default=""),
        sa.Column("named_entities_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("tickers_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("is_market_outlook", sa.Integer, nullable=False, server_default="0"),
        sa.Column("direction", sa.String(16)),
        sa.Column("horizon_days", sa.Integer),
        sa.Column("evaluation_due_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("video_id", "claim_key", name="uq_youtube_claim_video_key"),
        sa.CheckConstraint(
            "json_valid(named_entities_json)", name="ck_youtube_claim_entities_json"
        ),
        sa.CheckConstraint("json_valid(tickers_json)", name="ck_youtube_claim_tickers_json"),
        sa.CheckConstraint("is_market_outlook IN (0, 1)", name="ck_youtube_claim_market_bool"),
        sa.CheckConstraint(
            "status IN ('open', 'evaluated', 'unscored')", name="ck_youtube_claim_status"
        ),
    )
    op.create_index("ix_youtube_claims_due", "youtube_claims", ["status", "evaluation_due_at"])
    op.create_table(
        "youtube_claim_evaluations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "claim_id",
            sa.Integer,
            sa.ForeignKey("youtube_claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(24), nullable=False),
        sa.Column("score", sa.Float),
        sa.Column("subject_return_pct", sa.Float),
        sa.Column("benchmark_return_pct", sa.Float),
        sa.Column("excess_return_pct", sa.Float),
        sa.Column("evidence_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("evaluator_version", sa.String(64), nullable=False),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("claim_id", "evaluator_version", name="uq_youtube_claim_eval_version"),
        sa.CheckConstraint(
            "verdict IN ('correct', 'partially_correct', 'incorrect', 'inconclusive')",
            name="ck_youtube_claim_eval_verdict",
        ),
        sa.CheckConstraint("json_valid(evidence_json)", name="ck_youtube_claim_eval_evidence_json"),
    )
    op.create_index(
        "ix_youtube_claim_evaluations_evaluated", "youtube_claim_evaluations", ["evaluated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_youtube_claim_evaluations_evaluated", table_name="youtube_claim_evaluations")
    op.drop_table("youtube_claim_evaluations")
    op.drop_index("ix_youtube_claims_due", table_name="youtube_claims")
    op.drop_table("youtube_claims")
    op.drop_index("ix_youtube_videos_channel_analyzed", table_name="youtube_videos")
    op.drop_table("youtube_videos")
    op.drop_index("ix_youtube_channels_user_enabled", table_name="youtube_channels")
    op.drop_table("youtube_channels")
