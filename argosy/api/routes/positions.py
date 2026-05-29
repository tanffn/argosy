"""Per-position thesis endpoint (T4.1).

``GET /api/positions/thesis?user_id=...`` returns a list of
:class:`PositionThesis` cards derived from the user's pending plan
draft (or accepted plan if no draft is in flight) and their current
portfolio snapshot.

Pure derivation — no LLM. See ``argosy/services/per_position_thesis.py``
for the heuristic.
"""

from __future__ import annotations

import logging
from typing import Generator

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.per_position_thesis import (
    PositionThesis,
    derive_position_theses,
    emit_thesis_predictions,
)
from argosy.state.models import AgentReport, PlanVersion
from argosy.state.queries import get_current_plan, get_pending_draft

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/positions", tags=["positions"])


class PositionThesisDTO(BaseModel):
    """Wire-format mirror of :class:`PositionThesis`.

    Kept as an explicit pydantic model (rather than reusing the
    dataclass) so the FastAPI OpenAPI schema captures the contract.
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


def _to_dto(t: PositionThesis) -> PositionThesisDTO:
    return PositionThesisDTO(**t.to_dict())


def _load_portfolio_snapshot(user_id: str) -> object | None:
    """Return the freshest portfolio snapshot or None.

    Re-uses the same TSV-discovery + parser the /api/portfolio/snapshot
    route uses so the per-position cards agree with the allocation
    chart on "today's holdings". ``user_id`` is accepted for parity
    with other routes but Phase 2 still uses a global file lookup.
    """
    _ = user_id  # multi-tenant slot; matches /api/portfolio/snapshot
    try:
        from argosy.api.routes.portfolio import _find_latest_tsv
        from argosy.ingest.tsv import parse_portfolio_tsv

        tsv = _find_latest_tsv()
        if tsv is None:
            return None
        return parse_portfolio_tsv(tsv)
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("portfolio snapshot lookup failed", exc_info=True)
        return None


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
    """
    pv: PlanVersion | None = get_pending_draft(db, user_id)
    if pv is None:
        pv = get_current_plan(db, user_id)
    if pv is None:
        return []

    snapshot = _load_portfolio_snapshot(user_id)

    # Pull analyst reports for the draft's synthesis decision_run so we
    # can attribute conviction + cited sources. When the draft wasn't
    # produced by a synthesis run (manual ingest / legacy), we simply
    # have no analyst rows -> conviction falls through to LOW and the
    # cited_sources list is empty.
    reports: list[AgentReport] = []
    if pv.decision_run_id is not None:
        decision_id_str = f"plan-synth-{pv.decision_run_id}"
        reports = list(
            db.execute(
                select(AgentReport)
                .where(
                    AgentReport.user_id == user_id,
                    AgentReport.decision_id == decision_id_str,
                )
            ).scalars().all()
        )

    try:
        theses = derive_position_theses(
            plan_version=pv,
            portfolio_snapshot=snapshot,
            agent_reports=reports,
            # Spec C commit #6 / §6.3 — surface the reliability
            # annotation per contributing source on every PositionThesis
            # so the LLM / UI can downweight low-reliability inputs
            # without re-applying the multiplicative weight (the synth
            # already did that via its preamble at draft-creation time).
            session=db,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        logger.exception("derive_position_theses failed")
        raise HTTPException(
            status_code=500, detail=f"thesis derivation failed: {exc}"
        ) from exc

    # Spec C commit #3 — emit one prediction per thesis card so the
    # reliability ledger can score per_position_thesis output. Idempotent
    # on (plan_version_id, ticker) so multiple GETs of the same draft
    # don't duplicate rows.
    #
    # Spec C commit #6 / §6.6 (codex review BLOCKER 1, 2026-05-29) —
    # when the draft came from a synthesis run, the synth's preamble
    # ALREADY applied the per-source reliability weights to the
    # signals that produced these theses. Stamping
    # ``provenance_weights_applied=True`` on the resulting prediction
    # rows tells downstream consumers (a future synth iteration
    # reading internal_per_position_thesis reliability, etc.) NOT to
    # re-multiply by the same upstream weight. When the draft has no
    # decision_run_id (manual ingest / legacy), no synth banner ran,
    # so we leave provenance=False (the default).
    emit_thesis_predictions(
        db, user_id, plan_version_id=pv.id, theses=theses,
        provenance_weights_applied=(pv.decision_run_id is not None),
    )

    return [_to_dto(t) for t in theses]


__all__ = ["PositionThesisDTO", "router"]
