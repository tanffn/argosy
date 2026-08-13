"""Persist and read promotion gate outcomes.

The plan-synthesis orchestrator builds a list of ``GateOutcome`` objects at
the point it evaluates whether a draft can be promoted.  This module writes
them to ``gate_outcomes`` (migration 0102) and reads them back for the
API surface.

Design contracts:
- **Best-effort write**: a DB failure during ``persist_gate_outcomes`` MUST
  NOT crash the caller.  It logs at ERROR level so the failure is visible in
  logs / verify-run; silent failure is the exact antipattern this workstream
  exists to kill.
- **Re-run safe**: if the same (decision_run_id, gate) pair is written twice
  (e.g. a retry), the old row is deleted and replaced so the latest state wins.
- **Read path**: ``get_gate_outcomes`` returns ``GateOutcome`` dataclass
  instances; ``get_gate_receipt`` returns the outcomes plus the one-line
  summary produced by ``argosy.quality.verification.summarize``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from argosy.quality.verification import GateOutcome, GateStatus, summarize

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def persist_gate_outcomes(
    session: "Session",
    decision_run_id: int,
    outcomes: list[GateOutcome],
) -> None:
    """Write gate outcomes to the DB.  Best-effort — logs loudly on failure.

    Each (decision_run_id, gate) pair is upserted: any existing row for the
    same pair is deleted before the new one is inserted, so a re-run always
    reflects the latest state.

    A failure here MUST NOT propagate to the caller.  The caller's work
    (draft promotion / approval) must proceed even when the receipt table is
    unavailable.  But the failure is logged at ERROR so ``verify-run`` and
    the ops log can surface it — do not demote to WARNING.
    """
    from sqlalchemy import delete

    from argosy.state.models import GateOutcomeRow

    if not outcomes:
        return

    try:
        gate_names = [o.gate for o in outcomes]
        # Delete any stale rows for this run in one shot.
        session.execute(
            delete(GateOutcomeRow).where(
                GateOutcomeRow.decision_run_id == decision_run_id,
                GateOutcomeRow.gate.in_(gate_names),
            )
        )
        for outcome in outcomes:
            session.add(
                GateOutcomeRow(
                    decision_run_id=decision_run_id,
                    gate=outcome.gate,
                    status=str(outcome.status),
                    detail=outcome.detail,
                    override_by=outcome.override_by,
                    override_reason=outcome.override_reason,
                    meta_json=json.dumps(outcome.meta) if outcome.meta else "{}",
                )
            )
        session.commit()
    except Exception as exc:  # noqa: BLE001
        log.error(
            "gate_outcomes.persist_failed decision_run_id=%s err=%s",
            decision_run_id,
            exc,
            exc_info=True,
        )
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass


def get_gate_outcomes(
    session: "Session",
    decision_run_id: int,
) -> list[GateOutcome]:
    """Return persisted gate outcomes for a decision run, oldest-first.

    Returns an empty list when no rows exist (no synthesis has ever written
    outcomes for this run, or the table was unavailable during the run).
    Never raises.
    """
    from sqlalchemy import select

    from argosy.state.models import GateOutcomeRow

    try:
        rows = session.execute(
            select(GateOutcomeRow)
            .where(GateOutcomeRow.decision_run_id == decision_run_id)
            .order_by(GateOutcomeRow.created_at)
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "gate_outcomes.read_failed decision_run_id=%s err=%s",
            decision_run_id,
            exc,
        )
        return []

    result: list[GateOutcome] = []
    for row in rows:
        try:
            meta = json.loads(row.meta_json or "{}") if row.meta_json else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        try:
            result.append(
                GateOutcome(
                    gate=row.gate,
                    status=GateStatus(row.status),
                    detail=row.detail or "",
                    override_by=row.override_by,
                    override_reason=row.override_reason,
                    meta=meta,
                )
            )
        except Exception as exc:  # noqa: BLE001 — bad row, skip it
            log.warning(
                "gate_outcomes.bad_row id=%s err=%s", row.id, exc
            )
    return result


def get_gate_receipt(
    session: "Session",
    decision_run_id: int,
) -> tuple[list[GateOutcome], str] | None:
    """Return ``(outcomes, summary_line)`` for a decision run.

    Returns ``None`` when no gate outcomes exist for the run (legacy drafts
    pre-dating migration 0102, or runs where the orchestrator never reached
    the gate evaluation point).

    The ``summary_line`` is the output of
    ``argosy.quality.verification.summarize`` — one human-readable line
    suitable for a plan page header chip:
    ``"2/2 gates passed"`` or
    ``"1/2 gates passed; whole_artifact_reader DID_NOT_RUN (codex hung)"``
    """
    outcomes = get_gate_outcomes(session, decision_run_id)
    if not outcomes:
        return None
    return outcomes, summarize(outcomes)
