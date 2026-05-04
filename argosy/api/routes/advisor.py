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

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select

from argosy.adapters.data.cache import CacheKind, cached_call
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
from argosy.state.models import DailyBrief, PensionFundSnapshot, User, UserContext

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


# ----------------------------------------------------------------------
# /home-brief — composed glanceable summary for the home page
# ----------------------------------------------------------------------
#
# Stitches three lines from already-cached state — gap tracker, latest
# daily brief, and the most recent watchlist signal (pension snapshots,
# for now; SEC Form 4 etc. land in Phase 4). NO new LLM call. Per-user
# cache via `prices_cache` provider="advisor_home_brief", TTL 30 minutes.

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
    kind: str  # "gap" | "portfolio" | "signal"
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


async def _gap_bullet(user_id: str) -> HomeBriefBullet | None:
    """Top missing/stale field, with one-clause why-it-matters."""
    identity_yaml = ""
    goals_yaml = ""
    constraints_yaml = ""

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
    """One line from the most recent daily brief; fall back to a position note.

    Defensive on DB hiccups (missing table on stale schemas, etc.) — degrades
    to None rather than 500-ing the home page.
    """
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
    except Exception:  # noqa: BLE001
        _log.debug(
            "home_brief.portfolio_bullet_db_skipped", user_id=user_id, exc_info=True
        )

    # Fallback: try the on-disk portfolio TSV for a top-position concentration
    # one-liner. Keep this best-effort — if parsing fails, omit the bullet.
    try:
        from argosy.api.routes.portfolio import _find_latest_tsv
        from argosy.ingest.tsv import parse_portfolio_tsv
    except ImportError:  # pragma: no cover - defensive
        return None

    try:
        tsv = _find_latest_tsv()
        if tsv is None:
            return None
        snap = parse_portfolio_tsv(tsv)
    except Exception:  # pragma: no cover - parser failures shouldn't 500 the page
        return None

    total = snap.total_usd_value_k or 0.0
    if total <= 0 or not snap.positions:
        return None
    top = max(
        (p for p in snap.positions if p.usd_value_k),
        key=lambda p: p.usd_value_k or 0.0,
        default=None,
    )
    if top is None or not top.usd_value_k:
        return None
    pct = (top.usd_value_k / total) * 100
    return HomeBriefBullet(
        kind="portfolio",
        text=f"{top.symbol or 'Top position'} concentration at {pct:.0f}% of portfolio.",
    )


async def _signal_bullet(user_id: str) -> HomeBriefBullet | None:
    """Most recent watchlist event — pension snapshot for now (Phase 3 data).

    Defensive: the pension_fund_snapshots table only exists on environments
    that ran the Phase 3 migration. On older dev DBs the query raises
    OperationalError (no such table); we treat that the same as "no rows"
    and just omit the signal bullet rather than 500-ing the home page.
    """
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
    except Exception:  # noqa: BLE001 — degrade gracefully on missing table / DB issues
        _log.debug("home_brief.signal_bullet_skipped", user_id=user_id, exc_info=True)
        return None
    if row is None:
        return None

    name = row.fund_name or row.fund_id
    rel = row.relative_to_benchmark_pct
    if rel is not None:
        direction = "above" if float(rel) >= 0 else "below"
        return HomeBriefBullet(
            kind="signal",
            text=f"{name} {abs(float(rel)):.1f}% {direction} benchmark in latest pension snapshot.",
        )
    if row.return_pct_12m is not None:
        return HomeBriefBullet(
            kind="signal",
            text=f"{name} 12m return {float(row.return_pct_12m):.1f}% in latest pension snapshot.",
        )
    return HomeBriefBullet(
        kind="signal", text=f"New pension snapshot recorded for {name}."
    )


async def _compose_home_brief(user_id: str) -> HomeBriefResponse:
    now = datetime.now(UTC)
    headline = f"{_greeting_for_hour(now.hour)}. Here's where you stand."

    bullets: list[HomeBriefBullet] = []
    gap = await _gap_bullet(user_id)
    if gap is not None:
        bullets.append(gap)
    portfolio = await _portfolio_bullet(user_id)
    if portfolio is not None:
        bullets.append(portfolio)
    signal = await _signal_bullet(user_id)
    if signal is not None:
        bullets.append(signal)

    return HomeBriefResponse(
        headline=headline,
        bullets=bullets,
        cta=HomeBriefCTA(label="Talk to advisor", href="/advisor"),
        generated_at=now.isoformat(),
    )


@router.get("/home-brief", response_model=HomeBriefResponse)
async def get_home_brief(
    user_id: str = Query("ariel"),
) -> HomeBriefResponse:
    """Compose a 3–5 line glance card for the home page.

    Stitched from existing state — gap tracker, latest daily brief, most
    recent watchlist signal. NO new LLM call. Cached per-user for 30
    minutes via `prices_cache` (provider=`advisor_home_brief`).
    """
    async def _fetch() -> dict[str, Any]:
        composed = await _compose_home_brief(user_id)
        return composed.model_dump()

    data = await cached_call(
        kind=CacheKind.PRICES,
        provider="advisor_home_brief",
        key=f"user:{user_id}",
        ttl_seconds=30 * 60,
        fetch=_fetch,
    )
    return HomeBriefResponse(**data)


__all__ = [
    "AdvisorTurnRequest",
    "AdvisorTurnResponse",
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
