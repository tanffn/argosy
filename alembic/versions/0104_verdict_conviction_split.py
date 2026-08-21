"""verdict conviction split — action_conviction vs forecast_confidence.

Ariel-directed rewrite of the per-position conviction model (2026-08-21): the
fleet was confusing uncertainty about ALPHA (forecast) with uncertainty about
ACTION (verdict/size/timing) — 31 of 38 live positions read HOLD/LOW
including NVDA at 57.9% against its own 13% policy cap, because a missing
return forecast was capping conviction on decisions that don't need one
(policy-cap breach, domicile/situs, sizing band).

``verdicts.conviction`` (existing column, unchanged) is now documented as
ACTION conviction. This migration adds four purely-additive, nullable columns
so the split is visible and auditable without breaking any existing reader:

  * ``forecast_confidence`` — confidence in the return/thesis call (may be
    NULL/irrelevant for a pure CONSTRAINT action).
  * ``decision_basis`` — CONSTRAINT | FORECAST | MIXED.
  * ``binding_rules_json`` — JSON list of rule ids that produced the action.
  * ``decision_inputs_json`` — JSON list of the necessary inputs consulted
    (name/value/source/necessary/confidence), the audit trail behind the
    action_conviction = min(...) floor.

Additive + nullable + reversible; every existing row reads unchanged (all
four new columns default NULL). See argosy/services/per_position_thesis.py.

Revision ID: 0104_verdict_conviction_split
Revises: 0103_instrument_classification
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0104_verdict_conviction_split"
down_revision: str | None = "0103_instrument_classification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("verdicts", sa.Column("forecast_confidence", sa.String(16), nullable=True))
    op.add_column("verdicts", sa.Column("decision_basis", sa.String(16), nullable=True))
    op.add_column("verdicts", sa.Column("binding_rules_json", sa.Text(), nullable=True))
    op.add_column("verdicts", sa.Column("decision_inputs_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("verdicts", "decision_inputs_json")
    op.drop_column("verdicts", "binding_rules_json")
    op.drop_column("verdicts", "decision_basis")
    op.drop_column("verdicts", "forecast_confidence")
