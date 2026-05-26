"""Decisions API (Phase 3).

POST /api/decisions/run — manual ad-hoc decision flow trigger.

Takes a ticker, a tier (or 'auto' from the resolver), and a list of
analyst_report_ids that the flow will pull from `agent_reports`. Returns
the resulting decision-run id + proposal id (or block reason).

This route exists so the user can trigger a one-shot flow from the CLI
or the dashboard without waiting for a cadence to fire it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import asc, desc, func, select

from argosy.agent_settings import load_agent_settings
from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.billing.decorators import requires_feature, requires_within_quota
from argosy.decisions.flow import (
    ApprovedProposal,
    BlockedProposal,
    DecisionFlow,
)
from argosy.decisions.tiers import Tier, TierContext, apply_override_mode, resolve_tier
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    DecisionPhase,
    DecisionRun,
    UserFile,
)

router = APIRouter(prefix="/decisions", tags=["decisions"])


class RunRequest(BaseModel):
    user_id: str = "ariel"
    ticker: str
    tier: str = "auto"  # 'auto' | 'T0' | 'T1' | 'T2' | 'T3'
    analyst_report_ids: list[int] = Field(default_factory=list)
    positions_summary: str = ""
    plan_critique: dict[str, Any] | None = None
    user_constraints: str = ""
    risk_caps: dict[str, Any] = Field(default_factory=dict)
    account_class: str = "main"
    # Tier-context fields used when tier='auto'. For Phase 3 the dashboard
    # passes these in directly so the resolver has something to work on
    # without requiring a full price-feed wire-up.
    proposed_value_usd: float = 0.0
    portfolio_value_usd: float = 1.0
    account_value_usd: float = 0.0
    is_plan_structural: bool = False
    crosses_concentration_cap: bool = False
    recent_red_flag: bool = False


class RunResponse(BaseModel):
    decision_run_id: int
    status: str  # 'approved' | 'blocked'
    proposal_id: int | None = None
    blocked_reason: str | None = None
    blocked_by: str | None = None
    tier: str


@router.post("/run", response_model=RunResponse)
@requires_feature("agent_fleet_full")
@requires_within_quota("monthly_decisions")
@requires_within_quota("monthly_claude_spend_usd")
async def run_decision_flow(body: RunRequest) -> RunResponse:
    settings = load_agent_settings(body.user_id)

    if body.tier == "auto":
        ctx = TierContext(
            proposed_value_usd=body.proposed_value_usd,
            portfolio_value_usd=body.portfolio_value_usd,
            account_class=body.account_class,  # type: ignore[arg-type]
            ticker=body.ticker,
            is_nvda=body.ticker.upper() == "NVDA",
            is_plan_structural=body.is_plan_structural,
            crosses_concentration_cap=body.crosses_concentration_cap,
            recent_red_flag=body.recent_red_flag,
            account_value_usd=body.account_value_usd,
        )
        auto_tier = resolve_tier(ctx, settings)
        tier = apply_override_mode(auto_tier, settings)
    else:
        try:
            tier = Tier.from_str(body.tier)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    analyst_reports = await _load_analyst_reports(body.user_id, body.analyst_report_ids)

    flow = DecisionFlow(user_id=body.user_id, settings=settings)
    outcome = await flow.run(
        ticker=body.ticker,
        tier=tier,
        analyst_reports=analyst_reports,
        positions_summary=body.positions_summary,
        plan_critique=body.plan_critique,
        user_constraints=body.user_constraints,
        risk_caps=body.risk_caps,
        account_class=body.account_class,  # type: ignore[arg-type]
    )

    if isinstance(outcome, ApprovedProposal):
        return RunResponse(
            decision_run_id=outcome.decision_run_id,
            status="approved",
            proposal_id=outcome.proposal.id,
            tier=tier.value,
        )
    assert isinstance(outcome, BlockedProposal)
    return RunResponse(
        decision_run_id=outcome.decision_run_id,
        status="blocked",
        blocked_reason=outcome.reason,
        blocked_by=outcome.blocked_by,
        tier=tier.value,
    )


async def _load_analyst_reports(user_id: str, ids: list[int]) -> list[AgentReport]:
    """Load `agent_reports` rows by id and reconstruct minimal AgentReport.

    The decision flow only reads `agent_role` + `output.model_dump()` from
    each, so we wrap the row's response_text into a generic dict-shaped
    output. This keeps the API simple without requiring the user to
    serialize full pydantic instances back over JSON.
    """
    if not ids:
        return []

    from pydantic import BaseModel

    class _Anonymous(BaseModel):
        agent_role: str = "analyst"
        cited_sources: list[str] = []
        confidence: ConfidenceBand = ConfidenceBand.MEDIUM
        # Carry the raw payload as a string so trader/researcher prompts can read it.
        report: str = ""

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AgentReportRow).where(
                    AgentReportRow.id.in_(ids), AgentReportRow.user_id == user_id
                )
            )
        ).scalars().all()
        out: list[AgentReport] = []
        for r in rows:
            try:
                payload = json.loads(r.response_text)
                if isinstance(payload, dict):
                    payload.setdefault("cited_sources", ["agent_reports"])
                    payload.setdefault("agent_role", r.agent_role)
                    obj = _Anonymous(
                        agent_role=r.agent_role,
                        cited_sources=payload.get("cited_sources", []),
                        confidence=ConfidenceBand(r.confidence)
                        if r.confidence
                        else ConfidenceBand.MEDIUM,
                        report=json.dumps(payload),
                    )
                else:
                    raise ValueError("not dict payload")
            except Exception:
                obj = _Anonymous(
                    agent_role=r.agent_role,
                    cited_sources=["agent_reports"],
                    report=r.response_text,
                )
            out.append(
                AgentReport(
                    agent_role=r.agent_role,
                    user_id=r.user_id,
                    model=r.model,
                    response_text=r.response_text,
                    tokens_in=r.tokens_in,
                    tokens_out=r.tokens_out,
                    cost_usd=float(r.cost_usd),
                    prompt_hash=r.prompt_hash,
                    confidence=ConfidenceBand(r.confidence) if r.confidence else None,
                    output=obj,
                    decision_id=r.decision_id,
                )
            )
        return out


# ----------------------------------------------------------------------
# Provenance Wave D — replay endpoint
# ----------------------------------------------------------------------


class ParticipantDTO(BaseModel):
    agent_role: str
    agent_report_id: int
    side: str | None
    perspective: str | None
    round: int | None
    confidence: str | None
    model: str | None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


class PhaseDTO(BaseModel):
    id: int
    seq: int
    kind: str
    started_at: datetime
    finished_at: datetime | None
    verdict_kind: str | None
    verdict: dict[str, Any] | None
    tldr_md: str | None
    sequence_mmd: str | None
    participants: list[ParticipantDTO]
    transcript_md_url: str


class DecisionRunDTO(BaseModel):
    id: int
    user_id: str
    decision_kind: str | None
    ticker: str | None
    tier: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str | None
    fund_manager_decision: str | None
    proposal_id: int | None
    notes_json: str | None


class UserFileLite(BaseModel):
    id: int
    original_name: str
    kind: str
    source: str
    size_bytes: int
    created_at: datetime


class ReplayResponse(BaseModel):
    decision_run: DecisionRunDTO
    phases: list[PhaseDTO]
    inputs: dict[str, Any]
    sequence_mmd_full: str
    # T0.5 — relative URL the UI hits to fetch the FM-rooted agent-tree
    # view for this run. Only meaningful for synthesis runs; the route
    # itself returns 404 on non-synthesis kinds, so the UI can probe
    # without a kind check.
    agent_tree_url: str


@router.get("/{decision_run_id}/replay", response_model=ReplayResponse)
async def get_decision_replay(
    decision_run_id: int,
    user_id: str = "ariel",
) -> ReplayResponse:
    """Full provenance replay for one decision run.

    Returns the decision_run row, every recorded phase (with parsed verdict
    DTOs, TL;DR markdown, participating agent_reports, and a Mermaid
    sequence diagram), and the user_files inputs that fed the run.
    """
    async with db_mod.get_session() as session:
        run = (
            await session.execute(
                select(DecisionRun).where(DecisionRun.id == decision_run_id)
            )
        ).scalar_one_or_none()
        if run is None or run.user_id != user_id:
            # Don't leak existence — same 404 the wrong-user gets.
            raise HTTPException(status_code=404, detail="decision run not found")

        phase_rows = (
            await session.execute(
                select(DecisionPhase)
                .where(DecisionPhase.decision_run_id == decision_run_id)
                .order_by(asc(DecisionPhase.seq))
            )
        ).scalars().all()

        # Collect all participating agent_report ids across phases for one
        # batched load.
        all_ids: set[int] = set()
        per_phase_participants: list[list[dict]] = []
        for p in phase_rows:
            try:
                parts = json.loads(p.participants_json or "[]")
            except (ValueError, TypeError):
                parts = []
            per_phase_participants.append(parts)
            for entry in parts:
                aid = entry.get("agent_report_id")
                if isinstance(aid, int) and aid > 0:
                    all_ids.add(aid)

        ar_by_id: dict[int, AgentReportRow] = {}
        if all_ids:
            ar_rows = (
                await session.execute(
                    select(AgentReportRow).where(
                        AgentReportRow.id.in_(all_ids)
                    )
                )
            ).scalars().all()
            ar_by_id = {r.id: r for r in ar_rows}

        # Files associated with this run.
        files = (
            await session.execute(
                select(UserFile).where(
                    UserFile.decision_run_id == decision_run_id,
                    UserFile.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    phases_dto: list[PhaseDTO] = []
    full_seq_parts: list[str] = []
    for phase, parts in zip(phase_rows, per_phase_participants):
        # Build participants DTOs by joining the cached agent_reports rows.
        pps: list[ParticipantDTO] = []
        for entry in parts:
            aid = entry.get("agent_report_id") or 0
            ar = ar_by_id.get(aid) if isinstance(aid, int) else None
            pps.append(ParticipantDTO(
                agent_role=entry.get("agent_role", "unknown"),
                agent_report_id=aid if isinstance(aid, int) else 0,
                side=entry.get("side"),
                perspective=entry.get("perspective"),
                round=entry.get("round"),
                confidence=(ar.confidence if ar else entry.get("confidence")),
                model=(ar.model if ar else entry.get("model")),
                tokens_in=(ar.tokens_in if ar else None),
                tokens_out=(ar.tokens_out if ar else None),
                cost_usd=(float(ar.cost_usd) if ar else None),
            ))

        # Read the on-disk sequence.mmd if present (re-rendering on every
        # request would be redundant; the writer's output is canonical).
        sequence_mmd = None
        if phase.bundle_dir:
            mmd_path = Path(phase.bundle_dir) / "sequence.mmd"
            if mmd_path.exists():
                try:
                    sequence_mmd = mmd_path.read_text(encoding="utf-8")
                    full_seq_parts.append(
                        f"%% phase {phase.seq}: {phase.kind}\n{sequence_mmd}"
                    )
                except OSError:
                    sequence_mmd = None

        verdict_obj: dict[str, Any] | None = None
        if phase.verdict_json:
            try:
                verdict_obj = json.loads(phase.verdict_json)
            except (ValueError, TypeError):
                verdict_obj = None

        phases_dto.append(PhaseDTO(
            id=phase.id,
            seq=phase.seq,
            kind=phase.kind,
            started_at=phase.started_at,
            finished_at=phase.finished_at,
            verdict_kind=phase.verdict_kind,
            verdict=verdict_obj,
            tldr_md=phase.tldr_md,
            sequence_mmd=sequence_mmd,
            participants=pps,
            transcript_md_url=(
                f"/api/decisions/{decision_run_id}/phases/{phase.id}/transcript"
            ),
        ))

    return ReplayResponse(
        decision_run=DecisionRunDTO(
            id=run.id,
            user_id=run.user_id,
            decision_kind=run.decision_kind,
            ticker=run.ticker,
            tier=run.tier,
            started_at=run.started_at,
            finished_at=run.finished_at,
            status=run.status,
            fund_manager_decision=run.fund_manager_decision,
            proposal_id=run.proposal_id,
            notes_json=run.notes_json,
        ),
        phases=phases_dto,
        inputs={
            "user_files": [
                UserFileLite(
                    id=f.id,
                    original_name=f.original_name,
                    kind=f.kind,
                    source=f.source,
                    size_bytes=f.size_bytes,
                    created_at=f.created_at,
                ).model_dump()
                for f in files
            ],
        },
        sequence_mmd_full="\n\n".join(full_seq_parts),
        agent_tree_url=f"/api/decisions/{decision_run_id}/agent-tree",
    )


@router.get("/{decision_run_id}/phases/{phase_id}/transcript")
async def get_phase_transcript(
    decision_run_id: int,
    phase_id: int,
    user_id: str = "ariel",
) -> FileResponse:
    """Stream the on-disk transcript.md for one phase."""
    async with db_mod.get_session() as session:
        phase = (
            await session.execute(
                select(DecisionPhase).where(DecisionPhase.id == phase_id)
            )
        ).scalar_one_or_none()

    if (
        phase is None
        or phase.decision_run_id != decision_run_id
        or phase.user_id != user_id
    ):
        raise HTTPException(status_code=404, detail="phase not found")

    if phase.bundle_dir is None:
        raise HTTPException(
            status_code=404, detail="phase has no on-disk transcript bundle"
        )
    p = Path(phase.bundle_dir) / "transcript.md"
    if not p.exists():
        raise HTTPException(
            status_code=410, detail="transcript.md missing on disk"
        )
    return FileResponse(
        path=str(p), media_type="text/markdown",
        filename=f"transcript-run{decision_run_id}-phase{phase_id}.md",
    )


# ----------------------------------------------------------------------
# GET /api/decisions/recent — grouped cascade payload (spec §3.6)
# ----------------------------------------------------------------------


class DecisionGroupDTO(BaseModel):
    decision_id: str
    decision_kind: str | None
    tier: str | None
    ticker: str | None
    started_at: str          # ISO 8601 — min(created_at) in the group
    finished_at: str | None  # ISO 8601 — max(created_at) in the group
    status: str
    total_cost_usd: float
    agent_count: int
    agent_runs: list[Any]    # list of AgentActivityRow-shaped dicts


@router.get("/recent", response_model=list[DecisionGroupDTO])
async def get_decisions_recent(
    user_id: str = Query("ariel"),
    limit: int = Query(20, ge=1, le=200),
) -> list[DecisionGroupDTO]:
    """Return recent decision groups for `user_id`, each containing all
    agent_reports that share a decision_id.

    Groups are ordered by their max(created_at) DESC (most-recent first).
    Rows with NULL decision_id are omitted — the home page DecisionAccordion
    already handles them client-side via its "Standalone" fallback.

    `limit` caps the number of *groups* (decisions), not individual rows.
    """
    # Import here to avoid circular-import at module load; agent_activity
    # router imports no decisions symbols so this is safe.
    from argosy.api.routes.agent_activity import (
        AgentActivityRow,
        build_sources_preview,
    )

    async with db_mod.get_session() as session:
        # --- Step 1: find the top-N decision_ids by their max(created_at) ---
        subq = (
            select(
                AgentReportRow.decision_id,
                func.max(AgentReportRow.created_at).label("latest"),
            )
            .where(
                AgentReportRow.user_id == user_id,
                AgentReportRow.decision_id.is_not(None),
            )
            .group_by(AgentReportRow.decision_id)
            .order_by(desc("latest"))
            .limit(limit)
            .subquery()
        )
        top_ids_rows = (await session.execute(select(subq.c.decision_id))).scalars().all()
        if not top_ids_rows:
            return []

        # --- Step 2: load all agent_reports for those decision_ids ---
        ar_rows = (
            await session.execute(
                select(AgentReportRow)
                .where(
                    AgentReportRow.user_id == user_id,
                    AgentReportRow.decision_id.in_(top_ids_rows),
                )
                .order_by(asc(AgentReportRow.created_at))
            )
        ).scalars().all()

        # --- Step 3: batch-fetch matching DecisionRun rows (for tier/ticker/status) ---
        dr_by_id: dict[int, DecisionRun] = {}
        parseable_ids: list[int] = []
        for did in top_ids_rows:
            try:
                parseable_ids.append(int(did))
            except (ValueError, TypeError):
                pass
        if parseable_ids:
            dr_rows = (
                await session.execute(
                    select(DecisionRun).where(DecisionRun.id.in_(parseable_ids))
                )
            ).scalars().all()
            dr_by_id = {r.id: r for r in dr_rows}

    # --- Step 4: group rows by decision_id ---
    groups: dict[str, list[AgentReportRow]] = defaultdict(list)
    for r in ar_rows:
        groups[r.decision_id].append(r)  # type: ignore[index]

    # Preserve the ordering from top_ids_rows (already max(created_at) DESC).
    result: list[DecisionGroupDTO] = []
    for did in top_ids_rows:
        rows = groups.get(did, [])
        if not rows:
            continue

        # Resolve join to DecisionRun (may be None for non-integer decision_ids).
        dr: DecisionRun | None = None
        try:
            dr = dr_by_id.get(int(did))
        except (ValueError, TypeError):
            pass

        created_ats = [r.created_at for r in rows]
        started_at_dt = min(created_ats)
        finished_at_dt = max(created_ats)

        agent_runs_out: list[dict[str, Any]] = []
        for r in rows:
            citations_count = (
                len(json.loads(r.citations_json)) if r.citations_json else 0
            )
            sources_preview = build_sources_preview(r.sources_json)
            agent_runs_out.append(
                AgentActivityRow(
                    id=r.id,
                    user_id=r.user_id,
                    agent_role=r.agent_role,
                    decision_id=r.decision_id,
                    model=r.model,
                    confidence=r.confidence,
                    tokens_in=r.tokens_in,
                    tokens_out=r.tokens_out,
                    cost_usd=float(r.cost_usd or 0),
                    created_at=r.created_at.isoformat(),
                    cache_input_tokens=r.cache_input_tokens or 0,
                    cache_creation_tokens=r.cache_creation_tokens or 0,
                    thinking_tokens=r.thinking_tokens or 0,
                    citations_count=citations_count,
                    sources_preview=sources_preview,
                    # Wave B-UI follow-up Item 2 — correlation id for O(1)
                    # WS↔DB linking (migration 0028).
                    run_correlation_id=r.run_correlation_id,
                ).model_dump()
            )

        total_cost = sum(float(r.cost_usd or 0) for r in rows)

        result.append(DecisionGroupDTO(
            decision_id=did,
            decision_kind=(dr.decision_kind if dr else None),
            tier=(dr.tier if dr else None),
            ticker=(dr.ticker if dr else None),
            started_at=started_at_dt.isoformat(),
            finished_at=finished_at_dt.isoformat(),
            status=(dr.status if dr else "done"),
            total_cost_usd=total_cost,
            agent_count=len(rows),
            agent_runs=agent_runs_out,
        ))

    return result


__all__ = ["router"]
