"""Plan-related routes (Phase 2 + Wave 1 plan-distillate).

Phase 2 endpoints:
  GET  /api/plan/current  — latest plan version + latest critique
  POST /api/plan/critique — queue a re-critique on demand (returns the
                            new critique inline once complete; Phase 2
                            runs synchronously since there is no job
                            queue yet).

Wave 1 endpoints (T1.10 / T1.11 / T1.12):
  GET   /api/plan/baseline                              — fetch active baseline + distillate
  POST  /api/plan/baseline/distill                      — manual re-distill trigger
  PATCH /api/plan/baseline/distillate/{category}/{item} — apply a user edit to one item

Wave 2 will add the draft + current distillate endpoints.
"""

from __future__ import annotations

import json
from typing import Generator

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.api.events import publish_event
from argosy.state import db as db_mod
from argosy.state.models import PlanCritique, PlanVersion, UserContext
from argosy.state.queries import get_active_baseline

router = APIRouter(prefix="/plan", tags=["plan"])


# ---------------------------------------------------------------------------
# WebSocket event publish indirection (T2.16).
# ---------------------------------------------------------------------------


def _publish(event_type: str, payload: dict) -> None:
    """Publish a plan-lifecycle event via the in-process WebSocket layer.

    Indirection point so tests can monkeypatch this symbol on the module
    directly. Production behavior delegates to ``argosy.api.events`` (the
    actual module name in this codebase; the original spec referenced
    ``argosy.api.websocket`` which does not exist here).

    The underlying ``publish_event`` is async, but plan draft routes are
    sync, so we bridge by either scheduling on a running loop (when called
    inside an async context) or running a one-shot loop. Any failure is
    swallowed — event publishing must never break the route's primary work.

    Wave 2: if the events module is missing or anything else goes wrong,
    this is a best-effort no-op.
    """
    try:
        from argosy.api.events import publish_event
    except ImportError:
        return
    try:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — run the coroutine in a fresh one.
            asyncio.run(publish_event(event_type, payload))
            return
        loop.create_task(publish_event(event_type, payload))
    except Exception:  # pragma: no cover - defensive
        return


# ---------------------------------------------------------------------------
# Sync DB dependency — used by Wave 1 routes.
# The existing Phase 2 routes use the async db_mod.get_session() pattern.
# Wave 1 routes use sync def handlers + a sync SQLAlchemy session so they
# can call sync helpers (get_active_baseline, set_distillate_item_user_edit)
# without bridging back to async.  Tests override this dependency via
# app.dependency_overrides[get_db].
# ---------------------------------------------------------------------------


def get_db() -> Generator[Session, None, None]:
    """Yield a sync SQLAlchemy session.

    In production the session factory is configured by the first caller
    that triggers init via the module-level lazy-init below.  In tests
    the dependency is overridden via ``app.dependency_overrides[get_db]``
    (see ``conftest.client_with_db``).
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    global _sync_engine, _sync_session_factory
    if _sync_session_factory is None:
        from argosy.config import get_settings

        settings = get_settings()
        sync_url = settings.database_url.replace("+aiosqlite", "")
        _sync_engine = create_engine(sync_url, connect_args={"check_same_thread": False})
        _sync_session_factory = sessionmaker(bind=_sync_engine, expire_on_commit=False)

    db: Session = _sync_session_factory()
    try:
        yield db
    finally:
        db.close()


_sync_engine = None
_sync_session_factory = None


class PlanCurrentDTO(BaseModel):
    plan_version_id: int | None
    version_label: str | None
    raw_markdown: str
    imported_at: str | None
    latest_critique_json: dict | None
    latest_critique_created_at: str | None


@router.get("/current", response_model=PlanCurrentDTO)
async def get_plan_current(user_id: str = Query("ariel")) -> PlanCurrentDTO:
    async with db_mod.get_session() as session:
        plan = (
            await session.execute(
                select(PlanVersion)
                .where(PlanVersion.user_id == user_id)
                .order_by(desc(PlanVersion.imported_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if plan is None:
            return PlanCurrentDTO(
                plan_version_id=None,
                version_label=None,
                raw_markdown="",
                imported_at=None,
                latest_critique_json=None,
                latest_critique_created_at=None,
            )
        critique = (
            await session.execute(
                select(PlanCritique)
                .where(PlanCritique.plan_version_id == plan.id)
                .order_by(desc(PlanCritique.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        critique_json: dict | None = None
        critique_created_at: str | None = None
        if critique is not None:
            try:
                critique_json = json.loads(critique.critique_json or "{}")
            except json.JSONDecodeError:  # pragma: no cover - defensive
                critique_json = None
            critique_created_at = critique.created_at.isoformat()
        return PlanCurrentDTO(
            plan_version_id=plan.id,
            version_label=plan.version_label or None,
            raw_markdown=plan.raw_markdown,
            imported_at=plan.imported_at.isoformat() if plan.imported_at else None,
            latest_critique_json=critique_json,
            latest_critique_created_at=critique_created_at,
        )


class CritiqueRequestDTO(BaseModel):
    user_id: str = "ariel"


class CritiqueQueuedDTO(BaseModel):
    status: str
    plan_version_id: int | None
    critique_id: int | None = None
    detail: str = ""


@router.post("/critique", response_model=CritiqueQueuedDTO)
async def queue_critique(req: CritiqueRequestDTO) -> CritiqueQueuedDTO:
    """Run plan-critique on the latest plan synchronously.

    Phase 2 has no job queue, so this runs inline. The frontend should
    show a spinner and tolerate longer responses (Sonnet call).
    """
    async with db_mod.get_session() as session:
        plan = (
            await session.execute(
                select(PlanVersion)
                .where(PlanVersion.user_id == req.user_id)
                .order_by(desc(PlanVersion.imported_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if plan is None:
            raise HTTPException(status_code=404, detail="No plan_versions for this user.")
        ctx = (
            await session.execute(
                select(UserContext).where(UserContext.user_id == req.user_id)
            )
        ).scalar_one_or_none()

    user_context_yaml = ""
    if ctx is not None:
        for label in ("identity_yaml", "goals_yaml", "constraints_yaml"):
            v = getattr(ctx, label, "") or ""
            if v.strip():
                user_context_yaml += f"# --- {label.replace('_yaml', '')} ---\n{v}\n\n"

    agent = PlanCritiqueAgent(user_id=req.user_id)
    try:
        report = await agent.run(
            plan_label=plan.version_label or f"plan_version_id={plan.id}",
            plan_markdown=plan.raw_markdown,
            snapshot_label="(re-critique requested via API)",
            snapshot_summary="(no snapshot supplied with this critique run)",
            user_context_yaml=user_context_yaml,
            domain_kb_files={},
        )
    except MissingAPIKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AgentRunError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    async with db_mod.get_session() as session:
        critique = PlanCritique(
            user_id=req.user_id,
            plan_version_id=plan.id,
            critique_json=report.output.model_dump_json(),
            model=report.model,
        )
        session.add(critique)
        await session.commit()
        critique_id = critique.id

    try:
        await publish_event(
            "agent.run.finished",
            {"agent_role": "plan_critique", "user_id": req.user_id, "critique_id": critique_id},
        )
    except Exception:  # pragma: no cover - defensive
        pass

    return CritiqueQueuedDTO(
        status="ok",
        plan_version_id=plan.id,
        critique_id=critique_id,
        detail="Critique completed.",
    )


# ---------------------------------------------------------------------------
# Wave 1 — baseline distillate endpoints (T1.10 / T1.11 / T1.12)
# ---------------------------------------------------------------------------


class BaselineResponse(BaseModel):
    plan_version_id: int
    version_label: str
    raw_markdown: str
    distillate: dict | None
    distillate_rendered: str | None
    distilled_at: str | None
    source_hash: str | None


def _build_baseline_response(pv: PlanVersion) -> BaselineResponse:
    """Shape a PlanVersion row into the baseline API response."""
    distillate_obj = json.loads(pv.distillate_json) if pv.distillate_json else None
    return BaselineResponse(
        plan_version_id=pv.id,
        version_label=pv.version_label,
        raw_markdown=pv.raw_markdown,
        distillate=distillate_obj,
        distillate_rendered=pv.distillate_rendered,
        distilled_at=pv.distilled_at.isoformat() if pv.distilled_at else None,
        source_hash=pv.source_hash,
    )


@router.get("/baseline", response_model=BaselineResponse)
def get_baseline(user_id: str, db: Session = Depends(get_db)) -> BaselineResponse:
    """Return the active baseline plan + distillate for the user.

    404 when no baseline row exists (user hasn't uploaded a plan yet).
    """
    pv = get_active_baseline(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no baseline plan for user")
    return _build_baseline_response(pv)


@router.post("/baseline/distill", response_model=BaselineResponse)
async def post_baseline_distill(
    user_id: str,
    preserve_user_edits: bool = True,
    db: Session = Depends(get_db),
) -> BaselineResponse:
    """Trigger a fresh distillation pass on the active baseline.

    Used by the "Re-distill" UI button. Preserves user edits by default.
    Pass ``preserve_user_edits=false`` to overwrite all prior user edits.

    The async variant (distill_baseline_plan_async) opens its own DB
    session and dispatches the agent call to a thread, so the route's
    sync ``db`` session is only used for the initial lookup and the
    post-distill refresh.
    """
    pv = get_active_baseline(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no baseline plan for user")

    from argosy.services.plan_distiller_service import distill_baseline_plan_async

    await distill_baseline_plan_async(
        plan_version_id=pv.id,
        user_id=user_id,
        preserve_user_edits=preserve_user_edits,
    )
    # Re-read the updated row from the DB (the async function writes via
    # its own session; expire + refresh pulls the fresh columns).
    db.expire(pv)
    db.refresh(pv)
    return _build_baseline_response(pv)


class DistillateItemEditRequest(BaseModel):
    value: str | float | None = None
    rationale: str | None = None
    detail: str | None = None
    rule: str | None = None
    user_edit_note: str | None = None


@router.patch(
    "/baseline/distillate/{category}/{item_label}",
    response_model=BaselineResponse,
)
def patch_distillate_item(
    category: str,
    item_label: str,
    user_id: str,
    body: DistillateItemEditRequest,
    db: Session = Depends(get_db),
) -> BaselineResponse:
    """Apply a user edit to one item of the distillate.

    Sets ``user_edited=true`` on the matched item and merges the
    supplied fields in.  404 when no baseline exists or the item
    label doesn't exist in the named category.
    """
    pv = get_active_baseline(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no baseline plan for user")

    from argosy.services.plan_distiller_service import set_distillate_item_user_edit

    # Only pass non-None fields so the helper doesn't overwrite omitted ones.
    new_value = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        set_distillate_item_user_edit(
            db,
            plan_version_id=pv.id,
            category=category,
            item_label=item_label,
            new_value=new_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    db.refresh(pv)
    return _build_baseline_response(pv)


# ---------------------------------------------------------------------------
# Wave 2 — draft lifecycle endpoints (T2.13)
# ---------------------------------------------------------------------------

from datetime import datetime, timezone


class HorizonSectionView(BaseModel):
    horizon: str
    freshness_expected: str
    status: str
    posture: str
    targets: list[dict] = []
    themes: list[dict] = []
    actions: list[dict] = []
    speculative_candidates: list[dict] = []
    deltas_from_prior: list[dict] = []
    rationale: str = ""
    cited_sources: list[str] = []


class DraftResponse(BaseModel):
    plan_version_id: int
    drafted_at: str
    derived_from_id: int | None
    decision_run_id: str | None
    horizon_long: HorizonSectionView | None
    horizon_medium: HorizonSectionView | None
    horizon_short: HorizonSectionView | None
    horizon_long_md: str | None
    horizon_medium_md: str | None
    horizon_short_md: str | None


class AcceptResponse(BaseModel):
    status: str
    new_current_id: int


class RejectRequest(BaseModel):
    reason: str
    guidance: str = ""


def _horizon_view(json_str: str | None) -> HorizonSectionView | None:
    if not json_str:
        return None
    payload = json.loads(json_str)
    return HorizonSectionView(**payload)


@router.get("/draft", response_model=DraftResponse)
def get_draft(user_id: str, db: Session = Depends(get_db)) -> DraftResponse:
    from argosy.state.queries import get_pending_draft

    pv = get_pending_draft(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no pending draft for user")
    return DraftResponse(
        plan_version_id=pv.id,
        drafted_at=pv.imported_at.isoformat(),
        derived_from_id=pv.derived_from_id,
        decision_run_id=pv.decision_run_id,
        horizon_long=_horizon_view(pv.horizon_long_json),
        horizon_medium=_horizon_view(pv.horizon_medium_json),
        horizon_short=_horizon_view(pv.horizon_short_json),
        horizon_long_md=pv.horizon_long_md,
        horizon_medium_md=pv.horizon_medium_md,
        horizon_short_md=pv.horizon_short_md,
    )


@router.post("/draft/{draft_id}/accept", response_model=AcceptResponse)
def post_draft_accept(
    draft_id: int,
    user_id: str,
    db: Session = Depends(get_db),
) -> AcceptResponse:
    from argosy.state.queries import get_current_plan

    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found for user")

    now = datetime.now(timezone.utc)
    prior = get_current_plan(db, user_id)
    if prior is not None:
        prior.role = "superseded"
        prior.superseded_at = now

    pv.role = "current"
    pv.accepted_at = now
    pv.accepted_by_user_id = user_id
    db.commit()

    _publish("plan.draft.accepted", {"user_id": user_id, "draft_id": draft_id})
    _publish("plan.current.changed", {"user_id": user_id, "current_id": pv.id})

    return AcceptResponse(status="accepted", new_current_id=pv.id)


@router.post("/draft/{draft_id}/reject")
def post_draft_reject(
    draft_id: int,
    user_id: str,
    body: RejectRequest,
    db: Session = Depends(get_db),
):
    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found for user")

    pv.role = "superseded"
    pv.superseded_at = datetime.now(timezone.utc)
    # Stash the rejection reason in synthesis_inputs_json for forensics.
    inputs = json.loads(pv.synthesis_inputs_json) if pv.synthesis_inputs_json else {}
    inputs["rejection_reason"] = body.reason
    inputs["rejection_guidance"] = body.guidance
    pv.synthesis_inputs_json = json.dumps(inputs)
    db.commit()
    _publish(
        "plan.draft.rejected",
        {"user_id": user_id, "draft_id": draft_id, "reason": body.reason},
    )
    return {"status": "rejected", "draft_id": draft_id}


# ---------------------------------------------------------------------------
# Wave 2 — per-delta accept + edit endpoints (T2.14)
# ---------------------------------------------------------------------------


class DeltaEditRequest(BaseModel):
    proposed: dict | None = None
    user_edit_note: str | None = None


def _find_delta_horizon_field(pv, item_id: str) -> tuple[str, dict, dict] | None:
    """Find which horizon contains the given item_id; return (field, payload, delta)."""
    for field in ("horizon_long_json", "horizon_medium_json", "horizon_short_json"):
        raw = getattr(pv, field)
        if not raw:
            continue
        payload = json.loads(raw)
        for d in payload.get("deltas_from_prior") or []:
            if d.get("item_id") == item_id:
                return field, payload, d
    return None


@router.post("/draft/{draft_id}/items/{item_id}/accept")
def post_delta_accept(
    draft_id: int,
    item_id: str,
    user_id: str,
    db: Session = Depends(get_db),
):
    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found")
    found = _find_delta_horizon_field(pv, item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="item_id not found in any horizon delta list")
    field, payload, delta = found
    delta["accepted"] = True
    setattr(pv, field, json.dumps(payload))
    db.commit()
    _publish(
        "plan.draft.delta.accepted",
        {"user_id": user_id, "draft_id": draft_id, "item_id": item_id},
    )
    return {"status": "accepted", "draft_id": draft_id, "item_id": item_id}


@router.patch("/draft/{draft_id}/items/{item_id}")
def patch_delta_edit(
    draft_id: int,
    item_id: str,
    user_id: str,
    body: DeltaEditRequest,
    db: Session = Depends(get_db),
):
    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found")
    found = _find_delta_horizon_field(pv, item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="item_id not found")
    field, payload, delta = found
    if body.proposed is not None:
        delta["proposed"] = body.proposed
    if body.user_edit_note is not None:
        delta["user_edit_note"] = body.user_edit_note
    delta["user_edited"] = True
    setattr(pv, field, json.dumps(payload))
    db.commit()
    _publish(
        "plan.draft.delta.edited",
        {"user_id": user_id, "draft_id": draft_id, "item_id": item_id},
    )
    return {"status": "edited", "draft_id": draft_id, "item_id": item_id}


__all__ = [
    "AcceptResponse",
    "BaselineResponse",
    "DeltaEditRequest",
    "DistillateItemEditRequest",
    "DraftResponse",
    "HorizonSectionView",
    "RejectRequest",
    "_publish",
    "get_db",
    "router",
]
