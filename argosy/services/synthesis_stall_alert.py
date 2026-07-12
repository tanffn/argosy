"""Synthesis stall alerting — push when in-flight work goes quiet.

The liveness reaper (``synthesis_liveness.reap_stale_synthesis_runs``) only
helps when something calls it (API poll / live backend). Run 191 had a
1h47m unmonitored gap while the backend was dead. This module + the
``SynthesisStallAlertLoop`` write a monitor flag / inbox row when a
plan_revision run is still ``running`` but has had no phase heartbeat for
``ARGOSY_SYNTHESIS_STALL_ALERT_MINUTES`` (default 20) — even before the
reaper flips the row to failed.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.services.synthesis_liveness import (
    configured_stale_minutes,
    _as_utc,
)
from argosy.state.models import ActionProposal, DecisionPhase, DecisionRun, MonitorFlag

_log = get_logger("argosy.services.synthesis_stall_alert")

STALL_KIND = "synthesis_stall"
STALL_DEDUP = "synthesis_stall:{user_id}:{decision_run_id}"
DEFAULT_ALERT_MINUTES = 20
_ALERT_ENV = "ARGOSY_SYNTHESIS_STALL_ALERT_MINUTES"


def configured_alert_minutes() -> int:
    raw = os.environ.get(_ALERT_ENV, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ALERT_MINUTES
    # Alert window must be <= reaper window so we surface before/at reap.
    return max(1, min(value, configured_stale_minutes()))


def _last_activity(session: Session, run: DecisionRun) -> datetime:
    phases = list(
        session.execute(
            select(DecisionPhase).where(DecisionPhase.decision_run_id == run.id)
        ).scalars().all()
    )
    stamps: list[datetime] = []
    if run.started_at is not None:
        stamps.append(_as_utc(run.started_at))
    for p in phases:
        if p.started_at is not None:
            stamps.append(_as_utc(p.started_at))
        if p.finished_at is not None:
            stamps.append(_as_utc(p.finished_at))
    return max(stamps) if stamps else _as_utc(datetime.now(UTC))


def find_stalled_runs(
    session: Session,
    *,
    user_id: str,
    now: datetime | None = None,
    alert_minutes: int | None = None,
) -> list[tuple[DecisionRun, datetime, float]]:
    """Return (run, last_activity, quiet_minutes) for stalled running synths."""
    now_dt = _as_utc(now or datetime.now(UTC))
    window = alert_minutes if alert_minutes is not None else configured_alert_minutes()
    cutoff = now_dt - timedelta(minutes=window)
    runs = list(
        session.execute(
            select(DecisionRun).where(
                DecisionRun.user_id == user_id,
                DecisionRun.status == "running",
                DecisionRun.decision_kind == "plan_revision",
            )
        ).scalars().all()
    )
    out: list[tuple[DecisionRun, datetime, float]] = []
    for run in runs:
        last = _last_activity(session, run)
        if last <= cutoff:
            quiet = (now_dt - last).total_seconds() / 60.0
            out.append((run, last, quiet))
    return out


def write_stall_alerts(
    session: Session,
    *,
    user_id: str,
    now: datetime | None = None,
    alert_minutes: int | None = None,
) -> list[int]:
    """Upsert monitor flags + inbox rows for stalled runs. Returns run ids."""
    now_dt = _as_utc(now or datetime.now(UTC))
    stalled = find_stalled_runs(
        session, user_id=user_id, now=now_dt, alert_minutes=alert_minutes,
    )
    alerted: list[int] = []
    for run, last, quiet in stalled:
        dedup = STALL_DEDUP.format(user_id=user_id, decision_run_id=run.id)
        summary = (
            f"Synthesis stall: run {run.id} quiet {quiet:.0f} min "
            f"(last heartbeat {last.isoformat()})"
        )
        payload = {
            "decision_run_id": run.id,
            "last_activity": last.isoformat(),
            "quiet_minutes": round(quiet, 1),
            "message": (
                "synthesis in flight, no heartbeat "
                f"{quiet:.0f} minutes"
            ),
        }
        rationale = (
            f"**Synthesis in flight, no heartbeat {quiet:.0f} minutes.**\n\n"
            f"Run `{run.id}` is still `running` but has had no "
            f"`decision_phases` activity since {last.isoformat()}. "
            "If the backend died, restart the supervised wrapper; the "
            "liveness reaper alone cannot help while nothing is alive to "
            "call it."
        )

        existing_flag = session.execute(
            select(MonitorFlag).where(
                MonitorFlag.user_id == user_id,
                MonitorFlag.dedup_key == dedup,
                MonitorFlag.status == "active",
            )
        ).scalar_one_or_none()
        if existing_flag is None:
            session.add(
                MonitorFlag(
                    user_id=user_id,
                    kind=STALL_KIND,
                    severity="critical",
                    payload=json.dumps(payload),
                    dedup_key=dedup,
                    status="active",
                    surfaced_at=now_dt,
                )
            )
        else:
            existing_flag.payload = json.dumps(payload)
            existing_flag.surfaced_at = now_dt

        existing_prop = session.execute(
            select(ActionProposal).where(
                ActionProposal.user_id == user_id,
                ActionProposal.dedup_key == dedup,
                ActionProposal.status == "open",
            )
        ).scalar_one_or_none()
        if existing_prop is None:
            session.add(
                ActionProposal(
                    user_id=user_id,
                    summary=summary,
                    rationale_md=rationale,
                    suggested_payload=json.dumps(payload),
                    severity="critical",
                    surfaced_at=now_dt,
                    expires_at=now_dt + timedelta(days=7),
                    status="open",
                    kind="note_only",
                    dedup_key=dedup,
                    execution_state="proposed",
                )
            )
        else:
            existing_prop.summary = summary
            existing_prop.rationale_md = rationale
            existing_prop.suggested_payload = json.dumps(payload)
            existing_prop.surfaced_at = now_dt

        alerted.append(run.id)
        _log.warning(
            "synthesis_stall.alerted",
            user_id=user_id,
            decision_run_id=run.id,
            quiet_minutes=round(quiet, 1),
        )

    if alerted:
        session.flush()
    return alerted


def scan_and_alert(
    session: Session,
    *,
    user_id: str = "ariel",
    now: datetime | None = None,
    alert_minutes: int | None = None,
) -> dict[str, Any]:
    """One tick: write stall alerts. Caller commits."""
    ids = write_stall_alerts(
        session, user_id=user_id, now=now, alert_minutes=alert_minutes,
    )
    return {"alerted_run_ids": ids, "count": len(ids)}


__all__ = [
    "STALL_KIND",
    "configured_alert_minutes",
    "find_stalled_runs",
    "scan_and_alert",
    "write_stall_alerts",
]
