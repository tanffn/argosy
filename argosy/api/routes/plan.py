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
import logging
from typing import Generator

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from argosy.adapters.data.cache import invalidate_home_brief
from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.agents.plan_synthesizer_types import Delta, SpeculativeCandidate
from argosy.api.events import publish_event, publish_event_threadsafe
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport,
    DecisionPhase,
    DecisionRun,
    PlanCritique,
    PlanVersion,
    UserContext,
)
from argosy.state.queries import get_active_baseline

router = APIRouter(prefix="/plan", tags=["plan"])


# ---------------------------------------------------------------------------
# WebSocket event publish indirection (T2.16).
# ---------------------------------------------------------------------------


def _publish(event_type: str, payload: dict) -> None:
    """Publish a plan-lifecycle event via the in-process WebSocket layer.

    Thin shim kept for monkeypatch compatibility (tests patch this symbol on
    the module directly).  All sync→async bridging logic lives in
    ``publish_event_threadsafe`` in ``argosy.api.events`` (I3, I4, M2 fix).
    """
    publish_event_threadsafe(event_type, payload)


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
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    global _sync_engine, _sync_session_factory
    if _sync_session_factory is None:
        from argosy.config import get_settings

        settings = get_settings()
        sync_url = settings.database_url.replace("+aiosqlite", "")
        _sync_engine = create_engine(sync_url, connect_args={"check_same_thread": False})

        # SQLite WAL + busy_timeout + synchronous=NORMAL — see
        # argosy/state/db.py for the rationale. busy_timeout bumped to
        # 60 s after run #9 hit 11 s waits at 10 s.
        if sync_url.startswith("sqlite") and ":memory:" not in sync_url:
            @event.listens_for(_sync_engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=60000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

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
        # Prefer the user's accepted plan (role='current'); fall back to the
        # baseline if they haven't accepted any drafts yet. Never the draft —
        # /api/plan/draft serves that. Previously this ordered by
        # imported_at DESC and would surface a freshly-synthesized draft as
        # "current", which broke /plan and /home consumers that expected the
        # last accepted plan.
        plan = (
            await session.execute(
                select(PlanVersion)
                .where(
                    PlanVersion.user_id == user_id,
                    PlanVersion.role == "current",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if plan is None:
            plan = (
                await session.execute(
                    select(PlanVersion)
                    .where(
                        PlanVersion.user_id == user_id,
                        PlanVersion.role == "baseline",
                    )
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

from datetime import datetime, timedelta, timezone


class HorizonSectionView(BaseModel):
    horizon: str
    freshness_expected: str
    status: str
    posture: str
    targets: list[dict] = []
    themes: list[dict] = []
    actions: list[dict] = []
    # Tighter typing (M6): the speculative-candidate shape is fixed by the
    # synthesizer's pydantic model; surfacing it here lets the OpenAPI
    # schema document the contract and lets the TS client drop its casts.
    speculative_candidates: list[SpeculativeCandidate] = []
    deltas_from_prior: list[dict] = []
    rationale: str = ""
    cited_sources: list[str] = []


class SynthesisHealth(BaseModel):
    """Aggregate per-run agent + adapter health summary (T0.7).

    Derived from ``argosy.services.agent_tree_builder.build_agent_tree``'s
    ``status_summary`` for the draft's ``decision_run_id``. The UI's
    ``SynthesisHealthBanner`` renders this as a one-line drill-in chip
    above the FM-objections card so the user can see "all agents OK +
    all adapters OK" as positive confirmation even when FM approved.
    """

    agents_ok: int
    agents_failed: int
    # T0.7 follow-up — "skipped" is tracked separately from "failed" so the
    # banner can show "N OK · M failed · K skipped" instead of conflating
    # didn't-run with errored-out.
    agents_skipped: int
    adapters_ok: int
    adapters_failed: int
    decision_run_id: int


class NvdaPaceView(BaseModel):
    """NVDA divestment pace snapshot lifted from the latest concentration
    agent_report. Surfaced on the home page's "NVDA PACE" tile so the user
    sees real numbers instead of the prior hardcoded 0 / 10,000 placeholder.

    All four fields mirror ``argosy.agents.concentration_analyst.NvdaPace``;
    we re-declare them here to keep the route module free of an import-time
    dependency on the agent's pydantic schema (the agent module pulls in
    Anthropic SDK bits the API route doesn't need to import on cold start).
    """

    shares_sold_ytd: int = 0
    target_shares_ytd: int = 0
    delta_shares: int = 0
    on_track: bool = True


class DraftResponse(BaseModel):
    plan_version_id: int
    version_label: str | None
    drafted_at: str
    derived_from_id: int | None
    decision_run_id: int | None
    horizon_long: HorizonSectionView | None
    horizon_medium: HorizonSectionView | None
    horizon_short: HorizonSectionView | None
    horizon_long_md: str | None
    horizon_medium_md: str | None
    horizon_short_md: str | None
    # T0.7 — populated when the draft has a backing synthesis decision_run_id
    # and ``build_agent_tree`` succeeds. ``None`` for legacy drafts without
    # decision_run_id or when the agent-tree builder raises.
    synthesis_health: SynthesisHealth | None = None
    # Lifted from the latest concentration agent_report tied to the draft's
    # ``decision_run_id``. ``None`` when no concentration report exists yet
    # (no synthesis has run, or the report row is missing/malformed). The UI's
    # NVDA PACE tile renders a "Awaiting synthesis run" tooltip in that case.
    nvda_pace: NvdaPaceView | None = None


class AcceptResponse(BaseModel):
    status: str
    new_current_id: int


class RejectRequest(BaseModel):
    reason: str
    guidance: str = ""


_AGENT_CLASS_TO_LABEL = {
    "TaxAnalystAgent": "TaxAnalyst",
    "ConcentrationAnalystAgent": "ConcentrationAnalyst",
    "NewsAnalystAgent": "NewsAnalyst",
    "MacroAnalystAgent": "MacroAnalyst",
    "FXAnalystAgent": "FXAnalyst",
    "FxAnalystAgent": "FXAnalyst",
    "FundamentalsAnalystAgent": "FundamentalsAnalyst",
    "SentimentAnalystAgent": "SentimentAnalyst",
    "TechnicalAnalystAgent": "TechnicalAnalyst",
    "PlanCritiqueAgent": "PlanCritique",
    "PlanSynthesizerAgent": "PlanSynthesizer",
}


def _citation_to_provenance_label(citation: str) -> str | None:
    """Map a citation string to a human-readable provenance label.

    Citations follow patterns the synthesizer + analysts emit:

    * ``agent_report:<ClassName>`` → the agent's short name (e.g. "TaxAnalyst")
    * ``user_context.<key>``       → "user_context"
    * ``decision_run:debate_outcome_<horizon>`` → "Debate (<horizon>)"
    * ``portfolio/holdings``       → "portfolio"
    * ``fundamentals/<TICKER>``    → "FundamentalsAnalyst"
    * ``technical/<TICKER>``       → "TechnicalAnalyst"
    * ``fx/...``                   → "FXAnalyst"
    * ``news/...``                 → "NewsAnalyst"
    * ``macro/...``                → "MacroAnalyst"
    * ``concentration/...``        → "ConcentrationAnalyst"
    * ``tax/...``                  → "TaxAnalyst"
    * ``sentiment/...``            → "SentimentAnalyst"
    * ``domain_knowledge/...``     → "domain_kb"
    * ``docs/design/SDD*`` or ``SDD*`` → "SDD"

    Returns ``None`` when the citation doesn't match any known pattern;
    the caller can fall through and surface the raw string as a chip.
    """
    if not citation:
        return None
    c = citation.strip()
    if c.startswith("agent_report:"):
        cls = c.split(":", 1)[1].strip()
        return _AGENT_CLASS_TO_LABEL.get(cls, cls)
    if c.startswith("user_context"):
        return "user_context"
    if c.startswith("decision_run:debate_outcome_"):
        horizon = c.split("decision_run:debate_outcome_", 1)[1]
        return f"Debate ({horizon})"
    if c.startswith("portfolio/"):
        return "portfolio"
    prefix_map = (
        ("fundamentals/", "FundamentalsAnalyst"),
        ("technical/", "TechnicalAnalyst"),
        ("fx/", "FXAnalyst"),
        ("news/", "NewsAnalyst"),
        ("macro/", "MacroAnalyst"),
        ("concentration/", "ConcentrationAnalyst"),
        ("tax/", "TaxAnalyst"),
        ("sentiment/", "SentimentAnalyst"),
        ("domain_knowledge/", "domain_kb"),
    )
    for prefix, label in prefix_map:
        if c.startswith(prefix):
            return label
    if c.startswith("docs/design/SDD") or c.startswith("SDD"):
        return "SDD"
    return None


def _enrich_deltas(payload: dict) -> dict:
    """Inject ``provenance_agent_labels`` into each delta in-place.

    Dedup-preserving order: ``[FundamentalsAnalyst, TaxAnalyst]`` not
    ``[FundamentalsAnalyst, FundamentalsAnalyst, TaxAnalyst]``.
    """
    for d in payload.get("deltas_from_prior") or []:
        if not isinstance(d, dict):
            continue
        seen: dict[str, None] = {}
        for src in d.get("cited_sources") or []:
            label = _citation_to_provenance_label(str(src))
            if label and label not in seen:
                seen[label] = None
        d["provenance_agent_labels"] = list(seen.keys())
    return payload


def _horizon_view(json_str: str | None) -> HorizonSectionView | None:
    if not json_str:
        return None
    payload = json.loads(json_str)
    payload = _enrich_deltas(payload)
    return HorizonSectionView(**payload)


def _build_nvda_pace(
    db: Session, user_id: str, decision_run_id: int | None
) -> NvdaPaceView | None:
    """Lift NvdaPace from the latest concentration agent_report for this run.

    Returns ``None`` when the draft has no backing ``decision_run_id``, when
    no ``concentration`` agent_report row exists for ``plan-synth-<run_id>``,
    or when the row's ``response_text`` is malformed past a best-effort parse.
    The route returns these as a null field rather than raising — the UI
    falls back to a "Awaiting synthesis run" hint.

    The agent's ``response_text`` is typically wrapped in ```` ```json ... ```
    fences, so we use the same lenient ``JSONDecoder(strict=False).raw_decode``
    pattern ``_parse_fm_response`` uses for the fund-manager agent — find the
    first ``{`` and parse from there.
    """
    if decision_run_id is None:
        return None

    decision_id_str = f"plan-synth-{decision_run_id}"
    row = db.execute(
        select(AgentReport)
        .where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_id_str,
            AgentReport.agent_role == "concentration",
        )
        .order_by(desc(AgentReport.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if row is None or not row.response_text:
        return None

    text = row.response_text
    brace = text.find("{")
    if brace < 0:
        return None
    import json as _json

    decoder = _json.JSONDecoder(strict=False)
    try:
        payload, _idx = decoder.raw_decode(text[brace:])
    except _json.JSONDecodeError:
        logger.warning(
            "nvda_pace: could not parse concentration response_text for "
            "decision_id=%s",
            decision_id_str,
        )
        return None
    if not isinstance(payload, dict):
        return None
    pace = payload.get("nvda_pace")
    if not isinstance(pace, dict):
        return None
    try:
        return NvdaPaceView(
            shares_sold_ytd=int(pace.get("shares_sold_ytd") or 0),
            target_shares_ytd=int(pace.get("target_shares_ytd") or 0),
            delta_shares=int(pace.get("delta_shares") or 0),
            on_track=bool(pace.get("on_track", True)),
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "nvda_pace: bad scalar types in concentration payload for "
            "decision_id=%s: %s",
            decision_id_str,
            exc,
        )
        return None


def _build_synthesis_health(
    db: Session, decision_run_id: int | None
) -> SynthesisHealth | None:
    """Look up agent + adapter status_summary for the draft's synthesis run.

    Returns ``None`` when ``decision_run_id`` is missing (legacy / manually
    ingested drafts), or when the tree builder rejects the run (e.g. the
    decision_run isn't a synthesis kind, was deleted, etc.). The route
    deliberately refuses to crash for observability data — losing the
    health chip is acceptable; losing the whole draft response is not.
    """
    if decision_run_id is None:
        return None
    try:
        from argosy.services.agent_tree_builder import build_agent_tree

        tree = build_agent_tree(db, decision_run_id)
    except ValueError as exc:
        # Common reason: decision_run_id doesn't exist, or its decision_kind
        # is not a synthesis kind. Log + return None so the banner just
        # silently doesn't render.
        logger.warning(
            "synthesis_health unavailable for decision_run_id=%s: %s",
            decision_run_id,
            exc,
        )
        return None
    summary = tree.status_summary or {}
    return SynthesisHealth(
        agents_ok=int(summary.get("agents_ok", 0)),
        agents_failed=int(summary.get("agents_failed", 0)),
        agents_skipped=int(summary.get("agents_skipped", 0)),
        adapters_ok=int(summary.get("adapters_ok", 0)),
        adapters_failed=int(summary.get("adapters_failed", 0)),
        decision_run_id=decision_run_id,
    )


@router.get("/draft", response_model=DraftResponse)
def get_draft(user_id: str, db: Session = Depends(get_db)) -> DraftResponse:
    from argosy.state.queries import get_pending_draft

    pv = get_pending_draft(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no pending draft for user")
    return DraftResponse(
        plan_version_id=pv.id,
        version_label=pv.version_label or None,
        drafted_at=pv.imported_at.isoformat(),
        derived_from_id=pv.derived_from_id,
        decision_run_id=pv.decision_run_id,
        horizon_long=_horizon_view(pv.horizon_long_json),
        horizon_medium=_horizon_view(pv.horizon_medium_json),
        horizon_short=_horizon_view(pv.horizon_short_json),
        horizon_long_md=pv.horizon_long_md,
        horizon_medium_md=pv.horizon_medium_md,
        horizon_short_md=pv.horizon_short_md,
        synthesis_health=_build_synthesis_health(db, pv.decision_run_id),
        nvda_pace=_build_nvda_pace(db, user_id, pv.decision_run_id),
    )


# ---------------------------------------------------------------------------
# In-flight synthesis surface — for /plan to render a "Synthesis in flight"
# card when a plan_revision DecisionRun is running but the prior draft has
# been superseded (so /api/plan/draft 404s and the page would otherwise look
# blank). Polled every ~10 s by the UI; cheap (one indexed lookup + one
# count). Returns the payload (200) when a run is in flight, or null (200)
# when there isn't — never 404, so the UI's "loading" → "in flight" → "draft
# ready" transition is a single state machine and not three exception
# branches.
# ---------------------------------------------------------------------------


class InFlightSynthesisDTO(BaseModel):
    """Live snapshot of an in-flight plan synthesis run.

    Used by the /plan page to render the "Synthesis #N · phase X of 5"
    card while a synthesis is mid-flight. Updated by the UI's 10 s polling
    loop until either ``status`` flips away from "running" or the
    ``plan.draft.completed`` WS event fires (whichever happens first —
    the WS event is authoritative; the polling is just a fallback so
    the phase counter ticks up even without a WS event for each phase).
    """

    decision_run_id: int
    decision_audit_token: str  # always "plan-synth-<id>"
    started_at: str
    completed_phases: int  # decision_phases rows where finished_at IS NOT NULL
    total_phases: int = 5  # constant for synthesis runs (phase_1..phase_5)
    status: str  # "running" today; surfaced verbatim so we can extend later


class InFlightSynthesisResponse(BaseModel):
    """Wrapper so the route can return 200 + null when there's no in-flight run.

    Returning 200 with a nullable field (instead of 404) lets the /plan
    page treat the polling result as a normal state transition without a
    try/except branch every refresh tick.
    """

    in_flight_synthesis: InFlightSynthesisDTO | None = None


@router.get(
    "/in-flight-synthesis",
    response_model=InFlightSynthesisResponse,
)
def get_in_flight_synthesis(
    user_id: str = Query(...),
    db: Session = Depends(get_db),
) -> InFlightSynthesisResponse:
    """Return the user's currently-running plan synthesis run, if any.

    Picks the most recent ``decision_runs`` row with
    ``decision_kind='plan_revision'`` and ``status='running'``. The
    ``decision_audit_token`` always shapes as ``plan-synth-<id>`` to match
    the orchestrator's convention; the UI uses it to filter WS events
    + drill into the agent cascade panel for the live run.

    ``completed_phases`` is the count of ``decision_phases`` rows with
    ``finished_at IS NOT NULL`` for the matched run. Synthesis writes five
    phases (``synthesis.phase_1`` .. ``synthesis.phase_5``); we cap the
    UI-facing total at 5 regardless of what's actually in the DB so a
    bug-emitting orchestrator can't push the progress chip past 5/5.

    Returns ``{in_flight_synthesis: null}`` (200) when no in-flight run
    exists — never 404. The UI consumes this on a 10 s polling loop and
    we don't want every tick to look like an error in the network panel.
    """
    run = db.execute(
        select(DecisionRun)
        .where(
            DecisionRun.user_id == user_id,
            DecisionRun.status == "running",
            DecisionRun.decision_kind == "plan_revision",
        )
        .order_by(desc(DecisionRun.id))
        .limit(1)
    ).scalar_one_or_none()
    if run is None:
        return InFlightSynthesisResponse(in_flight_synthesis=None)

    completed_phases = db.execute(
        select(func.count(DecisionPhase.id)).where(
            DecisionPhase.decision_run_id == run.id,
            DecisionPhase.finished_at.is_not(None),
        )
    ).scalar_one() or 0
    # Cap at total_phases — a defensive bound so a stray
    # non-synthesis phase row (or a future bump to 6+ phases on a run
    # we haven't migrated yet) can't make the UI render "phase 7 of 5".
    if completed_phases > 5:
        completed_phases = 5

    return InFlightSynthesisResponse(
        in_flight_synthesis=InFlightSynthesisDTO(
            decision_run_id=run.id,
            decision_audit_token=f"plan-synth-{run.id}",
            started_at=run.started_at.isoformat() if run.started_at else "",
            completed_phases=int(completed_phases),
            total_phases=5,
            status=run.status or "running",
        ),
    )


# ---------------------------------------------------------------------------
# Wave (this session) — FM objections endpoint for /plan executive summary
# ---------------------------------------------------------------------------


class FMObjectionTranslationDTO(BaseModel):
    """Plain-English rendering of one FM objection, attached inline to
    the objection list returned by GET /api/plan/draft/objections.

    Populated by argosy.services.fm_objection_translation_cache on
    the first hit for a draft (parallel asyncio.gather batch of N
    translator calls, ~10-15 s for N=6). Persisted to
    fm_objection_translations so subsequent loads return inline
    with no LLM round-trip and the UI toggle is instant.

    None when the translator agent failed for that slot - the UI
    falls back to the lazy on-demand POST to
    /api/plan/draft/objections/translate.
    """

    headline: str
    plain_english: str
    recommended_actions: list[str] = []


class FMObjection(BaseModel):
    severity: str  # "RED" | "AMBER" | "YELLOW"
    topic: str
    detail: str
    # Precomputed plain-English rendering; None when the translator
    # failed or the cache helper was skipped (legacy clients). UI falls
    # back to the on-demand POST when null.
    translation: FMObjectionTranslationDTO | None = None


class FMObjectionsResponse(BaseModel):
    approved: bool
    objections: list[FMObjection]
    cited_sources: list[str]
    decision_run_id: int | None
    raw_response_excerpt: str
    # Prior-round FM objections — populated when the current draft has a
    # ``derived_from_id`` predecessor with role='superseded' AND that
    # predecessor has a Fund Manager agent_report.  Order is the same as
    # in the prior verdict's ``reasons`` array so the UI can map
    # "Blocker #N" / "BLOCKER N" / "Objection #N" tokens in the new
    # rationale text directly to ``prior_round_objections[N-1]``.
    # Empty list when there's no prior draft / no FM verdict to fetch.
    prior_round_objections: list[FMObjection] = []


_RED_KEYWORDS = (
    "hard constraint violation",
    "time-critical",
    "permanent-loss",
    "section 102",
    "statutory",
    "blocker",
    "catastrophic",
    "critical",
)
_AMBER_KEYWORDS = (
    "failure",
    "missing",
    "unquantified",
    "escalate",
    "unresolved",
    "conflation",
    "regression",
    "coherence gap",
    "amber",
)


def _classify_severity(topic: str, detail: str) -> str:
    blob = (topic + " " + detail).lower()
    if any(k in blob for k in _RED_KEYWORDS):
        return "RED"
    if any(k in blob for k in _AMBER_KEYWORDS):
        return "AMBER"
    return "YELLOW"


def _split_reason(reason: str) -> tuple[str, str]:
    """Split a FM reason string into (topic, detail).

    Recognized shapes (in order):
      * ``"[BLOCKER — TOPIC] detail"`` (severity-prefixed; FM post-f8faaca)
      * ``"[CATEGORY] TOPIC — detail"`` (legacy severity-prefixed)
      * ``"TOPIC — long-form detail"`` (plain em-dash split)
    Falls back to ``(topic=reason[:80], detail=reason)`` so the verbose
    text is never lost (was previously ``topic="objection"`` swallowing
    the detail entirely).
    """
    import re

    # Pattern 1: "[SEVERITY — TOPIC] detail" — used by FM post-f8faaca
    # (verdicts like "[BLOCKER — internal coherence] Tax-rate citation...").
    m = re.match(r"^\s*\[([A-Z]+)\s+[—-]+\s+([^\]]+)\]\s*(.*)$", reason, re.DOTALL)
    if m:
        sev_label = m.group(1).strip()
        topic_inside = m.group(2).strip()
        detail = m.group(3).strip()
        # Topic carries the severity hint forward so _classify_severity
        # can pick it up via the keyword scan ("blocker"/"amber" lowercased).
        topic = f"{sev_label} — {topic_inside}" if topic_inside else sev_label
        return (topic, detail or reason)

    # Pattern 2: plain "TOPIC — detail" — the original FM shape.
    for sep in (" — ", " -- ", " - "):
        if sep in reason:
            topic, detail = reason.split(sep, 1)
            return topic.strip(), detail.strip()

    # Fallback: keep the first 80 chars as a synthetic topic so the
    # detail isn't lost. Previously this used topic="objection" which
    # threw away searchability + made the UI render a uniform list of
    # "objection" pills.
    return (reason.strip()[:80], reason.strip())


def _parse_fm_response(response_text: str) -> dict:
    """Best-effort JSON parse of the FM agent's response_text.

    Tolerates trailing prose + raw control chars via the same
    ``JSONDecoder(strict=False).raw_decode`` pattern the synthesizer uses
    so an LLM that emits "{...} <free-form trailing note>" still parses.
    """
    import json as _json
    decoder = _json.JSONDecoder(strict=False)
    try:
        obj, _idx = decoder.raw_decode(response_text)
        if isinstance(obj, dict):
            return obj
    except _json.JSONDecodeError:
        pass
    # Last-ditch: try to find the first '{' and parse from there.
    brace = response_text.find("{")
    if brace >= 0:
        try:
            obj, _idx = decoder.raw_decode(response_text[brace:])
            if isinstance(obj, dict):
                return obj
        except _json.JSONDecodeError:
            pass
    return {}


class TargetProgress(BaseModel):
    """Live "current vs target" annotation for one plan target.

    Wire shape mirrors ``argosy.services.target_progress.TargetProgress``;
    the API layer wraps the service dataclass in this pydantic model so
    the OpenAPI schema documents the contract and the TS client gets a
    typed object. See the service module for the per-unit classifier.
    """

    item_id: str
    target_value: float
    target_unit: str
    current_value: float | None
    current_unit: str
    gap_value: float | None
    gap_pct: float | None
    status: str  # "AT_TARGET" | "ABOVE_TARGET" | "BELOW_TARGET" | "UNKNOWN"
    direction_is_good: bool | None
    compute_source: str
    last_observation: str


class TargetProgressResponse(BaseModel):
    """Map keyed by item_id so the UI can join O(1) against DeltaItem rows.

    ``plan_version_id`` is echoed so the UI can confirm which draft the
    progress strip is for (the route always reads from the pending draft).
    """

    plan_version_id: int
    progress: dict[str, TargetProgress]


@router.get("/draft/target-progress", response_model=TargetProgressResponse)
def get_draft_target_progress(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> TargetProgressResponse:
    """Return live target-progress annotations for the user's pending draft.

    For each ``target`` row in long/medium/short horizon JSON, computes the
    live ``current_value`` from the latest portfolio_snapshots row + the
    freshest household_budget agent_report + the concentration agent_report
    tied to the draft's decision_run_id. Returns a status classification
    (AT_TARGET / ABOVE_TARGET / BELOW_TARGET / UNKNOWN) the UI uses to
    render a thin progress strip on each TARGET DeltaCard.

    Pure-ish: no LLM calls, no external HTTP — three DB reads + a small
    amount of arithmetic. <10ms in practice.

    404 when no pending draft exists for the user (parity with GET
    /api/plan/draft).
    """
    from argosy.services.target_progress import compute_target_progress_for_plan
    from argosy.state.queries import get_pending_draft

    pv = get_pending_draft(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no pending draft for user")

    rows = compute_target_progress_for_plan(db, user_id=user_id, plan=pv)
    progress_map: dict[str, TargetProgress] = {}
    for row in rows:
        progress_map[row.item_id] = TargetProgress(
            item_id=row.item_id,
            target_value=row.target_value,
            target_unit=row.target_unit,
            current_value=row.current_value,
            current_unit=row.current_unit,
            gap_value=row.gap_value,
            gap_pct=row.gap_pct,
            status=row.status,
            direction_is_good=row.direction_is_good,
            compute_source=row.compute_source,
            last_observation=row.last_observation,
        )
    return TargetProgressResponse(
        plan_version_id=pv.id,
        progress=progress_map,
    )


class CashflowPointDTO(BaseModel):
    months_out: int
    age_years: float
    date: str  # YYYY-MM
    portfolio_value_base_usd: float
    portfolio_value_bear_usd: float
    portfolio_value_bull_usd: float
    portfolio_income_base_monthly_usd: float
    portfolio_income_bear_monthly_usd: float
    portfolio_income_bull_monthly_usd: float
    pension_annuity_monthly_usd: float
    pension_lump_available_usd: float
    expenses_monthly_usd: float
    surplus_base_monthly_usd: float
    surplus_bear_monthly_usd: float
    surplus_bull_monthly_usd: float


class CashflowProjectionResponse(BaseModel):
    today_date: str
    today_age_years: float
    fx_usd_nis: float
    retirement_age_assumed: float
    retire_ready_age_base: float | None
    retire_ready_age_bear: float | None
    retire_ready_age_bull: float | None
    retire_ready_months_out_base: int | None
    retire_ready_months_out_bear: int | None
    retire_ready_months_out_bull: int | None
    series: list[CashflowPointDTO]
    assumptions: dict


@router.get(
    "/draft/cashflow-projection", response_model=CashflowProjectionResponse
)
def get_draft_cashflow_projection(
    user_id: str = Query("ariel"),
    years: int = Query(30, ge=1, le=50),
    retirement_age: float = Query(49.0, ge=30.0, le=80.0),
    tax_rate: float = Query(0.25, ge=0.0, le=0.5),
    portfolio_value_usd_override: float | None = Query(
        None,
        ge=0.0,
        le=100_000_000.0,
        description=(
            "Replace the DB-computed portfolio value with this USD amount. "
            "Useful for what-if scenarios (e.g. 'what if I sold NVDA and ended "
            "up with $2.99M'). When omitted, uses the latest portfolio_snapshots row."
        ),
    ),
    mu_nominal_annual: float = Query(
        0.08,
        ge=0.02,
        le=0.15,
        description=(
            "Nominal expected portfolio return per year. Default 0.08 = S&P-historical. "
            "Drop to 0.04-0.05 to stress-test a flat/sideways decade scenario. "
            "Real return = mu_nominal - inflation_annual (the latter is fixed at 0.025)."
        ),
    ),
    sigma_annual: float = Query(
        0.18,
        ge=0.05,
        le=0.60,
        description=(
            "Portfolio volatility (annual standard deviation). Default 0.18 = "
            "diversified-equity historical. Crank up to 0.40-0.50 to model "
            "single-stock concentration risk (e.g., a NVDA-heavy portfolio). "
            "Widens the bear/bull band around the typical curve."
        ),
    ),
    lifestyle_drift_annual: float = Query(
        0.0,
        ge=0.0,
        le=0.10,
        description=(
            "Extra expense-growth ABOVE the inflation rate (per year). "
            "Default 0 means expenses grow exactly with CPI. Set to e.g. 0.015 "
            "to model personal lifestyle inflation running 1.5%/yr hotter than "
            "CPI (kids, healthcare, lifestyle creep). Affects expenses only — "
            "pension annuity still indexes to CPI."
        ),
    ),
    db: Session = Depends(get_db),
) -> CashflowProjectionResponse:
    """Return a per-month cashflow projection for the /plan retirement view.

    Pure-math endpoint — no LLM, no external HTTP, just three DB reads
    + the projection loop. <30 ms for a 30-year horizon."""
    from argosy.services.cashflow_projection import (
        extract_household_state,
        extract_pension_state,
        project_cashflow,
    )

    hh = extract_household_state(db, user_id)
    pen = extract_pension_state(db, user_id)

    # Apply the override (if any) BEFORE the projection. We swap the
    # ``portfolio_value_nis`` field on the immutable dataclass via
    # ``dataclasses.replace`` so the rest of the household state
    # (expenses, fx, age) is preserved.
    if portfolio_value_usd_override is not None:
        from dataclasses import replace as _dc_replace
        hh = _dc_replace(
            hh,
            portfolio_value_nis=portfolio_value_usd_override * hh.fx_usd_nis,
        )

    proj = project_cashflow(
        household=hh,
        pensions=pen,
        retirement_age=retirement_age,
        years=years,
        mu_nominal_annual=mu_nominal_annual,
        sigma_annual=sigma_annual,
        lifestyle_drift_annual=lifestyle_drift_annual,
        tax_rate=tax_rate,
    )

    fx = hh.fx_usd_nis if hh.fx_usd_nis > 0 else 1.0

    def to_usd(nis: float) -> float:
        return round(nis / fx, 2)

    series_dto = [
        CashflowPointDTO(
            months_out=p.months_out,
            age_years=round(p.age_years, 3),
            date=p.date_yyyy_mm,
            portfolio_value_base_usd=to_usd(p.portfolio_value_base_nis),
            portfolio_value_bear_usd=to_usd(p.portfolio_value_bear_nis),
            portfolio_value_bull_usd=to_usd(p.portfolio_value_bull_nis),
            portfolio_income_base_monthly_usd=to_usd(p.portfolio_income_base_monthly_nis),
            portfolio_income_bear_monthly_usd=to_usd(p.portfolio_income_bear_monthly_nis),
            portfolio_income_bull_monthly_usd=to_usd(p.portfolio_income_bull_monthly_nis),
            pension_annuity_monthly_usd=to_usd(p.pension_annuity_monthly_nis),
            pension_lump_available_usd=to_usd(p.pension_lump_available_nis),
            expenses_monthly_usd=to_usd(p.expenses_monthly_nis),
            surplus_base_monthly_usd=to_usd(p.surplus_base_monthly_nis),
            surplus_bear_monthly_usd=to_usd(p.surplus_bear_monthly_nis),
            surplus_bull_monthly_usd=to_usd(p.surplus_bull_monthly_nis),
        )
        for p in proj.series
    ]
    return CashflowProjectionResponse(
        today_date=datetime.now(timezone.utc).date().isoformat(),
        today_age_years=round(hh.current_age_years, 3),
        fx_usd_nis=fx,
        retirement_age_assumed=round(proj.retirement_age_assumed, 1),
        retire_ready_age_base=(
            round(proj.retire_ready_age_base, 2)
            if proj.retire_ready_age_base is not None else None
        ),
        retire_ready_age_bear=(
            round(proj.retire_ready_age_bear, 2)
            if proj.retire_ready_age_bear is not None else None
        ),
        retire_ready_age_bull=(
            round(proj.retire_ready_age_bull, 2)
            if proj.retire_ready_age_bull is not None else None
        ),
        retire_ready_months_out_base=proj.retire_ready_months_out_base,
        retire_ready_months_out_bear=proj.retire_ready_months_out_bear,
        retire_ready_months_out_bull=proj.retire_ready_months_out_bull,
        series=series_dto,
        assumptions=proj.assumptions,
    )


class MonteCarloPointDTO(BaseModel):
    months_out: int
    age_years: float
    date: str
    portfolio_value_p10_usd: float
    portfolio_value_p25_usd: float
    portfolio_value_p50_usd: float
    portfolio_value_p75_usd: float
    portfolio_value_p90_usd: float
    fraction_solvent: float
    pension_annuity_monthly_usd: float
    expenses_monthly_usd: float


class MonteCarloProjectionResponse(BaseModel):
    today_date: str
    today_age_years: float
    fx_usd_nis: float
    retirement_age_assumed: float
    n_paths: int
    p_failure_before_age_75: float
    p_failure_before_age_85: float
    p_failure_before_age_95: float
    series: list[MonteCarloPointDTO]
    assumptions: dict


@router.get(
    "/draft/cashflow-monte-carlo", response_model=MonteCarloProjectionResponse
)
def get_draft_cashflow_monte_carlo(
    user_id: str = Query("ariel"),
    years: int = Query(40, ge=1, le=50),
    retirement_age: float = Query(49.0, ge=30.0, le=80.0),
    tax_rate: float = Query(0.25, ge=0.0, le=0.5),
    mu_nominal_annual: float = Query(0.08, ge=0.02, le=0.15),
    sigma_annual: float = Query(0.18, ge=0.05, le=0.60),
    lifestyle_drift_annual: float = Query(0.0, ge=0.0, le=0.10),
    portfolio_value_usd_override: float | None = Query(None, ge=0, le=100_000_000),
    n_paths: int = Query(1000, ge=100, le=10_000),
    seed: int | None = Query(None),
    db: Session = Depends(get_db),
) -> MonteCarloProjectionResponse:
    """Monte Carlo retirement projection.

    Returns per-tick percentile bands (P10/P25/P50/P75/P90) + failure
    probabilities at ages 75/85/95. Use for stress-testing 'can I retire'
    against sequence-of-returns risk."""
    from argosy.services.cashflow_projection import (
        extract_household_state,
        extract_pension_state,
        project_monte_carlo,
    )
    from dataclasses import replace as _dc_replace

    hh = extract_household_state(db, user_id)
    pen = extract_pension_state(db, user_id)

    if portfolio_value_usd_override is not None:
        hh = _dc_replace(
            hh, portfolio_value_nis=portfolio_value_usd_override * hh.fx_usd_nis
        )

    proj = project_monte_carlo(
        household=hh, pensions=pen,
        retirement_age=retirement_age, years=years,
        mu_nominal_annual=mu_nominal_annual, sigma_annual=sigma_annual,
        tax_rate=tax_rate, lifestyle_drift_annual=lifestyle_drift_annual,
        n_paths=n_paths, seed=seed,
    )

    fx = hh.fx_usd_nis if hh.fx_usd_nis > 0 else 1.0

    def to_usd(nis: float) -> float:
        return round(nis / fx, 2)

    series_dto = [
        MonteCarloPointDTO(
            months_out=p.months_out,
            age_years=round(p.age_years, 3),
            date=p.date_yyyy_mm,
            portfolio_value_p10_usd=to_usd(p.portfolio_value_p10_nis),
            portfolio_value_p25_usd=to_usd(p.portfolio_value_p25_nis),
            portfolio_value_p50_usd=to_usd(p.portfolio_value_p50_nis),
            portfolio_value_p75_usd=to_usd(p.portfolio_value_p75_nis),
            portfolio_value_p90_usd=to_usd(p.portfolio_value_p90_nis),
            fraction_solvent=round(p.fraction_solvent, 4),
            pension_annuity_monthly_usd=to_usd(p.pension_annuity_monthly_nis),
            expenses_monthly_usd=to_usd(p.expenses_monthly_nis),
        )
        for p in proj.series
    ]

    return MonteCarloProjectionResponse(
        today_date=datetime.now(timezone.utc).date().isoformat(),
        today_age_years=round(hh.current_age_years, 3),
        fx_usd_nis=fx,
        retirement_age_assumed=round(proj.retirement_age_assumed, 1),
        n_paths=proj.n_paths,
        p_failure_before_age_75=round(proj.p_failure_before_age_75, 4),
        p_failure_before_age_85=round(proj.p_failure_before_age_85, 4),
        p_failure_before_age_95=round(proj.p_failure_before_age_95, 4),
        series=series_dto,
        assumptions=proj.assumptions,
    )


class NvdaVestEvent(BaseModel):
    date: str  # YYYY-MM-DD
    shares: int
    note: str = ""


class NvdaSaleEvent(BaseModel):
    date: str  # YYYY-MM (TSV captures month-only)
    shares: int
    price_usd: float | None = None


class NvdaTrajectoryResponse(BaseModel):
    today_date: str  # YYYY-MM-DD
    today_shares: int | None
    vests: list[NvdaVestEvent]
    past_sales: list[NvdaSaleEvent]
    reduction_program: dict
    ceiling_target_shares: float | None
    ceiling_target_label: str | None


def _deep_find(node, key: str):
    """First-match DFS for `key` anywhere in a nested dict/list. Returns the
    found value or None. Used to locate ``nvda_sale_progress`` regardless of
    which intake stage nested it (currently under ``brokerage_accounts``).
    """
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            r = _deep_find(v, key)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _deep_find(v, key)
            if r is not None:
                return r
    return None


def _extract_nvda_trajectory_from_yaml(yaml_text: str) -> tuple[list[dict], dict]:
    """Pull (vests, reduction_program) out of identity_yaml.

    Defensive: any parse failure yields empty defaults so the chart degrades
    gracefully to "no schedule available".
    """
    try:
        import yaml

        data = yaml.safe_load(yaml_text) or {}
    except Exception:  # noqa: BLE001
        return ([], {})

    if not isinstance(data, dict):
        return ([], {})

    vests: list[dict] = []
    rsu = _deep_find(data, "rsu_vest_schedule") or {}
    if isinstance(rsu, dict):
        # Explicit quarterly_vests entries (preferred — already have dates).
        for ev in rsu.get("quarterly_vests") or []:
            if not isinstance(ev, dict):
                continue
            d = ev.get("date")
            sh = ev.get("shares")
            if isinstance(d, str) and isinstance(sh, (int, float)):
                vests.append({
                    "date": d,
                    "shares": int(sh),
                    "note": str(ev.get("period") or ev.get("note") or ""),
                })

    reduction = _deep_find(data, "nvda_sale_progress") or {}
    if not isinstance(reduction, dict):
        reduction = {}

    return (vests, reduction)


@router.get("/draft/nvda-trajectory", response_model=NvdaTrajectoryResponse)
def get_draft_nvda_trajectory(
    user_id: str, db: Session = Depends(get_db)
) -> NvdaTrajectoryResponse:
    """Return NVDA share-count trajectory data for the /plan trajectory chart.

    Sources:
      - today_shares: from portfolio_positions / latest TSV (NVDA row).
      - vests: from identity_yaml::rsu_vest_schedule.quarterly_vests.
      - reduction_program: from identity_yaml::nvda_sale_progress.
      - ceiling_target_*: from the draft's long-horizon targets where
        the label mentions "share count" / "share ceiling".
    """
    from argosy.state.models import UserContext

    today = datetime.now(timezone.utc).date().isoformat()

    ctx = db.execute(
        select(UserContext).where(UserContext.user_id == user_id)
    ).scalar_one_or_none()
    vests: list[dict] = []
    reduction: dict = {}
    if ctx is not None and ctx.identity_yaml:
        vests, reduction = _extract_nvda_trajectory_from_yaml(ctx.identity_yaml)

    # today_shares — look up the most recent NVDA position from the TSV
    # parser. We re-use the same helper /api/portfolio/snapshot uses.
    # While we're parsing the TSV, also extract the historical NVDA sales
    # block (NVDASale rows: month + shares + price) so the trajectory chart
    # can plot the user's actual sell history, not just the future plan.
    today_shares: int | None = None
    past_sales_raw: list[NvdaSaleEvent] = []
    try:
        from argosy.api.routes.portfolio import _find_latest_tsv
        from argosy.ingest.tsv import parse_portfolio_tsv

        tsv = _find_latest_tsv()
        if tsv is not None:
            snap = parse_portfolio_tsv(tsv)
            for pos in snap.positions:
                if (pos.symbol or "").upper() == "NVDA" and pos.shares:
                    today_shares = int(pos.shares)
                    break

            # NVDA sales — the parser emits {month, shares, price}. Month is
            # the bare English name (Jan/Feb/...); convert to a YYYY-MM
            # date using the snapshot's snapshot_date as the anchor year.
            # Dedup on (month, shares) since the TSV occasionally repeats
            # the same row.
            from calendar import month_name, month_abbr
            month_map = {
                m.lower(): i
                for i, m in enumerate(month_name) if m
            }
            month_map.update({
                m.lower(): i
                for i, m in enumerate(month_abbr) if m
            })
            seen: set[tuple[str, int]] = set()
            anchor_year = (
                snap.snapshot_date.year
                if snap.snapshot_date is not None
                else datetime.now(timezone.utc).year
            )
            for s in snap.nvda_sales:
                if not s.month or not s.shares:
                    continue
                m_idx = month_map.get(s.month.strip().lower())
                if m_idx is None:
                    continue
                key = (s.month.strip().lower(), int(s.shares))
                if key in seen:
                    continue
                seen.add(key)
                past_sales_raw.append(NvdaSaleEvent(
                    date=f"{anchor_year:04d}-{m_idx:02d}",
                    shares=int(s.shares),
                    price_usd=s.price,
                ))
            past_sales_raw.sort(key=lambda x: x.date)
    except Exception:  # noqa: BLE001 — best-effort
        today_shares = None

    # Find the long-horizon ceiling target.
    ceiling_value: float | None = None
    ceiling_label: str | None = None
    from argosy.state.queries import get_pending_draft

    pv = get_pending_draft(db, user_id)
    if pv is not None and pv.horizon_long_json:
        try:
            payload = json.loads(pv.horizon_long_json)
            for t in payload.get("targets") or []:
                if not isinstance(t, dict):
                    continue
                label = (t.get("label") or "").lower()
                if "share count" in label or "share ceiling" in label or (
                    "ceiling" in label and "share" in label
                ):
                    val = t.get("value")
                    if isinstance(val, (int, float)):
                        ceiling_value = float(val)
                        ceiling_label = t.get("label") or None
                        break
        except json.JSONDecodeError:
            pass

    return NvdaTrajectoryResponse(
        today_date=today,
        today_shares=today_shares,
        vests=[
            NvdaVestEvent(
                date=v["date"], shares=int(v["shares"]), note=v.get("note", "")
            )
            for v in vests
        ],
        past_sales=past_sales_raw,
        reduction_program={
            "remaining": reduction.get("remaining"),
            "sold_ytd": reduction.get("sold_ytd_2026"),
            "target": reduction.get("target_shares"),
            "progress_pct": reduction.get("progress_pct"),
        },
        ceiling_target_shares=ceiling_value,
        ceiling_target_label=ceiling_label,
    )


class PlanItemHistoryEntry(BaseModel):
    plan_version_id: int
    version_label: str | None
    role: str
    drafted_at: str
    horizon: str
    summary: str
    label: str
    value: float | int | str | None
    unit: str | None
    rationale: str
    accepted: bool


class PlanItemHistoryResponse(BaseModel):
    item_id: str
    entries: list[PlanItemHistoryEntry]


@router.get(
    "/item-history",
    response_model=PlanItemHistoryResponse,
)
def get_item_history(
    item_id: str = Query(...),
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> PlanItemHistoryResponse:
    """Return the trajectory of one item_id across the user's plan_versions.

    T4.8b. Walks every plan_versions row for the user in chronological
    order; for each, scans the three horizon JSON payloads looking for
    matching ``item_id`` either in ``deltas_from_prior`` (Delta carries
    item_id directly) OR derived from ``targets``/``actions``/``themes``
    using the same slug heuristic as ``_pkg_build_prior_items_index``.

    Each match returns the proposed value, label, rationale, accepted
    flag, and the lineage metadata (which plan version, when drafted).
    The UI's history chip uses this to render "in plan #19 we said X,
    in plan #23 X became Y".
    """
    from argosy.state.models import PlanVersion
    from sqlalchemy import asc

    rows = db.execute(
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id)
        .order_by(asc(PlanVersion.imported_at))
    ).scalars().all()

    def _slug(label: str) -> str:
        return (
            "".join(c if c.isalnum() else "_" for c in label.lower()).strip("_")[
                :40
            ]
        )

    entries: list[PlanItemHistoryEntry] = []
    seen_per_plan: set[int] = set()  # dedupe to one entry per plan_version_id
    for pv in rows:
        for horizon, json_str in (
            ("long", pv.horizon_long_json),
            ("medium", pv.horizon_medium_json),
            ("short", pv.horizon_short_json),
        ):
            if not json_str:
                continue
            try:
                payload = json.loads(json_str)
            except (json.JSONDecodeError, TypeError):
                continue

            # First: scan deltas for explicit item_id matches.
            for delta in payload.get("deltas_from_prior") or []:
                if not isinstance(delta, dict):
                    continue
                if delta.get("item_id") != item_id:
                    continue
                if pv.id in seen_per_plan:
                    continue
                seen_per_plan.add(pv.id)
                proposed = delta.get("proposed") or {}
                if not isinstance(proposed, dict):
                    proposed = {}
                value = proposed.get("value")
                entries.append(
                    PlanItemHistoryEntry(
                        plan_version_id=pv.id,
                        version_label=pv.version_label,
                        role=pv.role or "?",
                        drafted_at=pv.imported_at.isoformat()
                        if pv.imported_at
                        else "",
                        horizon=delta.get("horizon") or horizon,
                        summary=delta.get("summary") or "",
                        label=proposed.get("label", "")
                        or delta.get("summary", ""),
                        value=value if isinstance(value, (int, float, str)) else None,
                        unit=proposed.get("unit"),
                        rationale=delta.get("rationale") or "",
                        accepted=bool(delta.get("accepted", False)),
                    )
                )

            # Second: scan targets/themes/actions by slug-of-label match
            # so items that existed BEFORE this revision (and weren't
            # emitted as deltas) still appear in the history. We compute
            # the synthetic id the same way _pkg_build_prior_items_index
            # does.
            for kind_key in ("targets", "themes", "actions"):
                for entry in payload.get(kind_key) or []:
                    if not isinstance(entry, dict):
                        continue
                    label = entry.get("label", "") or ""
                    if not label:
                        continue
                    synth_id = (
                        f"{horizon}.{kind_key}.{_slug(label)}"
                    )
                    if synth_id != item_id:
                        continue
                    if pv.id in seen_per_plan:
                        continue
                    seen_per_plan.add(pv.id)
                    value = entry.get("value")
                    entries.append(
                        PlanItemHistoryEntry(
                            plan_version_id=pv.id,
                            version_label=pv.version_label,
                            role=pv.role or "?",
                            drafted_at=pv.imported_at.isoformat()
                            if pv.imported_at
                            else "",
                            horizon=horizon,
                            summary=label,
                            label=label,
                            value=value if isinstance(value, (int, float, str)) else None,
                            unit=entry.get("unit"),
                            rationale=entry.get("rationale") or "",
                            accepted=False,
                        )
                    )

    return PlanItemHistoryResponse(item_id=item_id, entries=entries)


class ObjectionTranslateRequest(BaseModel):
    topic: str
    detail: str
    severity: str = "AMBER"
    cited_sources: list[str] = []


class ObjectionTranslateResponse(BaseModel):
    headline: str
    plain_english: str
    recommended_actions: list[str]
    cited_sources: list[str]


@router.post(
    "/draft/objections/translate",
    response_model=ObjectionTranslateResponse,
)
def post_translate_objection(
    body: ObjectionTranslateRequest,
    user_id: str = Query("ariel"),
) -> ObjectionTranslateResponse:
    """Render a Fund Manager objection in plain English (T4.6).

    Synchronous; Sonnet call, typically 2-5 seconds. UI fires this
    lazily when the user clicks "Explain in plain English" on an
    objection so we don't burn tokens translating every objection
    proactively.
    """
    from argosy.agents.objection_translator import (
        ObjectionTranslatorAgent,
    )
    from argosy.agents.errors import AgentRunError, MissingAPIKeyError

    agent = ObjectionTranslatorAgent(user_id=user_id)
    try:
        report = agent.run_sync(
            topic=body.topic,
            detail=body.detail,
            severity=body.severity,
            cited_sources=body.cited_sources or None,
        )
    except MissingAPIKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AgentRunError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    out = report.output
    return ObjectionTranslateResponse(
        headline=out.headline,
        plain_english=out.plain_english,
        recommended_actions=out.recommended_actions,
        cited_sources=out.cited_sources or body.cited_sources,
    )


@router.get("/draft/objections", response_model=FMObjectionsResponse)
def get_draft_objections(
    user_id: str, db: Session = Depends(get_db)
) -> FMObjectionsResponse:
    """Return structured FM objections for the pending draft.

    Parses the ``fund_manager`` agent_report.response_text into a list of
    ``{severity, topic, detail}`` objects. Severity is heuristic — a
    keyword scan over the topic + detail text. Empty objection list means
    FM approved; UI suppresses the objections card in that case.
    """
    from argosy.state.queries import get_pending_draft

    pv = get_pending_draft(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no pending draft for user")
    if pv.decision_run_id is None:
        # Synth-produced drafts always carry decision_run_id; manually-ingested
        # ones may not. Without it, we can't find the FM row.
        return FMObjectionsResponse(
            approved=True, objections=[], cited_sources=[],
            decision_run_id=None, raw_response_excerpt="",
        )

    # agent_reports.decision_id is a string column; synthesis writes
    # ``plan-synth-<int>`` per orchestrator.py.
    decision_id_str = f"plan-synth-{pv.decision_run_id}"
    fm_row = db.execute(
        select(AgentReport).where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_id_str,
            AgentReport.agent_role == "fund_manager",
        ).order_by(desc(AgentReport.created_at)).limit(1)
    ).scalar_one_or_none()

    if fm_row is None or not fm_row.response_text:
        return FMObjectionsResponse(
            approved=True, objections=[], cited_sources=[],
            decision_run_id=pv.decision_run_id, raw_response_excerpt="",
        )

    parsed = _parse_fm_response(fm_row.response_text)
    approved = bool(parsed.get("approved", True))
    reasons = parsed.get("reasons") or []
    cited = [c for c in (parsed.get("cited_sources") or []) if isinstance(c, str)]

    objections: list[FMObjection] = []
    raw_for_cache: list[dict] = []
    for r in reasons:
        if not isinstance(r, str) or not r.strip():
            continue
        topic, detail = _split_reason(r)
        sev = _classify_severity(topic, detail)
        objections.append(
            FMObjection(severity=sev, topic=topic, detail=detail)
        )
        raw_for_cache.append({"severity": sev, "topic": topic, "detail": detail})

    # Precompute (or read from cache) plain-English translations and
    # attach them inline so the UI toggle between original FM wording
    # and plain English is instant - no per-click round-trip. First
    # hit for this draft pays ~10-15 s for N translations in parallel
    # via asyncio.gather; subsequent loads return cached rows
    # immediately. Wrapped in a broad try/except so a cache-layer
    # failure (DB lock, translator crash) never breaks the route.
    if objections:
        try:
            from argosy.services.fm_objection_translation_cache import (
                get_or_compute_translations,
            )

            translations = get_or_compute_translations(
                db,
                user_id=user_id,
                plan_version_id=pv.id,
                objections=raw_for_cache,
                cited_sources=cited,
            )
        except Exception as exc:  # noqa: BLE001 - never crash the endpoint over cache
            logger.warning(
                "fm_objection_translation_cache failed user_id=%s "
                "plan_version_id=%s err=%s",
                user_id, pv.id, exc,
            )
            translations = {}

        for idx, obj in enumerate(objections):
            dto = translations.get(idx)
            if dto is None:
                continue
            obj.translation = FMObjectionTranslationDTO(
                headline=dto.headline,
                plain_english=dto.plain_english,
                recommended_actions=list(dto.recommended_actions or []),
            )

    # Prior-round objections — when this draft was synthesized to address
    # an earlier (now-superseded) draft's FM objections, surface those so
    # the UI can link "Blocker #N" / "Objection #N" tokens in the new
    # rationale text back to the exact prior-round objection by index.
    # We pick the most recent ``role='superseded'`` plan_version for this
    # user whose ``imported_at`` precedes the current draft — that's the
    # draft this synthesis run was redrafting.  ``derived_from_id`` can't
    # be used here because every draft in the chain points back to the
    # baseline, not to its immediate predecessor.
    prior_round_objections: list[FMObjection] = []
    prior_pv = db.execute(
        select(PlanVersion).where(
            PlanVersion.user_id == user_id,
            PlanVersion.role == "superseded",
            PlanVersion.imported_at < pv.imported_at,
        ).order_by(desc(PlanVersion.imported_at)).limit(1)
    ).scalar_one_or_none()
    if prior_pv is not None and prior_pv.decision_run_id is not None:
        prior_decision_id_str = f"plan-synth-{prior_pv.decision_run_id}"
        prior_fm_row = db.execute(
            select(AgentReport).where(
                AgentReport.user_id == user_id,
                AgentReport.decision_id == prior_decision_id_str,
                AgentReport.agent_role == "fund_manager",
            ).order_by(desc(AgentReport.created_at)).limit(1)
        ).scalar_one_or_none()
        if prior_fm_row is not None and prior_fm_row.response_text:
            prior_parsed = _parse_fm_response(prior_fm_row.response_text)
            prior_reasons = prior_parsed.get("reasons") or []
            for r in prior_reasons:
                if not isinstance(r, str) or not r.strip():
                    continue
                p_topic, p_detail = _split_reason(r)
                p_sev = _classify_severity(p_topic, p_detail)
                prior_round_objections.append(
                    FMObjection(severity=p_sev, topic=p_topic, detail=p_detail)
                )

    return FMObjectionsResponse(
        approved=approved,
        objections=objections,
        cited_sources=cited,
        decision_run_id=pv.decision_run_id,
        raw_response_excerpt=fm_row.response_text[:500],
        prior_round_objections=prior_round_objections,
    )


@router.get("/current/structured", response_model=DraftResponse)
def get_current_structured(
    user_id: str, db: Session = Depends(get_db)
) -> DraftResponse:
    """Return the user's currently-accepted plan as the same structured
    DraftResponse shape used by ``GET /api/plan/draft``.

    Wave 3 / Task 3.5. The legacy ``GET /api/plan/current`` endpoint
    returns a different DTO (raw markdown + latest critique) and remains
    in use by the home page + /plan page.  This sibling route mirrors
    the draft endpoint so the Argonaut page can read structured horizons
    (notably ``horizon_short.speculative_candidates``).

    404 when no current plan exists for the user.
    """
    from argosy.state.queries import get_current_plan

    pv = get_current_plan(db, user_id)
    if pv is None:
        raise HTTPException(status_code=404, detail="no current plan for user")
    return DraftResponse(
        plan_version_id=pv.id,
        version_label=pv.version_label or None,
        drafted_at=(pv.accepted_at or pv.imported_at).isoformat(),
        derived_from_id=pv.derived_from_id,
        decision_run_id=pv.decision_run_id,
        horizon_long=_horizon_view(pv.horizon_long_json),
        horizon_medium=_horizon_view(pv.horizon_medium_json),
        horizon_short=_horizon_view(pv.horizon_short_json),
        horizon_long_md=pv.horizon_long_md,
        horizon_medium_md=pv.horizon_medium_md,
        horizon_short_md=pv.horizon_short_md,
        nvda_pace=_build_nvda_pace(db, user_id, pv.decision_run_id),
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
    invalidate_home_brief(user_id)

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
    invalidate_home_brief(user_id)
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
    invalidate_home_brief(user_id)
    _publish(
        "plan.draft.delta.accepted",
        {"user_id": user_id, "draft_id": draft_id, "item_id": item_id},
    )
    return {"status": "accepted", "draft_id": draft_id, "item_id": item_id}


class DeltaRejectRequest(BaseModel):
    reason: str = ""


class DeltaPushbackRequest(BaseModel):
    feedback: str


@router.post("/draft/{draft_id}/items/{item_id}/reject")
def post_delta_reject(
    draft_id: int,
    item_id: str,
    user_id: str,
    body: DeltaRejectRequest,
    db: Session = Depends(get_db),
):
    """Mark one delta as user-rejected.

    Sets ``accepted=false``, ``user_edited=true``, and stamps the reason
    into ``user_edit_note`` with a ``REJECTED:`` prefix so the audit trail
    can distinguish a rejection from an edit. The pending-draft row stays
    in role='draft' — only the individual delta is closed out; the rest
    of the draft remains reviewable.
    """
    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found")
    found = _find_delta_horizon_field(pv, item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="item_id not found")
    field, payload, delta = found
    delta["accepted"] = False
    delta["user_edited"] = True
    reason = (body.reason or "").strip()
    delta["user_edit_note"] = (
        f"REJECTED: {reason}" if reason else "REJECTED"
    )
    setattr(pv, field, json.dumps(payload))
    db.commit()
    invalidate_home_brief(user_id)
    _publish(
        "plan.draft.delta.rejected",
        {"user_id": user_id, "draft_id": draft_id, "item_id": item_id},
    )
    return {"status": "rejected", "draft_id": draft_id, "item_id": item_id}


@router.post("/draft/{draft_id}/items/{item_id}/pushback")
def post_delta_pushback(
    draft_id: int,
    item_id: str,
    user_id: str,
    body: DeltaPushbackRequest,
    db: Session = Depends(get_db),
):
    """Record the user's pushback feedback + kick off a slim re-debate (T4.3).

    Two side effects (both happen, in order):

      1. The legacy persistence: prepend a ``PUSHBACK: <feedback>`` line
         to the delta's ``user_edit_note`` and flip ``user_edited=true``.
         Multiple pushbacks accumulate (a follow-up clicker sees their
         prior note plus the new one). This survives even if the slim
         re-debate flow refuses (e.g. cost cap breached) — the user's
         intent is captured in the draft regardless.

      2. **T4.3** — fire ``per_delta_pushback.start_per_delta_pushback``
         which opens a ``decision_runs`` row with
         ``decision_kind="delta_pushback"`` and dispatches a slim
         bull/bear/facilitator re-debate scoped to ONE horizon + ONE
         delta + the user's pushback text. Total cost ~$0.50/run. The
         flow runs on a background thread; the UI subscribes to
         ``plan.delta.pushback.completed`` WS events for completion and
         drills into ``/decisions/<run_id>`` for the verdict.

    Returns ``decision_run_id`` so the UI can:
      * subscribe to that specific run's WS events
      * surface a "re-debate running…" indicator on the delta-card
      * navigate to ``/decisions/<id>`` for the full verdict trail

    The endpoint returns synchronously after kicking off the background
    task (200 OK with ``decision_run_id``). If the cost cap refuses the
    dispatch, returns 200 with ``decision_run_id=null`` and a
    ``cost_cap_refused`` status so the UI can render a clean message
    rather than burning a generic 500.
    """
    feedback = (body.feedback or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback is required")
    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found")
    found = _find_delta_horizon_field(pv, item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="item_id not found")
    field, payload, delta = found
    delta["user_edited"] = True
    # Append rather than overwrite so multiple pushbacks accumulate.
    prior = (delta.get("user_edit_note") or "").strip()
    suffix = f"PUSHBACK: {feedback}"
    delta["user_edit_note"] = f"{prior}\n{suffix}" if prior else suffix
    setattr(pv, field, json.dumps(payload))
    db.commit()
    invalidate_home_brief(user_id)
    _publish(
        "plan.draft.delta.pushback",
        {"user_id": user_id, "draft_id": draft_id, "item_id": item_id},
    )

    # T4.3 — dispatch the slim re-debate. Defensive: the legacy
    # user_edit_note side-effect above is the source of truth for
    # user intent; if the slim flow refuses (cost cap, transient
    # dispatch failure) the UI still has the feedback persisted.
    #
    # The ``ARGOSY_DISABLE_PER_DELTA_PUSHBACK_REDEBATE`` env var is an
    # opt-out for tests / debugging: when set to "1" the route persists
    # the user_edit_note and returns with ``status="pushback_recorded"``
    # but does NOT fire the slim flow. The legacy ``test_plan_draft_api``
    # tests set this so they don't kick off background LLM calls.
    import os as _os
    from argosy.orchestrator.flows.per_delta_pushback import (
        CostCapExceededError,
        DeltaNotFoundError,
        start_per_delta_pushback,
    )

    decision_run_id: int | None = None
    inflight = False
    flow_status = "slim_redebate_started"
    detail: str | None = None

    if _os.environ.get("ARGOSY_DISABLE_PER_DELTA_PUSHBACK_REDEBATE") == "1":
        return {
            "status": "pushback_recorded",
            "draft_id": draft_id,
            "item_id": item_id,
            "feedback": feedback,
            "decision_run_id": None,
            "inflight": False,
            "detail": "slim re-debate disabled via env",
        }

    try:
        result = start_per_delta_pushback(
            db,
            user_id=user_id,
            draft_id=draft_id,
            item_id=item_id,
            user_feedback=feedback,
        )
        decision_run_id = result.decision_run_id
        inflight = result.inflight
        if inflight:
            flow_status = "slim_redebate_inflight"
    except DeltaNotFoundError:  # pragma: no cover — already validated above
        # The find_delta validation above should have caught this; if
        # not, treat as 404 for parity with the existing surface.
        raise HTTPException(status_code=404, detail="item_id not found")
    except CostCapExceededError as exc:
        flow_status = "cost_cap_refused"
        detail = str(exc)
        logger.warning(
            "post_delta_pushback cost cap refused user_id=%s draft_id=%s "
            "item_id=%s detail=%s",
            user_id, draft_id, item_id, detail,
        )
    except Exception as exc:  # noqa: BLE001
        # Dispatch failure: log + return so the legacy persistence side
        # effect is still surfaced cleanly.
        flow_status = "slim_redebate_failed_to_start"
        detail = str(exc)
        logger.warning(
            "post_delta_pushback dispatch failed user_id=%s draft_id=%s "
            "item_id=%s err=%s",
            user_id, draft_id, item_id, detail,
        )

    return {
        "status": flow_status,
        "draft_id": draft_id,
        "item_id": item_id,
        "feedback": feedback,
        "decision_run_id": decision_run_id,
        "inflight": inflight,
        "detail": detail,
    }


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
    # Validate the mutated delta against the Delta schema before persisting.
    try:
        from pydantic import ValidationError
        Delta.model_validate(delta)
    except (ValidationError, Exception) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid delta after edit: {exc}") from exc
    setattr(pv, field, json.dumps(payload))
    db.commit()
    invalidate_home_brief(user_id)
    _publish(
        "plan.draft.delta.edited",
        {"user_id": user_id, "draft_id": draft_id, "item_id": item_id},
    )
    return {"status": "edited", "draft_id": draft_id, "item_id": item_id}


# ---------------------------------------------------------------------------
# Wave 3 — speculative-candidate "Take a swing" endpoint (T3.4)
# ---------------------------------------------------------------------------


class TakeSpeculativeResponse(BaseModel):
    status: str
    proposal_id: int
    ticker: str
    paper: bool


@router.post("/current/speculative/{ticker}/take", response_model=TakeSpeculativeResponse)
def post_take_speculative(
    ticker: str,
    user_id: str,
    execution_mode: str = "paper",
    db: Session = Depends(get_db),
) -> TakeSpeculativeResponse:
    """Route an accepted speculative candidate -> Argonaut T0 proposal."""
    from argosy.orchestrator.speculation_router import (
        CapBreachError,
        UnknownCandidateError,
        route_accepted_candidate,
    )

    try:
        out = route_accepted_candidate(
            db, user_id=user_id, ticker=ticker, execution_mode=execution_mode,
        )
    except UnknownCandidateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CapBreachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return TakeSpeculativeResponse(
        status="routed", proposal_id=out.proposal_id, ticker=out.ticker, paper=out.paper,
    )


# ---------------------------------------------------------------------------
# Action items widget — short/medium horizon dated actions surfaced as a
# checklist on the home page. Read-only view over horizon_short_json /
# horizon_medium_json actions[] from the pending draft (or the current
# accepted plan when no draft exists).
# ---------------------------------------------------------------------------


from datetime import date as _date
from typing import Literal


class ActionItem(BaseModel):
    """One dated short- or medium-horizon action surfaced to the home page.

    Sourced from a plan-version's ``horizon_short_json.actions[]`` and
    ``horizon_medium_json.actions[]``.  Only actions that carry a parseable
    ISO date in ``stated_at`` / ``due_date`` / ``trigger_or_date`` are
    eligible; actions with directional or parameterized triggers are
    skipped because there's no calendar slot to slot them into.
    """

    item_id: str
    horizon: Literal["short", "medium", "long"]
    label: str
    detail: str
    dated: _date | None
    days_until: int | None
    status: Literal["UPCOMING", "DUE_SOON", "OVERDUE", "TODAY"]
    rationale: str
    cited_sources: list[str]
    plan_version_id: int


class ActionItemsResponse(BaseModel):
    items: list[ActionItem]
    next_due: _date | None
    overdue_count: int
    today_count: int
    upcoming_count: int


def _slug_action(label: str) -> str:
    """Match the slug heuristic in ``get_item_history`` so an action
    surfaced here lines up with the per-item history endpoint."""
    return (
        "".join(c if c.isalnum() else "_" for c in label.lower()).strip("_")[:40]
    )


_ISO_DATE_RE = None  # lazy compile


def _parse_action_date(action: dict) -> _date | None:
    """Best-effort: pull an ISO date out of an action dict.

    Recognized keys (in order of preference):
      * ``stated_at`` / ``due_date`` — explicit date fields (spec)
      * ``trigger_or_date`` — synthesizer's combined field; when
        ``horizon_kind == "dated"`` this is a bare YYYY-MM-DD string.
        For parameterized actions, scan for a YYYY-MM-DD substring and
        pick the *earliest* date mentioned (so a trip-wire whose text
        names multiple gating dates still surfaces under the soonest one).
    """
    import re

    for key in ("stated_at", "due_date"):
        v = action.get(key)
        if isinstance(v, str) and v.strip():
            try:
                return _date.fromisoformat(v.strip()[:10])
            except ValueError:
                continue

    tod = action.get("trigger_or_date")
    if not isinstance(tod, str) or not tod.strip():
        return None
    kind = action.get("horizon_kind")
    s = tod.strip()
    if kind == "dated":
        try:
            return _date.fromisoformat(s[:10])
        except ValueError:
            return None
    # Parameterized — scan for embedded YYYY-MM-DD literals and pick
    # the earliest. ``horizon_kind == "directional"`` typically lacks
    # any date but we apply the same regex defensively.
    global _ISO_DATE_RE
    if _ISO_DATE_RE is None:
        _ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
    matches = _ISO_DATE_RE.findall(s)
    parsed: list[_date] = []
    for m in matches:
        try:
            parsed.append(_date.fromisoformat(m))
        except ValueError:
            continue
    if not parsed:
        return None
    return min(parsed)


def _classify_status(
    dated: _date, today: _date
) -> Literal["UPCOMING", "DUE_SOON", "OVERDUE", "TODAY"]:
    if dated == today:
        return "TODAY"
    if dated < today:
        return "OVERDUE"
    delta = (dated - today).days
    if delta <= 3:
        return "DUE_SOON"
    return "UPCOMING"


def _collect_action_items(
    pv: PlanVersion,
    *,
    today: _date,
    window_days: int,
) -> list[ActionItem]:
    """Walk a plan version's short + medium horizon actions and emit
    surfaced ``ActionItem`` rows.

    Cutoff: keep an action when its ``dated`` is on or before
    ``today + window_days``. Past-due dates (``dated < today``) are
    *always* kept — the user wants overdue items at the top regardless
    of how far back they slipped.
    """
    cutoff = today + timedelta(days=window_days)
    items: list[ActionItem] = []
    for horizon, json_str in (
        ("short", pv.horizon_short_json),
        ("medium", pv.horizon_medium_json),
    ):
        if not json_str:
            continue
        try:
            payload = json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            continue
        for action in payload.get("actions") or []:
            if not isinstance(action, dict):
                continue
            dated = _parse_action_date(action)
            if dated is None:
                continue
            if dated > cutoff:
                continue
            label = (action.get("label") or "").strip()
            if not label:
                continue
            detail = (action.get("detail") or "").strip()
            rationale = (action.get("rationale") or "").strip()[:200]
            cited = [
                s for s in (action.get("cited_sources") or []) if isinstance(s, str)
            ]
            days_until = (dated - today).days
            status = _classify_status(dated, today)
            item_id = f"{horizon}.actions.{_slug_action(label)}"
            items.append(
                ActionItem(
                    item_id=item_id,
                    horizon=horizon,
                    label=label,
                    detail=detail,
                    dated=dated,
                    days_until=days_until,
                    status=status,
                    rationale=rationale,
                    cited_sources=cited,
                    plan_version_id=pv.id,
                )
            )
    # Sort ASC by date (overdue first because their days_until is negative).
    items.sort(key=lambda it: (it.dated or _date.max))
    return items


@router.get("/action-items", response_model=ActionItemsResponse)
def get_action_items(
    user_id: str = Query("ariel"),
    window_days: int = Query(14, ge=1, le=365),
    db: Session = Depends(get_db),
) -> ActionItemsResponse:
    """Return a flat list of dated short/medium-horizon actions for the
    home-page Action Items widget.

    Source order:
      1. The user's pending draft (role='draft'), if any.
      2. Else the user's currently-accepted plan (role='current').
      3. Else an empty list with 200 (never 404).

    The widget is intentionally read-only. Accepting / rejecting individual
    items still flows through ``/draft/{id}/items/{item_id}/accept`` etc.
    """
    from argosy.state.queries import get_current_plan, get_pending_draft

    pv = get_pending_draft(db, user_id) or get_current_plan(db, user_id)
    if pv is None:
        return ActionItemsResponse(
            items=[],
            next_due=None,
            overdue_count=0,
            today_count=0,
            upcoming_count=0,
        )

    today = datetime.now(timezone.utc).date()
    items = _collect_action_items(pv, today=today, window_days=window_days)

    overdue_count = sum(1 for it in items if it.status == "OVERDUE")
    today_count = sum(1 for it in items if it.status == "TODAY")
    upcoming_count = sum(
        1 for it in items if it.status in ("UPCOMING", "DUE_SOON")
    )
    # Earliest non-past date — "what's the next thing on the calendar?".
    future_dates = [it.dated for it in items if it.dated and it.dated >= today]
    next_due = min(future_dates) if future_dates else None

    return ActionItemsResponse(
        items=items,
        next_due=next_due,
        overdue_count=overdue_count,
        today_count=today_count,
        upcoming_count=upcoming_count,
    )


# Markdown export — one-pager snapshot of plan + wealth dashboard
# ---------------------------------------------------------------------------


@router.get("/export")
def get_plan_export(
    user_id: str = Query("ariel"),
    format: str = Query("markdown"),
    window_days: int = Query(14, ge=1, le=365),
    db: Session = Depends(get_db),
) -> Response:
    """Return a downloadable one-pager export of the user's current plan +
    wealth dashboard + action items + FM objections.

    Only ``format=markdown`` is supported today — PDF generation is
    intentionally out of scope. Downstream tools (pandoc, browser
    print-to-PDF) handle conversion. The endpoint returns a
    ``text/markdown`` body with a ``Content-Disposition: attachment``
    header so the browser triggers a save dialog.
    """
    if format != "markdown":
        raise HTTPException(
            status_code=400,
            detail=f"unsupported format: {format!r}; only 'markdown' is supported",
        )
    from argosy.services.plan_export import (
        build_plan_export_markdown,
        export_filename,
    )

    body = build_plan_export_markdown(
        db, user_id=user_id, window_days=window_days,
    )
    fname = export_filename()
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
        },
    )


__all__ = [
    "AcceptResponse",
    "ActionItem",
    "ActionItemsResponse",
    "ActionItem",
    "ActionItemsResponse",
    "BaselineResponse",
    "CashflowPointDTO",
    "CashflowProjectionResponse",
    "DeltaEditRequest",
    "DistillateItemEditRequest",
    "DraftResponse",
    "HorizonSectionView",
    "InFlightSynthesisDTO",
    "InFlightSynthesisResponse",
    "NvdaPaceView",
    "RejectRequest",
    "SynthesisHealth",
    "TakeSpeculativeResponse",
    "TargetProgress",
    "TargetProgressResponse",
    "_publish",
    "get_db",
    "router",
]
