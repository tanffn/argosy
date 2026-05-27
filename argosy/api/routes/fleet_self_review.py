"""Fleet self-review API endpoints.

Endpoints:

  * ``GET /api/fleet-self-review/latest?user_id=`` — most recent
    persisted report for the user (or 404).
  * ``GET /api/fleet-self-review/{id}?user_id=`` — full markdown +
    structured findings for one report by id.
  * ``GET /api/fleet-self-review/list?user_id=&limit=`` — lightweight
    index of recent reports (no markdown body) — drives the
    /fleet-review list page.
  * ``GET /api/fleet-self-review/trends?user_id=&days=`` — time-series
    of severity counts + most-persistent detectors over the last N
    days — drives the /fleet-review trends chart.
  * ``POST /api/fleet-self-review/run?user_id=`` — manual fire
    (scope_kind='manual') for the user.  Useful while developing /
    when the user explicitly asks for a fresh sweep.

The endpoints are tenant-scoped via the ``user_id`` query param,
consistent with the rest of the Argosy API.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
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
    """Lighter shape for index views (no markdown body).

    ``findings_total`` is the count of structured findings in the row's
    ``findings_json`` — equivalent to ``len(findings)`` on the full
    DTO.  Surfaced here so the list page can render a per-row count
    badge without re-fetching the markdown body.
    """

    id: int
    generated_at: str
    scope_kind: str
    decision_run_id: int | None
    severity_summary: dict
    findings_total: int


class SeverityTrendPointDTO(BaseModel):
    """One sample on the severity trends chart.

    Each persisted report becomes one point so the chart shows the
    raw cadence of post_synthesis sweeps (typically clustered around
    plan revisions) rather than collapsing reports into daily buckets.
    """

    id: int
    generated_at: str
    red: int
    amber: int
    yellow: int


class SeverityTrendsDTO(BaseModel):
    """Time-series of severity counts over the last ``days`` days.

    ``points`` is sorted by ``generated_at`` ASC so the chart renders
    oldest-on-the-left without re-sorting client-side.

    ``most_persistent_findings`` lists detector titles (``Dn — title``)
    that appear in ≥50% of the reports inside the window.  These are
    the issues the fleet has consistently failed to close — the
    headline signal a user wants above the report list.
    """

    points: list[SeverityTrendPointDTO]
    days: int
    report_count: int
    most_persistent_findings: list[str]


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


def _parse_findings(blob: str | None) -> list[dict]:
    """Best-effort parse of ``findings_json`` — never raises."""
    if not blob:
        return []
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [f for f in parsed if isinstance(f, dict)]


def _parse_severity(blob: str | None) -> dict:
    """Best-effort parse of ``severity_summary_json`` — never raises."""
    if not blob:
        return {}
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@router.get("/list", response_model=list[FleetSelfReviewListItemDTO])
def list_reports(
    user_id: str = Query("ariel"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[FleetSelfReviewListItemDTO]:
    """Light index of recent reports (no markdown body).

    Returns newest-first; default ``limit=50`` matches the spec.  Each
    item carries the pre-joined severity badge counts plus
    ``findings_total`` so the UI doesn't need to re-fetch the heavy
    markdown body just to render a list row.
    """
    rows = db.execute(
        select(FleetSelfReviewReport)
        .where(FleetSelfReviewReport.user_id == user_id)
        .order_by(desc(FleetSelfReviewReport.generated_at))
        .limit(limit)
    ).scalars().all()
    out: list[FleetSelfReviewListItemDTO] = []
    for r in rows:
        sev = _parse_severity(r.severity_summary_json)
        findings = _parse_findings(r.findings_json)
        out.append(FleetSelfReviewListItemDTO(
            id=r.id,
            generated_at=_iso(r.generated_at),
            scope_kind=r.scope_kind,
            decision_run_id=r.decision_run_id,
            severity_summary=sev,
            findings_total=len(findings),
        ))
    return out


@router.get("/trends", response_model=SeverityTrendsDTO)
def severity_trends(
    user_id: str = Query("ariel"),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> SeverityTrendsDTO:
    """Severity counts over time + the detectors that won't go away.

    Window: last ``days`` calendar days, by ``generated_at``.  Returns
    one point per persisted report (no bucketing) — keeps the cadence
    of post_synthesis bursts visible in the chart so the user can see
    "the AMBER count flattened after the f8faaca fix" rather than a
    smoothed daily average.

    ``most_persistent_findings`` is the set of detector ids whose
    ``id`` (the deterministic detector-key) appears in ≥50% of reports
    inside the window.  Empty when ``report_count < 2`` (one sample
    isn't a pattern).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_naive = cutoff.replace(tzinfo=None)

    rows = db.execute(
        select(FleetSelfReviewReport)
        .where(FleetSelfReviewReport.user_id == user_id)
        .where(FleetSelfReviewReport.generated_at >= cutoff_naive)
        .order_by(FleetSelfReviewReport.generated_at.asc())
    ).scalars().all()

    points: list[SeverityTrendPointDTO] = []
    # Counter keyed by a stable detector identity so we can rank
    # persistence across reports.  We use the finding's ``detector``
    # (D1..D10) plus its ``id`` suffix when available so e.g. a D4
    # hit on macro/FRED/X vs macro/FRED/Y count separately — same
    # detector, different specific concerns.
    presence_by_key: Counter[str] = Counter()
    # Stash one title per key for human-readable rendering.
    title_by_key: dict[str, str] = {}

    for r in rows:
        sev = _parse_severity(r.severity_summary_json)
        points.append(SeverityTrendPointDTO(
            id=r.id,
            generated_at=_iso(r.generated_at),
            red=int(sev.get("RED") or 0),
            amber=int(sev.get("AMBER") or 0),
            yellow=int(sev.get("YELLOW") or 0),
        ))
        findings = _parse_findings(r.findings_json)
        # Dedup within a single report so one finding cited twice
        # doesn't double-count toward persistence.
        seen_in_this_report: set[str] = set()
        for f in findings:
            key = str(f.get("id") or f.get("detector") or "")
            if not key or key in seen_in_this_report:
                continue
            seen_in_this_report.add(key)
            presence_by_key[key] += 1
            if key not in title_by_key:
                detector = str(f.get("detector") or "")
                title = str(f.get("title") or "").strip()
                if detector and title:
                    title_by_key[key] = f"{detector} — {title}"
                elif title:
                    title_by_key[key] = title
                else:
                    title_by_key[key] = key

    report_count = len(rows)
    persistent: list[str] = []
    if report_count >= 2:
        threshold = report_count / 2.0  # ≥50%
        # Rank by descending frequency, then alphabetic for stability.
        ranked = sorted(
            presence_by_key.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )
        for key, count in ranked:
            if count >= threshold:
                label = title_by_key.get(key, key)
                persistent.append(f"{label} ({count} runs)")

    return SeverityTrendsDTO(
        points=points,
        days=days,
        report_count=report_count,
        most_persistent_findings=persistent,
    )


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
