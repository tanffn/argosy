"""Add ``payslip_facts`` — parsed Hilan payslip facts + §102 withholding verdict.

Backs the closed-loop "is my RSU (§102 equity) withholding adequate?" feature.
One row per ``(user_id, period_year, period_month)`` recording, for a single
monthly payslip:

  * ``source_file_id`` / ``source_sha256`` — the catalog row the raw PDF bytes
    were stored under (``argosy/services/file_catalog.py::catalog_upload``); the
    sha is the idempotency key so re-ingesting the same bytes updates in place.
  * ``parsed_json`` — the serialized :class:`PayslipFacts` (every YTD field +
    per-field confidence + warnings).
  * ``verdict_json`` — the serialized :class:`WithholdingVerdict` (status, the
    ₪ numbers, summary, caveats) so the latest verdict reads fast without
    re-parsing the PDF.

Idempotent re-ingest: ``(user_id, period_year, period_month)`` is UNIQUE; a
changed PDF for the same period (new sha) UPDATEs the row in place.

Mirrors the table-creation precedent of migration 0073 (plan_action_acks): FK
to users with ON DELETE CASCADE, idempotent create, real downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074_payslip_facts"
down_revision: str | None = "0073_plan_action_acks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "payslip_facts" in inspector.get_table_names():
        return  # idempotent — already applied out-of-band

    op.create_table(
        "payslip_facts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_year", sa.Integer, nullable=False),
        sa.Column("period_month", sa.Integer, nullable=False),
        sa.Column(
            "source_file_id",
            sa.Integer,
            sa.ForeignKey("user_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("parsed_json", sa.Text, nullable=False),
        sa.Column("verdict_json", sa.Text, nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "period_year",
            "period_month",
            name="uq_payslip_facts_user_period",
        ),
    )


def downgrade() -> None:
    op.drop_table("payslip_facts")
