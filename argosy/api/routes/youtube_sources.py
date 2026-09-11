"""YouTube discovery input sources and their calibration statistics."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from argosy.services.youtube_intelligence import (
    list_youtube_sources,
    set_youtube_source_enabled,
    subscribe_youtube_source,
    sync_youtube_subscriptions,
)

router = APIRouter(prefix="/input-sources/youtube", tags=["input-sources"])


class AddYouTubeSourceRequest(BaseModel):
    reference: str = Field(min_length=3, max_length=2048)
    user_id: str = "ariel"


class UpdateYouTubeSourceRequest(BaseModel):
    enabled: bool
    user_id: str = "ariel"


@router.get("")
async def get_sources(user_id: str = Query("ariel")) -> dict:
    return await asyncio.to_thread(list_youtube_sources, user_id=user_id)


@router.post("", status_code=201)
async def add_source(body: AddYouTubeSourceRequest) -> dict:
    try:
        return await asyncio.to_thread(
            subscribe_youtube_source, body.reference, user_id=body.user_id
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sync")
async def sync_sources(user_id: str = Query("ariel")) -> dict:
    return await sync_youtube_subscriptions(user_id=user_id)


@router.patch("/{source_id}")
async def update_source(source_id: int, body: UpdateYouTubeSourceRequest) -> dict:
    try:
        return await asyncio.to_thread(
            set_youtube_source_enabled, source_id, body.enabled, user_id=body.user_id
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="YouTube source not found") from exc


__all__ = ["router"]
