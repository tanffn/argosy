"""Advisor API — persistent gap-tracker panel (Phase 1 reframe).

Endpoints:
  - POST /api/advisor/turn  — drive one turn (gap_driven OR user_driven)
  - GET  /api/advisor/gaps  — current GapStatus as JSON, for the sidebar

Backwards-compat: the legacy `/api/intake/turn`, `/api/intake/upload`,
`/api/intake/file-to-text`, `/api/intake/status` endpoints still exist
(see `argosy.api.routes.intake`) and continue to use the IntakeAgent.
The two routes share the persist + auto-advance logic via the
`_persist_turn(...)` helper exported from this module.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from argosy.agents.advisor import AdvisorAgent, AdvisorTurnOutput
from argosy.agents.gap_tracker import (
    FieldSpec,
    compute_field_timestamps,
    field_by_path,
    gap_status,
    gaps_for_prompt,
    pick_gap_driven_target,
)
from argosy.agents.intake import IntakeTurnOutput
from argosy.agents.intake_fields import stage_status
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow
from argosy.state.models import User, UserContext

_log = get_logger("argosy.api.advisor")
router = APIRouter(prefix="/advisor", tags=["advisor"])


# ----------------------------------------------------------------------
# DI hooks
# ----------------------------------------------------------------------

_AGENT_FACTORY = None


def set_advisor_agent_factory(factory) -> None:
    """Override the agent factory for tests."""
    global _AGENT_FACTORY
    _AGENT_FACTORY = factory


def reset_advisor_agent_factory() -> None:
    global _AGENT_FACTORY
    _AGENT_FACTORY = None


# ----------------------------------------------------------------------
# Request / response models
# ----------------------------------------------------------------------


class AdvisorTurnRequest(BaseModel):
    user_id: str = "ariel"
    last_user_message: str = ""
    history_excerpt: str = ""
    current_stage: str | None = None
    # New: optional dotted path the user clicked in the sidebar so the
    # agent focuses on that gap (and its cluster). Ignored in user_driven mode.
    target_field: str | None = None


class AdvisorTurnResponse(BaseModel):
    stage: str
    question_for_user: str
    stage_complete: bool
    next_stage: str | None
    confidence: str
    cited_sources: list[str]
    notes_for_orchestrator: str
    context_updates: list[dict[str, Any]]
    intake_session_id: str
    mode: str


class GapItemDTO(BaseModel):
    path: str
    label: str
    section: str
    freshness: str
    priority: int
    state: str  # "fresh" | "missing" | "stale"
    last_updated: str | None  # ISO 8601, or None


class GapStatusResponse(BaseModel):
    user_id: str
    current_stage: str
    items: list[GapItemDTO]
    counts: dict[str, int]


# ----------------------------------------------------------------------
# Mode classifier
# ----------------------------------------------------------------------


def classify_mode(last_user_message: str) -> str:
    """Return 'gap_driven' or 'user_driven' based on the request shape.

    Rules (per the brief):
      - empty message (page load)        → gap_driven
      - ends with "?"                    → user_driven
      - statement (no "?")               → user_driven
                                           (acknowledge + log + ask follow-up)
    """
    msg = (last_user_message or "").strip()
    if not msg:
        return "gap_driven"
    return "user_driven"


# ----------------------------------------------------------------------
# Shared persist helper — called by both /api/advisor/turn and
# /api/intake/turn so we DRY the audit-log + auto-advance logic.
# ----------------------------------------------------------------------


async def _persist_turn(
    *,
    user_id: str,
    stage: str,
    session_id: str | None,
    report,
    out: IntakeTurnOutput | AdvisorTurnOutput,
    apply_turn_update,
) -> tuple[str | None, str | None]:
    """Stamp agent_reports + apply context_updates + auto-advance stage.

    Returns (resolved_session_id, next_current_stage_or_None_if_unchanged).
    `apply_turn_update` is the YAML-merge helper from the intake route
    (passed in to avoid a circular import).
    """
    next_stage_default = {
        "stage_1": "stage_2",
        "stage_2": "stage_3",
        "stage_3": "stage_4",
        "stage_4": "stage_5",
        "stage_5": "stage_6",
        "stage_6": "stage_7",
        "stage_7": "stage_8",
        "stage_8": "stage_9",
        "stage_9": "stage_10",
        "stage_10": "stage_11",
        "stage_11": "complete",
    }.get(stage, "complete")

    advanced_to: str | None = None

    async with db_mod.get_session() as session:
        ar_row = AgentReportRow(
            user_id=user_id,
            agent_role=report.agent_role,
            decision_id=None,
            intake_session_id=session_id,
            prompt_hash=report.prompt_hash,
            response_text=report.response_text,
            tokens_in=report.tokens_in,
            tokens_out=report.tokens_out,
            cost_usd=report.cost_usd,
            model=report.model,
            confidence=(report.confidence.value if report.confidence else None),
        )
        session.add(ar_row)

        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if user is None:
            user = User(id=user_id)
            session.add(user)
            await session.flush()

        ctx = (
            await session.execute(
                select(UserContext).where(UserContext.user_id == user_id)
            )
        ).scalar_one_or_none()
        if ctx is None:
            ctx = UserContext(user_id=user_id)
            session.add(ctx)
            await session.flush()

        # Merge each context_update into the right YAML section.
        try:
            for u in out.context_updates:
                target = u.target_section
                patch = u.yaml_patch or ""
                if not patch.strip():
                    continue
                if target == "identity":
                    ctx.identity_yaml = apply_turn_update(
                        ctx.identity_yaml or "", patch
                    )
                elif target == "goals":
                    ctx.goals_yaml = apply_turn_update(ctx.goals_yaml or "", patch)
                elif target == "constraints":
                    ctx.constraints_yaml = apply_turn_update(
                        ctx.constraints_yaml or "", patch
                    )
        except Exception:  # pragma: no cover - defensive
            _log.exception(
                "advisor.turn.context_update_apply_failed",
                intake_session_id=session_id,
            )

        post_status = stage_status(
            identity_yaml=ctx.identity_yaml or "",
            goals_yaml=ctx.goals_yaml or "",
            constraints_yaml=ctx.constraints_yaml or "",
            stage=stage,
        )
        post_complete = len(post_status["missing"]) == 0

        if out.stage_complete and out.next_stage:
            ctx.current_stage = out.next_stage
            advanced_to = out.next_stage
        elif post_complete:
            ctx.current_stage = next_stage_default
            advanced_to = next_stage_default
            _log.info(
                "advisor.stage_auto_advanced",
                from_stage=stage,
                to_stage=next_stage_default,
                intake_session_id=session_id,
            )
        elif ctx.current_stage is None:
            ctx.current_stage = out.stage

        if session_id and not ctx.intake_session_id:
            ctx.intake_session_id = session_id

        await session.commit()

    return session_id, advanced_to


# ----------------------------------------------------------------------
# /turn
# ----------------------------------------------------------------------


@router.post("/turn", response_model=AdvisorTurnResponse)
async def post_turn(req: AdvisorTurnRequest) -> AdvisorTurnResponse:
    """Drive one advisor turn. Two modes:

    - gap_driven: agent asks the next missing/stale gap (batched).
    - user_driven: agent answers the user's message (and may log
      context_updates extracted from it, plus a related follow-up).
    """
    # local import to avoid circulars on module load
    from argosy.api.routes.intake import _apply_turn_update

    # Resolve current stage + intake_session_id (same rules as intake/turn).
    stage = req.current_stage
    session_id: str | None = None
    accumulated = ""
    last_updated_per_field: dict[str, Any] = {}

    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(
                select(UserContext).where(UserContext.user_id == req.user_id)
            )
        ).scalar_one_or_none()

        if stage is None:
            if ctx is None or ctx.current_stage is None:
                stage = "stage_1"
            elif ctx.current_stage == "complete":
                stage = "stage_11"
            else:
                stage = ctx.current_stage

        if stage == "stage_1" and (ctx is None or ctx.current_stage in (None, "complete")):
            session_id = uuid4().hex
            if ctx is not None:
                ctx.intake_session_id = session_id
                await session.commit()
        elif ctx is not None:
            session_id = ctx.intake_session_id or uuid4().hex
            if ctx.intake_session_id is None:
                ctx.intake_session_id = session_id
                await session.commit()
        else:
            session_id = uuid4().hex

        if ctx is not None:
            parts = []
            if ctx.identity_yaml:
                parts.append("# identity\n" + ctx.identity_yaml)
            if ctx.goals_yaml:
                parts.append("# goals\n" + ctx.goals_yaml)
            if ctx.constraints_yaml:
                parts.append("# constraints\n" + ctx.constraints_yaml)
            accumulated = "\n\n".join(parts)

    # Compute field timestamps from the audit log; pass to gap_status.
    last_updated_per_field = await compute_field_timestamps(req.user_id)

    identity_yaml = ""
    goals_yaml = ""
    constraints_yaml = ""
    if accumulated:
        async with db_mod.get_session() as session:
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == req.user_id)
                )
            ).scalar_one_or_none()
            if ctx is not None:
                identity_yaml = ctx.identity_yaml or ""
                goals_yaml = ctx.goals_yaml or ""
                constraints_yaml = ctx.constraints_yaml or ""

    status = gap_status(
        identity_yaml=identity_yaml,
        goals_yaml=goals_yaml,
        constraints_yaml=constraints_yaml,
        last_updated_per_field=last_updated_per_field,
    )
    answered_paths, missing_paths = gaps_for_prompt(status)

    mode = classify_mode(req.last_user_message)

    factory = _AGENT_FACTORY
    if factory is None:
        agent = AdvisorAgent(user_id=req.user_id)
    else:
        agent = factory(req.user_id)

    try:
        report = await agent.run(
            current_stage=stage,
            accumulated_context=accumulated,
            last_user_message=req.last_user_message,
            history_excerpt=req.history_excerpt,
            answered_fields=answered_paths,
            missing_fields=missing_paths,
            mode=mode,
            target_field=req.target_field,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.exception("advisor.turn_failed", intake_session_id=session_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    out: AdvisorTurnOutput = report.output  # type: ignore[assignment]

    await _persist_turn(
        user_id=req.user_id,
        stage=stage,
        session_id=session_id,
        report=report,
        out=out,
        apply_turn_update=_apply_turn_update,
    )

    return AdvisorTurnResponse(
        stage=out.stage,
        question_for_user=out.question_for_user,
        stage_complete=out.stage_complete,
        next_stage=out.next_stage,
        confidence=out.confidence.value if out.confidence else "MEDIUM",
        cited_sources=out.cited_sources,
        notes_for_orchestrator=out.notes_for_orchestrator,
        context_updates=[u.model_dump() for u in out.context_updates],
        intake_session_id=session_id or "",
        mode=getattr(out, "mode", mode) or mode,
    )


# ----------------------------------------------------------------------
# /gaps
# ----------------------------------------------------------------------


@router.get("/gaps", response_model=GapStatusResponse)
async def get_gaps(user_id: str = Query("ariel")) -> GapStatusResponse:
    """Return the full GapStatus as a flat list with state markers.

    The UI calls this on mount and after each turn to re-render the
    sidebar tracker.
    """
    identity_yaml = ""
    goals_yaml = ""
    constraints_yaml = ""
    current_stage = "stage_1"

    async with db_mod.get_session() as session:
        ctx = (
            await session.execute(
                select(UserContext).where(UserContext.user_id == user_id)
            )
        ).scalar_one_or_none()
        if ctx is not None:
            identity_yaml = ctx.identity_yaml or ""
            goals_yaml = ctx.goals_yaml or ""
            constraints_yaml = ctx.constraints_yaml or ""
            if ctx.current_stage:
                current_stage = ctx.current_stage

    last_updated_per_field = await compute_field_timestamps(user_id)
    status = gap_status(
        identity_yaml=identity_yaml,
        goals_yaml=goals_yaml,
        constraints_yaml=constraints_yaml,
        last_updated_per_field=last_updated_per_field,
    )

    items: list[GapItemDTO] = []
    for f in status.fresh:
        items.append(_field_to_dto(f, "fresh", last_updated_per_field.get(f.path)))
    for f in status.missing:
        items.append(_field_to_dto(f, "missing", None))
    for f, ts in status.stale:
        items.append(_field_to_dto(f, "stale", ts))

    # Re-sort by canonical (stage, then priority, then path) so the UI
    # order is stable regardless of insertion order above.
    from argosy.agents.gap_tracker import all_fields as _all_fields  # local import

    canonical_order = {f.path: i for i, f in enumerate(_all_fields())}
    items.sort(key=lambda i: canonical_order.get(i.path, 999))

    counts = {
        "fresh": len(status.fresh),
        "missing": len(status.missing),
        "stale": len(status.stale),
    }

    return GapStatusResponse(
        user_id=user_id,
        current_stage=current_stage,
        items=items,
        counts=counts,
    )


def _field_to_dto(spec: FieldSpec, state: str, ts) -> GapItemDTO:
    return GapItemDTO(
        path=spec.path,
        label=spec.label,
        section=spec.section,
        freshness=spec.freshness,
        priority=spec.priority,
        state=state,
        last_updated=(ts.isoformat() if ts is not None else None),
    )


__all__ = [
    "AdvisorTurnRequest",
    "AdvisorTurnResponse",
    "GapItemDTO",
    "GapStatusResponse",
    "_persist_turn",
    "classify_mode",
    "field_by_path",
    "pick_gap_driven_target",
    "reset_advisor_agent_factory",
    "router",
    "set_advisor_agent_factory",
]
