"""Per-FM-objection user stance (agree / disagree / defer) + start-new-round.

Lets the user mark each Fund Manager objection AGREE / DISAGREE / DEFER
and, on DISAGREE, attach a free-text counter-position. The companion
"start new round with my decisions" endpoint composes a structured
guidance string from the stances and routes through the existing
advisor check-in flow so the cost-cap wiring is reused unchanged.

Persistence is per-(user_id, plan_version_id, objection_index) so the
user's choices survive navigation away from /plan and back.

This module is intentionally separate from ``plan.py`` so the agree /
disagree work doesn't have to share the file with sibling tasks
(translation cache, NVDA PACE) that are concurrently editing the
existing FM-objections handler. The endpoints live under the same
``/plan/draft/objections/*`` URL prefix so the UI doesn't need to
distinguish.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.api.routes.plan import (
    FMObjection,
    _classify_severity,
    _parse_fm_response,
    _split_reason,
    get_db,
)
from argosy.state.models import (
    AgentReport,
    DecisionRun,
    FMObjectionUserState,
    PlanVersion,
)
from argosy.state.queries import get_active_baseline, get_pending_draft

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plan", tags=["plan"])


_VALID_STANCES = ("AGREE", "DISAGREE", "DEFER")


def _hash_objection_topic(topic: str, detail: str) -> str:
    """Stable short hash of one FM objection (topic+detail).

    Defense-in-depth: the FM objection list is parsed live from the
    ``fund_manager`` agent_report.response_text on every GET, so if the
    list mutates between renders we can detect a stale user-state row
    via the hash. First 16 hex chars of SHA-256 over
    ``"{topic}\\n{detail}"`` — collision-resistant enough at our scale.
    """
    blob = f"{topic.strip()}\n{detail.strip()}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _compose_new_round_guidance(
    objections: list[FMObjection],
    state_by_idx: dict[int, Any],
) -> str:
    """Compose a structured guidance string from per-objection user stances.

    Fed as ``guidance`` to ``run_synthesis`` so the analyst + synthesizer
    + risk + FM agents see the user's resolved positions on each FM
    concern. Bucketed by stance so the synthesizer can distinguish
    "user accepted this constraint" from "user pushes back; here's
    their counter-position which is authoritative" from "user hasn't
    decided; re-evaluate honestly".
    """
    agreed: list[str] = []
    disagreed: list[str] = []
    deferred: list[str] = []

    for idx, obj in enumerate(objections):
        row = state_by_idx.get(idx)
        stance = (row.stance if row else "DEFER").upper()
        line = f"  - [{obj.severity}] {obj.topic} — {obj.detail}"
        if stance == "AGREE":
            agreed.append(line)
        elif stance == "DISAGREE":
            cp = (row.counter_position or "").strip() if row else ""
            cp_line = (
                f"\n    USER COUNTER-POSITION (authoritative): {cp}"
                if cp else ""
            )
            disagreed.append(line + cp_line)
        else:
            deferred.append(line)

    sections: list[str] = []
    sections.append(
        "The prior draft was rejected by the Fund Manager. The user has "
        "now reviewed each FM objection individually and recorded their "
        "stance. Use the following decisions as authoritative input to "
        "the new draft. Each section below specifies how to treat its "
        "objections."
    )

    if agreed:
        sections.append(
            "AGREED OBJECTIONS — the user accepts these constraints; "
            "treat them as resolved and bake them into the new draft:\n"
            + "\n".join(agreed)
        )
    if disagreed:
        sections.append(
            "DISAGREED OBJECTIONS — the user pushes back on these. "
            "Where a USER COUNTER-POSITION is given, treat it as "
            "authoritative and re-derive the affected targets / "
            "constraints from it. If the counter-position is empty or "
            "incoherent, flag that fact in the rationale rather than "
            "silently dropping the FM concern:\n"
            + "\n".join(disagreed)
        )
    if deferred:
        sections.append(
            "DEFERRED OBJECTIONS — the user has not made a call. "
            "Re-evaluate these honestly and surface a fresh "
            "recommendation in the new draft's rationale:\n"
            + "\n".join(deferred)
        )

    return "\n\n".join(sections)


class FMObjectionStateRow(BaseModel):
    stance: str  # "AGREE" | "DISAGREE" | "DEFER"
    counter_position: str | None = None


class FMObjectionStateMapResponse(BaseModel):
    """Map of ``{objection_index_str: {stance, counter_position}}``.

    The UI keys this by index (as a string) since JSON object keys are
    strings. ``DEFER`` is the implicit default for any index that
    doesn't appear in the map.
    """

    states: dict[str, FMObjectionStateRow]
    plan_version_id: int


class FMObjectionStateUpsertRequest(BaseModel):
    user_id: str
    plan_version_id: int
    objection_index: int
    stance: str  # "AGREE" | "DISAGREE" | "DEFER"
    counter_position: str | None = None
    # Optional — when present, we stamp the row's ``topic_hash`` so
    # future readers can detect a stale row if the FM rendering changes
    # between draft loads. The UI passes the current objection's
    # topic+detail and the hash is computed server-side.
    topic: str | None = None
    detail: str | None = None


class FMObjectionStateUpsertResponse(BaseModel):
    status: str
    objection_index: int
    stance: str


class StartNewRoundResponse(BaseModel):
    status: str
    decision_run_id: int
    decision_audit_token: str  # "plan-synth-<id>"
    n_agreed: int
    n_disagreed: int
    n_deferred: int
    guidance_preview: str  # first ~500 chars of composed guidance, for debug


@router.get(
    "/draft/objections/state",
    response_model=FMObjectionStateMapResponse,
)
def get_fm_objection_state(
    user_id: str,
    plan_version_id: int,
    db: Session = Depends(get_db),
) -> FMObjectionStateMapResponse:
    """Return the user's per-objection stance map for one draft.

    Empty map when no rows exist for ``(user_id, plan_version_id)`` —
    the UI treats that as DEFER for every objection. We don't validate
    that ``plan_version_id`` is a draft of this user here; the GET is
    read-only and an unknown id simply returns an empty map.
    """
    rows = db.execute(
        select(FMObjectionUserState).where(
            FMObjectionUserState.user_id == user_id,
            FMObjectionUserState.plan_version_id == plan_version_id,
        )
    ).scalars().all()
    return FMObjectionStateMapResponse(
        plan_version_id=plan_version_id,
        states={
            str(r.objection_index): FMObjectionStateRow(
                stance=r.stance,
                counter_position=r.counter_position,
            )
            for r in rows
        },
    )


@router.put(
    "/draft/objections/state",
    response_model=FMObjectionStateUpsertResponse,
)
def put_fm_objection_state(
    body: FMObjectionStateUpsertRequest,
    db: Session = Depends(get_db),
) -> FMObjectionStateUpsertResponse:
    """Upsert one (user_id, plan_version_id, objection_index) stance row.

    Validates:
      * ``stance`` in the allowed enum.
      * ``counter_position`` is non-empty (after .strip()) when
        ``stance='DISAGREE'``.
      * ``plan_version_id`` belongs to ``user_id`` and is a draft.

    The UI saves on blur (DISAGREE counter-position textarea) and on
    stance change. Idempotent — repeated PUTs for the same triple just
    update ``stance`` + ``counter_position`` + ``updated_at``.
    """
    stance = (body.stance or "").upper().strip()
    if stance not in _VALID_STANCES:
        raise HTTPException(
            status_code=400,
            detail=f"stance must be one of {_VALID_STANCES}; got {body.stance!r}",
        )
    counter = (body.counter_position or "").strip() or None
    if stance == "DISAGREE" and not counter:
        raise HTTPException(
            status_code=400,
            detail="counter_position is required when stance='DISAGREE'",
        )
    if body.objection_index < 0:
        raise HTTPException(
            status_code=400, detail="objection_index must be >= 0",
        )

    pv = db.get(PlanVersion, body.plan_version_id)
    if pv is None:
        raise HTTPException(
            status_code=404,
            detail=f"plan_version_id={body.plan_version_id} not found",
        )
    if pv.user_id != body.user_id:
        raise HTTPException(
            status_code=403,
            detail="plan_version does not belong to this user",
        )
    if pv.role != "draft":
        raise HTTPException(
            status_code=400,
            detail=(
                f"plan_version_id={body.plan_version_id} has role={pv.role!r}; "
                "only drafts can carry per-objection user state"
            ),
        )

    topic_hash = _hash_objection_topic(body.topic or "", body.detail or "")

    row = db.execute(
        select(FMObjectionUserState).where(
            FMObjectionUserState.user_id == body.user_id,
            FMObjectionUserState.plan_version_id == body.plan_version_id,
            FMObjectionUserState.objection_index == body.objection_index,
        )
    ).scalar_one_or_none()

    if row is None:
        row = FMObjectionUserState(
            user_id=body.user_id,
            plan_version_id=body.plan_version_id,
            objection_index=body.objection_index,
            topic_hash=topic_hash,
            stance=stance,
            counter_position=counter,
        )
        db.add(row)
    else:
        row.stance = stance
        # Only set counter_position to the new value when stance is
        # DISAGREE; AGREE clears it; DEFER preserves the prior value
        # (it's neutral, the user may flip back).
        if stance == "DISAGREE":
            row.counter_position = counter
        elif stance == "AGREE":
            row.counter_position = None
        if body.topic or body.detail:
            row.topic_hash = topic_hash
    db.commit()

    return FMObjectionStateUpsertResponse(
        status="ok",
        objection_index=body.objection_index,
        stance=stance,
    )


@router.post(
    "/draft/objections/start-new-round",
    response_model=StartNewRoundResponse,
    status_code=202,
)
def post_start_new_round(
    user_id: str,
    plan_version_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> StartNewRoundResponse:
    """Start a new synthesis round seeded by the user's per-objection decisions.

    Reads ``fm_objection_user_state`` rows for ``(user_id, plan_version_id)``,
    composes a structured guidance string (AGREED / DISAGREED /
    DEFERRED buckets with the user's counter-positions for each
    DISAGREE), and dispatches the existing advisor check-in flow so
    the cost-cap wiring, baseline guard, and DecisionRun bookkeeping
    are reused unchanged.

    Refuses with 400 if every objection is still DEFER — there's nothing
    to act on; the user should use "Re-synthesize with all concerns"
    for the legacy behavior.

    Refuses with 404/403 if the plan_version isn't a pending draft of
    this user.
    """
    # Validate the draft belongs to this user.
    pv = db.get(PlanVersion, plan_version_id)
    if pv is None:
        raise HTTPException(
            status_code=404,
            detail=f"plan_version_id={plan_version_id} not found",
        )
    if pv.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="plan_version does not belong to this user",
        )
    if pv.role != "draft":
        raise HTTPException(
            status_code=400,
            detail=(
                f"plan_version_id={plan_version_id} has role={pv.role!r}; "
                "only drafts can start a new round"
            ),
        )
    pending = get_pending_draft(db, user_id)
    if pending is None or pending.id != plan_version_id:
        raise HTTPException(
            status_code=404,
            detail="no pending draft for user matching plan_version_id",
        )

    # Reparse the FM objections so the guidance composer sees the same
    # ordering the UI does. The user-state rows are keyed by index, so
    # we MUST recompute the index against the same parse path.
    if pv.decision_run_id is None:
        raise HTTPException(
            status_code=400,
            detail="draft has no synthesis run; can't start new round",
        )
    decision_id_str = f"plan-synth-{pv.decision_run_id}"
    fm_row = db.execute(
        select(AgentReport).where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_id_str,
            AgentReport.agent_role == "fund_manager",
        ).order_by(desc(AgentReport.created_at)).limit(1)
    ).scalar_one_or_none()
    if fm_row is None or not fm_row.response_text:
        raise HTTPException(
            status_code=400,
            detail="no fund_manager agent_report for this draft; nothing to re-round",
        )
    parsed = _parse_fm_response(fm_row.response_text)
    reasons = parsed.get("reasons") or []
    objections: list[FMObjection] = []
    for r in reasons:
        if not isinstance(r, str) or not r.strip():
            continue
        topic, detail = _split_reason(r)
        objections.append(
            FMObjection(
                severity=_classify_severity(topic, detail),
                topic=topic,
                detail=detail,
            )
        )
    if not objections:
        raise HTTPException(
            status_code=400,
            detail="FM raised no objections to act on",
        )

    # Pull state rows + count buckets.
    state_rows = db.execute(
        select(FMObjectionUserState).where(
            FMObjectionUserState.user_id == user_id,
            FMObjectionUserState.plan_version_id == plan_version_id,
        )
    ).scalars().all()
    state_by_idx = {row.objection_index: row for row in state_rows}

    n_agreed = sum(
        1 for r in state_rows if (r.stance or "").upper() == "AGREE"
    )
    n_disagreed = sum(
        1 for r in state_rows if (r.stance or "").upper() == "DISAGREE"
    )
    n_total = len(objections)
    n_deferred = n_total - n_agreed - n_disagreed

    if n_agreed == 0 and n_disagreed == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "every objection is still DEFER — nothing to start a new "
                "round with. Use 'Re-synthesize with all concerns' for "
                "the legacy behavior."
            ),
        )

    guidance = _compose_new_round_guidance(objections, state_by_idx)

    # Baseline guard (mirrors POST /api/advisor/check-in semantics so
    # we don't leak a status='running' DecisionRun zombie row).
    baseline = get_active_baseline(db, user_id)
    if baseline is None:
        raise HTTPException(
            status_code=404,
            detail=f"user {user_id!r} has no active baseline plan",
        )

    # Pre-create the DecisionRun row + schedule the background wrapper,
    # mirroring POST /api/advisor/check-in exactly so the same cost-cap
    # wiring + WS-event publishing fires.
    decision_run = DecisionRun(
        user_id=user_id,
        ticker="(plan)",
        tier="T3",
        decision_kind="plan_revision",
        started_at=datetime.now(UTC),
        status="running",
    )
    db.add(decision_run)
    db.commit()
    db.refresh(decision_run)
    decision_run_id = decision_run.id
    decision_audit_token = f"plan-synth-{decision_run_id}"

    from sqlalchemy.orm import sessionmaker

    from argosy.api.routes.advisor import _run_synthesis_background

    engine = db.get_bind()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    background_tasks.add_task(
        _run_synthesis_background,
        user_id=user_id,
        guidance=guidance,
        decision_run_id=decision_run_id,
        session_factory=SessionLocal,
    )

    return StartNewRoundResponse(
        status="accepted",
        decision_run_id=decision_run_id,
        decision_audit_token=decision_audit_token,
        n_agreed=n_agreed,
        n_disagreed=n_disagreed,
        n_deferred=n_deferred,
        guidance_preview=guidance[:500],
    )


__all__ = [
    "FMObjectionStateMapResponse",
    "FMObjectionStateRow",
    "FMObjectionStateUpsertRequest",
    "FMObjectionStateUpsertResponse",
    "StartNewRoundResponse",
    "_compose_new_round_guidance",
    "_hash_objection_topic",
    "router",
]
