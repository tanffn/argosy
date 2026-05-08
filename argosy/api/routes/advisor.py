"""Advisor API — persistent gap-tracker panel (Phase 1 reframe).

Endpoints:
  - POST /api/advisor/turn         — drive one turn (gap_driven OR user_driven)
  - GET  /api/advisor/gaps         — current GapStatus as JSON, for the sidebar
  - GET  /api/advisor/home-brief   — composed glanceable summary for home page

Backwards-compat: the legacy `/api/intake/turn`, `/api/intake/upload`,
`/api/intake/file-to-text`, `/api/intake/status` endpoints still exist
(see `argosy.api.routes.intake`) and continue to use the IntakeAgent.
The two routes share the persist + auto-advance logic via the
`_persist_turn(...)` helper exported from this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.agents.advisor import AdvisorAgent, AdvisorTurnOutput
from argosy.agents.advisor_amendment_types import AmendmentResultDTO
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
from argosy.state.models import (
    DailyBrief,
    PensionFundSnapshot,
    PlanVersion,
    User,
    UserContext,
)
from argosy.state.queries import get_latest_investor_event

# Sync `get_db` dependency from the plan route. Both /turn (amendment
# dispatch) and /check-in need a sync Session; the existing
# `client_with_db` fixture already overrides this dependency.
from argosy.api.routes.plan import get_db  # noqa: E402

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
    # Wave 4: populated when the advisor classified a plan-amendment
    # request in this turn. None on plain Q&A / gap-driven turns.
    amendment: AmendmentResultDTO | None = None


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

    def _has_open_stage_11_gap(status_obj) -> bool:
        """True iff stage_11 has any missing/stale field for this user.

        Used to decide whether mapping ``complete → stage_11`` is
        legitimate (an existing user who finished intake before stage_11
        was added must NOT be thrown back unless there's actually a
        stage_11 gap to fill).
        """
        from argosy.agents.gap_tracker import STAGE_FIELDS as _SF

        stage_11_paths = {f.path for f in _SF.get("stage_11", [])}
        if not stage_11_paths:
            return False
        # Both `missing` and `stale` count as open gaps.
        for f in status_obj.missing:
            if f.path in stage_11_paths:
                return True
        for f, _ts in status_obj.stale:
            if f.path in stage_11_paths:
                return True
        return False

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

        # Compute the post-update GapStatus once so we can suppress
        # spurious "complete → stage_11" redirects: existing users who
        # finished intake before stage_11 (special situations) was added
        # have ``current_stage="complete"`` and no stage_11 gap. They
        # must NOT be thrown back to stage_11 just because the agent
        # claimed ``next_stage="stage_11"`` or the default-map points
        # there. Only redirect if there's an actual missing/stale
        # stage_11 field.
        from argosy.agents.gap_tracker import gap_status as _gap_status

        full_status = _gap_status(
            identity_yaml=ctx.identity_yaml or "",
            goals_yaml=ctx.goals_yaml or "",
            constraints_yaml=ctx.constraints_yaml or "",
        )

        def _resolve_next(claimed: str | None) -> str | None:
            """Veto a complete→stage_11 hop when the user has no real gap."""
            if claimed != "stage_11":
                return claimed
            if stage != "stage_11" and not _has_open_stage_11_gap(full_status):
                # User is already past stage_11 (or never had a gap there).
                # Pin them at "complete" instead of bouncing back.
                return "complete"
            return claimed

        if out.stage_complete and out.next_stage:
            resolved = _resolve_next(out.next_stage)
            if resolved is not None:
                ctx.current_stage = resolved
                advanced_to = resolved
        elif post_complete:
            resolved = _resolve_next(next_stage_default)
            if resolved is not None:
                ctx.current_stage = resolved
                advanced_to = resolved
                _log.info(
                    "advisor.stage_auto_advanced",
                    from_stage=stage,
                    to_stage=resolved,
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
async def post_turn(
    req: AdvisorTurnRequest,
    db: Session = Depends(get_db),
) -> AdvisorTurnResponse:
    """Drive one advisor turn. Two modes:

    - gap_driven: agent asks the next missing/stale gap (batched).
    - user_driven: agent answers the user's message (and may log
      context_updates extracted from it, plus a related follow-up).

    Wave 4: when the agent's structured output carries an `amendment`
    field (a chat-borne plan-change request), classify and dispatch:
    Small applies inline; Medium/Large open a DecisionRun row and spawn
    a worker. The dispatch result rides back on `AdvisorTurnResponse.amendment`.
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
                # Existing-user backwards-compat: stage_11 (special
                # situations) was added after some users had already
                # finished intake. Only re-enter stage_11 if there's
                # actually an open gap there; otherwise stay "complete".
                from argosy.agents.gap_tracker import (
                    STAGE_FIELDS as _SF,
                )
                from argosy.agents.gap_tracker import (
                    gap_status as _gs,
                )

                _post = _gs(
                    identity_yaml=ctx.identity_yaml or "",
                    goals_yaml=ctx.goals_yaml or "",
                    constraints_yaml=ctx.constraints_yaml or "",
                )
                _stage_11_paths = {f.path for f in _SF.get("stage_11", [])}
                _missing = {f.path for f in _post.missing}
                _stale = {f.path for f, _t in _post.stale}
                if (_missing | _stale) & _stage_11_paths:
                    stage = "stage_11"
                else:
                    stage = "complete"
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

    # The advisor agent doesn't know the synthetic ``complete`` stage —
    # it only understands stage_1..stage_11. Map ``complete`` to
    # stage_11 for the agent call (the last real stage); the persist
    # helper's stage_11 veto then keeps the user pinned at ``complete``
    # if there's no actual gap to fill.
    agent_stage = "stage_11" if stage == "complete" else stage

    # Wave 4: gate the AMENDMENT INTENT DETECTION block on whether the
    # user has a current plan. Without this kwarg the dispatcher path is
    # dead code — the LLM never receives the classification instructions
    # and never emits an `amendment` field.
    async with db_mod.get_session() as _hcp_session:
        _hcp_row = (
            await _hcp_session.execute(
                select(PlanVersion.id)
                .where(
                    PlanVersion.user_id == req.user_id,
                    PlanVersion.role == "current",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    has_current_plan = _hcp_row is not None

    try:
        report = await agent.run(
            current_stage=agent_stage,
            accumulated_context=accumulated,
            last_user_message=req.last_user_message,
            history_excerpt=req.history_excerpt,
            answered_fields=answered_paths,
            missing_fields=missing_paths,
            mode=mode,
            target_field=req.target_field,
            has_current_plan=has_current_plan,
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

    # Wave 4: amendment dispatch.
    # When the advisor classified a plan-amendment request, route it:
    #   - Small (after classifier confirms tighten + delta) → run_small inline.
    #   - Medium/Large (or escalated Small) → dispatch_async opens a
    #     DecisionRun and spawns a worker thread; we return immediately.
    # Failures here are logged and swallowed: the chat turn itself
    # already succeeded and we don't want a dispatcher hiccup to break
    # the user's conversation. The amendment field stays None.
    amendment_dto: AmendmentResultDTO | None = None
    advisor_amendment = getattr(out, "amendment", None)
    if advisor_amendment is not None:
        from argosy.orchestrator.flows.plan_amendment import (
            classify,
            dispatch_async,
            run_small,
        )
        from argosy.orchestrator.flows.plan_amendment._types import EffectiveTier

        classified = classify(advisor_amendment)
        try:
            if classified.effective_tier == EffectiveTier.SMALL:
                # classified.proposed_delta is guaranteed non-None for SMALL.
                small_intent = advisor_amendment.model_copy(
                    update={"proposed_delta": classified.proposed_delta},
                )
                amendment_dto = run_small(
                    db,
                    user_id=req.user_id,
                    message=req.last_user_message,
                    intent=small_intent,
                )
            else:
                # Classifier may have escalated small→medium; rebuild the
                # intent so the dispatcher sees the effective tier.
                effective_intent = advisor_amendment.model_copy(
                    update={"tier": classified.effective_tier.value},
                )
                amendment_dto = dispatch_async(
                    db,
                    user_id=req.user_id,
                    message=req.last_user_message,
                    tier=classified.effective_tier.value,
                    intent=effective_intent,
                    cancel_existing=getattr(
                        advisor_amendment, "cancel_existing", False,
                    ),
                )
        except Exception as exc:
            # Don't fail the chat turn over a dispatch error.
            # _log.exception captures the traceback so the audit log
            # has enough to debug a dispatcher hiccup post-hoc.
            _log.exception(
                "advisor.turn.amendment_dispatch_failed",
                user_id=req.user_id,
                error=str(exc),
            )
            amendment_dto = None

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
        amendment=amendment_dto,
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


# ----------------------------------------------------------------------
# /home-brief — composed glanceable summary for the home page
# ----------------------------------------------------------------------
#
# Stitches three lines from already-cached state — gap tracker, latest
# daily brief, and the most recent watchlist signal. The signal bullet
# prefers Phase 4 investor events (SEC Form 4 / 13F / TipRanks) when
# they exist and falls back to the most recent pension fund snapshot.
# NO new LLM call. Per-user cache via `kv_cache` (CacheKind.UI),
# provider="advisor_home_brief", TTL 30 minutes.

# Why-it-matters one-liners keyed by gap field path. Used to add a tiny
# "because X" clause after the gap label, so a missing field reads
# instructively rather than just bureaucratically. Falls back silently
# when a path isn't in the dict — the bullet just shows the label.
_GAP_REASON: dict[str, str] = {
    "identity.tax_residency": "anchors every tax-side decision",
    "identity.user_citizenship": "drives FATCA / PFIC exposure",
    "identity.spouse_citizenship": "affects estate planning",
    "identity.spouse_tax_residency": "affects joint-filing options",
    "identity.children": "shapes 529 / education planning",
    "identity.user_date_of_birth": "needed for retirement timing",
    "identity.spouse_date_of_birth": "needed for survivor benefits",
    "identity.dependents_count": "affects withholding & deductions",
    "identity.primary_residence_country": "drives reporting jurisdiction",
    "identity.employment_status": "affects income forecasting",
    "identity.marital_status": "drives filing status & estate",
    "goals.retirement_target_year": "anchors withdrawal planning",
    "goals.target_annual_income": "anchors withdrawal planning",
    "goals.risk_tolerance": "drives allocation guardrails",
    "goals.investment_time_horizon_years": "drives allocation guardrails",
    "goals.near_term_spending": "affects liquidity reserves",
    "goals.lifestyle_aspirations": "informs goal sequencing",
}


class HomeBriefBullet(BaseModel):
    kind: str  # "draft_plan" | "gap" | "portfolio" | "signal"
    text: str


class HomeBriefCTA(BaseModel):
    label: str
    href: str


class HomeBriefResponse(BaseModel):
    headline: str
    bullets: list[HomeBriefBullet]
    cta: HomeBriefCTA
    generated_at: str


def _greeting_for_hour(hour: int) -> str:
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 18:
        return "Good afternoon"
    return "Good evening"


def _trim_summary(text: str, max_len: int = 140) -> str:
    """Pick a representative one-liner from the daily brief summary."""
    if not text:
        return ""
    # Prefer the first non-empty line; fall back to a slice.
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = first_line or text.strip()
    if len(line) <= max_len:
        return line
    return line[: max_len - 1].rstrip() + "…"


async def _draft_bullet(user_id: str) -> HomeBriefBullet | None:
    """If the user has a pending draft, surface it as the top bullet.

    Mirrors ``argosy.state.queries.get_pending_draft`` but executes against
    the async session because the home-brief route is fully async. Defensive
    on DB hiccups (missing table on stale dev schemas, etc.) — degrades to
    None rather than 500-ing the home page.
    """
    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    try:
        async with db_mod.get_session() as session:
            pv = (
                await session.execute(
                    select(PlanVersion).where(
                        PlanVersion.user_id == user_id,
                        PlanVersion.role == "draft",
                    )
                )
            ).scalar_one_or_none()
    except (SQLAlchemyError, OperationalError):
        _log.debug(
            "home_brief.draft_bullet_db_skipped", user_id=user_id, exc_info=True
        )
        return None
    if pv is None:
        return None
    imported = pv.imported_at
    date_str = imported.strftime("%Y-%m-%d") if imported is not None else "recently"
    return HomeBriefBullet(
        kind="draft_plan",
        text=(
            f"Monthly plan revision drafted on {date_str} — ready to review."
        ),
    )


async def _gap_bullet(user_id: str) -> HomeBriefBullet | None:
    """Top missing/stale field, with one-clause why-it-matters."""
    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    identity_yaml = ""
    goals_yaml = ""
    constraints_yaml = ""

    try:
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
    except (SQLAlchemyError, OperationalError):
        # Stale schema / connection hiccup — degrade rather than 500
        # the home page. Re-raise on anything else (genuine bugs).
        _log.debug(
            "home_brief.gap_bullet_db_skipped", user_id=user_id, exc_info=True
        )
        return None

    last_updated_per_field = await compute_field_timestamps(user_id)
    status = gap_status(
        identity_yaml=identity_yaml,
        goals_yaml=goals_yaml,
        constraints_yaml=constraints_yaml,
        last_updated_per_field=last_updated_per_field,
    )

    target = pick_gap_driven_target(status)

    # Empty user (no YAML at all) — surface a friendly intake-invite
    # rather than the bureaucratic "Tax residency still missing".
    if not (identity_yaml or goals_yaml or constraints_yaml):
        return HomeBriefBullet(
            kind="gap",
            text="Let's start with intake — answer a few questions so I can plan with you.",
        )

    if target is None:
        # Fully fresh catalog — no gap bullet at all.
        return None

    reason = _GAP_REASON.get(target.path)
    stale_paths = {s.path for s, _ in status.stale}
    if target.path in stale_paths:
        verb = "due for refresh"
    else:
        verb = "still missing"

    if reason:
        text = f"{target.label} {verb} — {reason}."
    else:
        text = f"{target.label} {verb}."
    return HomeBriefBullet(kind="gap", text=text)


async def _portfolio_bullet(user_id: str) -> HomeBriefBullet | None:
    """One line from the most recent daily brief.

    Defensive on DB hiccups (missing table on stale schemas, etc.) —
    degrades to None rather than 500-ing the home page. We only catch
    SQLAlchemy/DB-shaped errors so that genuine bugs (AttributeError,
    KeyError after a refactor) still surface.

    Note on the (deliberately removed) TSV fallback: ``_find_latest_tsv``
    in ``argosy.api.routes.portfolio`` is a global pick of the newest
    ``*.tsv`` under ``ARGOSY_HOME``, NOT user-scoped. A multi-tenant
    home brief that walked that path would leak Ariel's portfolio into
    Dana's bullets. Until per-user TSV path resolution lands, return
    None when no DailyBrief row exists for this user.
    """
    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    try:
        async with db_mod.get_session() as session:
            row = (
                await session.execute(
                    select(DailyBrief)
                    .where(DailyBrief.user_id == user_id)
                    .order_by(desc(DailyBrief.run_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                line = _trim_summary(row.summary_text or "")
                if line:
                    return HomeBriefBullet(kind="portfolio", text=line)
    except (SQLAlchemyError, OperationalError):
        _log.debug(
            "home_brief.portfolio_bullet_db_skipped", user_id=user_id, exc_info=True
        )

    return None


async def _signal_bullet(user_id: str) -> HomeBriefBullet | None:
    """Most recent watchlist event.

    Preference order:
      1. Phase 4 investor event (SEC Form 4 / 13F / TipRanks /
         CapitolTrades / news) — written by the daily-brief loop into
         ``investor_events``. Most recent by ``occurred_at DESC``,
         capped at 14 days. Stale-by-default investor events (older
         than 14 days) fall through to the pension snapshot rather
         than misleading the user about "today's signal."
      2. Pension fund snapshot (Phase 3 data) — fallback when no fresh
         investor event exists. Capped at 365 days; older snapshots
         omit the bullet entirely (no signal beats a stale signal).

    Both queries are defensive: a missing table on older dev DBs
    (pre-migration) is logged at debug and treated as "no rows" rather
    than 500-ing the home page. We only catch SQLAlchemy / DB-shaped
    errors so genuine bugs surface.
    """
    from datetime import timedelta as _td

    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    now = datetime.now(UTC)
    EVENT_CUTOFF = _td(days=14)
    PENSION_CUTOFF = _td(days=365)

    # 1. Investor events first.
    try:
        event = await get_latest_investor_event(user_id)
    except (SQLAlchemyError, OperationalError):
        _log.debug(
            "home_brief.signal_bullet_investor_skipped",
            user_id=user_id,
            exc_info=True,
        )
        event = None
    if event is not None:
        text = (event.get("headline") or "").strip()
        # Recency check — events older than 14 days are stale signal,
        # not "today's signal". Treat them as absent and fall through.
        occ_iso = event.get("occurred_at") or event.get("ingested_at")
        occ_dt: datetime | None = None
        if occ_iso:
            try:
                occ_dt = datetime.fromisoformat(str(occ_iso).replace("Z", "+00:00"))
                if occ_dt.tzinfo is None:
                    occ_dt = occ_dt.replace(tzinfo=UTC)
            except ValueError:
                occ_dt = None
        is_fresh = occ_dt is None or (now - occ_dt) <= EVENT_CUTOFF
        if text and is_fresh:
            # Match _trim_summary's 140-char cap so the home-brief card
            # has a stable width regardless of source verbosity.
            return HomeBriefBullet(kind="signal", text=_trim_summary(text))

    # 2. Pension snapshot fallback.
    try:
        async with db_mod.get_session() as session:
            row = (
                await session.execute(
                    select(PensionFundSnapshot)
                    .where(PensionFundSnapshot.user_id == user_id)
                    .order_by(desc(PensionFundSnapshot.snapshot_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
    except (SQLAlchemyError, OperationalError):
        _log.debug("home_brief.signal_bullet_skipped", user_id=user_id, exc_info=True)
        return None
    if row is None:
        return None
    # Skip pension snapshots older than 365 days entirely — the user
    # would rather see no signal bullet than a year-old pension stat.
    snap_at = row.snapshot_at
    if snap_at is not None:
        if snap_at.tzinfo is None:
            snap_at = snap_at.replace(tzinfo=UTC)
        if (now - snap_at) > PENSION_CUTOFF:
            return None

    name = row.fund_name or row.fund_id
    rel = row.relative_to_benchmark_pct
    if rel is not None:
        direction = "above" if float(rel) >= 0 else "below"
        return HomeBriefBullet(
            kind="signal",
            text=(
                f"{name} {abs(float(rel)):.1f}% {direction} "
                "benchmark in latest pension snapshot."
            ),
        )
    if row.return_pct_12m is not None:
        return HomeBriefBullet(
            kind="signal",
            text=(
                f"{name} 12m return {float(row.return_pct_12m):.1f}% "
                "in latest pension snapshot."
            ),
        )
    return HomeBriefBullet(
        kind="signal", text=f"New pension snapshot recorded for {name}."
    )


def _time_of_day_greeting(now: datetime) -> str:
    """Build the greeting headline. Pure function so the route can call
    it on every request after a cache hit, never serving a stale
    'Good morning' at 11pm."""
    return f"{_greeting_for_hour(now.hour)}. Here's where you stand."


async def _compose_home_brief_cacheable(user_id: str) -> dict[str, Any]:
    """Compose the *cacheable* portion of the home brief — bullets +
    cta + generated_at. Headline is computed fresh at response time
    (see ``get_home_brief``) so the greeting always matches the
    user's current time-of-day window even on a cache hit."""
    now = datetime.now(UTC)

    bullets: list[HomeBriefBullet] = []
    draft = await _draft_bullet(user_id)
    if draft is not None:
        bullets.append(draft)
    gap = await _gap_bullet(user_id)
    if gap is not None:
        bullets.append(gap)
    portfolio = await _portfolio_bullet(user_id)
    if portfolio is not None:
        bullets.append(portfolio)
    signal = await _signal_bullet(user_id)
    if signal is not None:
        bullets.append(signal)

    # When a draft is pending, swap the CTA so the user lands on the
    # review-draft action rather than the generic advisor entry point.
    cta = (
        HomeBriefCTA(
            label="Review monthly plan", href="/advisor?action=review-draft"
        )
        if draft is not None
        else HomeBriefCTA(label="Talk to advisor", href="/advisor")
    )

    return {
        "bullets": [b.model_dump() for b in bullets],
        "cta": cta.model_dump(),
        "generated_at": now.isoformat(),
    }


@router.get("/home-brief", response_model=HomeBriefResponse)
async def get_home_brief(
    user_id: str = Query("ariel"),
) -> HomeBriefResponse:
    """Compose a 3–5 line glance card for the home page.

    Stitched from existing state — gap tracker, latest daily brief, most
    recent watchlist signal. NO new LLM call.

    Caching: bullets / cta / generated_at are cached per-user for 30
    minutes via `kv_cache` (CacheKind.UI, provider=`advisor_home_brief`).
    The ``headline`` is intentionally NOT cached — we compute it fresh
    at response time so a "Good morning" generated at 7am isn't served
    back to the user at 11pm just because the bullets are still warm.
    """
    async def _fetch() -> dict[str, Any]:
        return await _compose_home_brief_cacheable(user_id)

    data = await cached_call(
        kind=CacheKind.UI,
        provider="advisor_home_brief",
        key=f"user:{user_id}",
        ttl_seconds=30 * 60,
        fetch=_fetch,
    )
    # Build the response by adding a fresh headline on every call.
    return HomeBriefResponse(
        headline=_time_of_day_greeting(datetime.now(UTC)),
        bullets=[HomeBriefBullet(**b) for b in data.get("bullets", [])],
        cta=HomeBriefCTA(**data.get("cta", {"label": "Talk to advisor", "href": "/advisor"})),
        generated_at=data.get("generated_at") or datetime.now(UTC).isoformat(),
    )


# ----------------------------------------------------------------------
# /check-in — user-initiated plan synthesis (spec §7.6)
# ----------------------------------------------------------------------
#
# Reuses the sync `get_db` dependency from `argosy.api.routes.plan` so
# the existing `client_with_db` test fixture's `app.dependency_overrides`
# entry covers this route too. `run_synthesis` is synchronous and takes
# a sync SQLAlchemy `Session`, hence the sync handler here (the rest of
# this module is async, but mixing is fine — FastAPI handles each route
# independently). `get_db` is imported at module top.


class CheckInRequest(BaseModel):
    user_id: str
    guidance: str = ""
    urgency: str = "now"  # currently informational only


class CheckInResponse(BaseModel):
    status: str
    decision_run_id: int
    draft_id: int


@router.post("/check-in", response_model=CheckInResponse, status_code=202)
def post_check_in(
    body: CheckInRequest,
    db: Session = Depends(get_db),
) -> CheckInResponse:
    """User-initiated plan synthesis (spec §7.6).

    Calls ``plan_synthesis.run_synthesis`` with ``trigger="check_in"`` and
    returns the resulting decision_run_id + draft_id. 404 when the user
    has no active baseline plan (the synthesis flow raises
    ``NoBaselineError`` before any DB writes).
    """
    from argosy.orchestrator.flows.plan_synthesis import (
        NoBaselineError,
        run_synthesis,
    )

    try:
        result = run_synthesis(
            db,
            user_id=body.user_id,
            trigger="check_in",
            guidance=body.guidance,
        )
    except NoBaselineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return CheckInResponse(
        status="accepted",
        decision_run_id=result.decision_run_id,
        draft_id=result.draft_id,
    )


# ----------------------------------------------------------------------
# /amendment/{decision_run_id}/cancel — Wave 4
# ----------------------------------------------------------------------
#
# Cancel a running plan-amendment-chat DecisionRun. 404 when the run
# doesn't exist, isn't owned by the user, or isn't a plan-amendment-chat
# run. 409 when the run isn't in `running` status (already completed,
# already cancelled, or failed).


class AmendmentCancelResponse(BaseModel):
    status: str
    decision_run_id: int


@router.post(
    "/amendment/{decision_run_id}/cancel",
    response_model=AmendmentCancelResponse,
)
def post_amendment_cancel(
    decision_run_id: int,
    user_id: str,
    db: Session = Depends(get_db),
) -> AmendmentCancelResponse:
    """Cancel a running plan-amendment-chat DecisionRun."""
    from argosy.orchestrator.flows.plan_amendment import cancel
    from argosy.state.models import DecisionRun

    run = db.get(DecisionRun, decision_run_id)
    if (
        run is None
        or run.user_id != user_id
        or run.decision_kind != "plan_amendment_chat"
    ):
        raise HTTPException(status_code=404, detail="amendment not found for user")
    if run.status != "running":
        raise HTTPException(
            status_code=409,
            detail=f"amendment is in status {run.status!r}; cannot cancel",
        )

    ok = cancel(db, user_id=user_id, decision_run_id=decision_run_id)
    if not ok:
        raise HTTPException(status_code=409, detail="cancel failed")

    return AmendmentCancelResponse(
        status="cancelled", decision_run_id=decision_run_id,
    )


__all__ = [
    "AdvisorTurnRequest",
    "AdvisorTurnResponse",
    "AmendmentCancelResponse",
    "CheckInRequest",
    "CheckInResponse",
    "GapItemDTO",
    "GapStatusResponse",
    "HomeBriefBullet",
    "HomeBriefCTA",
    "HomeBriefResponse",
    "_persist_turn",
    "classify_mode",
    "field_by_path",
    "pick_gap_driven_target",
    "reset_advisor_agent_factory",
    "router",
    "set_advisor_agent_factory",
]
