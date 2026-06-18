"""Persistence helpers for Layer-2 change-requests + their negotiation threads.

Maps the in-memory ChangeRequest / LadderResult onto the ChangeRequestRow /
DialogueTurnRow tables (Phase-1c schema, reused by Phase 2), and reloads a
thread for the Replay view. The terminal TerminalState is written onto the
change_request's status so a settled dispute cannot silently reopen.

NOTE: the persisted change_requests table is keyed by ``plan_id`` (FK ->
plan_versions, NOT NULL) — there is no standalone user_id column. The store
API therefore takes ``plan_id`` directly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.orchestrator.flows.negotiation_ladder import LadderResult
from argosy.quality.change_adjudication import (
    Author, AuthorKind, ChangeRequest,
)
from argosy.state.models import ChangeRequestRow, DialogueTurnRow


def _encode_author(author: Author) -> str:
    if author.kind is AuthorKind.USER:
        return "user"
    return f"agent:{author.role}"


def open_change_request(
    session: Session, *, plan_id: int, cr: ChangeRequest,
) -> int:
    row = ChangeRequestRow(
        plan_id=plan_id,
        target_node_key=cr.target_node_key,
        author=_encode_author(cr.author),
        kind=cr.kind.value,
        payload_json=json.dumps(cr.payload, default=str),
        rationale=cr.rationale or "",
        status="proposed",
        round_count=0,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.id


def record_ladder_result(
    session: Session, *, change_request_id: int, result: LadderResult,
) -> None:
    """Persist every LadderTurn and stamp the typed terminal state on the row."""
    for t in result.turns:
        session.add(DialogueTurnRow(
            change_request_id=change_request_id,
            round=t.round,
            speaker=t.speaker.value,
            stance=t.stance.value,
            text=t.text,
            cited_nodes_json=json.dumps(t.cited_nodes, default=str),
            created_at=datetime.now(timezone.utc),
        ))
    row = session.get(ChangeRequestRow, change_request_id)
    if row is not None:
        row.status = result.terminal_state.value
        row.round_count = max((t.round for t in result.turns), default=0)
        row.terminal_reason = result.user_question or (
            result.arbiter_class.value if result.arbiter_class else None
        )
        row.updated_at = datetime.now(timezone.utc)
    session.commit()


def load_thread(session: Session, *, change_request_id: int) -> dict:
    """Reconstruct the full replayable thread for a change-request."""
    row = session.get(ChangeRequestRow, change_request_id)
    if row is None:
        raise KeyError(f"change_request {change_request_id} not found")
    turns = session.execute(
        select(DialogueTurnRow)
        .where(DialogueTurnRow.change_request_id == change_request_id)
        .order_by(DialogueTurnRow.id)
    ).scalars().all()
    return {
        "id": row.id,
        "target_node_key": row.target_node_key,
        "author": row.author,
        "kind": row.kind,
        "status": row.status,
        "terminal_reason": row.terminal_reason,
        "turns": [
            {
                "round": t.round,
                "speaker": t.speaker,
                "stance": t.stance,
                "text": t.text,
                "cited_nodes": json.loads(t.cited_nodes_json or "[]"),
            }
            for t in turns
        ],
    }


class ReopenError(Exception):
    """A settled (terminal) change-request cannot silently reopen."""


# Statuses past which a change-request is settled and must not reopen.
_TERMINAL_STATUSES = {
    "A_conceded", "B_conceded", "arbiter_ruled", "superseded",
}


def supersede_change_request(
    session: Session, *, change_request_id: int, reason: str = "",
) -> None:
    row = session.get(ChangeRequestRow, change_request_id)
    if row is None:
        raise KeyError(f"change_request {change_request_id} not found")
    row.status = "superseded"
    row.terminal_reason = reason or row.terminal_reason
    row.updated_at = datetime.now(timezone.utc)
    session.commit()


def assert_reopenable(session: Session, *, change_request_id: int) -> None:
    """Raise ReopenError if the change-request is already in a terminal state."""
    row = session.get(ChangeRequestRow, change_request_id)
    if row is None:
        raise KeyError(f"change_request {change_request_id} not found")
    if row.status in _TERMINAL_STATUSES:
        raise ReopenError(
            f"change_request {change_request_id} is terminal ({row.status}); "
            "a settled dispute cannot reopen"
        )


__all__ = [
    "open_change_request", "record_ladder_result", "load_thread",
    "supersede_change_request", "assert_reopenable", "ReopenError",
]
