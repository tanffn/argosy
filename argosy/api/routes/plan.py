"""Plan-related routes (Phase 2).

  GET  /api/plan/current  — latest plan version + latest critique
  POST /api/plan/critique — queue a re-critique on demand (returns the
                            new critique inline once complete; Phase 2
                            runs synchronously since there is no job
                            queue yet).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.api.events import publish_event
from argosy.state import db as db_mod
from argosy.state.models import PlanCritique, PlanVersion, UserContext

router = APIRouter(prefix="/plan", tags=["plan"])


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


__all__ = ["router"]
