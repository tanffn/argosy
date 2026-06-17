# argosy/quality/coherence/ledger.py
"""Persist / supersede / load coherence rulings. Active = not superseded. A
replacement supersedes the prior only AFTER it is written (callers conform+verify
before calling supersede), so the old invariant stays enforced until replaced."""
from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa

from argosy.state.models import CoherenceDecision


def record_ruling(
    session, *, user_id: str, decision_run_id: int | None, dispute_key: str,
    subject_type: str, question: str, ruling: str, rationale: str, basis: str,
    resolved_by: str, invariants: list[dict[str, Any]], conformed_surfaces: list[str],
) -> CoherenceDecision:
    row = CoherenceDecision(
        user_id=user_id, decision_run_id=decision_run_id, dispute_key=dispute_key,
        subject_type=subject_type, question=question, ruling=ruling, rationale=rationale,
        basis=basis, resolved_by=resolved_by,
        coherence_invariant_json=json.dumps(invariants, ensure_ascii=False),
        conformed_surfaces_json=json.dumps(conformed_surfaces, ensure_ascii=False),
    )
    session.add(row); session.commit()
    return row


def load_active_rulings(session, *, user_id: str) -> list[CoherenceDecision]:
    return list(
        session.execute(
            sa.select(CoherenceDecision).where(
                CoherenceDecision.user_id == user_id,
                CoherenceDecision.superseded_by_id.is_(None),
            ).order_by(CoherenceDecision.id)
        ).scalars()
    )


def supersede(session, *, old_id: int, new_id: int) -> None:
    row = session.get(CoherenceDecision, old_id)
    if row is not None:
        row.superseded_by_id = new_id
        session.commit()


def invariants_of(row: CoherenceDecision) -> list[dict[str, Any]]:
    try:
        return json.loads(row.coherence_invariant_json or "[]")
    except json.JSONDecodeError:
        return []
