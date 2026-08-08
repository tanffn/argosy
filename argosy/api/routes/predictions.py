"""Prediction ledger scorecard API — Stream C standing metric surface.

Endpoints:

  * ``GET /api/predictions/scorecard?user_id=`` — per-source hit rate,
    HOLD metrics, and every exclusion bucket (auditable reconcile).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.predictions.reliability import ledger_scorecard_by_source

router = APIRouter(prefix="/predictions", tags=["predictions"])


class SourceScorecardDTO(BaseModel):
    source: str
    hit_rate: float | None = None
    win_rate: float | None = None
    avg_pnl_pct: float | None = None
    falsifiable_denominator: int
    hit_target_count: int
    expired_positive_count: int
    hold_scored: int
    hold_correct_count: int
    hold_correct_rate: float | None = None
    excluded_unparseable: int
    excluded_neutral: int
    excluded_archived: int
    excluded_unscored: int
    excluded_pending_entry: int
    excluded_superseded: int
    excluded_hold_self_benchmark: int = 0
    excluded_hold_non_equity: int = 0
    excluded_hold_incomplete_benchmark: int = 0
    total_predictions: int
    exclusions: list[str] = Field(default_factory=list)
    hit_rate_definition: str | None = None
    win_rate_definition: str | None = None
    hold_correct_rate_definition: str | None = None
    hold_benchmark: str | None = None
    hold_benchmark_band_pct: float | None = None


class LedgerScorecardResponse(BaseModel):
    user_id: str
    by_source: list[SourceScorecardDTO]
    totals: dict[str, Any]


@router.get("/scorecard", response_model=LedgerScorecardResponse)
def get_ledger_scorecard(
    user_id: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> LedgerScorecardResponse:
    rows = ledger_scorecard_by_source(db, user_id)
    payloads = [SourceScorecardDTO(**r.to_dict()) for r in rows]
    totals = {
        "sources": len(payloads),
        "total_predictions": sum(p.total_predictions for p in payloads),
        "falsifiable_denominator": sum(
            p.falsifiable_denominator for p in payloads
        ),
        "excluded_pending_entry": sum(
            p.excluded_pending_entry for p in payloads
        ),
        "excluded_superseded": sum(p.excluded_superseded for p in payloads),
        "excluded_unscored": sum(p.excluded_unscored for p in payloads),
        "excluded_unparseable": sum(
            p.excluded_unparseable for p in payloads
        ),
        "excluded_hold_self_benchmark": sum(
            p.excluded_hold_self_benchmark for p in payloads
        ),
        "excluded_hold_non_equity": sum(
            p.excluded_hold_non_equity for p in payloads
        ),
        "excluded_hold_incomplete_benchmark": sum(
            p.excluded_hold_incomplete_benchmark for p in payloads
        ),
        "hold_scored": sum(p.hold_scored for p in payloads),
    }
    return LedgerScorecardResponse(
        user_id=user_id, by_source=payloads, totals=totals
    )
