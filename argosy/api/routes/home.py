"""Home routes — the FM first-greeting.

GET /api/home/greeting — server-side assembly of the greeting card the
client sees when opening the app (how you stand / what I need from you
/ what I'm watching). Pure projection of canonical state; see
``argosy/services/home_greeting.py`` for sources + classification.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.home_greeting import build_greeting

router = APIRouter(prefix="/home", tags=["home"])


class GreetingCtaDTO(BaseModel):
    label: str
    href: str


class GreetingNeedsYouItemDTO(BaseModel):
    id: str
    kind: str
    headline: str
    why_md: str
    cta: GreetingCtaDTO


class GreetingWatchingItemDTO(BaseModel):
    id: str
    headline: str
    note: str


class GreetingBookDTO(BaseModel):
    total_usd: float | None
    on_plan: bool
    on_plan_note: str
    fi_line: str


class GreetingResponse(BaseModel):
    greeting_name: str
    book: GreetingBookDTO
    needs_you: list[GreetingNeedsYouItemDTO]
    watching: list[GreetingWatchingItemDTO]
    quiet: bool
    next_review_local: str | None


@router.get("/greeting", response_model=GreetingResponse)
def get_greeting(
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> GreetingResponse:
    """The FM's first greeting — assembled server-side so the UI renders
    one payload with zero client-side triage."""
    return GreetingResponse(**build_greeting(db, user_id))
