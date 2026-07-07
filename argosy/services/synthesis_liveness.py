"""Stale plan-synthesis run liveness + reap.

A ``decision_runs`` row with ``decision_kind='plan_revision'`` and
``status='running'`` is only *actually* in flight while the orchestrator
is making progress. When the synthesis process dies without flipping the
row (phase-spawn failure, server restart mid-run), the row stays
``running`` forever and the UI's in-flight banner shows a zombie run
indefinitely.

Liveness rule (deterministic hygiene, not a judgment gate): a running
plan_revision run whose latest ``decision_phases`` activity — the max of
every phase's ``started_at``/``finished_at``, or the run's own
``started_at`` when no phase row exists yet — is older than
``stale_minutes`` is NOT in flight. :func:`reap_stale_synthesis_runs`
flips it to ``status='failed'`` with a machine-readable note in
``notes_json`` and closes any stuck ``job_runs`` wrapper rows (the
``monthly_cycle`` tick that fired the synthesis opens a ``job_runs`` row
that is orphaned in ``running`` by the same crash — matched by job name
+ started-at proximity, mirroring the reap pattern in
``argosy/orchestrator/loops/job_runs_retention.py``).

Called from ``GET /api/plan/in-flight-synthesis`` before the in-flight
select, so the endpoint self-heals: polling the banner is what clears
zombie rows — no manual SQL, no separate cron required.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import DecisionPhase, DecisionRun, JobRun

_log = get_logger("argosy.services.synthesis_liveness")

#: No phase activity for this long => the run is stale. Overridable via
#: the ``ARGOSY_SYNTHESIS_STALE_MINUTES`` env var. 30 min is ~3x the
#: longest observed healthy phase gap; a healthy synthesizer phase
#: writes its decision_phases row on completion, so gaps between phases
#: are bounded by one LLM call.
DEFAULT_STALE_MINUTES = 30
_STALE_ENV_VAR = "ARGOSY_SYNTHESIS_STALE_MINUTES"

#: Jobs that wrap ``plan_synthesis.run_synthesis`` inside their tick. A
#: crash that orphans the decision run also orphans the wrapper's
#: ``job_runs`` row in ``status='running'``.
SYNTHESIS_WRAPPER_JOB_NAMES: tuple[str, ...] = ("monthly_cycle",)

#: ``job_runs`` has no FK to ``decision_runs`` (lineage is threaded via
#: audit-token strings, not persisted). A wrapper row is matched to a
#: reaped run by job name + started_at within this window — observed
#: real pairs are sub-second apart.
WRAPPER_MATCH_WINDOW = timedelta(minutes=5)


def configured_stale_minutes() -> int:
    """The staleness window in minutes (env-overridable, floor 1)."""
    raw = os.environ.get(_STALE_ENV_VAR, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_STALE_MINUTES
    return max(1, value)


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on read; the convention is naive == UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _merged_reap_note(existing_json: str | None, note: dict) -> str:
    """Merge the reap note into ``notes_json`` without clobbering
    whatever a future run kind may have stored there."""
    base: dict = {}
    if existing_json:
        try:
            parsed = json.loads(existing_json)
            if isinstance(parsed, dict):
                base = parsed
        except (TypeError, ValueError):
            base = {"prior_notes_raw": existing_json}
    base["reaped"] = note
    return json.dumps(base, default=str)


def reap_stale_synthesis_runs(
    session: Session,
    *,
    user_id: str,
    now: datetime | None = None,
    stale_minutes: int | None = None,
) -> list[int]:
    """Flip stale 'running' plan_revision runs to 'failed'; close their
    stuck wrapper ``job_runs`` rows. Returns the reaped run ids.

    Deterministic + idempotent: a second call finds no stale rows and is
    a no-op. Commits only when something was reaped.
    """
    now_dt = _as_utc(now or datetime.now(timezone.utc))
    window_min = stale_minutes if stale_minutes is not None else configured_stale_minutes()
    cutoff = now_dt - timedelta(minutes=window_min)

    runs = (
        session.execute(
            select(DecisionRun).where(
                DecisionRun.user_id == user_id,
                DecisionRun.status == "running",
                DecisionRun.decision_kind == "plan_revision",
            )
        )
        .scalars()
        .all()
    )

    reaped_ids: list[int] = []
    for run in runs:
        phase_times = session.execute(
            select(DecisionPhase.started_at, DecisionPhase.finished_at).where(
                DecisionPhase.decision_run_id == run.id
            )
        ).all()
        candidates = [_as_utc(run.started_at)]
        for started, finished in phase_times:
            if started is not None:
                candidates.append(_as_utc(started))
            if finished is not None:
                candidates.append(_as_utc(finished))
        last_activity = max(candidates)
        if last_activity >= cutoff:
            continue  # genuinely in flight (or at least recently alive)

        run.status = "failed"
        run.finished_at = now_dt
        run.notes_json = _merged_reap_note(
            run.notes_json,
            {
                "reason": (
                    f"reaped: no synthesis activity since "
                    f"{last_activity.isoformat()} (> {window_min} min stale window)"
                ),
                "reaped_at": now_dt.isoformat(),
                "reaper": "in_flight_synthesis_liveness",
            },
        )

        # Close the orphaned wrapper job_runs row(s) for this run — same
        # terminal shape as job_runs_retention's reap pass.
        wrapper_rows = (
            session.execute(
                select(JobRun).where(
                    JobRun.job_name.in_(SYNTHESIS_WRAPPER_JOB_NAMES),
                    JobRun.status == "running",
                )
            )
            .scalars()
            .all()
        )
        run_started = _as_utc(run.started_at)
        closed_job_run_ids: list[int] = []
        for jr in wrapper_rows:
            if abs(_as_utc(jr.started_at) - run_started) > WRAPPER_MATCH_WINDOW:
                continue
            jr.status = "cancelled"
            jr.error_message = (
                f"reaped: wrapped stale plan synthesis run {run.id} "
                "(in-flight liveness)"
            )
            jr.finished_at = now_dt
            closed_job_run_ids.append(jr.id)

        _log.info(
            "synthesis_liveness.reaped",
            decision_run_id=run.id,
            user_id=user_id,
            last_activity=last_activity.isoformat(),
            stale_minutes=window_min,
            closed_job_run_ids=closed_job_run_ids,
        )
        reaped_ids.append(run.id)

    if reaped_ids:
        session.commit()
    return reaped_ids


__all__ = [
    "DEFAULT_STALE_MINUTES",
    "SYNTHESIS_WRAPPER_JOB_NAMES",
    "WRAPPER_MATCH_WINDOW",
    "configured_stale_minutes",
    "reap_stale_synthesis_runs",
]
