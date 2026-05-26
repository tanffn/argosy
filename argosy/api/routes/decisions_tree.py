"""FM-rooted agent-tree view for one decision_run (T0.5 + T4.4).

GET /api/decisions/{decision_run_id}/agent-tree?user_id=...

Thin HTTP wrapper around
``argosy.services.agent_tree_builder.build_agent_tree``. The builder is
pure / sync and returns a nested dataclass DAG; this route serializes it
to JSON via ``dataclasses.asdict`` (FastAPI does not auto-encode plain
dataclasses).

Error mapping:

* Unknown ``decision_run_id`` (or wrong owner): ``ValueError`` -> 404
  (same status the missing-row case returns so a cross-tenant probe
  can't distinguish).
* Non-synthesis ``decision_kind`` (T4.4): builder returns an
  ``AgentTreeResponse`` with ``root=None`` + a populated
  ``unsupported_reason``; route returns 200 so the UI can render a
  kind-appropriate placeholder instead of crashing.

The ``user_id`` query param is enforced by ownership-check on the
``decision_runs`` row so a different tenant cannot probe a foreign run
id.

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
    """Return the FM-rooted DAG for one decision_run as JSON.

    Status mapping:
      * 200 + ``root != None``           — synthesis run, full DAG.
      * 200 + ``root = None`` + reason   — non-synthesis run (T4.4). UI
                                           shows a kind-appropriate
                                           placeholder.
      * 404 — row doesn't exist OR belongs to a different ``user_id``.
    """
    # Ownership check first — same 404 the missing-row case returns so a
    # cross-tenant probe can't distinguish.
    run = db.get(DecisionRun, decision_run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(status_code=404, detail="decision run not found")

    try:
        tree = build_agent_tree(db, decision_run_id)
    except ValueError as exc:
        # T4.4: the only remaining ValueError case is "row vanished
        # between ownership check and builder call" (race). Unknown
        # decision_kinds no longer raise — the builder returns a
        # root=None DTO instead.
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # dataclasses.asdict walks the nested AgentNode / AdapterNode tree
    # recursively. All leaf types are JSON-safe (int, str, float, None,
    # bool) — cost_usd is already float-coerced inside the builder.
    return asdict(tree)


__all__ = ["router"]
