"""Overview — plain-language plan-explainer route.

Thin wrapper over :func:`argosy.services.overview_assembler.build_overview`.
``GET /api/overview?user_id=ariel`` returns an :class:`OverviewResponse` whose
shape is the authoritative contract in the design spec (§3.3). Every magnitude
in chapter prose is resolver-sourced via the fact registry; the endpoint always
returns 200 (degrades gracefully — see the assembler).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.overview_assembler import OverviewModel, build_overview

router = APIRouter(prefix="/overview", tags=["overview"])


class FactRef(BaseModel):
    key: str
    value: float | None
    unit: str
    status: str
    display: str | None
    source_locator: str
    confidence: str | None


class VizPayload(BaseModel):
    kind: str
    data: dict


class YourMove(BaseModel):
    label: str
    href: str


class OverviewChapter(BaseModel):
    id: str
    title: str
    eyebrow: str
    headline: str
    degraded: bool
    facts: list[FactRef]
    viz: VizPayload
    drill_label: str
    drill_href: str
    your_move: YourMove | None


class OverviewActionsBanner(BaseModel):
    open_count: int
    href: str


class OverviewResponse(BaseModel):
    available: bool
    reason: str | None
    plan_version_id: int | None
    decision_run_id: int | None
    as_of: str | None
    chapters: list[OverviewChapter]
    actions_banner: OverviewActionsBanner


def _to_response(model: OverviewModel) -> OverviewResponse:
    return OverviewResponse(
        available=model.available,
        reason=model.reason,
        plan_version_id=model.plan_version_id,
        decision_run_id=model.decision_run_id,
        as_of=model.as_of,
        chapters=[
            OverviewChapter(
                id=c.id,
                title=c.title,
                eyebrow=c.eyebrow,
                headline=c.headline,
                degraded=c.degraded,
                facts=[
                    FactRef(
                        key=f.key,
                        value=f.value,
                        unit=f.unit,
                        status=f.status,
                        display=f.display,
                        source_locator=f.source_locator,
                        confidence=f.confidence,
                    )
                    for f in c.facts
                ],
                viz=VizPayload(kind=c.viz.kind, data=c.viz.data),
                drill_label=c.drill_label,
                drill_href=c.drill_href,
                your_move=(
                    YourMove(label=c.your_move.label, href=c.your_move.href)
                    if c.your_move is not None
                    else None
                ),
            )
            for c in model.chapters
        ],
        actions_banner=OverviewActionsBanner(
            open_count=model.actions_banner.open_count,
            href=model.actions_banner.href,
        ),
    )


@router.get("")
def get_overview(
    user_id: str = "ariel", db: Session = Depends(get_db)
) -> OverviewResponse:
    """Return the plain-language Overview story for ``user_id`` (always 200)."""
    return _to_response(build_overview(db, user_id=user_id))
