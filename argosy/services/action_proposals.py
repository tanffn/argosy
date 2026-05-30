"""Action-proposals service — Spec E commit #6 (/proposals UI extension).

Service-layer CRUD + lifecycle helpers for ``action_proposals`` rows.
Pair to the action-proposer runner (writer) and the /api/proposals/
actions routes (consumer). Implements the four user-facing decision
verbs from spec §6:

  * ``list_open_action_proposals(session, user_id)``
        — read all ``status='open'`` rows for the page render,
          severity-sorted critical > warning > info, then by
          ``surfaced_at`` descending.
  * ``accept_action_proposal(session, id, *, custom_payload=None)``
        — flip ``status='accepted'`` + ``execution_state='accepted_
          pending_user_action'`` + stamp ``decided_at=now``. When the
          caller supplies ``custom_payload`` (the Customize flow) the
          edited payload is persisted into ``suggested_payload``; the
          original is NOT preserved per the codex-light v1 contract
          (spec §1.2 reserves ``customized_payload`` for a future
          schema bump — this commit overwrites in place + writes a
          ``decided_by_user_note`` record of the edit).
  * ``defer_action_proposal(session, id, defer_until, note=None)``
        — flip to ``status='deferred'``; store ``defer_until`` as the
          encoded ``decided_by_user_note`` (``"defer_until=<iso>; <note>"``)
          since the v1 migration ships no dedicated ``defer_until``
          column. The housekeeping loop (out of v1 scope) will re-open
          deferred rows on schedule.
  * ``reject_action_proposal(session, id, reason=None)``
        — flip to ``status='rejected'`` + write the reason note.

Capability-boundary invariant (codex BLOCKER #1 / spec §2.2.1)
==============================================================

The ``execution_state`` column has THREE values:

  * ``proposed``                    — writer-only; the row is open.
  * ``accepted_pending_user_action`` — Accept handler only; means
    "user clicked Accept; money has NOT moved; the row is queued for
    the existing proposals -> action_engine -> orders pipeline which
    has its own user-confirmation gates".
  * ``dismissed``                   — terminal; defer/reject targets.

There is NO code path in this module that advances a row to a state
where money moves. The Accept handler ONLY flips the column to
``accepted_pending_user_action``; downstream materialization (writing
an allocation_actions row, firing a plan_synthesis job, etc.) is the
responsibility of a separate explicit handler that the user invokes
through the existing per-kind pipelines. The
``test_action_proposals_service.py`` no-execution invariant pin walks
the service module and asserts no import / no reference to broker /
fx_execution / order placement modules.

Status transitions enforced (spec §1.2):

  open -> accepted          (only via accept_action_proposal)
  open -> deferred          (only via defer_action_proposal)
  open -> rejected          (only via reject_action_proposal)

Any other transition raises ``InvalidProposalStateError``. The route
layer surfaces this as a 409 (Conflict) HTTP response.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.state.models import ActionProposal

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors (translated to 404/409 at the route layer)
# ---------------------------------------------------------------------------


class ProposalNotFoundError(LookupError):
    """No row matching (id, user_id). Translated to 404 at the route."""


class InvalidProposalStateError(ValueError):
    """The proposal is not in the right state for the requested
    transition (e.g. accepting a row that's already deferred). The
    spec §1.2 lifecycle is the source of truth; the route layer
    translates to 409 (Conflict)."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The terminal status set — rows in any of these states are NOT
#: candidates for accept/defer/reject. The /proposals UI filters
#: ``status='open'`` so these never appear in the queue; the service
#: still defends against a stale UI POST'ing to a just-decided row.
_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "accepted", "deferred", "rejected", "superseded",
})


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def list_open_action_proposals(
    session: Session,
    user_id: str,
) -> list[ActionProposal]:
    """Return all ``status='open'`` rows for the user.

    Sort order matches spec §6.1: severity-first (critical > warning >
    info), then most-recent ``surfaced_at`` first within each band.
    The UI doesn't have to re-sort.

    Note: ``expires_at`` filtering is INTENTIONALLY not applied here
    — the housekeeping loop (spec §1.6, out of v1 scope) transitions
    expired open rows out of 'open'. Until that loop ships, the UI
    is the only consumer and shows a stale row at worst; the explicit
    accept/defer/reject by the user still works.
    """
    stmt = (
        select(ActionProposal)
        .where(ActionProposal.user_id == user_id)
        .where(ActionProposal.status == "open")
    )
    rows = list(session.execute(stmt).scalars().all())
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    rows.sort(
        key=lambda r: (
            severity_rank.get(r.severity, 99),
            # surfaced_at descending => negate via sort-key reverse below
            -(r.surfaced_at.timestamp() if r.surfaced_at else 0.0),
        )
    )
    return rows


def get_action_proposal(
    session: Session,
    proposal_id: int,
    *,
    user_id: str | None = None,
) -> ActionProposal:
    """Fetch one row by id; raise ProposalNotFoundError if missing or
    if ``user_id`` is supplied and doesn't match.

    The user_id check is the multi-tenant guard so an Accept POST from
    user A cannot mutate user B's row. The route layer always passes
    a user_id; tests can omit for raw fetches.
    """
    row = session.get(ActionProposal, proposal_id)
    if row is None:
        raise ProposalNotFoundError(f"action_proposal id={proposal_id} not found")
    if user_id is not None and row.user_id != user_id:
        # Don't leak existence across tenants — same 404 shape.
        raise ProposalNotFoundError(f"action_proposal id={proposal_id} not found")
    return row


# ---------------------------------------------------------------------------
# Lifecycle helpers — Accept / Defer / Reject
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _assert_open(row: ActionProposal) -> None:
    """Guard rail: a transition only proceeds if the row is still open.

    Routes translate ``InvalidProposalStateError`` to HTTP 409 so a
    stale UI tab doesn't silently overwrite a freshly-decided row.
    """
    if row.status != "open":
        raise InvalidProposalStateError(
            f"action_proposal id={row.id} is in status={row.status!r}; "
            "only 'open' rows accept lifecycle transitions"
        )


def accept_action_proposal(
    session: Session,
    proposal_id: int,
    *,
    user_id: str | None = None,
    custom_payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ActionProposal:
    """Mark a proposal accepted.

    Two paths:

      * **Plain Accept** (``custom_payload=None``) — the user accepts
        the LLM's suggested_payload unchanged.
      * **Customize Accept** (``custom_payload`` supplied) — the user
        edited the form before clicking Accept; the edited payload is
        persisted in place of the original. The original is NOT
        preserved by v1 (spec §1.2's dedicated ``customized_payload``
        column lands in a future migration); a structured
        ``decided_by_user_note='customized: <json>'`` records the
        edit for audit.

    In both paths:

      * ``status`` flips to ``'accepted'`` — the partial-unique dedup
        slot is released so the proposer can re-fire if the situation
        re-emerges.
      * ``execution_state`` flips to
        ``'accepted_pending_user_action'`` — the Accept means "user
        consented to the action"; money has NOT moved. Downstream
        materialization (write allocation_actions, fire plan_synthesis)
        is a separate explicit step in the per-kind pipeline (out of
        v1 scope for this commit).
      * ``decided_at`` is stamped.

    Capability-boundary invariant: this function does NOT call any
    broker / fx_execution / order-placement code. The
    ``test_action_proposals_service.py`` no-execution invariant pin
    walks the module and asserts no such reference.
    """
    if now is None:
        now = _utcnow()
    row = get_action_proposal(session, proposal_id, user_id=user_id)
    _assert_open(row)

    if custom_payload is not None:
        # Persist the edited payload in place. Round-trip through json
        # so a non-serializable value blows up at the API surface
        # rather than at SQLite write time.
        row.suggested_payload = json.dumps(custom_payload, default=str)
        row.decided_by_user_note = (
            f"customized: {json.dumps(custom_payload, default=str)}"
        )
    row.status = "accepted"
    row.execution_state = "accepted_pending_user_action"
    row.decided_at = now
    session.flush()
    session.commit()
    _log.info(
        "action_proposals.accept",
        extra={
            "proposal_id": row.id,
            "user_id": row.user_id,
            "kind": row.kind,
            "customized": custom_payload is not None,
        },
    )
    return row


def defer_action_proposal(
    session: Session,
    proposal_id: int,
    defer_until: date | datetime | str | None = None,
    *,
    user_id: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> ActionProposal:
    """Mark a proposal deferred.

    ``defer_until`` is the date the user wants the proposal to
    re-surface. The housekeeping loop (spec §1.6, out of v1 scope)
    will flip ``deferred`` rows back to ``'open'`` when the date
    passes. Until that loop ships, deferred rows are inert.

    Because the v1 schema (migration 0055) ships no dedicated
    ``defer_until`` column, the date is encoded into
    ``decided_by_user_note`` as ``"defer_until=<iso>; <free-text>"``.
    A future migration adds a dedicated column; this string format
    is forward-compatible (the housekeeping loop parses it).

    ``execution_state`` is INTENTIONALLY not touched here — defer
    means "not now, maybe later"; the row hasn't been accepted, so
    leaving the column at ``'proposed'`` keeps the no-execution
    invariant clean (codex BLOCKER #1).
    """
    if now is None:
        now = _utcnow()
    row = get_action_proposal(session, proposal_id, user_id=user_id)
    _assert_open(row)

    # Normalize defer_until -> iso string for the note encoding.
    defer_iso: str | None = None
    if defer_until is not None:
        if isinstance(defer_until, str):
            defer_iso = defer_until
        elif isinstance(defer_until, datetime):
            defer_iso = defer_until.date().isoformat()
        elif isinstance(defer_until, date):
            defer_iso = defer_until.isoformat()
        else:  # pragma: no cover — defensive
            raise TypeError(
                f"defer_until must be date | datetime | str, got "
                f"{type(defer_until).__name__}"
            )

    parts: list[str] = []
    if defer_iso is not None:
        parts.append(f"defer_until={defer_iso}")
    if note:
        parts.append(note)
    encoded = "; ".join(parts) if parts else "deferred (no date / note)"

    row.status = "deferred"
    row.decided_at = now
    row.decided_by_user_note = encoded
    # execution_state stays at 'proposed' — defer is not consent.
    session.flush()
    session.commit()
    _log.info(
        "action_proposals.defer",
        extra={
            "proposal_id": row.id,
            "user_id": row.user_id,
            "kind": row.kind,
            "defer_until": defer_iso,
        },
    )
    return row


def reject_action_proposal(
    session: Session,
    proposal_id: int,
    *,
    user_id: str | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> ActionProposal:
    """Mark a proposal rejected.

    Status flips to ``'rejected'``; ``execution_state`` flips to
    ``'dismissed'`` (terminal — the row will never advance to
    ``'accepted_pending_user_action'``); the reason note is stored
    on ``decided_by_user_note`` (NULL allowed if the user didn't
    type one).
    """
    if now is None:
        now = _utcnow()
    row = get_action_proposal(session, proposal_id, user_id=user_id)
    _assert_open(row)

    row.status = "rejected"
    row.execution_state = "dismissed"
    row.decided_at = now
    row.decided_by_user_note = reason or None
    session.flush()
    session.commit()
    _log.info(
        "action_proposals.reject",
        extra={
            "proposal_id": row.id,
            "user_id": row.user_id,
            "kind": row.kind,
            "had_reason": bool(reason),
        },
    )
    return row


# ---------------------------------------------------------------------------
# DTO shaping (used by the route layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionProposalView:
    """Plain-data view of an ActionProposal row for JSON serialization.

    The route layer wraps this in a Pydantic model; isolating the shape
    here keeps the service module Pydantic-free.
    """

    id: int
    user_id: str
    kind: str
    severity: str
    status: str
    execution_state: str
    summary: str
    rationale_md: str
    suggested_payload: dict[str, Any]
    surfaced_at: str
    expires_at: str
    decided_at: str | None
    decided_by_user_note: str | None
    source_flag_id: int | None
    source_observation_id: int | None
    source_inferred_event_id: int | None


def to_view(row: ActionProposal) -> ActionProposalView:
    """Project an ORM row to a JSON-friendly view.

    Parses ``suggested_payload`` from JSON text. A malformed payload
    is replaced with an empty dict + the row is logged (this should
    never happen — the writer round-trips through json.dumps — but
    the UI shouldn't crash on a single bad row).
    """
    try:
        payload = json.loads(row.suggested_payload) if row.suggested_payload else {}
    except json.JSONDecodeError:
        _log.warning(
            "action_proposals.view.malformed_payload",
            extra={"proposal_id": row.id},
        )
        payload = {}
    if not isinstance(payload, dict):
        payload = {"_raw": payload}

    return ActionProposalView(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        severity=row.severity,
        status=row.status,
        execution_state=row.execution_state,
        summary=row.summary,
        rationale_md=row.rationale_md,
        suggested_payload=payload,
        surfaced_at=row.surfaced_at.isoformat() if row.surfaced_at else "",
        expires_at=row.expires_at.isoformat() if row.expires_at else "",
        decided_at=row.decided_at.isoformat() if row.decided_at else None,
        decided_by_user_note=row.decided_by_user_note,
        source_flag_id=row.source_flag_id,
        source_observation_id=row.source_observation_id,
        source_inferred_event_id=row.source_inferred_event_id,
    )


__all__ = [
    "ActionProposalView",
    "InvalidProposalStateError",
    "ProposalNotFoundError",
    "accept_action_proposal",
    "defer_action_proposal",
    "get_action_proposal",
    "list_open_action_proposals",
    "reject_action_proposal",
    "to_view",
]
