"""Private Discord advisor durability and analysis-only policy."""
from alembic import op
import sqlalchemy as sa

revision = "0119_private_discord_advisor"
down_revision = "0118_shared_research_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decision_runs", sa.Column("execution_policy", sa.String(32), nullable=False, server_default="normal"))
    op.create_table(
        "chat_bindings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("channel_id", sa.String(32), nullable=False),
        sa.Column("provider_user_id", sa.String(32), nullable=False),
        sa.Column("household_user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("read_scope", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "guild_id", "channel_id", "provider_user_id", name="uq_chat_binding_identity"),
    )
    op.create_index("ix_chat_bindings_household", "chat_bindings", ["household_user_id", "enabled"])
    op.create_table(
        "chat_threads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("chat_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", sa.String(32), nullable=False),
        sa.Column("owner_provider_user_id", sa.String(32), nullable=False),
        sa.Column("recommendation_id", sa.String(128)),
        sa.Column("recommendation_version", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("binding_id", "thread_id", name="uq_chat_thread_binding"),
    )
    op.create_table(
        "chat_turns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("chat_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("household_user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_key", sa.String(96), nullable=False),
        sa.Column("inbound_message_id", sa.String(32), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("response", sa.Text()), sa.Column("response_nonce", sa.String(32)), sa.Column("response_message_ids_json", sa.Text(), nullable=False, server_default="[]"), sa.Column("citations_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"), sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("binding_id", "inbound_message_id", name="uq_chat_turn_inbound"),
        sa.UniqueConstraint("response_nonce", name="uq_chat_turn_response_nonce"),
    )
    op.create_index("ix_chat_turns_conversation", "chat_turns", ["household_user_id", "conversation_key", "created_at"])
    op.create_table(
        "chat_analysis_requests",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("binding_id", sa.Integer(), sa.ForeignKey("chat_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("household_user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inbound_message_id", sa.String(32), nullable=False), sa.Column("channel_id", sa.String(32), nullable=False),
        sa.Column("instruments_json", sa.Text(), nullable=False), sa.Column("prompt_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("run_ids_json", sa.Text(), nullable=False, server_default="[]"), sa.Column("result_json", sa.Text(), nullable=False, server_default="[]"), sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("progress_message_id", sa.String(32)), sa.Column("progress_nonce", sa.String(32)), sa.Column("progress_cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_of_id", sa.String(36), sa.ForeignKey("chat_analysis_requests.id", ondelete="SET NULL")), sa.Column("final_nonce", sa.String(32)), sa.Column("final_message_ids_json", sa.Text(), nullable=False, server_default="[]"), sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("binding_id", "inbound_message_id", name="uq_chat_analysis_inbound"),
    )
    op.create_index("ix_chat_analysis_active", "chat_analysis_requests", ["household_user_id", "status", "created_at"])
    op.create_table(
        "chat_agent_progress",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("request_id", sa.String(36), sa.ForeignKey("chat_analysis_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("decision_runs.id", ondelete="SET NULL")), sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("dedup_key", sa.String(128), nullable=False), sa.Column("agent", sa.String(128), nullable=False), sa.Column("state", sa.String(24), nullable=False),
        sa.Column("detail", sa.Text()), sa.Column("error", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("request_id", "sequence", name="uq_chat_progress_sequence"), sa.UniqueConstraint("request_id", "dedup_key", name="uq_chat_progress_dedup"),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("binding_id", sa.Integer(), sa.ForeignKey("chat_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("household_user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("semantic_key", sa.String(256), nullable=False), sa.Column("material_version", sa.String(64), nullable=False), sa.Column("category", sa.String(64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False), sa.Column("citations_json", sa.Text(), nullable=False, server_default="[]"), sa.Column("nonce", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"), sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_after", sa.DateTime(timezone=True)), sa.Column("sent_message_id", sa.String(32)), sa.Column("supersedes_message_id", sa.String(32)), sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("binding_id", "semantic_key", "material_version", name="uq_notification_material"), sa.UniqueConstraint("nonce", name="uq_notification_nonce"),
    )
    op.create_index("ix_notification_delivery", "notification_outbox", ["household_user_id", "status", "retry_after"])
    op.create_table(
        "chat_cursors", sa.Column("binding_id", sa.Integer(), sa.ForeignKey("chat_bindings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("channel_id", sa.String(32), nullable=False), sa.Column("last_message_id", sa.String(32)), sa.Column("gateway_session_id", sa.String(128)),
        sa.Column("gateway_sequence", sa.Integer()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("chat_cursors", "notification_outbox", "chat_agent_progress", "chat_analysis_requests", "chat_turns", "chat_threads", "chat_bindings"):
        op.drop_table(table)
    with op.batch_alter_table("decision_runs") as batch:
        batch.drop_column("execution_policy")
