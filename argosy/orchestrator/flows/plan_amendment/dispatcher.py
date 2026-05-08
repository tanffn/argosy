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

import json
import threading
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
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
    """Apply the advisor-emitted Delta inline. Synchronous; returns immediately.

    Validation order matters: any precondition that can raise (missing
    delta, claimed-tightening-but-numbers-loosen, no current plan to seed
    a draft from) is checked BEFORE the DecisionRun row is committed, so
    a failure does not orphan a `status='running'` row that would
    permanently jam the partial unique index for this user. Any
    exception raised AFTER the row is committed flips it to
    `status='failed'` with the error merged into notes_json.
    """
    if intent.proposed_delta is None:
        raise ValueError("run_small requires intent.proposed_delta")

    _validate_tightening(intent.proposed_delta)

    # Resolve the target draft up-front so a missing precondition raises
    # BEFORE the DecisionRun is committed (otherwise the "running" row
    # would permanently jam the partial unique index for this user).
    pending_draft = get_pending_draft(session, user_id)
    seed_current: PlanVersion | None = None
    if pending_draft is None:
        seed_current = get_current_plan(session, user_id)
        if seed_current is None:
            raise RuntimeError(f"user {user_id!r} has no current plan to amend")

    # Open a DecisionRun row for audit lineage even on the inline path.
    notes_payload: dict = {"message": message, "intent": intent.model_dump()}
    run = DecisionRun(
        user_id=user_id, ticker="(plan)", tier="small",
        decision_kind="plan_amendment_chat", status="running",
        notes_json=json.dumps(notes_payload),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    try:
        if pending_draft is not None:
            target = pending_draft
        else:
            # Create a new draft seeded from the current plan + this single delta.
            assert seed_current is not None  # narrowed above
            target = PlanVersion(
                user_id=user_id, role="draft",
                version_label=f"amend-small-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}",
                source_path="", raw_markdown="",
                decision_run_id=run.id,
                derived_from_id=seed_current.id,
                horizon_long_json=seed_current.horizon_long_json,
                horizon_medium_json=seed_current.horizon_medium_json,
                horizon_short_json=seed_current.horizon_short_json,
                horizon_long_md=seed_current.horizon_long_md,
                horizon_medium_md=seed_current.horizon_medium_md,
                horizon_short_md=seed_current.horizon_short_md,
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
    except Exception as exc:
        # Don't leave the DecisionRun in `running` — it would jam the
        # partial unique index for this user. Merge the error into notes
        # rather than overwriting the original message+intent (so the
        # row can still be replayed for debugging).
        log.error("plan_amendment.small.failed",
                  decision_run_id=run.id, user_id=user_id, error=str(exc))
        try:
            existing_notes = json.loads(run.notes_json or "{}")
        except (ValueError, TypeError):
            existing_notes = {}
        existing_notes["error"] = str(exc)
        run.notes_json = json.dumps(existing_notes)
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        publish_event_threadsafe("plan.amendment.failed", {
            "user_id": user_id,
            "decision_run_id": run.id,
            "tier": "small",
            "error": str(exc),
        })
        raise


def dispatch_async(
    session: Session, *,
    user_id: str, message: str, tier: str, intent: AmendmentIntent,
    cancel_existing: bool = False,
) -> AmendmentResultDTO:
    """Spawn the medium or large worker; return 202-shaped DTO.

    If a running amendment already exists for this user:
      - cancel_existing=False: return status='needs_confirmation' so the
        chat surface can ask the user "cancel and restart vs queue?".
      - cancel_existing=True: cancel the prior run (emits
        ``plan.amendment.cancelled`` for that row), then dispatch the new
        run (emits ``plan.amendment.started`` for the new row), and
        return status='cancelled_existing' to confirm both events landed.
        The two events fire back-to-back; the UI is expected to handle
        latest-event-wins.

    Concurrency belt-and-suspenders: even with the in-Python existing
    check, two simultaneous dispatch calls can both pass the SELECT and
    race to insert. The partial unique index on decision_runs (migration
    0018) makes the loser raise IntegrityError; we catch it, refetch the
    surviving running row, and return status='needs_confirmation' so
    user-facing semantics match the slow-path check.
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
    cancelled_a_prior = False
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
        cancelled_a_prior = True

    run = DecisionRun(
        user_id=user_id, ticker="(plan)", tier=tier,
        decision_kind="plan_amendment_chat", status="running",
        notes_json=json.dumps({"message": message, "intent": intent.model_dump()}),
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError:
        # Race: another concurrent dispatch slipped a running row in
        # between our SELECT and our INSERT. The partial unique index
        # (migration 0018) caught it. Roll back, refetch the survivor,
        # and degrade to needs_confirmation so the user gets a coherent
        # answer regardless of which path they hit.
        session.rollback()
        winner = (
            session.query(DecisionRun)
            .filter_by(
                user_id=user_id,
                decision_kind="plan_amendment_chat",
                status="running",
            )
            .first()
        )
        if winner is None:
            # Extremely unlikely: index fired but no row survives. Re-raise
            # so the caller sees the underlying IntegrityError rather than
            # silently swallowing it.
            raise
        return AmendmentResultDTO(
            tier=tier,  # type: ignore[arg-type]
            decision_run_id=winner.id,
            status="needs_confirmation",
        )
    session.refresh(run)

    _spawn_worker(
        session=session, user_id=user_id, decision_run=run, tier=tier, guidance=message,
    )

    eta = 30 if tier == "medium" else 900
    return AmendmentResultDTO(
        tier=tier,  # type: ignore[arg-type]
        decision_run_id=run.id,
        status="cancelled_existing" if cancelled_a_prior else "running",
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


def _validate_tightening(delta) -> None:
    """Defensive: confirm numeric values move in the tightening direction.

    Tightening rules (aligned with the spec's "lowers risk surface"):
      - cap-like: ``proposed.value < prior.value``
        Keywords: ``cap``, ``max``, ``ceiling``, ``limit``, ``ratio``,
        ``threshold``. ``ratio`` is treated as cap-like — for a quantity
        like ``expense_ratio``, "lower number = less expense = tightening".
      - floor-like: ``proposed.value > prior.value``
        Keywords: ``floor``, ``min``.
      - if kind absent or prior/proposed missing: trust the advisor's
        classification (the classifier's direction filter is the primary
        gate; this is just belt-and-suspenders).

    When neither bucket matches, log at debug so future audits can spot
    common LLM kind typos that fall through the safety net.
    """
    prior = (delta.prior or {})
    proposed = (delta.proposed or {})
    pv = prior.get("value")
    qv = proposed.get("value")
    if pv is None or qv is None:
        return  # not enough info; trust the advisor

    kind = (proposed.get("kind") or prior.get("kind") or "").lower()
    if not isinstance(pv, (int, float)) or not isinstance(qv, (int, float)):
        return

    is_floor_like = any(k in kind for k in ("floor", "min"))
    # "ratio" and "threshold" semantically behave like caps (lower
    # number = tighter). "limit" likewise is read as an upper bound.
    is_cap_like = any(
        k in kind
        for k in ("cap", "max", "ceiling", "limit", "ratio", "threshold")
    )

    if is_cap_like:
        if qv >= pv:
            raise ValueError(
                f"intent claims tightening but proposed value {qv} >= prior {pv} on a cap-like target"
            )
    elif is_floor_like:
        if qv <= pv:
            raise ValueError(
                f"intent claims tightening but proposed value {qv} <= prior {pv} on a floor-like target"
            )
    else:
        # Unrecognized kind — the classifier's direction filter still
        # gated the path, but log so we can spot common LLM typos
        # (e.g. "maxiumum", "treshhold") that should have been caps.
        log.debug(
            "_validate_tightening.no_rule_matched",
            kind=kind, prior_value=pv, proposed_value=qv,
        )


__all__ = ["run_small", "dispatch_async", "cancel", "_spawn_worker"]
