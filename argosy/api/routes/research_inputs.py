"""Inspect the same attributable evidence available to decision consumers."""
import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.config import get_settings
from argosy.services.research_inputs import collect_research_inputs
from argosy.state.db import create_sync_engine

router = APIRouter(prefix="/input-sources/research", tags=["input-sources"])


def _read(user_id: str, ticker: str) -> dict:
    engine = create_sync_engine(str(get_settings().database_url).replace("+aiosqlite", ""))
    try:
        with Session(engine) as session:
            items = collect_research_inputs(session, user_id=user_id, ticker=ticker)
            return {"ticker": ticker.upper(), "items": items}
    finally:
        engine.dispose()


@router.get("")
async def get_research_inputs(
    ticker: str = Query(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9.^/=-]+$"),
    user_id: str = Query("ariel"),
) -> dict:
    return await asyncio.to_thread(_read, user_id, ticker)


class AddSource(BaseModel):
    user_id: str = "ariel"
    name: str = Field(min_length=1, max_length=256)
    kind: Literal["rss", "sec13d", "sec13f", "manual"]
    reference: str = Field(min_length=1, max_length=2048)
    cadence_hours: int = Field(default=24, ge=1, le=2160)
    priority: int = Field(default=50, ge=0, le=100)


class UpdateSource(BaseModel):
    user_id: str = "ariel"
    enabled: bool | None = None
    cadence_hours: int | None = Field(default=None, ge=1, le=2160)
    priority: int | None = Field(default=None, ge=0, le=100)


class ManualDocument(BaseModel):
    user_id: str = "ariel"
    source_id: int
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(default="", max_length=2048)
    body: str = Field(min_length=100, max_length=150000)


@router.get("/sources")
def get_sources(user_id: str = "ariel"):
    from argosy.services.research_catalog import research_session, list_sources
    with research_session() as session:
        return {"sources": list_sources(session, user_id), "daily_fleet_limit": 3}


@router.post("/sources", status_code=201)
def add_source(body: AddSource):
    from argosy.services.research_catalog import research_session, upsert_source
    try:
        with research_session() as session:
            source = upsert_source(session, **body.model_dump())
            session.commit()
            return {"id": source.id}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/sources/{source_id}")
def update_source(source_id: int, body: UpdateSource):
    from argosy.services.research_catalog import research_session
    from argosy.state.research_models import ResearchSource
    with research_session() as session:
        source = session.get(ResearchSource, source_id)
        if source is None or source.user_id != body.user_id:
            raise HTTPException(404, "Source not found")
        if source.kind in {"youtube", "existing"}:
            raise HTTPException(422, "Manage this source in its existing subscription or job controls")
        for key, value in body.model_dump(exclude_none=True, exclude={"user_id"}).items():
            setattr(source, key, value)
        session.commit()
        return {"id": source.id, "enabled": source.enabled}


@router.post("/documents", status_code=201)
def add_document(body: ManualDocument):
    from argosy.services.research_catalog import research_session, enqueue, digest
    from argosy.state.research_models import ResearchSource
    with research_session() as session:
        source = session.get(ResearchSource, body.source_id)
        if source is None or source.user_id != body.user_id or source.kind != "manual":
            raise HTTPException(404, "Manual source not found")
        # Pasted documents use their supplied body, never silently fetch a different page.
        item, created = enqueue(session, source, external_id="manual:" + digest(body.url, body.title, body.body),
                                title=body.title, url=body.url, body=body.body)
        session.commit()
        return {"id": item.id, "created": created, "status": item.status}


@router.get("/items")
def get_items(user_id: str = "ariel", source_id: int | None = None):
    import json
    from argosy.services.research_catalog import research_session
    from argosy.state.research_models import ResearchItem, ResearchClaim, ResearchReviewRequest
    with research_session() as session:
        query = select(ResearchItem).where(ResearchItem.user_id == user_id)
        if source_id is not None:
            query = query.where(ResearchItem.source_id == source_id)
        items = session.scalars(query.order_by(ResearchItem.observed_at.desc()).limit(50)).all()
        return {"items": [{"id": i.id, "source_id": i.source_id, "title": i.title, "url": i.url,
                           "status": i.status, "error": i.error, "published_at": i.published_at,
                           "observed_at": i.observed_at,
                           "summary": (json.loads(i.analysis_json).get("synthesis") or {}).get("executive_summary"),
                           "claims": [{"id": c.id, "ticker": c.ticker, "statement": c.statement,
                                       "due_at": c.due_at, "outcome": json.loads(c.outcome_json) if c.outcome_json else None}
                                      for c in session.scalars(select(ResearchClaim).where(ResearchClaim.item_id == i.id))],
                           "reviews": [{"ticker": r.ticker, "state": r.state, "reason": r.reason,
                                        "result": json.loads(r.result_json)} for r in session.scalars(
                                            select(ResearchReviewRequest).where(ResearchReviewRequest.item_id == i.id))]}
                          for i in items]}


@router.post("/items/{item_id}/retry")
def retry_item(item_id: str, user_id: str = "ariel"):
    from argosy.services.research_catalog import research_session
    from argosy.state.research_models import ResearchItem
    with research_session() as session:
        item = session.get(ResearchItem, item_id)
        if item is None or item.user_id != user_id:
            raise HTTPException(404, "Research item not found")
        if item.status != "failed":
            raise HTTPException(409, "Only failed items can be retried")
        item.status = "queued"
        item.attempts = 0
        item.next_attempt_at = None
        session.commit()
        return {"id": item.id, "status": item.status}
