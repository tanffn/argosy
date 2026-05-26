"""Fleet self-review API endpoints.

Endpoints:

  * ``GET /api/fleet-self-review/latest?user_id=`` — most recent
    persisted report for the user (or 404).
  * ``GET /api/fleet-self-review/{id}?user_id=`` — full markdown +
    structured findings for one report by id.
  * ``POST /api/fleet-self-review/run?user_id=`` — manual fire
    (scope_kind='manual') for the user.  Useful while developing /
    when the user explicitly asks for a fresh sweep.

The endpoints are tenant-scoped via the ``user_id`` query param,
consistent with the rest of the Argosy API.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Generator

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.state.models import FleetSelfReviewReport

router = APIRouter(prefix="/fleet-self-review", tags=["fleet-self-review"])


class FleetSelfReviewDTO(BaseModel):
    """Wire shape — both ``/latest`` and ``/{id}`` return this."""

    id: int
    user_id: str
    generated_at: str
    scope_kind: str
    decision_run_id: int | None
    content_md: str
    findings: list[dict]
    severity_summary: dict


class FleetSelfReviewListItemDTO(BaseModel):
    """Lighter shape for index views (no markdown body)."""

    id: int
    generated_at: str
    scope_kind: str
    decision_run_id: int | None
    severity_summary: dict


def _row_to_dto(row: FleetSelfReviewReport) -> FleetSelfReviewDTO:
    try:
        findings = json.loads(row.findings_json or "[]")
        if not isinstance(findings, list):
            findings = []
    except (json.JSONDecodeError, TypeError):
        findings = []
    try:
        sev = json.loads(row.severity_summary_json or "{}")
        if not isinstance(sev, dict):
            sev = {}
    except (json.JSONDecodeError, TypeError):
        sev = {}
    return FleetSelfReviewDTO(
        id=row.id,
        user_id=row.user_id,
        generated_at=_iso(row.generated_at),
        scope_kind=row.scope_kind,
        decision_run_id=row.decision_run_id,
        content_md=row.content_md or "",
        findings=findings,
        severity_summary=sev,
    )


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


@router.get("/latest", response_model=FleetSelfReviewDTO | None)
def get_latest_report(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> FleetSelfReviewDTO | None:
    """Most-recent fleet self-review report for the user, or None."""
    row = db.execute(
        select(FleetSelfReviewReport)
        .where(FleetSelfReviewReport.user_id == user_id)
        .order_by(desc(FleetSelfReviewReport.generated_at))
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return _row_to_dto(row)


@router.get("/list", response_model=list[FleetSelfReviewListItemDTO])
def list_reports(
    user_id: str = Query("ariel"),
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[FleetSelfReviewListItemDTO]:
    """Light index of recent reports (no markdown body)."""
    rows = db.execute(
        select(FleetSelfReviewReport)
        .where(FleetSelfReviewReport.user_id == user_id)
        .order_by(desc(FleetSelfReviewReport.generated_at))
        .limit(limit)
    ).scalars().all()
    out: list[FleetSelfReviewListItemDTO] = []
    for r in rows:
        try:
            sev = json.loads(r.severity_summary_json or "{}")
        except (json.JSONDecodeError, TypeError):
            sev = {}
        out.append(FleetSelfReviewListItemDTO(
            id=r.id,
            generated_at=_iso(r.generated_at),
            scope_kind=r.scope_kind,
            decision_run_id=r.decision_run_id,
            severity_summary=sev if isinstance(sev, dict) else {},
        ))
    return out


@router.get("/{report_id}", response_model=FleetSelfReviewDTO)
def get_report(
    report_id: int,
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> FleetSelfReviewDTO:
    """Full report by id.  Returns 404 when the row is owned by a
    different tenant — never reveals existence cross-tenant."""
    row = db.get(FleetSelfReviewReport, report_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="report not found")
    return _row_to_dto(row)


@router.post("/run", response_model=FleetSelfReviewDTO)
def run_now(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> FleetSelfReviewDTO:
    """Manual fire — useful while developing / when the user asks for
    a fresh sweep.  Persists a row with ``scope_kind='manual'``.
    """
    from argosy.services.fleet_self_review_runner import (
        generate_fleet_self_review,
    )
    row = generate_fleet_self_review(
        db, user_id=user_id, scope_kind="manual", decision_run_id=None,
    )
    return _row_to_dto(row)


__all__ = ["router"]
