"""Anomaly-detection API endpoints (EX2).

Endpoints:

  * ``GET /api/anomalies/latest?user_id=`` — most recent persisted
    ``anomaly_reports`` row for the user. Returns 200 + ``null`` when
    none exist (parallels ``/api/fleet-self-review/latest``).
  * ``GET /api/anomalies/{id}?user_id=`` — full detail by id. 404 when
    the row is owned by a different tenant — never reveals existence
    cross-tenant.

The endpoints are tenant-scoped via the ``user_id`` query param,
consistent with the rest of the Argosy API.

The home-page banner consumes ``/latest`` on mount + on every
``anomaly.detected`` WebSocket event; the viewer page at
``/anomalies/[id]`` consumes ``/{id}``.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.state.models import AnomalyReport

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


class AnomalyReportDTO(BaseModel):
    """Wire shape — both ``/latest`` and ``/{id}`` return this."""

    id: int
    user_id: str
    triggered_by: str
    triggered_at: str
    source_statement_id: int | None
    report: dict
    severity_summary: dict
    agent_report_id: int | None


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


def _row_to_dto(row: AnomalyReport) -> AnomalyReportDTO:
    try:
        report = json.loads(row.report_json or "{}")
        if not isinstance(report, dict):
            report = {}
    except (json.JSONDecodeError, TypeError):
        report = {}
    try:
        sev = json.loads(row.severity_summary_json or "{}")
        if not isinstance(sev, dict):
            sev = {}
    except (json.JSONDecodeError, TypeError):
        sev = {}
    return AnomalyReportDTO(
        id=row.id,
        user_id=row.user_id,
        triggered_by=row.triggered_by,
        triggered_at=_iso(row.triggered_at),
        source_statement_id=row.source_statement_id,
        report=report,
        severity_summary=sev,
        agent_report_id=row.agent_report_id,
    )


@router.get("/latest", response_model=AnomalyReportDTO | None)
def get_latest(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> AnomalyReportDTO | None:
    """Most-recent anomaly report for the user. ``null`` when none exist."""
    row = db.execute(
        select(AnomalyReport)
        .where(AnomalyReport.user_id == user_id)
        .order_by(desc(AnomalyReport.triggered_at))
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return _row_to_dto(row)


@router.get("/{report_id}", response_model=AnomalyReportDTO)
def get_one(
    report_id: int,
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> AnomalyReportDTO:
    """Full report by id. 404 when the row is owned by a different tenant."""
    row = db.get(AnomalyReport, report_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="anomaly report not found")
    return _row_to_dto(row)


__all__ = ["AnomalyReportDTO", "router"]
