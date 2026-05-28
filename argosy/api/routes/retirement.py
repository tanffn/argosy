"""Umbrella router for retirement-engine endpoints (Wave 0+).

Wave 0 surfaces only the sources + reference primitives. Later waves
register additional endpoints on this same ``/api/retirement/*`` prefix
without touching the cross-cutting plumbing here.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.retirement.citations import as_dict
from argosy.services.retirement.reference import ResolveError, resolve
from argosy.services.retirement.sources import load_sources

router = APIRouter(prefix="/retirement", tags=["retirement"])


class SourceDTO(BaseModel):
    id: str
    title: str
    url: str
    as_of: str
    kind: str
    notes: str = ""


class SourcesResponse(BaseModel):
    sources: dict[str, SourceDTO]


@router.get("/sources", response_model=SourcesResponse)
def get_sources() -> SourcesResponse:
    reg = load_sources()
    return SourcesResponse(
        sources={
            sid: SourceDTO(
                id=s.id,
                title=s.title,
                url=s.url,
                as_of=s.as_of,
                kind=s.kind,
                notes=s.notes,
            )
            for sid, s in reg.sources.items()
        },
    )


@router.get("/sources/{source_id}", response_model=SourceDTO)
def get_source(source_id: str) -> SourceDTO:
    reg = load_sources()
    s = reg.get(source_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"unknown source: {source_id!r}")
    return SourceDTO(
        id=s.id,
        title=s.title,
        url=s.url,
        as_of=s.as_of,
        kind=s.kind,
        notes=s.notes,
    )


@router.get("/reference/{key}")
def get_reference(
    key: str,
    user_id: str,
    db: Session = Depends(get_db),
) -> dict:
    try:
        v = resolve(key, user_id=user_id, session=db)
    except ResolveError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return as_dict(v)
