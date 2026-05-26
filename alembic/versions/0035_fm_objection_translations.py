"""fm_objection_translations: precomputed plain-English FM objection cache.

Revision ID: 0035_fm_objection_translations
Revises: 0034_daily_briefs_runner
Create Date: 2026-05-26

Adds a durable cache table for the plain-English translations produced by
``ObjectionTranslatorAgent`` (see ``argosy/agents/objection_translator.py``).

Today the translator is invoked lazily when the user clicks "Explain in
plain English" on each FM objection card — a Sonnet call per click, each
costing the user ~5-10 s of wait.  After this migration the cache helper
``argosy/services/fm_objection_translation_cache.py`` precomputes all N
translations on the first hit of ``GET /api/plan/draft/objections`` for a
given draft (one parallel ``asyncio.gather`` batch), persists them, and
returns them inline on every subsequent load — the UI toggles between
"original FM wording" and "plain English" instantly with zero API calls.

Key columns:
  * ``plan_version_id`` — FK to the draft row; rows cascade-delete with
    the draft so we don't accumulate cache rows for superseded plans.
  * ``objection_index`` — the index of the objection in the sorted list
    returned by the endpoint (RED → AMBER → YELLOW, then encounter
    order).  Stable per (decision_run_id) since the FM emits a fixed
    list per synthesis run.
  * ``topic_hash`` — sha256 of ``(severity, topic, detail)``.  Defense in
    depth: if FM produces the same plan_version_id with a different
    objection set (shouldn't happen — objections are tied to the
    backing ``decision_run_id`` which is immutable per draft), the
    hash mismatch causes the helper to re-translate that slot rather
    than returning stale text.
  * ``headline`` / ``plain_english`` / ``recommended_actions_json``:
    the three fields of ``ObjectionTranslation`` (recommended_actions
    is JSON-serialised since SQLite has no native list type).

Unique constraint on ``(plan_version_id, objection_index)`` so the
cache helper can use ``ON CONFLICT`` style upserts safely.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_fm_objection_translations"
down_revision: str | None = "0034_daily_briefs_runner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fm_objection_translations",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False
        ),
        sa.Column(
            "plan_version_id",
            sa.Integer(),
            sa.ForeignKey("plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("objection_index", sa.Integer(), nullable=False),
        sa.Column("topic_hash", sa.String(length=64), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("plain_english", sa.Text(), nullable=False),
        sa.Column("recommended_actions_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "plan_version_id",
            "objection_index",
            name="uq_fm_objection_translations_plan_idx",
        ),
    )
    op.create_index(
        "ix_fm_objection_translations_plan_version",
        "fm_objection_translations",
        ["plan_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fm_objection_translations_plan_version",
        table_name="fm_objection_translations",
    )
    op.drop_table("fm_objection_translations")
