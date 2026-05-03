"""Intake wizard API (SDD §11.1 #9, Phase 7).

Endpoints:
  - POST /api/intake/turn      — drive one Q→A turn

The page presents the question, collects the answer, advances stages,
shows confidence flags and missing-data warnings. The CLI logic (intake
agent) is the same; this route exposes it via HTTP.

For Phase 7 we only wire the prompt-builder + a stub agent path: tests
inject a mocked `IntakeAgent`. Production wires through the real agent.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from argosy.agents.intake import IntakeAgent, IntakeTurnOutput
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import User, UserContext

_log = get_logger("argosy.api.intake")
router = APIRouter(prefix="/intake", tags=["intake"])


# ----------------------------------------------------------------------
# DI hook so tests can mock the agent without spinning Anthropic.
# ----------------------------------------------------------------------

_AGENT_FACTORY = None


def set_intake_agent_factory(factory) -> None:
    """Override the agent factory for tests. Called as
    `set_intake_agent_factory(lambda user_id: MyMock(user_id=user_id))`."""
    global _AGENT_FACTORY
    _AGENT_FACTORY = factory


def reset_intake_agent_factory() -> None:
    global _AGENT_FACTORY
    _AGENT_FACTORY = None


class TurnRequest(BaseModel):
    user_id: str = "ariel"
    last_user_message: str = ""
    history_excerpt: str = ""
    # Optional: explicit current_stage; if absent, we read from
    # user_context.current_stage (or default to stage_1).
    current_stage: str | None = None


class TurnResponse(BaseModel):
    stage: str
    question_for_user: str
    stage_complete: bool
    next_stage: str | None
    confidence: str
    cited_sources: list[str]
    notes_for_orchestrator: str
    context_updates: list[dict[str, Any]]


@router.post("/turn", response_model=TurnResponse)
async def post_turn(req: TurnRequest) -> TurnResponse:
    """Drive one intake turn. Resolves current_stage if absent.

    Phase 7 keeps the actual `user_context` mutation in the CLI path
    (see `argosy.cli.intake`); this route is the surface the dashboard
    talks to. The orchestrator merges `context_updates` after the user
    confirms.
    """
    # Resolve current stage.
    stage = req.current_stage
    if stage is None:
        async with db_mod.get_session() as session:
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == req.user_id)
                )
            ).scalar_one_or_none()
            if ctx is None or ctx.current_stage is None:
                stage = "stage_1"
            elif ctx.current_stage == "complete":
                stage = "stage_6"
            else:
                stage = ctx.current_stage

    factory = _AGENT_FACTORY
    if factory is None:
        agent = IntakeAgent(user_id=req.user_id)
    else:
        agent = factory(req.user_id)

    accumulated = ""
    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(
                select(UserContext).where(UserContext.user_id == req.user_id)
            )
        ).scalar_one_or_none()
        if ctx is not None:
            parts = []
            if ctx.identity_yaml:
                parts.append("# identity\n" + ctx.identity_yaml)
            if ctx.goals_yaml:
                parts.append("# goals\n" + ctx.goals_yaml)
            if ctx.constraints_yaml:
                parts.append("# constraints\n" + ctx.constraints_yaml)
            accumulated = "\n\n".join(parts)

    try:
        report = await agent.run(
            current_stage=stage,
            accumulated_context=accumulated,
            last_user_message=req.last_user_message,
            history_excerpt=req.history_excerpt,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("intake.turn_failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    out: IntakeTurnOutput = report.output  # type: ignore[assignment]
    return TurnResponse(
        stage=out.stage,
        question_for_user=out.question_for_user,
        stage_complete=out.stage_complete,
        next_stage=out.next_stage,
        confidence=out.confidence.value if out.confidence else "MEDIUM",
        cited_sources=out.cited_sources,
        notes_for_orchestrator=out.notes_for_orchestrator,
        context_updates=[u.model_dump() for u in out.context_updates],
    )


@router.get("/status")
async def get_status(user_id: str = Query("ariel")) -> dict[str, Any]:
    """Lightweight status — what stage the user is on."""
    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(select(UserContext).where(UserContext.user_id == user_id))
        ).scalar_one_or_none()
        user_exists = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none() is not None
    return {
        "user_id": user_id,
        "user_exists": user_exists,
        "current_stage": (ctx.current_stage if ctx else None) or "stage_1",
    }


__all__ = ["router", "set_intake_agent_factory", "reset_intake_agent_factory"]
