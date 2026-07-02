"""Period-directive API — the team's assembled "here's your move this period".

``GET /api/period-directive`` returns ONE grouped verdict: the buy half (idle cash
→ the canonical engine, incl. the discovery sleeve) and the sell half (the NVDA
glide policy sell), stamped with a freshness record. The ``/proposals`` (and inbox)
"Your move this period" card projects this directly — one card, buy + sell + tax
note together, per SDD §1.6.

``?refresh=true`` is the on-demand "wait while I refresh stale inputs" path: it
refreshes stale FX before advising so the directive is never computed on stale
data. Omitted/false is the read-only steady state.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.period_directive import assemble_period_directive

router = APIRouter(prefix="/period-directive", tags=["period-directive"])


@router.get("")
def get_period_directive(
    user_id: str = Query("ariel"),
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the assembled period directive for ``user_id``.

    Always 200: a quiet directive (nothing due) is the common steady state, not an
    error. ``refresh=true`` refreshes stale FX first.
    """
    directive = assemble_period_directive(db=db, user_id=user_id, refresh=refresh)
    return directive.to_dict()
