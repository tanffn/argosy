"""Dispatcher — routes a classified AmendmentIntent into the right execution path.

Three entry points:
  - run_small(...) — synchronous, applies the Delta inline, returns AmendmentResultDTO.
  - dispatch_async(...) — opens a DecisionRun, spawns the right worker via
    asyncio.to_thread, returns AmendmentResultDTO with status='running'.
  - cancel(...) — flips a running DecisionRun to status='cancelled'.

Concurrency: the partial unique index on decision_runs (migration 0018)
prevents a second running amendment per user. dispatch_async detects this
and returns status='needs_confirmation' so the chat surface can ask the
user to cancel-and-restart vs queue.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from argosy.agents.advisor_amendment_types import (
    AmendmentIntent,
    AmendmentResultDTO,
)
from argosy.api.events import publish_event_threadsafe
from argosy.logging import get_logger
from argosy.orchestrator.flows.plan_amendment import workers as _workers
from argosy.state.models import DecisionRun, PlanVersion
from argosy.state.queries import get_current_plan, get_pending_draft

log = get_logger(__name__)


def run_small(
    session: Session, *, user_id: str, message: str, intent: AmendmentIntent,
) -> AmendmentResultDTO:
    """Apply the advisor-emitted Delta inline. Synchronous; returns immediately."""
    if intent.proposed_delta is None:
        raise ValueError("run_small requires intent.proposed_delta")

    # Open a DecisionRun row for audit lineage even on the inline path.
    run = DecisionRun(
        user_id=user_id, ticker="(plan)", tier="small",
        decision_kind="plan_amendment_chat", status="running",
        notes_json=json.dumps({"message": message, "intent": intent.model_dump()}),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    # Find target plan: pending draft if exists, else need a fresh minimal draft.
    target = get_pending_draft(session, user_id)
    if target is None:
        # Create a new draft seeded from the current plan + this single delta.
        current = get_current_plan(session, user_id)
        if current is None:
            raise RuntimeError(f"user {user_id!r} has no current plan to amend")
        target = PlanVersion(
            user_id=user_id, role="draft",
            version_label=f"amend-small-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}",
            source_path="", raw_markdown="",
            decision_run_id=run.id,
            derived_from_id=current.id,
            horizon_long_json=current.horizon_long_json,
            horizon_medium_json=current.horizon_medium_json,
            horizon_short_json=current.horizon_short_json,
            horizon_long_md=current.horizon_long_md,
            horizon_medium_md=current.horizon_medium_md,
            horizon_short_md=current.horizon_short_md,
        )
        session.add(target)
        session.commit()
        session.refresh(target)

    # Apply the delta into the target draft.
    delta = intent.proposed_delta
    delta_dict = delta.model_dump()
    delta_dict["accepted"] = True
    delta_dict["user_edited"] = True

    horizon_field = f"horizon_{delta.horizon}_json"
    raw = getattr(target, horizon_field) or "{}"
    payload = json.loads(raw)
    deltas = payload.get("deltas_from_prior") or []
    # Replace existing delta with same item_id, else append.
    existing_idx = next(
        (i for i, d in enumerate(deltas) if d.get("item_id") == delta.item_id),
        None,
    )
    if existing_idx is not None:
        deltas[existing_idx] = delta_dict
    else:
        deltas.append(delta_dict)
    payload["deltas_from_prior"] = deltas
    setattr(target, horizon_field, json.dumps(payload))

    run.status = "completed"
    run.finished_at = datetime.now(timezone.utc)
    session.commit()

    publish_event_threadsafe("plan.amendment.completed", {
        "user_id": user_id,
        "decision_run_id": run.id,
        "tier": "small",
        "draft_id": target.id,
    })

    return AmendmentResultDTO(
        tier="small", decision_run_id=run.id,
        status="applied", draft_id=target.id,
    )


def dispatch_async(
    session: Session, *,
    user_id: str, message: str, tier: str, intent: AmendmentIntent,
    cancel_existing: bool = False,
) -> AmendmentResultDTO:
    """Spawn the medium or large worker; return 202-shaped DTO.

    If a running amendment already exists for this user:
      - cancel_existing=False: return status='needs_confirmation'
      - cancel_existing=True: cancel the prior, then dispatch this one.
    """
    if tier not in ("medium", "large"):
        raise ValueError(f"dispatch_async expects tier in (medium, large); got {tier!r}")

    existing = (
        session.query(DecisionRun)
        .filter_by(
            user_id=user_id, decision_kind="plan_amendment_chat", status="running",
        )
        .first()
    )
    if existing is not None:
        if not cancel_existing:
            return AmendmentResultDTO(
                tier=tier,  # type: ignore[arg-type]
                decision_run_id=existing.id,
                status="needs_confirmation",
            )
        existing.status = "cancelled"
        existing.finished_at = datetime.now(timezone.utc)
        session.commit()
        publish_event_threadsafe("plan.amendment.cancelled", {
            "user_id": user_id,
            "decision_run_id": existing.id,
            "tier": existing.tier,
        })

    run = DecisionRun(
        user_id=user_id, ticker="(plan)", tier=tier,
        decision_kind="plan_amendment_chat", status="running",
        notes_json=json.dumps({"message": message, "intent": intent.model_dump()}),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    _spawn_worker(
        session=session, user_id=user_id, decision_run=run, tier=tier, guidance=message,
    )

    eta = 30 if tier == "medium" else 900
    return AmendmentResultDTO(
        tier=tier,  # type: ignore[arg-type]
        decision_run_id=run.id,
        status="running",
        eta_seconds=eta,
    )


def _spawn_worker(
    *, session: Session, user_id: str, decision_run: DecisionRun,
    tier: str, guidance: str,
) -> None:
    """Spawn the right worker on a background thread.

    Indirection point so tests can monkeypatch.
    """
    worker = _workers._medium_worker if tier == "medium" else _workers._large_worker

    # The session is bound to the calling thread; spawn a fresh session for the
    # worker thread tied to the same engine.
    engine = session.get_bind()
    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    decision_run_id = decision_run.id

    def _runnable():
        worker_session = SessionLocal()
        try:
            run = worker_session.get(DecisionRun, decision_run_id)
            worker(session=worker_session, user_id=user_id, decision_run=run, guidance=guidance)
        finally:
            worker_session.close()

    threading.Thread(target=_runnable, daemon=True, name=f"amendment-{tier}-{decision_run_id}").start()


def cancel(session: Session, *, user_id: str, decision_run_id: int) -> bool:
    """Flip a running amendment to cancelled. Returns True on success.

    Returns False if the run doesn't exist, isn't owned by the user, or
    is already finished.
    """
    run = session.get(DecisionRun, decision_run_id)
    if run is None or run.user_id != user_id:
        return False
    if run.decision_kind != "plan_amendment_chat":
        return False
    if run.status != "running":
        return False
    run.status = "cancelled"
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    publish_event_threadsafe("plan.amendment.cancelled", {
        "user_id": user_id,
        "decision_run_id": decision_run_id,
        "tier": run.tier,
    })
    return True


__all__ = ["run_small", "dispatch_async", "cancel", "_spawn_worker"]
