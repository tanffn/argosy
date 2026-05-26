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

from sqlalchemy.exc import IntegrityError

from argosy.logging import get_logger
from argosy.services.transcript_writer import (
    ParticipantRef,
    bundle_path,
    fresh_uniq_suffix,
    write_phase_bundle,
)
from argosy.state import db as db_mod
from argosy.state.models import AgentReport, AuditLog, DecisionPhase

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_aware(dt: datetime) -> datetime:
    """Promote a naive datetime to UTC-aware; pass aware ones through.

    SQLite via SQLAlchemy's ``DateTime`` (without ``timezone=True``) reads
    back stored timestamps as naive, even when written as offset-aware.
    Downstream arithmetic such as ``finished_at - started_at`` then raises
    ``TypeError: can't subtract offset-naive and offset-aware datetimes``.
    Normalizing both ends here keeps the rest of the recorder + transcript
    writer tz-uniform.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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
    started_at = _as_utc_aware(started_at)
    finished_at = _as_utc_aware(finished_at) if finished_at is not None else _utcnow()
    ids = list(agent_report_ids)
    side_by_id = side_by_id or {}
    perspective_by_id = perspective_by_id or {}
    round_by_id = round_by_id or {}
    # One unique suffix per invocation so concurrent recorders for the
    # same `(decision_run_id, kind)` write to distinct bundle dirs and
    # don't accidentally delete each other's bytes on cleanup. The
    # suffix is shared between the initial write and any recompute
    # for cleanup so we always point at our own dir.
    uniq_suffix = fresh_uniq_suffix(started_at)

    async with db_mod.get_session() as session:
        # Compute next seq for this run. `max(seq)+1` is racy at the
        # ORM layer — two recorders firing concurrently for the same
        # `decision_run_id` may compute the same `next_seq`. Migration
        # 0025 added a DB-level unique constraint on
        # `(decision_run_id, seq)` so the race-loser gets
        # `IntegrityError` on commit; we catch it below and clean up.
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
            uniq_suffix=uniq_suffix,
        )
    except Exception:
        # Best-effort cleanup of any partial bundle. We pass the same
        # `uniq_suffix` so the recomputed path points at OUR dir, not
        # a sibling recorder's.
        partial_dir = bundle_path(
            user_id=user_id,
            decision_run_id=decision_run_id,
            phase_kind=kind,
            started_at=started_at,
            uniq_suffix=uniq_suffix,
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
    except IntegrityError as exc:
        # Race-loser path: another recorder for the same
        # `(decision_run_id, kind)` already inserted at our `seq`.
        # Per migration 0025, the unique constraint on
        # `(decision_run_id, seq)` makes this fast-fail rather than
        # silently double-write. Drop our own bundle (uniqueness
        # suffix ensures we don't nuke the winner's dir), emit a
        # `phase.failed` audit event so the SDD §17.4 taxonomy is
        # honored, and re-raise so the caller's try/except can log
        # the conflict.
        shutil.rmtree(bundle_dir, ignore_errors=True)
        await _emit_phase_failed(
            user_id=user_id, decision_run_id=decision_run_id,
            kind=kind, started_at=started_at, reason="race_lost",
            error=str(exc),
        )
        log.warning(
            "negotiation.phase.race_lost",
            user_id=user_id, decision_run_id=decision_run_id,
            phase_kind=kind, seq=next_seq,
        )
        raise
    except Exception as exc:
        # Compensating cleanup: drop the on-disk bundle so we don't leak
        # an orphan directory unreferenced by any decision_phases row.
        # Safe to delete because `uniq_suffix` is per-invocation — we
        # never share `bundle_dir` with another recorder. Also emit a
        # `phase.failed` audit event (SDD §17.4) before re-raising.
        shutil.rmtree(bundle_dir, ignore_errors=True)
        await _emit_phase_failed(
            user_id=user_id, decision_run_id=decision_run_id,
            kind=kind, started_at=started_at, reason="recorder_error",
            error=str(exc),
        )
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


async def _emit_phase_failed(
    *,
    user_id: str,
    decision_run_id: int,
    kind: str,
    started_at: datetime,
    reason: str,
    error: str,
) -> None:
    """Best-effort `provenance.phase.failed` audit emit.

    Closes part of §17 zigzag finding #5 — the SDD §17.4 taxonomy
    promised four event types but only two were emitted. This helper
    fires from the recorder's except paths so recorder-side failures
    (IntegrityError race-loser, FK violation, etc.) leave an audit
    trail. Call-site failures (agents threw before the recorder was
    called) still need separate instrumentation; flagged as deferred
    in §17.4.

    Best-effort: any failure here is swallowed so the surrounding
    try/except can re-raise the original exception cleanly.
    """
    try:
        async with db_mod.get_session() as session:
            session.add(AuditLog(
                user_id=user_id,
                event_type="provenance.phase.failed",
                entity_type="decision_phase",
                entity_id=None,
                payload_json=json.dumps({
                    "decision_run_id": decision_run_id,
                    "phase_kind": kind,
                    "started_at": started_at.isoformat(),
                    "reason": reason,
                    "error": error[:500],  # truncate to avoid bloating audit_log
                }),
            ))
            await session.commit()
    except Exception:
        log.exception(
            "negotiation.phase.failed_audit_emit_failed",
            user_id=user_id, decision_run_id=decision_run_id, phase_kind=kind,
        )


__all__ = ["record_negotiation_phase"]
