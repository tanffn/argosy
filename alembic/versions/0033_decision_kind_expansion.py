"""decision_kind taxonomy expansion: delta_pushback + daily_brief (T4.4).

Revision ID: 0033_decision_kind_expansion
Revises: 0031_decision_phases_output_json
Create Date: 2026-05-26

NO SCHEMA CHANGE. This migration is documentation-only.

``decision_runs.decision_kind`` has always been a free-text ``String(32)``
column with no CHECK constraint or enum binding — see
``argosy/state/models.py``::DecisionRun. We verified that before writing
this revision: ``grep -n 'decision_kind' argosy/state/models.py`` shows a
plain ``Mapped[str]`` column, and the SQLite table reflection in dev
confirms no ``CHECK`` clause is attached.

The taxonomy is enforced application-side. Up through migration 0031 the
recognized values were:

  * ``trade_proposal``      — per-trade decision flow (Phase 3 / legacy)
  * ``plan_revision``       — full 5-phase plan-synthesis flow
  * ``plan_amendment_chat`` — Wave 4 chat-driven amendment flow

T4.4 extends the taxonomy to include two new kinds that will be produced
by tasks shipping later in the Tier 3 + Tier 4 wave:

  * ``delta_pushback`` — T4.3: per-delta slim re-debate triggered when
    the user pushes back on a single PlanDeltaItem. The decision_run row
    carries ``notes_json`` keyed on the delta_item_id being disputed.
    Smaller than a full synthesis: typically 3-5 agent_reports per run
    (bull, bear, plan_synthesizer scoped to the item).

  * ``daily_brief`` — T4.5: daily-brief generation run, one per
    user-day. Emits a single brief artifact rather than a proposal or
    plan revision. ``notes_json`` carries the brief date (ISO-8601) and
    the source-event correlation ids.

Both kinds participate in /api/decisions/recent and /api/decisions/{id}
without further schema work — the existing agent_reports + decision_phases
tables carry their per-agent traces, and the UI's row renderer (T4.4)
branches on decision_kind to surface the right summary text.

T4.5 will land migration 0034 (daily_brief_briefs table or equivalent
artifact storage). T4.4 itself does NOT need a table — it only validates
that the taxonomy expansion is recognised by ``decisions.py`` route
filters and ``agent_tree_builder.build_agent_tree``.

If a future migration adds a CHECK constraint or moves decision_kind to
an enum, that migration MUST include both ``delta_pushback`` and
``daily_brief`` in the allowed set.
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0033_decision_kind_expansion"
down_revision: str | None = "0031_decision_phases_output_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op. Taxonomy expansion is documentation-only — decision_kind is
    already free-text. See module docstring for the full rationale.

    Intentionally no-op so the Alembic chain advances and downstream
    migrations (0034 daily_brief table for T4.5) can extend from here.
    """


def downgrade() -> None:
    """No-op — nothing to undo for a documentation-only revision."""
