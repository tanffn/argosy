"""anomaly_reports: persisted output of the AnomalyDetectionAgent (EX2).

Revision ID: 0038_anomaly_reports
Revises: 0037_fleet_self_review_reports
Create Date: 2026-05-27

Adds the ``anomaly_reports`` table.  One row per anomaly-check run —
fired automatically by either:

  * ``triggered_by='event'``  — after a Discount Bank statement ingests
    (event-driven path; see ``argosy/api/routes/expenses.py::upload_statements``
    and ``argosy/services/anomaly_runner.py::run_anomaly_check``).
    ``source_statement_id`` points at the triggering statement.
  * ``triggered_by='daily'``  — daily backstop alongside the daily
    brief (gated by ``ARGOSY_ANOMALY_DETECTION_ENABLED=1``).
    ``source_statement_id`` is NULL.
  * ``triggered_by='manual'`` — explicit ``POST /api/anomalies/run``
    from the UI / CLI.

``report_json`` is the AnomalyDetectionReport pydantic model serialized.
``severity_summary_json`` is the pre-joined ``{"RED": N, "AMBER": M,
"YELLOW": K}`` so the home-page banner doesn't have to parse the full
report on every request.

``agent_report_id`` is an optional back-link into ``agent_reports`` so
the audit surface can show the underlying LLM call (prompt, tokens,
cost) directly from the anomaly row.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_anomaly_reports"
down_revision: str | None = "0037_fleet_self_review_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "anomaly_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("triggered_by", sa.String(length=16), nullable=False),
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "source_statement_id",
            sa.Integer(),
            sa.ForeignKey("expense_statements.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("report_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "severity_summary_json",
            sa.Text(),
            nullable=False,
            server_default='{"RED":0,"AMBER":0,"YELLOW":0}',
        ),
        sa.Column(
            "agent_report_id",
            sa.Integer(),
            sa.ForeignKey("agent_reports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "triggered_by IN ('event','daily','manual')",
            name="ck_anomaly_reports_triggered_by",
        ),
    )
    op.create_index(
        "ix_anomaly_reports_user_triggered",
        "anomaly_reports",
        ["user_id", "triggered_at"],
    )
    op.create_index(
        "ix_anomaly_reports_source_statement",
        "anomaly_reports",
        ["source_statement_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_anomaly_reports_source_statement",
        table_name="anomaly_reports",
    )
    op.drop_index(
        "ix_anomaly_reports_user_triggered",
        table_name="anomaly_reports",
    )
    op.drop_table("anomaly_reports")
