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

# In-process thesis cache (perf). Derivation is deterministic in
# (plan_version, snapshot), so we recompute only when one of those changes —
# steady-state /portfolio refreshes are served from cache with ZERO derivation
# and ZERO reliability-ledger writes (the emit happens only on a cache MISS,
# once per plan/snapshot). Bounded; a handful of entries in practice. Cleared
# on server restart. A new plan draft is a new row (new id) so it misses
# naturally; a new snapshot changes the snapshot key.
_THESIS_CACHE: dict[tuple, list["PositionThesisDTO"]] = {}
_THESIS_CACHE_MAX = 32


def _snapshot_cache_key(snapshot) -> tuple:
    return (
        getattr(snapshot, "snapshot_date", None),
        len(getattr(snapshot, "positions", []) or []),
        round(float(getattr(snapshot, "total_usd_value_k", 0.0) or 0.0), 2),
    )


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


def _load_portfolio_snapshot(user_id: str, db: Session | None = None) -> object | None:
    """Return the freshest portfolio snapshot or None.

    DB-FIRST (matching /api/portfolio/snapshot) so the per-position cards agree
    with the allocation chart AND load fast: the prior TSV-discovery path walked
    ARGOSY_HOME (which includes the Google Drive folder) + re-parsed the TSV on
    EVERY request — ~1.4s, the real cost behind the slow Verdict column. The DB
    row is the same data the snapshot endpoint serves; we fall back to the TSV
    walk only when the DB has nothing.
    """
    try:
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
            row_to_snapshot,
        )

        if db is not None:
            row = get_latest_snapshot_row(db, user_id)
            if row is not None:
                return row_to_snapshot(row)
    except Exception:  # noqa: BLE001 - fall through to the filesystem path
        logger.warning("portfolio snapshot DB lookup failed", exc_info=True)
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

    snapshot = _load_portfolio_snapshot(user_id, db)

    # Cache hit -> return immediately (no derivation, no analyst-report load, no
    # ledger write). Keyed on the plan version + snapshot identity.
    cache_key = (user_id, pv.id, _snapshot_cache_key(snapshot))
    cached = _THESIS_CACHE.get(cache_key)
    if cached is not None:
        return cached

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

    dtos = [_to_dto(t) for t in theses]
    if len(_THESIS_CACHE) >= _THESIS_CACHE_MAX:
        _THESIS_CACHE.clear()  # simple bound — keys are plan/snapshot scoped
    _THESIS_CACHE[cache_key] = dtos
    return dtos


__all__ = ["PositionThesisDTO", "router"]
