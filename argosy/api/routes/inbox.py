"""Inbox API — the server-owned "what needs me now?" feed.

``GET /api/inbox`` returns ONE ordered list of typed items + quiet-state
liveness metadata. The browser renders it as-is; it computes no queue
membership, rank, materiality, or rank-reason (those are domain decisions, made
in ``argosy.services.inbox``). This is the single source the ``/inbox`` page
projects.

``?debug=true`` additionally exposes the per-item policy ``signals``, the raw
sort key, and the ``dropped`` (suppressed / deduped / errored) list — for the
Decisions-tab debug view, never the client surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.inbox.service import build_inbox

router = APIRouter(prefix="/inbox", tags=["inbox"])


@router.get("")
def get_inbox(
    user_id: str = Query("ariel"),
    debug: bool = Query(False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the ranked inbox feed for ``user_id``.

    Always returns 200 with a well-formed feed (a quiet/empty feed is the
    common steady state, not an error). A single failing source is isolated
    inside ``build_inbox`` and recorded in ``dropped`` rather than failing the
    request.
    """
    feed = build_inbox(db, user_id=user_id)
    return feed.to_dict(debug=debug)
