"""One visible entry point for the real Argosy decision-to-outcome spine."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.e2e_proof import build_e2e_proof

router = APIRouter(prefix="/e2e-proof", tags=["e2e-proof"])


class RunRequest(BaseModel):
    user_id: str = "ariel"
    cash_usd: float = Field(ge=0)
    allow_sells: bool = True
    horizon_years_min: int = Field(default=1, ge=1, le=30)
    horizon_years_max: int = Field(default=5, ge=1, le=30)

    @model_validator(mode="after")
    def ordered_horizon(self) -> RunRequest:
        if self.horizon_years_max < self.horizon_years_min:
            raise ValueError("horizon_years_max must be >= horizon_years_min")
        return self


@router.get("")
def get_e2e_proof(
    user_id: str = Query("ariel"), db: Session = Depends(get_db)  # noqa: B008
) -> dict:
    return build_e2e_proof(db, user_id)


@router.post("/run")
def run_e2e_proof(
    body: RunRequest, db: Session = Depends(get_db)  # noqa: B008
) -> dict:
    """Author and persist one run through the canonical live deploy path."""
    run_started_at = datetime.now(UTC)
    from argosy.async_bridge import run_async_from_sync
    from argosy.orchestrator.cost_guard import get_cost_guard

    guard = get_cost_guard(user_id=body.user_id)
    if run_async_from_sync(
        lambda: guard.should_pause_non_routine(loop_name="period_directive_daily")
    ):
        raise HTTPException(status_code=409, detail="Monthly LLM budget is paused")

    from argosy.api.routes.portfolio import get_deploy_cash
    from argosy.services.current_recommendations import recommendation_ids, recommendation_keys
    from argosy.services.allocation_research import refresh_due_research
    from argosy.services.jobs.period_directive_daily import (
        _find_open_directives,
        _has_fresh_validated_sheet,
        _consume_component_recommendations,
        _link_team_telemetry,
        _load_recommendations,
        _supersede_open_directives,
        _write_directive_proposal,
    )
    refresh_due_research(db, body.user_id)
    outcome = get_deploy_cash(
        cash_usd=body.cash_usd,
        user_id=body.user_id,
        live=True,
        sleeve_pct=5.0,
        use_high_potential=True,
        fleet_review=False,
        include_order_sheet=True,
        allow_sells=body.allow_sells,
        horizon_years_min=body.horizon_years_min,
        horizon_years_max=body.horizon_years_max,
        db=db,
    )
    artifact = getattr(outcome, "order_sheet", None)
    review_resolution = (
        getattr(artifact.sheet, "review_resolution", None)
        if artifact is not None and artifact.sheet is not None
        else None
    )
    if (
        artifact is None
        or artifact.status != "validated"
        or artifact.sheet is None
        or review_resolution is None
        or not review_resolution.one_voice
    ):
        failures = list(getattr(artifact, "failures", []) or [])
        if review_resolution is None:
            failures.append("independent review resolution is missing")
        elif not review_resolution.one_voice:
            failures.append("independent reviewers did not reconcile to one voice")
        prior_valid = next(
            (
                row
                for row in _find_open_directives(db, body.user_id)
                if _has_fresh_validated_sheet(row)
            ),
            None,
        )
        superseded = _supersede_open_directives(
            db,
            body.user_id,
            keep_id=(prior_valid.id if prior_valid is not None else None),
            reason="explicit E2E replacement failed and current artifact is invalid",
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Argosy stopped: no validated order sheet was produced",
                "failures": failures,
                "superseded_invalid_directives": superseded,
            },
        )
    recommendations = _load_recommendations(db, user_id=body.user_id)
    source_ids = recommendation_ids(recommendations)
    proposal_id = _write_directive_proposal(
        db,
        body.user_id,
        excess_usd=body.cash_usd,
        source_proposal_ids=source_ids,
        source_recommendation_keys=recommendation_keys(recommendations),
        sheet=artifact.sheet,
    )
    if proposal_id is None:
        raise HTTPException(status_code=500, detail="Validated sheet could not be persisted")
    _link_team_telemetry(
        db,
        body.user_id,
        directive_proposal_id=proposal_id,
        started_at=run_started_at,
    )
    _supersede_open_directives(
        db,
        body.user_id,
        keep_id=proposal_id,
        reason="replaced by the latest explicit E2E run",
    )
    _consume_component_recommendations(
        db,
        body.user_id,
        source_proposal_ids=source_ids,
        directive_proposal_id=proposal_id,
    )
    return build_e2e_proof(db, body.user_id)


__all__ = ["router"]
