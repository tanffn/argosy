"""derivation-graph persistence + replay trace

plan_nodes / plan_edges persist a DerivationGraph; change_requests /
dialogue_turns are the change-substrate schema (populated in Phase 2);
propagation_events records the per-change blast-radius ripple for replay.

Revision ID: 0071_derivation_graph_persistence
Revises: 0070_tax_simulation_lots
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0071_derivation_graph_persistence"
down_revision: str | None = "0070_tax_simulation_lots"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "plan_nodes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_key", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("status_validity", sa.String(length=8), nullable=False, server_default="stale"),
        sa.Column("status_flag", sa.String(length=8), nullable=False, server_default="none"),
        sa.Column("provenance_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("owner", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("compute_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plan_nodes_plan_id", "plan_nodes", ["plan_id"])
    op.create_index("ix_plan_nodes_plan_key", "plan_nodes", ["plan_id", "node_key"], unique=True)

    op.create_table(
        "plan_edges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_node_key", sa.String(length=256), nullable=False),
        sa.Column("to_node_key", sa.String(length=256), nullable=False),
        sa.Column("edge_kind", sa.String(length=16), nullable=False, server_default="named"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plan_edges_plan_id", "plan_edges", ["plan_id"])
    op.create_index(
        "ix_plan_edges_plan_from_to", "plan_edges",
        ["plan_id", "from_node_key", "to_node_key", "edge_kind"], unique=True,
    )

    op.create_table(
        "change_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_node_key", sa.String(length=256), nullable=False),
        sa.Column("author", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="proposed"),
        sa.Column("round_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("adjudicated_by", sa.String(length=64), nullable=True),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_change_requests_plan_id", "change_requests", ["plan_id"])

    op.create_table(
        "dialogue_turns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("change_request_id", sa.Integer(), sa.ForeignKey("change_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("speaker", sa.String(length=16), nullable=False),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("cited_nodes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dialogue_turns_change_request_id", "dialogue_turns", ["change_request_id"])

    op.create_table(
        "propagation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), nullable=False),
        sa.Column("trigger_node_key", sa.String(length=256), nullable=False),
        sa.Column("invalidated_node_keys_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("recomputed_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("rerendered_surfaces_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("verification_verdicts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_propagation_events_plan_id", "propagation_events", ["plan_id"])
    op.create_index("ix_propagation_events_plan_cycle", "propagation_events", ["plan_id", "cycle_id"])


def downgrade() -> None:
    op.drop_index("ix_propagation_events_plan_cycle", table_name="propagation_events")
    op.drop_index("ix_propagation_events_plan_id", table_name="propagation_events")
    op.drop_table("propagation_events")

    op.drop_index("ix_dialogue_turns_change_request_id", table_name="dialogue_turns")
    op.drop_table("dialogue_turns")

    op.drop_index("ix_change_requests_plan_id", table_name="change_requests")
    op.drop_table("change_requests")

    op.drop_index("ix_plan_edges_plan_from_to", table_name="plan_edges")
    op.drop_index("ix_plan_edges_plan_id", table_name="plan_edges")
    op.drop_table("plan_edges")

    op.drop_index("ix_plan_nodes_plan_key", table_name="plan_nodes")
    op.drop_index("ix_plan_nodes_plan_id", table_name="plan_nodes")
    op.drop_table("plan_nodes")
