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
            critique_created_at = _iso_utc(critique.created_at)
        return PlanCurrentDTO(
            plan_version_id=plan.id,
            version_label=plan.version_label or None,
            raw_markdown=plan.raw_markdown,
            imported_at=_iso_utc(plan.imported_at),
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
        distilled_at=_iso_utc(pv.distilled_at),
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


def _iso_utc(dt: datetime | None) -> str | None:
    """Render a datetime as ISO 8601 with explicit UTC marker.

    SQLite stores naive datetimes — but every write in this codebase
    uses ``datetime.now(timezone.utc)``, so a naive value coming back
    out IS UTC by convention. Stamping the tzinfo before ``isoformat``
    ensures the rendered string carries a timezone suffix, so the
    frontend can correctly convert UTC -> the user's local time
    instead of misreading the timestamp as local-naive (which was
    showing 06:42 in the UI when actual local was 09:42 IDT).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


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
    # Known, non-actionable adapter gaps (auth/tier-blocked sources,
    # Cloudflare challenges, instruments a source structurally doesn't
    # cover). Split out from adapters_failed so the chip alarms only on
    # genuinely actionable failures. Defaults to 0 for legacy payloads.
    adapters_unavailable: int = 0
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
    # The PlanVersion.role the row was sourced from. "draft" when a real
    # pending draft exists; "superseded" when the route fell back to the most
    # recent non-pending draft (e.g. FM-rejected drafts that were auto-
    # superseded by the next synthesis attempt). The UI uses this to
    # render a "this draft is no longer pending" banner and to gate the
    # accept/reject CTAs.
    effective_role: str = "draft"


class AcceptResponse(BaseModel):
    status: str
    new_current_id: int
    # Phase 6 of docs/plans/argosy-comprehensive-plan-integration.md.
    # `gate_warning` is populated when the plan_output_gate found
    # violations on the draft about to be promoted AND the
    # `plan_gate_enforce` setting was False (the gate ran as a
    # warning). It's None on a clean promotion. When the setting is
    # True, gate failures don't surface here — they raise 422
    # before reaching this response.
    gate_warning: dict | None = None


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
        adapters_unavailable=int(summary.get("adapters_unavailable", 0)),
        decision_run_id=decision_run_id,
    )


@router.get("/draft", response_model=DraftResponse)
def get_draft(user_id: str, db: Session = Depends(get_db)) -> DraftResponse:
    """Return the user's pending draft, OR the most recent superseded
    draft when no pending one exists.

    Falling back to the most recent draft (any role except baseline /
    current) keeps the /plan rich view alive between synthesis runs: a
    failed/blocked synthesis attempt demotes the prior draft as part of
    its idempotency step but doesn't roll back when the run never
    produces a successor, leaving the user with no surface to act on.
    The orchestrator commit that fixes the transactional ordering ships
    alongside this; this route is the data-layer half of the safety net
    so the surface is robust even when an orchestrator bug recurs.

    The UI consumes ``effective_role`` to decide whether the standard
    accept/reject CTAs should fire or whether to show a "press Run
    synthesis to refresh" banner.
    """
    from argosy.state.queries import get_current_plan, get_pending_draft

    pv = get_pending_draft(db, user_id)
    effective_role = "draft"
    if pv is None:
        # v4 #25 — no pending draft. Prefer the CURRENT plan so the
        # synthesis-health chip reflects the accepted plan's decision_run
        # (drun), not a stale superseded draft's. Before this fix the
        # fallback excluded 'current' and picked the most-recent
        # superseded draft, so the health chip showed an OLD drun (e.g.
        # 71) while the live plan was a newer run (73). The UI treats
        # effective_role != "draft" as a non-pending/stale surface
        # (plan-view-state.isPendingDraft), so returning the current plan
        # here does not surface accept/reject CTAs.
        current = get_current_plan(db, user_id)
        if current is not None and current.horizon_long_json is not None:
            pv = current
            effective_role = "current"
        else:
            pv = (
                db.execute(
                    select(PlanVersion)
                    .where(
                        PlanVersion.user_id == user_id,
                        PlanVersion.role.notin_(("baseline", "current")),
                        # Draft-shaped rows carry horizon JSON; baselines
                        # don't. Defensive filter so a malformed row can't
                        # masquerade as a draft.
                        PlanVersion.horizon_long_json.is_not(None),
                    )
                    .order_by(desc(PlanVersion.id))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if pv is None:
                raise HTTPException(status_code=404, detail="no draft for user")
            effective_role = pv.role or "superseded"
    return DraftResponse(
        plan_version_id=pv.id,
        version_label=pv.version_label or None,
        drafted_at=_iso_utc(pv.imported_at) or "",
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
        effective_role=effective_role,
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
    # Live phase visibility — derived from the latest decision_phases row.
    # Lets the UI render "Synthesizer (phase 3 of 5) — running for 24
    # minutes" instead of just "phase 2 of 5 complete" (which is
    # technically true but doesn't tell the user that phase 3 is the
    # one actually chewing right now).
    current_phase: int | None = None  # 1..5; None when finished.
    current_phase_label: str | None = None
    current_phase_started_at: str | None = None  # ISO-UTC
    current_phase_elapsed_seconds: int | None = None


# Synthesis phase names — used to label which phase is mid-flight in
# the in-flight DTO. Mirrors the synthesis.phase_N kinds written by
# decision_phases rows in argosy/orchestrator/flows/plan_synthesis/.
_SYNTHESIS_PHASE_LABELS = {
    1: "analysts",
    2: "debate teams",
    3: "synthesizer",
    4: "risk officers",
    5: "fund manager",
}


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

    # Derive current-phase visibility from the latest decision_phases
    # row for this run. The orchestrator writes a phase row when each
    # phase STARTS (and updates finished_at when it commits). When
    # the most-recent row has no finished_at, that phase is in flight
    # right now — even if the LLM call inside is mid-retry. Surfaces
    # in the DTO so the UI can render "Synthesizer (phase 3 of 5) —
    # running for N minutes" instead of just "phase 2 of 5 complete"
    # (the user can't otherwise tell that phase 3 is the one actively
    # chewing).
    from datetime import datetime as _dt, timezone as _tz

    latest_phase = db.execute(
        select(DecisionPhase)
        .where(DecisionPhase.decision_run_id == run.id)
        .order_by(desc(DecisionPhase.seq))
        .limit(1)
    ).scalar_one_or_none()

    current_phase: int | None = None
    current_phase_label: str | None = None
    current_phase_started_at: str | None = None
    current_phase_elapsed_seconds: int | None = None
    if latest_phase is not None:
        if latest_phase.finished_at is None:
            # Phase is in flight.
            current_phase = int(latest_phase.seq)
            current_phase_started_at = _iso_utc(latest_phase.started_at)
            if latest_phase.started_at is not None:
                started = latest_phase.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=_tz.utc)
                current_phase_elapsed_seconds = int(
                    (_dt.now(_tz.utc) - started).total_seconds()
                )
        elif completed_phases < 5:
            # Last persisted phase is done; the NEXT phase has begun
            # but hasn't written its decision_phases row yet (the
            # orchestrator's _record_phase_completion writes both
            # started_at and finished_at on commit, so the gap
            # between phases looks like "no current phase" until the
            # next phase's LLM call returns). Surface the gap state
            # honestly so the UI can render "Phase 3 (synthesizer) —
            # starting" instead of pretending phase 2 is still
            # running.
            current_phase = int(latest_phase.seq) + 1
    if current_phase is not None:
        current_phase_label = _SYNTHESIS_PHASE_LABELS.get(current_phase)

    return InFlightSynthesisResponse(
        in_flight_synthesis=InFlightSynthesisDTO(
            decision_run_id=run.id,
            decision_audit_token=f"plan-synth-{run.id}",
            started_at=_iso_utc(run.started_at) or "",
            completed_phases=int(completed_phases),
            total_phases=5,
            status=run.status or "running",
            current_phase=current_phase,
            current_phase_label=current_phase_label,
            current_phase_started_at=current_phase_started_at,
            current_phase_elapsed_seconds=current_phase_elapsed_seconds,
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
    # When True, this objection was carried over from a prior draft's
    # FM verdict because the current draft has no Fund-Manager
    # evaluation of its own (typical for plan_amendment_chat drafts).
    # The UI badges these as "carried over from draft #N" so the user
    # doesn't mistake an un-re-evaluated concern for a fresh one.
    carried_over: bool = False
    carried_over_from_plan_version_id: int | None = None
    # Auto-dialogue status, populated by the route from the dialogue
    # run + FM verdict agent_report (if any) keyed to this objection's
    # (plan_version_id, objection_index). Drives the /plan filter
    # described below.
    #
    #   "not_dispatched": no auto-dialogue ran for this row. Happens
    #     when the objection has no analyst owner OR when the synthesis
    #     was older than the auto-dispatch feature (commit 75e24c8).
    #   "running":  dialogue dispatched but FM verdict hasn't landed
    #     yet. UI shows a "resolving..." pill + polls until done.
    #   "completed": dialogue produced an FM verdict. See
    #     auto_dialogue_resolution for the outcome.
    #   "failed" / "superseded": dialogue ran but errored or was raced.
    auto_dialogue_status: str = "not_dispatched"
    auto_dialogue_resolution: str | None = None
    # Derived: True when the user must take action on this objection
    # (it's a Blocker or a Decision). False when the fleet resolved
    # it internally (FM_ACCEPTS_ANALYST). The /plan filter hides
    # ``user_action_required=False`` rows behind a collapsed footer.
    user_action_required: bool = True
    # Categorization of what kind of user action is needed:
    #   "blocker"  — user must AGREE/DISAGREE/DEFER on the underlying
    #     concern; agents couldn't resolve it.
    #   "decision" — FM proposed a revised objection; user picks
    #     original-vs-revised.
    #   None — no action needed (auto-resolved by fleet).
    action_kind: str | None = "blocker"


class FMObjectionsResponse(BaseModel):
    # `approved` is the Fund Manager's literal verdict (True/False).
    # `None` means no FM has evaluated this draft yet — typically a
    # plan_amendment_chat draft whose worker writes a synthetic phase
    # record but never invokes the FM agent. The UI maps None to
    # "Not FM-evaluated — run synthesis for a verdict" instead of
    # silently rendering "Approved".
    approved: bool | None
    # Verdict provenance — drives the /plan banner state machine.
    #   "evaluated"    — a real FM agent_report exists for this draft
    #   "not_evaluated"— no FM has scored this draft; objections list
    #                    is empty AND no carry-forward source found
    #   "carried_over" — no FM verdict on this draft, but the prior
    #                    draft had one and its objections are surfaced
    #                    below with carried_over=True
    verdict_status: str = "evaluated"
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
    retirement_age: float | None = Query(
        None,
        ge=30.0,
        le=80.0,
        description=(
            "Retirement age for the projection. Defaults to the canonical "
            "dual-track headline age (resolved server-side) so the chart agrees "
            "with /retirement; pass a value only for what-if overrides."
        ),
    ),
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
    monthly_expenses_nis_override: float | None = Query(
        None,
        ge=0.0,
        le=500_000.0,
        description=(
            "Wave 8 v2.4 — replace the household_budget-derived monthly "
            "expenses with this NIS amount. Useful for stress-tests "
            "('what if I move to a more expensive city — 35k/mo instead "
            "of 23k'). When omitted, uses the latest household_budget."
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

    # T2.3 — the cashflow chart's retirement age defaults to the ONE canonical
    # headline age (the dual-track typical drawdown age), not a magic 49. This
    # keeps /plan's chart age == /retirement's headline == the dual-track age
    # (the cross-surface guardrail). The UI may still override for what-ifs.
    if retirement_age is None:
        try:
            from argosy.services.retirement.retirement_plan import (
                canonical_feasible_dual_track,
            )

            _canon = canonical_feasible_dual_track(session=db, user_id=user_id)
            retirement_age = (
                float(_canon.earliest_feasible_age)
                if _canon.earliest_feasible_age is not None
                else 49.0
            )
        except Exception:  # noqa: BLE001 — fall back to the prior default
            retirement_age = 49.0

    # Apply the override (if any) BEFORE the projection. We swap the
    # ``portfolio_value_nis`` field on the immutable dataclass via
    # ``dataclasses.replace`` so the rest of the household state
    # (expenses, fx, age) is preserved.
    from dataclasses import replace as _dc_replace
    if portfolio_value_usd_override is not None:
        hh = _dc_replace(
            hh,
            portfolio_value_nis=portfolio_value_usd_override * hh.fx_usd_nis,
        )
    if monthly_expenses_nis_override is not None:
        hh = _dc_replace(hh, monthly_expenses_nis=monthly_expenses_nis_override)

    # Spec D commit #3 — load the user's life events so the projected
    # expense series reflects their cashflow-shape deltas (one_shot,
    # recurring, phase_change_*).  apply_life_event_deltas runs inside
    # project_cashflow when the list is non-empty.
    from argosy.state.models import LifeEvent as _LifeEvent
    life_events = (
        db.query(_LifeEvent)
        .filter(_LifeEvent.user_id == user_id)
        .all()
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
        life_events=life_events,
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
    # Deterministic income-composition fields for the cashflow-coverage chart.
    bl_monthly_usd: float = 0.0
    lump_amount_usd: float = 0.0
    portfolio_net_draw_monthly_usd: float = 0.0
    portfolio_gross_withdrawal_monthly_usd: float = 0.0


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


def _mc_projection_to_response(proj) -> MonteCarloProjectionResponse:
    """Serialize a ``MonteCarloProjection`` to the wire DTO (USD), deriving fx +
    age from the projection's household-at-start. Shared by the
    cashflow-monte-carlo + plan-series endpoints so they stay identical on the
    wire."""
    hh = proj.household_state_at_start
    fx = hh.fx_usd_nis if (hh and hh.fx_usd_nis and hh.fx_usd_nis > 0) else 1.0

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
            bl_monthly_usd=to_usd(p.bl_monthly_nis),
            lump_amount_usd=to_usd(p.lump_amount_nis),
            portfolio_net_draw_monthly_usd=to_usd(p.portfolio_net_draw_monthly_nis),
            portfolio_gross_withdrawal_monthly_usd=to_usd(
                p.portfolio_gross_withdrawal_monthly_nis
            ),
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
    monthly_expenses_nis_override: float | None = Query(
        None, ge=0.0, le=500_000.0
    ),
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
    if monthly_expenses_nis_override is not None:
        hh = _dc_replace(hh, monthly_expenses_nis=monthly_expenses_nis_override)

    proj = project_monte_carlo(
        household=hh, pensions=pen,
        retirement_age=retirement_age, years=years,
        mu_nominal_annual=mu_nominal_annual, sigma_annual=sigma_annual,
        tax_rate=tax_rate, lifestyle_drift_annual=lifestyle_drift_annual,
        n_paths=n_paths, seed=seed,
    )

    return _mc_projection_to_response(proj)


@router.get(
    "/current/cashflow-monte-carlo", response_model=MonteCarloProjectionResponse
)
def get_current_cashflow_monte_carlo(
    user_id: str = Query("ariel"),
    years: int = Query(40, ge=1, le=50),
    retirement_age: float = Query(49.0, ge=30.0, le=80.0),
    tax_rate: float = Query(0.25, ge=0.0, le=0.5),
    mu_nominal_annual: float = Query(0.08, ge=0.02, le=0.15),
    sigma_annual: float = Query(0.18, ge=0.05, le=0.60),
    lifestyle_drift_annual: float = Query(0.0, ge=0.0, le=0.10),
    portfolio_value_usd_override: float | None = Query(None, ge=0, le=100_000_000),
    monthly_expenses_nis_override: float | None = Query(
        None, ge=0.0, le=500_000.0
    ),
    n_paths: int = Query(1000, ge=100, le=10_000),
    seed: int | None = Query(None),
    db: Session = Depends(get_db),
) -> MonteCarloProjectionResponse:
    """Wave 8 Piece D — Monte Carlo retirement projection for the
    canonical CURRENT plan's recap surface.

    The math is identical to ``/api/plan/draft/cashflow-monte-carlo``;
    routing them under ``/current/...`` keeps the recap's wire surface
    symmetric with ``/current/headline`` + ``/current/allocation-glidepath``
    so the UI's recap layer reads exclusively from ``/current/*``."""
    return get_draft_cashflow_monte_carlo(
        user_id=user_id,
        years=years,
        retirement_age=retirement_age,
        tax_rate=tax_rate,
        mu_nominal_annual=mu_nominal_annual,
        sigma_annual=sigma_annual,
        lifestyle_drift_annual=lifestyle_drift_annual,
        portfolio_value_usd_override=portfolio_value_usd_override,
        monthly_expenses_nis_override=monthly_expenses_nis_override,
        n_paths=n_paths,
        seed=seed,
        db=db,
    )


@router.get(
    "/current/plan-series", response_model=MonteCarloProjectionResponse
)
def get_current_plan_series(
    user_id: str = Query("ariel"),
    retire_age: float = Query(47.0, ge=30.0, le=80.0),
    regime: str = Query("typical"),
    n_paths: int = Query(1200, ge=100, le=10_000),
    db: Session = Depends(get_db),
) -> MonteCarloProjectionResponse:
    """Monte Carlo series on the DUAL-TRACK PLAN basis (deconcentrated NVDA,
    σ-glide 34→18%, reserve-netted at PV, 5% real / 10% interim tax) for a
    selected ``retire_age`` + market ``regime`` ('typical'|'bull'|'bear').

    This is the feed the /plan portfolio-bands + cashflow-coverage charts SHOULD
    use so they reconcile with the headline dual-track ages — unlike
    ``/current/cashflow-monte-carlo``, which runs the stale 'keep-NVDA, do
    nothing' config (full concentration, σ flat, 25% tax) and therefore reads
    'stress-test fails'. 404 when the FI spend basis can't be sourced."""
    from argosy.services.retirement.retirement_plan import (
        RetirementAssumptions,
        plan_series,
    )

    try:
        proj = plan_series(
            session=db, user_id=user_id, retire_age=retire_age, regime=regime,
            assumptions=RetirementAssumptions(n_paths=n_paths),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _mc_projection_to_response(proj)


class NvdaVestEvent(BaseModel):
    date: str  # YYYY-MM-DD
    shares: int
    note: str = ""


class NvdaSaleEvent(BaseModel):
    date: str  # YYYY-MM (TSV captures month-only)
    shares: int
    price_usd: float | None = None


class NvdaProjectionPathPoint(BaseModel):
    date: str  # YYYY-MM-DD
    shares: int
    tradeable_weight_pct: float


class NvdaTrajectoryResponse(BaseModel):
    today_date: str  # YYYY-MM-DD
    today_shares: int | None
    vests: list[NvdaVestEvent]
    past_sales: list[NvdaSaleEvent]
    reduction_program: dict
    ceiling_target_shares: float | None
    ceiling_target_label: str | None
    # The canonical forward sell glide (today_shares → target_shares at the
    # 13% cap) from compute_nvda_projection — the planned reduction path the
    # chart draws as the "sell points". Empty when the projection is unavailable.
    projected_path: list[NvdaProjectionPathPoint] = []


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

    # Prefer the authoritative Schwab sale records (nvda_sale_progress.sales_2026,
    # ingested from the Equity Awards Transactions CSV) over the TSV's sale block,
    # which is generated from internal state and can lag the brokerage. Each entry:
    # {date: YYYY-MM-DD, shares, avg_price_usd}.
    _sales_2026 = reduction.get("sales_2026") if isinstance(reduction, dict) else None
    if isinstance(_sales_2026, list) and _sales_2026:
        _authoritative: list[NvdaSaleEvent] = []
        for s in _sales_2026:
            if not isinstance(s, dict):
                continue
            d = s.get("date")
            sh = s.get("shares")
            if isinstance(d, str) and isinstance(sh, (int, float)):
                _authoritative.append(NvdaSaleEvent(
                    date=d,
                    shares=int(sh),
                    price_usd=s.get("avg_price_usd"),
                ))
        if _authoritative:
            _authoritative.sort(key=lambda x: x.date)
            past_sales_raw = _authoritative

    # T2.4 — the canonical NVDA target comes from the single projection
    # (codex-verified share math: 11,471 -> floor(cap/current x shares) = 2,299
    # at the 13% cap), wiring the previously-orphaned compute_nvda_projection so
    # the trajectory reconciles to the plan instead of an identity_yaml ceiling.
    ceiling_value: float | None = None
    ceiling_label: str | None = None
    projected_path: list[NvdaProjectionPathPoint] = []
    try:
        from argosy.services.nvda_projection import compute_nvda_projection

        _proj = compute_nvda_projection(
            db, user_id, datetime.now(timezone.utc).date()
        )
        if _proj is not None:
            ceiling_value = float(_proj.target_shares)
            ceiling_label = f"Canonical NVDA target ({_proj.cap_pct:.0f}% cap)"
            if today_shares is None:
                today_shares = _proj.today_shares
            projected_path = [
                NvdaProjectionPathPoint(
                    date=p.point_date.isoformat(),
                    shares=int(p.shares),
                    tradeable_weight_pct=round(float(p.tradeable_weight_pct), 2),
                )
                for p in _proj.points
            ]
    except Exception:  # noqa: BLE001 — best-effort; fall back to the draft ceiling
        pass

    # Fallback: the draft's long-horizon share-ceiling target.
    if ceiling_value is None:
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
        projected_path=projected_path,
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
                        drafted_at=_iso_utc(pv.imported_at) or "",
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
                            drafted_at=_iso_utc(pv.imported_at) or "",
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

    # Look up the FM agent_report for this draft's synthesis run. Drafts
    # produced by plan_amendment_chat do NOT carry a fund_manager
    # agent_report (the medium worker stamps a synthetic negotiation
    # phase record but never invokes the agent), so we fall through to
    # the carry-forward path below in that case.
    fm_row = None
    decision_id_str = (
        f"plan-synth-{pv.decision_run_id}" if pv.decision_run_id is not None else None
    )
    if decision_id_str is not None:
        fm_row = db.execute(
            select(AgentReport).where(
                AgentReport.user_id == user_id,
                AgentReport.decision_id == decision_id_str,
                AgentReport.agent_role == "fund_manager",
            ).order_by(desc(AgentReport.created_at)).limit(1)
        ).scalar_one_or_none()

    if fm_row is None or not fm_row.response_text:
        # No real FM verdict for this draft. Try to carry forward the
        # most recent earlier draft's FM objections so the user sees
        # what's still potentially open instead of a misleading silent
        # "Approved" state. The carry-forward is purely informational:
        # the objections come from a draft that was evaluated against
        # different inputs and may not still apply.
        return _build_carried_over_response(
            db, user_id=user_id, current_pv=pv,
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

    # Enrich each objection with its auto-dialogue resolution status so
    # the UI can filter the surface to Blocker / Decision rows only.
    # See FMObjection field docstrings for the state machine. Best-
    # effort: cache failures or schema misses fall through to the
    # default (action_kind="blocker", user_action_required=True) which
    # is the safe direction — we'd rather surface a row that could
    # have been auto-resolved than hide one that needs user input.
    if objections:
        try:
            _enrich_with_auto_dialogue_status(
                db,
                user_id=user_id,
                plan_version_id=pv.id,
                objections=objections,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "auto_dialogue.status_enrich_failed user_id=%s "
                "plan_version_id=%s err=%s",
                user_id, pv.id, exc,
            )

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
        verdict_status="evaluated",
        objections=objections,
        cited_sources=cited,
        decision_run_id=pv.decision_run_id,
        raw_response_excerpt=fm_row.response_text[:500],
        prior_round_objections=prior_round_objections,
    )


def _enrich_with_auto_dialogue_status(
    db: Session,
    *,
    user_id: str,
    plan_version_id: int,
    objections: list[FMObjection],
) -> None:
    """Populate auto_dialogue_status / resolution / action_kind /
    user_action_required on each FMObjection IN-PLACE.

    Reads dialogue runs from ``decision_runs`` (decision_kind=
    ``fm_objection_dialogue``) keyed by the notes_json field
    {plan_version_id, objection_index}, then resolves each to its FM
    verdict agent_report (decision_id=``fm-obj-dialogue-<run_id>``).

    The action_kind decision table:
      - No analyst owner       -> blocker      (no dialogue possible)
      - Dialogue running       -> blocker      (placeholder; UI polls)
      - Dialogue failed        -> blocker      (degrade to user)
      - FM_ACCEPTS_ANALYST     -> None         (auto-resolved; HIDDEN)
      - FM_MAINTAINS_OBJECTION -> blocker      (real disagreement)
      - ESCALATE_TO_USER       -> blocker      (judgment call)
      - FM_REVISES_OBJECTION   -> decision     (pick original vs revised)
    """
    import json as _json
    import re as _re

    from argosy.state.models import DecisionRun

    # Pull every dialogue run for this draft.
    dialogue_runs = db.execute(
        select(DecisionRun)
        .where(
            DecisionRun.user_id == user_id,
            DecisionRun.decision_kind == "fm_objection_dialogue",
        )
        .order_by(desc(DecisionRun.id))
    ).scalars().all()

    # Bucket by objection_index, keeping the most recent (largest id).
    latest_by_idx: dict[int, DecisionRun] = {}
    for run in dialogue_runs:
        if not run.notes_json:
            continue
        try:
            notes = _json.loads(run.notes_json)
        except (ValueError, TypeError):
            continue
        if notes.get("plan_version_id") != plan_version_id:
            continue
        idx = notes.get("objection_index")
        if not isinstance(idx, int):
            continue
        if idx not in latest_by_idx:
            latest_by_idx[idx] = run

    # Detect analyst ownership per objection so we know which
    # rows COULD have had a dialogue dispatched. Without it we'd
    # mistakenly mark "no analyst owner" rows as
    # auto_dialogue_status="not_dispatched" when the underlying state
    # is "can't dispatch". The UI uses action_kind not the status
    # string for the surface decision, so both branches map to
    # "blocker", but the status string is more honest this way.
    from argosy.orchestrator.flows.fm_objection_dialogue import (
        _parse_analyst_refs_any_form,
    )

    for idx, obj in enumerate(objections):
        analyst_refs = _parse_analyst_refs_any_form(f"{obj.topic} {obj.detail}")
        run = latest_by_idx.get(idx)
        if run is None:
            # No dialogue dispatched. If no analyst owner, this is
            # structural — user must arbitrate. Otherwise it's an
            # older synthesis (pre-auto-dispatch) — user can still
            # fire a manual dialogue.
            obj.auto_dialogue_status = "not_dispatched"
            obj.action_kind = "blocker"
            obj.user_action_required = True
            continue

        # Map run.status to dialogue status. The orchestrator writes
        # status='running' initially and the dialogue background
        # thread later finalizes via _execute_and_finalize (which
        # internally writes 'completed' / 'failed' via the
        # negotiation_recorder ladder).
        run_status = (run.status or "").lower()
        if run_status in ("running", "starting"):
            obj.auto_dialogue_status = "running"
            obj.action_kind = "blocker"  # Provisional until done.
            obj.user_action_required = True
            continue
        if run_status in ("failed", "superseded", "blocked"):
            obj.auto_dialogue_status = run_status
            obj.action_kind = "blocker"
            obj.user_action_required = True
            continue

        # Completed — look up the FM verdict agent_report for this
        # dialogue and extract the resolution.
        fm_dialogue_decision_id = f"fm-obj-dialogue-{run.id}"
        fm_dialogue_row = db.execute(
            select(AgentReport)
            .where(
                AgentReport.user_id == user_id,
                AgentReport.decision_id == fm_dialogue_decision_id,
                AgentReport.agent_role == "fund_manager_dialogue_verdict",
            )
            .order_by(desc(AgentReport.created_at))
            .limit(1)
        ).scalar_one_or_none()
        if fm_dialogue_row is None or not fm_dialogue_row.response_text:
            obj.auto_dialogue_status = "completed_no_verdict"
            obj.action_kind = "blocker"
            obj.user_action_required = True
            continue

        # Parse resolution out of the FM verdict's JSON-shaped output.
        resolution: str | None = None
        text = fm_dialogue_row.response_text
        try:
            obj_payload = _parse_fm_response(text)
            if isinstance(obj_payload, dict):
                resolution = obj_payload.get("resolution")
        except Exception:  # noqa: BLE001
            resolution = None
        # Fallback: regex sniff if JSON parse missed it.
        if not isinstance(resolution, str):
            m = _re.search(
                r'"resolution"\s*:\s*"([A-Z_]+)"', text
            )
            if m:
                resolution = m.group(1)

        obj.auto_dialogue_status = "completed"
        obj.auto_dialogue_resolution = resolution

        if resolution == "FM_ACCEPTS_ANALYST":
            obj.action_kind = None
            obj.user_action_required = False
        elif resolution == "FM_REVISES_OBJECTION":
            obj.action_kind = "decision"
            obj.user_action_required = True
        else:
            # FM_MAINTAINS_OBJECTION, ESCALATE_TO_USER, or unrecognized.
            obj.action_kind = "blocker"
            obj.user_action_required = True

        # Defense in depth: if no analyst could be parsed yet a
        # dialogue resolution is somehow present, leave it as-is
        # (the analyst owner check is informational, not a gate).
        _ = analyst_refs  # silence unused — kept for future audit logs


def _build_carried_over_response(
    db: Session, *, user_id: str, current_pv: PlanVersion
) -> FMObjectionsResponse:
    """Build an FMObjectionsResponse for a draft that has no FM verdict.

    Walks back through earlier plan_versions in descending imported_at
    order until it finds one with a real ``fund_manager`` agent_report,
    then surfaces THAT verdict's objections tagged ``carried_over=True``.
    The walk-back is necessary because a chain of plan_amendment_chat
    drafts can stack with none of them carrying an FM report — a
    single-hop lookup would still erase the prior open objections from
    the user's view. Stops at the first row with a real FM report or
    when history is exhausted; returns the bare ``not_evaluated`` shape
    when nothing is found.

    The carried-over objections aren't a fresh judgment — they're
    surfaced so the user knows what was still potentially open before
    the amendment landed. The /plan UI banners this state explicitly.
    """
    candidate_rows = db.execute(
        select(PlanVersion).where(
            PlanVersion.user_id == user_id,
            PlanVersion.id != current_pv.id,
            PlanVersion.imported_at < current_pv.imported_at,
            PlanVersion.decision_run_id.is_not(None),
        ).order_by(desc(PlanVersion.imported_at))
    ).scalars().all()

    for prior_pv in candidate_rows:
        if prior_pv.decision_run_id is None:
            continue
        prior_decision_id_str = f"plan-synth-{prior_pv.decision_run_id}"
        prior_fm_row = db.execute(
            select(AgentReport).where(
                AgentReport.user_id == user_id,
                AgentReport.decision_id == prior_decision_id_str,
                AgentReport.agent_role == "fund_manager",
            ).order_by(desc(AgentReport.created_at)).limit(1)
        ).scalar_one_or_none()
        if prior_fm_row is None or not prior_fm_row.response_text:
            # No FM on this candidate (e.g. another plan_amendment_chat
            # draft). Keep walking back.
            continue
        # Found a real FM verdict — carry forward its objections.
        parsed = _parse_fm_response(prior_fm_row.response_text)
        reasons = parsed.get("reasons") or []
        cited = [
            c for c in (parsed.get("cited_sources") or []) if isinstance(c, str)
        ]
        carried: list[FMObjection] = []
        raw_for_cache: list[dict] = []
        for r in reasons:
            if not isinstance(r, str) or not r.strip():
                continue
            topic, detail = _split_reason(r)
            sev = _classify_severity(topic, detail)
            carried.append(
                FMObjection(
                    severity=sev,
                    topic=topic,
                    detail=detail,
                    carried_over=True,
                    carried_over_from_plan_version_id=prior_pv.id,
                )
            )
            raw_for_cache.append({"severity": sev, "topic": topic, "detail": detail})

        # Pre-computed plain-English translations for these objections
        # exist in fm_objection_translations keyed by the SOURCE draft's
        # plan_version_id. Look them up there so the UI's instant
        # toggle works on carried-over rows too. Skip silently on cache
        # failure — the per-row "Explain in plain English" lazy
        # fallback still works on click.
        if carried:
            try:
                from argosy.services.fm_objection_translation_cache import (
                    get_or_compute_translations,
                )

                translations = get_or_compute_translations(
                    db,
                    user_id=user_id,
                    plan_version_id=prior_pv.id,
                    objections=raw_for_cache,
                    cited_sources=cited,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "fm_objection_translation_cache failed on "
                    "carry-forward source_plan_version_id=%s err=%s",
                    prior_pv.id,
                    exc,
                )
                translations = {}
            for idx, obj in enumerate(carried):
                dto = translations.get(idx)
                if dto is None:
                    continue
                obj.translation = FMObjectionTranslationDTO(
                    headline=dto.headline,
                    plain_english=dto.plain_english,
                    recommended_actions=list(dto.recommended_actions or []),
                )

        return FMObjectionsResponse(
            # approved stays None — the current draft has not been
            # evaluated. The prior bool would mislead if surfaced as
            # the current verdict; raw_response_excerpt carries the
            # prior FM's prose for context if the UI wants it.
            approved=None,
            verdict_status="carried_over",
            objections=carried,
            cited_sources=cited,
            decision_run_id=current_pv.decision_run_id,
            raw_response_excerpt=prior_fm_row.response_text[:500],
        )

    # Walked the entire history; no FM verdict anywhere.
    return FMObjectionsResponse(
        approved=None,
        verdict_status="not_evaluated",
        objections=[],
        cited_sources=[],
        decision_run_id=current_pv.decision_run_id,
        raw_response_excerpt="",
    )


# ---------------------------------------------------------------------------
# Wave 8 v2 polish — plain-English narrative for /plan recap "Full plan" card
# ---------------------------------------------------------------------------


class PlanNarrativeResponse(BaseModel):
    plan_version_id: int
    narrative_md_en: str
    narrative_md_he: str
    confidence: str


@router.get(
    "/current/narrative", response_model=PlanNarrativeResponse | None
)
async def get_current_plan_narrative(
    user_id: str = Query("ariel"),
    force_refresh: bool = Query(False),
    db: Session = Depends(get_db),
) -> PlanNarrativeResponse | None:
    """Wave 8 v2 polish — bilingual plain-English narrative for the
    recap's "Full plan" surface. Wraps the PlanNarrativeAgent +
    process-local cache; returns 200 + null when no current plan
    exists (matches the other /current/* routes' absence-of-data
    convention).

    Set ``force_refresh=true`` to bypass the cache (admin-style
    knob — the UI never sets this)."""
    from argosy.services.plan_narrative import get_plan_narrative

    result = await get_plan_narrative(db, user_id, force_refresh=force_refresh)
    if result is None:
        return None
    return PlanNarrativeResponse(
        plan_version_id=result.plan_version_id,
        narrative_md_en=result.narrative_md_en,
        narrative_md_he=result.narrative_md_he,
        confidence=result.confidence,
    )


# ---------------------------------------------------------------------------
# Wave 8 Piece C — cashflow assumption defaults for /plan recap sliders
# ---------------------------------------------------------------------------


class AssumptionFieldDTO(BaseModel):
    value: float
    source: str  # "sigma_calibrator" | "goals_yaml" | "default"
    rationale_md: str


class DefaultAssumptionsResponseDTO(BaseModel):
    mu_nominal_annual: AssumptionFieldDTO
    sigma_annual: AssumptionFieldDTO
    tax_rate: AssumptionFieldDTO
    inflation_annual: AssumptionFieldDTO
    retirement_age: AssumptionFieldDTO
    lifestyle_drift_annual: AssumptionFieldDTO


@router.get(
    "/current/cashflow-default-assumptions",
    response_model=DefaultAssumptionsResponseDTO,
)
def get_current_cashflow_default_assumptions(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> DefaultAssumptionsResponseDTO:
    """Return pre-populated cashflow-projection defaults for the recap's
    assumption sliders (Wave 8 Piece C). Every field carries source +
    rationale_md so the UI can show a `▸ why?` tooltip explaining
    where the default came from."""
    from argosy.services.cashflow_assumptions import (
        get_default_assumptions,
    )

    out = get_default_assumptions(session=db, user_id=user_id)

    def _to_dto(f) -> AssumptionFieldDTO:
        return AssumptionFieldDTO(
            value=f.value, source=f.source, rationale_md=f.rationale_md
        )

    return DefaultAssumptionsResponseDTO(
        mu_nominal_annual=_to_dto(out.mu_nominal_annual),
        sigma_annual=_to_dto(out.sigma_annual),
        tax_rate=_to_dto(out.tax_rate),
        inflation_annual=_to_dto(out.inflation_annual),
        retirement_age=_to_dto(out.retirement_age),
        lifestyle_drift_annual=_to_dto(out.lifestyle_drift_annual),
    )


# ---------------------------------------------------------------------------
# Wave 8 Piece B1 — allocation glidepath for /plan recap
# ---------------------------------------------------------------------------


class GlidepathPointDTO(BaseModel):
    months_out: int
    date: str  # ISO YYYY-MM-DD (first-of-month)
    composition_pct_by_class: dict[str, float]


class CollapsedWaypointDTO(BaseModel):
    asset_class: str
    waypoint_date: str
    target_pct: float
    source_horizon: str
    reason: str


class ExcludedTargetDTO(BaseModel):
    target_label: str
    target_unit: str
    target_value: float
    target_date: str
    reason: str


class AssetClassAnchorStatusDTO(BaseModel):
    asset_class: str
    matched: bool
    today_value: float
    alias_source: str | None


class AllocationGlidepathResponse(BaseModel):
    points: list[GlidepathPointDTO]
    collapsed_waypoints: list[CollapsedWaypointDTO]
    excluded_targets: list[ExcludedTargetDTO]
    asset_classes: list[str]
    anchor_status: list[AssetClassAnchorStatusDTO] = []
    today: str | None
    end_date: str | None


@router.get(
    "/current/allocation-glidepath",
    response_model=AllocationGlidepathResponse | None,
)
def get_current_allocation_glidepath(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> AllocationGlidepathResponse | None:
    """Return the projected allocation glidepath for the user's current
    plan (Wave 8 Piece B1).

    Returns 200 + null when no current plan exists (matches the
    /current/structured + /current/headline absence-of-data
    convention)."""
    from argosy.services.allocation_glidepath import (
        compute_allocation_glidepath,
    )

    today = datetime.now(timezone.utc).date()
    out = compute_allocation_glidepath(db, user_id, today)
    if out is None:
        return None
    return AllocationGlidepathResponse(
        points=[
            GlidepathPointDTO(
                months_out=p.months_out,
                date=p.point_date.isoformat(),
                composition_pct_by_class=p.composition_pct_by_class,
            )
            for p in out.points
        ],
        collapsed_waypoints=[
            CollapsedWaypointDTO(
                asset_class=w.asset_class,
                waypoint_date=w.waypoint_date.isoformat(),
                target_pct=w.target_pct,
                source_horizon=w.source_horizon,
                reason=w.reason,
            )
            for w in out.collapsed_waypoints
        ],
        excluded_targets=[
            ExcludedTargetDTO(
                target_label=t.target_label,
                target_unit=t.target_unit,
                target_value=t.target_value,
                target_date=t.target_date.isoformat(),
                reason=t.reason,
            )
            for t in out.excluded_targets
        ],
        asset_classes=out.asset_classes,
        anchor_status=[
            AssetClassAnchorStatusDTO(
                asset_class=a.asset_class,
                matched=a.matched,
                today_value=a.today_value,
                alias_source=a.alias_source,
            )
            for a in out.anchor_status
        ],
        today=out.today.isoformat() if out.today else None,
        end_date=out.end_date.isoformat() if out.end_date else None,
    )


# ---------------------------------------------------------------------------
# Wave 8 Piece G — plain-English headline + recap summary for /plan
# ---------------------------------------------------------------------------


class HeadlineLinesDTO(BaseModel):
    retirement_readiness: str
    next_big_move: str | None
    then: str | None


class AcceptedDeltaSummaryDTO(BaseModel):
    horizon: str
    item_kind: str
    summary: str


class PortfolioValueAnchorDTO(BaseModel):
    total_usd_value_k: float | None
    snapshot_date: str | None


class InsuranceGapsSummaryDTO(BaseModel):
    one_line: str
    has_data: bool


class AuditLineDTO(BaseModel):
    plan_version_id: int
    decision_run_id: int | None
    approved_at: str | None
    synthesis_trail_link: str | None


class ReadinessVerdictSummaryDTO(BaseModel):
    policy: str
    retire_ready_age: float | None
    rationale: str


class HeadlineDerivationDTO(BaseModel):
    mu_nominal_annual: float
    sigma_annual: float
    tax_rate: float
    retirement_target_age: float
    # list of [mu, retire_age | null] pairs.
    sensitivity_by_mu: list[list[float | None]]
    sourced_from: str
    readiness_by_policy: list[ReadinessVerdictSummaryDTO] = []


class RecapSummaryDTO(BaseModel):
    headline: HeadlineLinesDTO
    derivation: HeadlineDerivationDTO | None
    accepted_deltas: list[AcceptedDeltaSummaryDTO]
    portfolio_value: PortfolioValueAnchorDTO
    insurance_gaps: InsuranceGapsSummaryDTO
    audit: AuditLineDTO


@router.get("/current/headline", response_model=RecapSummaryDTO | None)
def get_current_headline(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> RecapSummaryDTO | None:
    """Return the recap-summary block for the /plan recap view (Wave 8 Piece G).

    Returns 200 + null when no current plan exists (matches the
    /current/structured + /in-flight-synthesis "absence of data"
    contract used elsewhere in /api/plan/*)."""
    from argosy.services.plan_headline import compute_recap_summary

    summary = compute_recap_summary(db, user_id)
    if summary is None:
        return None
    derivation_dto: HeadlineDerivationDTO | None = None
    if summary.derivation is not None:
        derivation_dto = HeadlineDerivationDTO(
            mu_nominal_annual=summary.derivation.mu_nominal_annual,
            sigma_annual=summary.derivation.sigma_annual,
            tax_rate=summary.derivation.tax_rate,
            retirement_target_age=summary.derivation.retirement_target_age,
            sensitivity_by_mu=[
                [mu, age]
                for (mu, age) in summary.derivation.sensitivity_by_mu
            ],
            sourced_from=summary.derivation.sourced_from,
            readiness_by_policy=[
                ReadinessVerdictSummaryDTO(
                    policy=v.policy,
                    retire_ready_age=v.retire_ready_age,
                    rationale=v.rationale,
                )
                for v in summary.derivation.readiness_by_policy
            ],
        )

    return RecapSummaryDTO(
        headline=HeadlineLinesDTO(
            retirement_readiness=summary.headline.retirement_readiness,
            next_big_move=summary.headline.next_big_move,
            then=summary.headline.then,
        ),
        derivation=derivation_dto,
        accepted_deltas=[
            AcceptedDeltaSummaryDTO(
                horizon=d.horizon, item_kind=d.item_kind, summary=d.summary
            )
            for d in summary.accepted_deltas
        ],
        portfolio_value=PortfolioValueAnchorDTO(
            total_usd_value_k=summary.portfolio_value.total_usd_value_k,
            snapshot_date=summary.portfolio_value.snapshot_date,
        ),
        insurance_gaps=InsuranceGapsSummaryDTO(
            one_line=summary.insurance_gaps.one_line,
            has_data=summary.insurance_gaps.has_data,
        ),
        audit=AuditLineDTO(
            plan_version_id=summary.audit.plan_version_id,
            decision_run_id=summary.audit.decision_run_id,
            approved_at=summary.audit.approved_at,
            synthesis_trail_link=summary.audit.synthesis_trail_link,
        ),
    )


@router.get("/current/structured", response_model=DraftResponse | None)
def get_current_structured(
    user_id: str, db: Session = Depends(get_db)
) -> DraftResponse | None:
    """Return the user's currently-accepted plan as the same structured
    DraftResponse shape used by ``GET /api/plan/draft``.

    Wave 3 / Task 3.5. The legacy ``GET /api/plan/current`` endpoint
    returns a different DTO (raw markdown + latest critique) and remains
    in use by the home page + /plan page.  This sibling route mirrors
    the draft endpoint so the Argonaut page can read structured horizons
    (notably ``horizon_short.speculative_candidates``).

    Returns 200 + null when no current plan exists -- matches the
    "absence of data" contract used by /in-flight-synthesis. The
    Argonaut page expects to render with no plan gracefully; a 404
    here previously surfaced as a console error on every load even
    though it was the expected state.
    """
    from argosy.state.queries import get_current_plan

    pv = get_current_plan(db, user_id)
    if pv is None:
        return None
    return DraftResponse(
        plan_version_id=pv.id,
        version_label=pv.version_label or None,
        drafted_at=_iso_utc(pv.accepted_at or pv.imported_at) or "",
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
    override_gate: bool = Query(
        False,
        description=(
            "Phase 6 override: when true AND plan_gate_enforce is on, "
            "skip the gate check and proceed with promotion. The "
            "override is audit-logged via plan.draft.accepted.override."
        ),
    ),
    override_fm_rejection: bool = Query(
        False,
        description=(
            "v4 #20 override: when true, promote a draft whose synthesis "
            "run was rejected by the fund_manager. The user remains the "
            "final gate (the FM is advisory), so an explicit override is "
            "honoured — and audit-logged via "
            "plan.draft.accepted.fm_override."
        ),
    ),
    db: Session = Depends(get_db),
) -> AcceptResponse:
    from argosy.state.queries import get_current_plan

    pv = db.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise HTTPException(status_code=404, detail="draft not found for user")

    # v4 #20 — FM-rejection blocks auto-promotion. The fund_manager is an
    # advisory integrity check, not the final gate (the user is). But a
    # draft the FM REJECTED must never silently become 'current': drun 73
    # was promoted with an inverted-math claim the FM had caught. So a
    # plain /accept on an FM-rejected draft returns 422 with the FM's
    # reasons; promotion requires an explicit ?override_fm_rejection=true.
    # The verdict lives on the backing decision_run (set at synthesis end
    # in orchestrator.run_synthesis: decision_run.fund_manager_decision).
    if pv.decision_run_id is not None and not override_fm_rejection:
        run = db.get(DecisionRun, pv.decision_run_id)
        if run is not None and run.fund_manager_decision == "rejected":
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "fund_manager_rejected",
                    "decision_run_id": pv.decision_run_id,
                    "hint": (
                        "The fund_manager rejected this draft's synthesis. "
                        "Review the objections at "
                        "/api/plan/draft/objections, re-run synthesis after "
                        "addressing them, or pass "
                        "?override_fm_rejection=true to promote anyway "
                        "(audit-logged)."
                    ),
                },
            )

    # Phase 6 — run the plan_output_gate against the draft we're about
    # to promote. When `plan_gate_enforce` is True, a failing gate
    # raises 422; when False, it logs a warning and surfaces the
    # violation summary on the AcceptResponse. The `?override_gate=true`
    # query param bypasses the check (audit-logged).
    gate_verdict = _run_plan_output_gate(pv, db)
    gate_warning: dict | None = None
    if gate_verdict is not None and not gate_verdict.passes:
        from argosy.config import get_settings
        from argosy.quality.gate_types import GateCheck

        # The trust contract's CORE checks always BLOCK: history_leak,
        # jargon_leak, headline_numeric_source (no fabrication), and
        # section_coverage (the plan must cover the canonical sections). This is
        # codex's enforce set {1,2,3,6}.
        #
        # The per-section EVIDENCE-quality checks (evidence_per_section,
        # distillate_binding) are WARN during the evidence-hardening transition:
        # the synthesizer emits structured sections (now persisted) but its
        # per-fact citation completeness isn't yet contract-tight, so blocking on
        # them would make every fresh plan un-promotable. Surfaced as
        # `warned_only`; tracked task = harden synth evidence, then re-enforce.
        # (codex 2026-06-10: WARN 4/5 during transition.) For LEGACY rows with no
        # persisted sections, section_coverage is also demoted (it can't run).
        _EVIDENCE_WARN = {
            GateCheck.EVIDENCE_PER_SECTION,
            GateCheck.DISTILLATE_SECTION_BINDING,
        }
        sections_present = bool(getattr(pv, "sections_json", None))
        blocking_checks = set(GateCheck) - _EVIDENCE_WARN
        if not sections_present:
            blocking_checks.discard(GateCheck.SECTION_COVERAGE)
        blocking = {
            check: gate_verdict.for_check(check)
            for check in blocking_checks
            if gate_verdict.violations[check]
        }
        warned = {
            check: gate_verdict.for_check(check)
            for check in gate_verdict.violations
            if gate_verdict.violations[check] and check not in blocking
        }

        if override_gate:
            _publish(
                "plan.draft.accepted.override",
                {
                    "user_id": user_id,
                    "draft_id": draft_id,
                    "gate_summary": gate_verdict.summary(),
                },
            )
        elif blocking and get_settings().plan_gate_enforce:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "plan_output_gate_failed",
                    "summary": gate_verdict.summary(),
                    "violations_by_check": {
                        check.value: [
                            {"detail": v.detail, "locator": v.locator}
                            for v in viols
                        ]
                        for check, viols in blocking.items()
                    },
                    "warned_only": {
                        check.value: len(viols) for check, viols in warned.items()
                    },
                    "hint": (
                        "Re-run synthesis after addressing the violations, "
                        "or pass ?override_gate=true to force-accept "
                        "(audit-logged)."
                    ),
                },
            )
        else:
            # Warning mode (or only warn-demoted checks tripped): proceed
            # with accept but include the violation summary on the response
            # so the UI can surface a banner.
            gate_warning = {
                "summary": gate_verdict.summary(),
                "total_violations": gate_verdict.total_violations,
                "violations_by_check": {
                    check.value: len(gate_verdict.for_check(check))
                    for check in gate_verdict.violations
                    if gate_verdict.violations[check]
                },
            }

    # v4 #20 — audit the explicit override of an FM rejection so the
    # decision trail records that the user knowingly promoted a draft the
    # fund_manager had rejected.
    if override_fm_rejection and pv.decision_run_id is not None:
        _run = db.get(DecisionRun, pv.decision_run_id)
        if _run is not None and _run.fund_manager_decision == "rejected":
            _publish(
                "plan.draft.accepted.fm_override",
                {
                    "user_id": user_id,
                    "draft_id": draft_id,
                    "decision_run_id": pv.decision_run_id,
                },
            )

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

    # Wave 8 v2.4.2 — auto-regenerate the bilingual plan narrative
    # against the freshly-accepted plan. Drops the stale cache entry
    # (keyed on the prior plan_version_id) and kicks a background
    # task that runs the PlanNarrativeAgent so the recap reads warm
    # the moment the user navigates back. Fire-and-forget; failure
    # degrades to "next /plan visit will regen on-demand" (the
    # original behavior).
    _auto_regen_narrative(user_id, pv.id)

    _publish("plan.draft.accepted", {"user_id": user_id, "draft_id": draft_id})
    _publish("plan.current.changed", {"user_id": user_id, "current_id": pv.id})

    return AcceptResponse(
        status="accepted",
        new_current_id=pv.id,
        gate_warning=gate_warning,
    )


def _run_plan_output_gate(pv: "PlanVersion", db: "Session | None" = None):
    """Phase 6 helper — run plan_output_gate on a draft PlanVersion.

    Returns a GateVerdict or None when no horizon MD exists yet
    (in which case the gate is silently skipped — pre-Phase-1 rows
    have no audit columns to compare against, and a row in this
    state should not have been a candidate for /accept in the first
    place). All exceptions are caught and logged; the gate is
    defense-in-depth and must never break the accept path itself —
    only its verdict matters.

    #24 — when `db` is supplied, the headline_numeric_source check runs:
    we rebuild the deterministic resolver manifest from `pv.decision_run_id`
    and validate every headline number in the user-facing markdown against
    it. If the manifest CANNOT be rebuilt (no decision_run_id, or the
    resolver raised) we FAIL CLOSED *only when `plan_gate_enforce` is on* —
    a synthetic HEADLINE_NUMERIC_SOURCE violation is recorded so the accept
    is blocked rather than silently passing an unvalidated draft. In warn
    mode an un-runnable resolver just skips the check (no false alarm).
    """
    try:
        from argosy.quality import gate_plan_output
        from argosy.quality.gate_types import GateCheck, GateViolation
        # Reconstruct PlanSynthesisOutput from the persisted JSON
        # columns so the structured checks (section_coverage,
        # evidence_per_section, distillate_section_binding) have
        # something to read.
        synth = None
        try:
            from argosy.agents.plan_synthesizer_types import (
                HorizonSection,
                PlanSynthesisOutput,
                Section,
                SynthesisInputs,
            )
            import json as _json
            if pv.horizon_long_json and pv.horizon_medium_json and pv.horizon_short_json:
                # Reconstruct the structured sections the synthesizer produced
                # (persisted in sections_json from migration 0065). Without this
                # the rebuilt object has zero sections and section_coverage /
                # evidence_per_section fail for EVERY plan. NULL on legacy rows
                # → empty list → the evidence checks WARN, never block.
                _sections: list[Section] = []
                if pv.sections_json:
                    _sections = [
                        Section.model_validate(d)
                        for d in _json.loads(pv.sections_json)
                    ]
                synth = PlanSynthesisOutput(
                    long=HorizonSection.model_validate_json(pv.horizon_long_json),
                    medium=HorizonSection.model_validate_json(pv.horizon_medium_json),
                    short=HorizonSection.model_validate_json(pv.horizon_short_json),
                    inputs=SynthesisInputs.model_validate_json(
                        pv.synthesis_inputs_json
                        or '{"baseline_id":null,"prior_current_id":null,'
                           '"snapshot_id":null,"fill_ids":[],'
                           '"agent_report_ids":[],"debate_outcome_ids":[],'
                           '"decision_run_id":null}'
                    ),
                    sections=_sections,
                )
        except Exception:
            synth = None
        horizon_text = {
            "long": pv.horizon_long_md or "",
            "medium": pv.horizon_medium_md or "",
            "short": pv.horizon_short_md or "",
        }
        if not any(horizon_text.values()) and synth is None:
            return None

        # #24 — rebuild the resolver manifest for the numeric-source check.
        # `resolved is None` means "couldn't run"; the fail-closed branch
        # below decides whether that blocks (enforce) or is tolerated (warn).
        resolved = None
        resolver_error: str | None = None
        if db is not None:
            if pv.decision_run_id is None:
                resolver_error = "draft has no decision_run_id"
            else:
                try:
                    from argosy.services.plan_numeric_resolver import (
                        resolve_plan_numbers,
                    )
                    # include_canonical_ages so the gate sanctions the canonical
                    # earliest-safe age (46) the synth body now states — otherwise
                    # the headline age check flags it as unsourced (the manifest
                    # would carry only fi_age ~49).
                    resolved = resolve_plan_numbers(
                        db,
                        user_id=pv.user_id,
                        decision_run_id=pv.decision_run_id,
                        include_canonical_ages=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    resolver_error = f"resolver raised: {exc}"
                    import logging
                    logging.getLogger(__name__).warning(
                        "headline_numeric_resolver_failed pv=%s err=%s",
                        pv.id, exc,
                    )

        verdict = gate_plan_output(
            horizon_text=horizon_text,
            synth=synth,
            distillate=None,  # Phase 4 will wire the typed distillate
            resolved=resolved,
        )

        # S18 — instrument-domicile check on the STRUCTURED doc. The frozen
        # US-domiciled-ETF ship slipped through because nothing validated the
        # doc's tickers against the estate-tax knowledge. RED = a non-sanctioned
        # US-situs primary → block (auto-blocking: INSTRUMENT_DOMICILE ∉ the
        # WARN set). Unknown-domicile (YELLOW) is NOT added so legacy plans with
        # unstamped instruments aren't blocked retroactively.
        try:
            from argosy.services.target_allocation_doc import (
                load_plan_target_allocation,
                validate_instrument_domicile,
            )
            _doc = load_plan_target_allocation(pv)
            if _doc is not None:
                for _viol in validate_instrument_domicile(_doc):
                    if _viol.severity == "RED":
                        verdict.add(
                            GateViolation(
                                check=GateCheck.INSTRUMENT_DOMICILE,
                                detail=_viol.reason,
                                locator=f"class={_viol.class_label} symbol={_viol.symbol}",
                            )
                        )
        except Exception:  # noqa: BLE001 — defense-in-depth, never break accept
            import logging
            logging.getLogger(__name__).warning(
                "instrument_domicile_check_failed pv=%s", getattr(pv, "id", "?"),
            )

        # S18 — technical-citation integrity. A symbol-level reading cited in
        # the prose (e.g. "RSI 73.4") must match the run's TechnicalAnalyst
        # payload. Auto-blocking (∉ the WARN set). Guards the stale
        # carry-forward the FM rejected on run 95. Best-effort: a missing
        # technical report → empty payload → check simply does not run (we do
        # NOT fail closed here — unlike the headline resolver, a plan with no
        # technical citations is legitimately un-checkable, not unvalidated).
        try:
            if db is not None and pv.decision_run_id is not None:
                from argosy.quality.technical_citation_gate import (
                    check_technical_citation_integrity,
                    load_run_technical_indicators,
                )
                _indicators = load_run_technical_indicators(
                    db, pv.decision_run_id
                )
                if _indicators:
                    verdict.extend(
                        check_technical_citation_integrity(
                            horizon_text, _indicators
                        )
                    )
        except Exception:  # noqa: BLE001 — defense-in-depth, never break accept
            import logging
            logging.getLogger(__name__).warning(
                "technical_citation_check_failed pv=%s", getattr(pv, "id", "?"),
            )

        # Fail closed: if the numeric manifest could not be rebuilt and the
        # gate is in ENFORCE mode, record a violation so the draft cannot
        # promote unvalidated. In warn mode we don't manufacture a false
        # alarm — the check simply did not run.
        if resolved is None and db is not None:
            from argosy.config import get_settings
            if get_settings().plan_gate_enforce:
                verdict.add(
                    GateViolation(
                        check=GateCheck.HEADLINE_NUMERIC_SOURCE,
                        detail=(
                            "numeric-source gate could not run "
                            f"({resolver_error or 'no resolver manifest'}); "
                            "failing closed in enforce mode"
                        ),
                        locator=f"plan_version_id={pv.id}",
                    )
                )
        return verdict
    except Exception:  # pragma: no cover - defense-in-depth
        import logging
        logging.getLogger(__name__).exception(
            "plan_output_gate_check_crashed",
        )
        return None


def _auto_regen_narrative(user_id: str, new_current_plan_version_id: int) -> None:
    """Fire-and-forget background task that warms the
    PlanNarrativeAgent cache for the freshly-accepted plan.

    Wave 8 v2.4.2 — without this hook the user had to navigate
    to /plan and wait ~6 min for the agent to regenerate. Now
    it warms in the background so the recap reads fresh on the
    next visit.

    Runs in a fresh sync session created here (the route's `db`
    session has already committed + will be closed before the task
    fires). Logs errors but never raises.
    """
    import asyncio
    import logging
    import threading

    log = logging.getLogger("plan_narrative_auto_regen")

    # Invalidate any stale cache entry up-front so a concurrent
    # /plan visit doesn't read a pre-accept narrative while the
    # background regen is still running.
    try:
        from argosy.services.plan_narrative import (
            invalidate_narrative_cache,
        )
        invalidate_narrative_cache(user_id, new_current_plan_version_id)
    except Exception:  # pragma: no cover - defensive
        pass

    def _worker() -> None:
        try:
            from argosy.services.plan_narrative import get_plan_narrative

            global _sync_session_factory
            if _sync_session_factory is None:
                # Cold-start path: re-initialise the sync session
                # factory via get_db so we can spawn a session.
                next(get_db())
            assert _sync_session_factory is not None
            session = _sync_session_factory()
            try:
                asyncio.run(
                    get_plan_narrative(
                        session, user_id, force_refresh=True
                    )
                )
                log.info(
                    "plan_narrative_auto_regen.completed user=%s pv=%s",
                    user_id,
                    new_current_plan_version_id,
                )
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001
            log.error(
                "plan_narrative_auto_regen.failed user=%s pv=%s err=%s",
                user_id,
                new_current_plan_version_id,
                exc,
            )

    # Spawn in a background thread so the /accept route returns
    # immediately. The agent call is ~5-15 min; we don't block the
    # user on it. Daemon=True so the thread doesn't prevent
    # process shutdown.
    threading.Thread(target=_worker, daemon=True).start()


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
