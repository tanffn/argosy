"""Per-position thesis endpoint (T4.1) — a projection of the STANCE REGISTRY.

``GET /api/positions/thesis?user_id=...`` returns a list of
:class:`PositionThesisDTO` cards. Since the stance-registry build
(2026-07-10) the route is a thin projection of
``argosy/services/position_stance.py``: ONE canonical stance per position
(precedence: open proposal > verified fleet review > plan stance) that every
surface projects — the reconciliation that used to live here as
``_overlay_open_proposals`` moved into the service, semantics intact.

The wire DTO shape is unchanged, so the UI needs no changes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.position_stance import (
    _load_portfolio_snapshot,  # module-level for test monkeypatching
    project_thesis_dtos,
)
from argosy.state.models import PlanVersion
from argosy.state.queries import get_current_plan, get_pending_draft

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/positions", tags=["positions"])


class PositionThesisDTO(BaseModel):
    """Wire-format mirror of :class:`PositionThesis`.

    Kept as an explicit pydantic model (rather than reusing the
    dataclass) so the FastAPI OpenAPI schema captures the contract.

    Additive provenance fields (2026-07-12 §7.1): falsifier_state /
    falsifiers / next_validation / last_fleet_check_at — sourced from
    the verdicts registry (+ holding_reviews / decision_runs fallbacks).
    """

    ticker: str
    current_shares: float | None
    current_weight_pct: float | None
    current_usd_value: float | None
    verdict: str
    conviction: str
    reasoning_md: str
    cited_sources: list[str] = []
    target_weight_pct: float | None = None
    target_shares: int | None = None
    # Provenance (additive — never break older clients that ignore them)
    falsifier_state: str = "none_recorded"  # armed | fired | none_recorded
    falsifiers: list[str] = []
    next_validation: str | None = None  # ISO date
    last_fleet_check_at: str | None = None  # ISO datetime


@router.get("/thesis", response_model=list[PositionThesisDTO])
def get_position_theses(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> list[PositionThesisDTO]:
    """Return per-position thesis cards for the given user.

    Prefers the in-flight draft (``role='draft'``) so the cards reflect
    the *proposed* plan; falls back to the accepted plan when no draft
    is pending. Returns an empty list when the user has no plan at all
    rather than 404 — an empty positions page is a valid UI state.

    Each card's verdict/reasoning comes from the stance registry
    (rebuilt on demand when any source moved), so /portfolio, the fleet
    review and the inbox can never disagree on a ticker's stance.
    """
    pv: PlanVersion | None = get_pending_draft(db, user_id)
    if pv is None:
        pv = get_current_plan(db, user_id)
    if pv is None:
        return []

    snapshot = _load_portfolio_snapshot(user_id, db)

    try:
        dtos = project_thesis_dtos(db, user_id, plan_version=pv, snapshot=snapshot)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - defensive
        logger.exception("stance projection failed")
        raise HTTPException(
            status_code=500, detail=f"thesis derivation failed: {exc}"
        ) from exc
    return [PositionThesisDTO(**d) for d in dtos]


__all__ = ["PositionThesisDTO", "router"]
