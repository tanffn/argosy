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


class GreetingAckDTO(BaseModel):
    """Payload for confirming a looks-executed plan action item through the
    EXISTING ack endpoint (kind == 'action_item_confirm' items only)."""

    method: str
    endpoint: str
    content_fingerprint: str
    user_id: str


class GreetingNeedsYouItemDTO(BaseModel):
    id: str
    kind: str
    headline: str
    why_md: str | None = None
    cta: GreetingCtaDTO | None = None
    # Presentation tone: "decision" = the client must decide something;
    # "confirm" = Argosy did the work / found the evidence and only needs
    # a one-click confirmation (the action_item_confirm entries).
    tone: str = "decision"
    # Present only on kind='action_item_confirm' (Argosy found execution
    # evidence; the client confirms — never auto-acked).
    ack: GreetingAckDTO | None = None


class GreetingWatchingItemDTO(BaseModel):
    id: str
    headline: str
    note: str


class GreetingBookDTO(BaseModel):
    total_usd: float | None
    on_plan: bool
    on_plan_note: str
    fi_line: str
    # ISO date of the snapshot behind ``total_usd`` (None when no snapshot).
    as_of: str | None = None


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
