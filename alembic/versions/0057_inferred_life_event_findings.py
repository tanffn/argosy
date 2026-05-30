"""inferred_life_event_findings — detector ledger for phase-change proposals.

Revision ID: 0057_inferred_life_event_findings
Revises: 0056_replan_dispatch_log
Create Date: 2026-05-30

Spec E (last-mile delivery) commit #5 — the inferred-life-event
detector's persistent ledger.  See
``docs/superpowers/specs/2026-05-29-last-mile-delivery-design.md``:

* §5 — inferred_life_event_detector architecture (heuristic-first,
  LLM-augmented).
* §5.1 — five heuristic detectors (tuition_stopped,
  recurring_car_purchase, wedding_scale_transfer,
  recurring_renovation, kid_started_college; +
  ``phase_drop_other`` reserved for v1.1 partner_change / catch-all).
* §5.4 — false-positive control + the five guardrails, including the
  codex BLOCKER #3 pre-proposal conflict resolver outcome that the
  ``conflict_resolution`` column records.
* §5.5 / Appendix A.9 — DDL.
* §9 commit table row #5.

Schema shape vs the full spec
-----------------------------

The table is the audit + idempotency surface for the detector.  Each
row records ONE finding — one (pattern, evidence_window) tuple per
user.  The action_proposer is fired separately (via
``run_action_proposer_for_inferred_event``) when the finding's status
moves to ``proposed``; the action_proposals row's PK is FK'd back via
``proposed_action_id``.

Spec note vs the prompt's schema: the prompt asked for ``heuristic_kind``
+ ``confidence`` REAL.  The spec (§5.5 / Appendix A.9) uses
``pattern`` + ``heuristic_confidence`` ENUM(high/medium/low) — same
information, more aligned with the agent's existing
``InferredEventTrigger.pattern`` literal.  We use the spec shape so
``argosy/services/action_proposer_runner.run_action_proposer_for_
inferred_event`` can read ``finding.pattern`` directly without a
field-name remap.  ``confidence`` is the heuristic confidence band;
``llm_confirmed`` is the disambiguator's outcome (NULL when the
heuristic was high-confidence and skipped the LLM).

Five guardrails ledgered here
-----------------------------

* ``conflict_resolution`` (codex BLOCKER #3 / spec §5.4 guardrail #5)
  records the pre-proposal conflict resolver's outcome:

  - ``aliased_pair_suppressed`` — tuition_stopped + kid_started_college
    on the same counterparty (re-categorisation masked as life event);
    BOTH findings dismissed.
  - ``aliased_pair_disambiguator_required`` — overlapping windows but
    no stable counterparty match; sent through the LLM
    disambiguator to decide.
  - ``superseded_by_user_event`` — wedding_scale_transfer within 30
    days of a user-logged ``family_event:marriage`` LifeEvent; finding
    suppressed.
  - ``no_conflict`` — checked, nothing fired.

* The ``llm_confirmed`` column captures guardrail #2 (LLM
  disambiguator gate); ``dismissed`` records the final pre-proposer
  outcome regardless of which guardrail dismissed.

* The UNIQUE(user_id, pattern, evidence_window_start,
  evidence_window_end) index is guardrail-style idempotency: re-running
  the detector on the same window cannot insert a duplicate finding.

Column shape
------------

* ``id`` PK auto-increment.
* ``user_id`` FK -> users.id ON DELETE CASCADE (per-tenant scoping;
  losing a user wipes their detector history).
* ``pattern`` TEXT NOT NULL CHECK enum — six values (the five v1
  heuristics + ``phase_drop_other`` reserved).
* ``heuristic_confidence`` TEXT NOT NULL CHECK enum (high/medium/low).
* ``llm_confirmed`` BOOLEAN NULL — NULL=disambiguator not run
  (high-confidence heuristic); TRUE=confirmed; FALSE=dismissed by LLM.
* ``dismissed`` BOOLEAN NOT NULL DEFAULT 0 — final pre-proposer flag.
* ``evidence_window_start`` / ``evidence_window_end`` DATE NOT NULL —
  the rolling-window bounds the heuristic examined.
* ``evidence_transaction_ids`` TEXT NOT NULL CHECK json_valid — JSON
  list of ``expense_transactions.id`` values the heuristic cited.
* ``evidence_summary`` TEXT NOT NULL — human-readable summary the
  proposer's prompt + the UI consume.
* ``proposed_action_id`` INTEGER NULL FK -> action_proposals.id
  ON DELETE SET NULL — populated AFTER the proposer materialises
  the finding into an action proposal.  Losing the proposal (housekeep
  sweep) must NOT cascade away the finding's audit trail.
* ``conflict_resolution`` TEXT NULL CHECK enum (4 values + NULL) —
  see guardrails section above.
* ``detected_at`` DATETIME default CURRENT_TIMESTAMP — when the
  detector run that produced this row started.

SQLite version requirement: ``json_valid()`` (>= 3.38) — already a
project baseline.  Partial-UNIQUE not needed here; the UNIQUE
constraint is unconditional (the natural-key uniqueness is the
detector's idempotency contract, NOT a write-orchestrated tombstone).

Downgrade
---------

Drops the table + index.  No data preservation — fresh in this
migration.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_inferred_life_event_findings"
down_revision: str | None = "0056_replan_dispatch_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Spec §5.5 — six pattern values.  ``phase_drop_other`` is reserved
# for v1.1 (partner_change stub + future catch-all).  Mirrors the
# literal in ``argosy/agents/action_proposer.py::InferredEventTrigger.
# pattern`` — a test asserts the two stay aligned.
_VALID_PATTERNS: tuple[str, ...] = (
    "tuition_stopped",
    "recurring_car_purchase",
    "wedding_scale_transfer",
    "recurring_renovation",
    "kid_started_college",
    "phase_drop_other",
)


# Heuristic confidence band (spec §5.3 confidence rule column).
_VALID_HEURISTIC_CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")


# Conflict-resolver outcomes (spec §5.4 guardrail #5; codex BLOCKER #3).
_VALID_CONFLICT_RESOLUTIONS: tuple[str, ...] = (
    "aliased_pair_suppressed",
    "aliased_pair_disambiguator_required",
    "superseded_by_user_event",
    "no_conflict",
)


def _quoted_csv(values: Sequence[str]) -> str:
    return ", ".join(repr(v) for v in values)


def upgrade() -> None:
    op.create_table(
        "inferred_life_event_findings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Pattern identity — discriminator for the six v1 patterns.
        # CHECK enforced.  Matches InferredEventTrigger.pattern literal.
        sa.Column("pattern", sa.Text, nullable=False),
        # Heuristic-layer confidence (high / medium / low).  CHECK
        # enforced.  Drives whether the LLM disambiguator fires (spec
        # §5.2 — medium / low -> LLM).
        sa.Column("heuristic_confidence", sa.Text, nullable=False),
        # LLM disambiguator outcome.  NULL = disambiguator NOT run
        # (high-confidence heuristic skipped the LLM per spec §5.2).
        # TRUE = confirmed; FALSE = dismissed by LLM.
        sa.Column("llm_confirmed", sa.Boolean, nullable=True),
        # Final pre-proposer flag.  TRUE = no action_proposals row
        # was queued for this finding (any guardrail dismissed it).
        sa.Column(
            "dismissed",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Evidence window bounds (calendar dates per spec §5.5).
        sa.Column("evidence_window_start", sa.Date, nullable=False),
        sa.Column("evidence_window_end", sa.Date, nullable=False),
        # JSON list of expense_transactions.id — the heuristic's
        # citation surface.  json_valid CHECK below.
        sa.Column("evidence_transaction_ids", sa.Text, nullable=False),
        # Human-readable evidence summary (prompt input + UI text).
        sa.Column("evidence_summary", sa.Text, nullable=False),
        # FK to the materialised proposal once the proposer fires.
        # ON DELETE SET NULL — losing the proposal must NOT cascade
        # away the finding's audit trail.
        sa.Column(
            "proposed_action_id",
            sa.Integer,
            sa.ForeignKey("action_proposals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Conflict-resolver outcome (codex BLOCKER #3 / spec §5.4).
        # NULL = no conflict checks ran yet (the resolver runs AFTER
        # heuristics but BEFORE the proposer); the four enum values
        # cover the four guardrail #5 outcomes.
        sa.Column("conflict_resolution", sa.Text, nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # ----- CHECK constraints (declared inline; SQLite materialises
        # them into CREATE TABLE so a future batch-rebuild round-trips
        # them faithfully). -----
        sa.CheckConstraint(
            "pattern IN (" + _quoted_csv(_VALID_PATTERNS) + ")",
            name="ck_inferred_findings_pattern",
        ),
        sa.CheckConstraint(
            "heuristic_confidence IN ("
            + _quoted_csv(_VALID_HEURISTIC_CONFIDENCES)
            + ")",
            name="ck_inferred_findings_heuristic_confidence",
        ),
        sa.CheckConstraint(
            "conflict_resolution IS NULL OR conflict_resolution IN ("
            + _quoted_csv(_VALID_CONFLICT_RESOLUTIONS)
            + ")",
            name="ck_inferred_findings_conflict_resolution",
        ),
        sa.CheckConstraint(
            "json_valid(evidence_transaction_ids)",
            name="ck_inferred_findings_evidence_tx_ids_json_valid",
        ),
    )

    # Idempotent re-detection per (user, pattern, window).  The
    # natural-key uniqueness — re-running the detector with the same
    # window-and-pattern combination MUST NOT insert a duplicate.
    # See spec §5.5 / Appendix A.9 unique-index clause.
    op.create_index(
        "ix_inferred_findings_pattern_evidence",
        "inferred_life_event_findings",
        [
            "user_id",
            "pattern",
            "evidence_window_start",
            "evidence_window_end",
        ],
        unique=True,
    )

    # Hot-path index: per-user, most-recent-first detector audit.
    # Feeds the (future commit #6) /proposals UI "show me what the
    # detector saw" debug surface.
    op.create_index(
        "ix_inferred_findings_user_detected",
        "inferred_life_event_findings",
        ["user_id", sa.text("detected_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inferred_findings_user_detected",
        table_name="inferred_life_event_findings",
    )
    op.drop_index(
        "ix_inferred_findings_pattern_evidence",
        table_name="inferred_life_event_findings",
    )
    op.drop_table("inferred_life_event_findings")
