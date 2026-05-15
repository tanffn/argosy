"""Negotiation recorder — persists phase rows + writes transcript bundles.

Wave C of provenance. Every multi-agent phase boundary in a
``decision_run`` calls ``record_negotiation_phase`` with:

  * the decision_run_id and user_id already in scope
  * a phase ``kind`` from the kind taxonomy in migration 0020's docstring
  * the list of agent_report ids that participated, in chronological
    order (the recorder fetches them so it can build the transcript)
  * an optional structured verdict DTO (DebateOutcome / RiskOutcome /
    FundManagerDecision / FundManagerPlanRevisionDecision / etc.)

The recorder:

  1. Computes the next ``seq`` for this decision_run.
  2. Fetches participating ``agent_reports`` rows and turns them into
     ``ParticipantRef`` items for the transcript writer.
  3. Writes the four-file FS bundle via ``transcript_writer.write_phase_bundle``.
  4. Inserts the ``decision_phases`` row with verdict_json, tldr_md,
     bundle_dir.
  5. Back-fills ``agent_reports.phase_id`` for the participating rows.
  6. Emits an ``audit_log`` event ``provenance.phase.finished``.

All callers wrap the call in try/except and log on failure (best-effort
— a transcript-writer bug must never fail the underlying flow). The
recorder itself raises only on internal bugs (e.g. SQL constraint
violations); the caller logs and moves on.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from typing import Iterable

from pydantic import BaseModel
from sqlalchemy import func, select, update

from argosy.logging import get_logger
from argosy.services.transcript_writer import (
    ParticipantRef,
    write_phase_bundle,
)
from argosy.state import db as db_mod
from argosy.state.models import AgentReport, AuditLog, DecisionPhase

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def record_negotiation_phase(
    *,
    user_id: str,
    decision_run_id: int,
    kind: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    agent_report_ids: Iterable[int],
    verdict: BaseModel | None,
    side_by_id: dict[int, str] | None = None,
    perspective_by_id: dict[int, str] | None = None,
    round_by_id: dict[int, int] | None = None,
) -> int:
    """Persist a phase row + write the FS bundle. Returns the phase id.

    Args:
        user_id: owning user.
        decision_run_id: the parent decision_runs row.
        kind: phase taxonomy value (see migration 0020 docstring).
        started_at / finished_at: phase timing. If ``finished_at`` is None,
            we stamp it as ``_utcnow()``.
        agent_report_ids: iterable of agent_reports.id values that
            participated, in chronological order.
        verdict: optional pydantic DTO. ``None`` for analyst-only phases.
        side_by_id / perspective_by_id / round_by_id: optional metadata
            maps so the transcript can label each participant. Caller
            knows the labels (it scheduled the agents); the recorder
            doesn't try to infer them.
    """
    finished_at = finished_at or _utcnow()
    ids = list(agent_report_ids)
    side_by_id = side_by_id or {}
    perspective_by_id = perspective_by_id or {}
    round_by_id = round_by_id or {}

    async with db_mod.get_session() as session:
        # Compute next seq for this run (monotonic; phases are written
        # serially per the call-site contract).
        max_seq = (
            await session.execute(
                select(func.coalesce(func.max(DecisionPhase.seq), 0)).where(
                    DecisionPhase.decision_run_id == decision_run_id
                )
            )
        ).scalar_one()
        next_seq = int(max_seq) + 1

        # Fetch the participating agent_reports rows in id order.
        rows: list[AgentReport] = []
        if ids:
            rows = list(
                (
                    await session.execute(
                        select(AgentReport).where(AgentReport.id.in_(ids))
                    )
                ).scalars().all()
            )
            id_to_row = {r.id: r for r in rows}
            rows = [id_to_row[i] for i in ids if i in id_to_row]

        participants = [
            ParticipantRef(
                agent_role=r.agent_role,
                agent_report_id=r.id,
                response_text=r.response_text or "",
                side=side_by_id.get(r.id),
                perspective=perspective_by_id.get(r.id),
                round=round_by_id.get(r.id),
                confidence=r.confidence,
                model=r.model,
            )
            for r in rows
        ]

    # Write FS bundle (no DB session held — disk IO can be slow).
    # Codex/§17 zigzag finding #4 v2: if `write_phase_bundle` itself
    # fails mid-write (e.g. wrote TLDR.md, then disk full before
    # transcript.md), we'd leave a partial bundle under the
    # deterministic `<run_id>__<kind>` directory. Wrap with cleanup so
    # a partial bundle gets removed before the exception escapes —
    # otherwise a retry would silently inherit the corruption.
    try:
        bundle_dir, tldr_md, _sequence_mmd = write_phase_bundle(
            user_id=user_id,
            decision_run_id=decision_run_id,
            phase_kind=kind,
            started_at=started_at,
            finished_at=finished_at,
            verdict=verdict,
            participants=participants,
        )
    except Exception:
        # Best-effort cleanup of any partial bundle. The bundle path
        # is deterministic from inputs, so we can recompute it here
        # even though `write_phase_bundle` didn't return it.
        from argosy.services.transcript_writer import bundle_path
        partial_dir = bundle_path(
            user_id=user_id,
            decision_run_id=decision_run_id,
            phase_kind=kind,
            started_at=started_at,
        )
        shutil.rmtree(partial_dir, ignore_errors=True)
        raise

    # Build participants_json for the row (compact reference list).
    participants_json = [
        {
            "agent_role": p.agent_role,
            "agent_report_id": p.agent_report_id,
            "side": p.side,
            "perspective": p.perspective,
            "round": p.round,
            "confidence": p.confidence,
            "model": p.model,
        }
        for p in participants
    ]

    # Codex/§17 zigzag finding #4: bundle is written before the DB
    # commit. If the commit fails (FK violation, transient DB error,
    # ...) we'd leave an orphan transcript bundle on disk with no row
    # pointing at it. Worse, since `bundle_dir` is keyed by
    # `<decision_run_id>__<kind>` (no timestamp), a retry for the same
    # phase would silently inherit the prior partial bundle.
    #
    # Fix: wrap the commit in try/except; on any failure, delete the
    # bundle dir we just wrote (idempotent / best-effort) before
    # re-raising. The caller already wraps recorder calls in try/except
    # so this just keeps the FS clean.
    try:
        async with db_mod.get_session() as session:
            phase_row = DecisionPhase(
                decision_run_id=decision_run_id,
                user_id=user_id,
                seq=next_seq,
                kind=kind,
                started_at=started_at,
                finished_at=finished_at,
                participants_json=json.dumps(participants_json),
                verdict_json=(
                    verdict.model_dump_json() if verdict is not None else None
                ),
                verdict_kind=(type(verdict).__name__ if verdict is not None else None),
                tldr_md=tldr_md,
                bundle_dir=str(bundle_dir),
            )
            session.add(phase_row)
            await session.flush()
            phase_id = phase_row.id

            # Back-fill agent_reports.phase_id for participants.
            if ids:
                await session.execute(
                    update(AgentReport)
                    .where(AgentReport.id.in_(ids))
                    .values(phase_id=phase_id)
                )

            # Audit-log emission.
            session.add(AuditLog(
                user_id=user_id,
                event_type="provenance.phase.finished",
                entity_type="decision_phase",
                entity_id=str(phase_id),
                payload_json=json.dumps({
                    "decision_run_id": decision_run_id,
                    "phase_kind": kind,
                    "verdict_kind": type(verdict).__name__ if verdict else None,
                    "participants_count": len(ids),
                    "bundle_dir": str(bundle_dir),
                }),
            ))
            await session.commit()
    except Exception:
        # Compensating cleanup: drop the on-disk bundle so we don't leak
        # an orphan directory unreferenced by any decision_phases row.
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise

    log.info(
        "negotiation.phase.recorded",
        user_id=user_id,
        decision_run_id=decision_run_id,
        phase_kind=kind,
        phase_id=phase_id,
        seq=next_seq,
        participants=len(ids),
    )
    return phase_id


__all__ = ["record_negotiation_phase"]
