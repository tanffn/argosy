"""plan_versions distillate columns.

Revision ID: 0016_plan_versions_distillate
Revises: 0015_plan_versions_lifecycle
Create Date: 2026-05-06

Per spec §5.2: populated only when role=baseline. Synthesized rows
(role in {draft,current,superseded}) leave these NULL.

  - distillate_json: PlanDistillate pydantic JSON
  - distillate_rendered: pre-rendered markdown view (UI consumes)
  - source_hash: sha256 of raw_markdown — drives plan_watcher diff detection
  - distilled_at: when the last distill run completed

Note: source_path already exists on plan_versions (added pre-0001).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_plan_versions_distillate"
down_revision: str | Sequence[str] | None = "0015_plan_versions_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.add_column(sa.Column("distillate_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("distillate_rendered", sa.Text(), nullable=True))
        batch.add_column(sa.Column("source_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("distilled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("plan_versions") as batch:
        batch.drop_column("distilled_at")
        batch.drop_column("source_hash")
        batch.drop_column("distillate_rendered")
        batch.drop_column("distillate_json")
