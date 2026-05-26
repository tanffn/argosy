"""FM-rooted agent-tree view for one decision_run (T0.5).

GET /api/decisions/{decision_run_id}/agent-tree?user_id=...

Thin HTTP wrapper around
``argosy.services.agent_tree_builder.build_agent_tree``. The builder is
pure / sync and returns a nested dataclass DAG; this route serializes it
to JSON via ``dataclasses.asdict`` (FastAPI does not auto-encode plain
dataclasses) and maps the builder's ``ValueError`` (unknown run id or
non-synthesis kind) onto HTTP 404.

The ``user_id`` query param is enforced by ownership-check on the
``decision_runs`` row so a different tenant cannot probe a foreign run
id (same 404 a missing row would get — no existence leakage).

The session is opened via the sync ``get_db`` dependency from
``argosy.api.routes.plan`` to keep the call synchronous; the builder
itself is sync. Tests override this dependency in ``conftest.py``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.services.agent_tree_builder import build_agent_tree
from argosy.state.models import DecisionRun

router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.get("/{decision_run_id}/agent-tree")
def get_agent_tree(
    decision_run_id: int,
    user_id: str = "ariel",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the FM-rooted DAG for one synthesis decision_run as JSON.

    404 cases:
      * row doesn't exist
      * row exists but belongs to a different ``user_id`` (no leak)
      * row exists but its ``decision_kind`` isn't a synthesis kind
        (builder raises ``ValueError``)
    """
    # Ownership check first — same 404 the missing-row case returns so a
    # cross-tenant probe can't distinguish.
    run = db.get(DecisionRun, decision_run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(status_code=404, detail="decision run not found")

    try:
        tree = build_agent_tree(db, decision_run_id)
    except ValueError as exc:
        # Either the run vanished between the ownership check and the
        # builder call (race) or the decision_kind isn't synthesis.
        # Either way, 404 is the right answer for the caller.
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # dataclasses.asdict walks the nested AgentNode / AdapterNode tree
    # recursively. All leaf types are JSON-safe (int, str, float, None,
    # bool) — cost_usd is already float-coerced inside the builder.
    return asdict(tree)


__all__ = ["router"]
