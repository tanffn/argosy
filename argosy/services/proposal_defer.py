"""Defer a TRADE proposal — the ``proposals``-table equivalent of
``defer_action_proposal`` (SDD §10; feedback: a promised defer must be
PERSISTED, not session-local).

Defer means "not now, revisit on <date>": the row is parked back to
``cooling`` with ``cooling_off_until`` set to the resurface date, and the
existing :class:`~argosy.orchestrator.loops.process_cooling.ProcessCoolingLoop`
re-queues it to ``awaiting_human`` when the date passes — no new loop, no new
status. The reason lands in ``ProposalHistory`` (audit trail), encoded the
same way ``defer_action_proposal`` encodes it (``"defer_until=<iso>; <note>"``)
so the two defer surfaces read alike.

Deliberately NOT supported: limited-account proposals. Their cooling path
auto-promotes to approved/executed (paper or live T0/T1) instead of returning
to the human queue — deferring one would schedule an execution, not a
re-review. The guard fails loud.

The mutator is session-agnostic (it only touches the row and returns the
history row to add) so the async API route and sync callers (scripts, tests)
share ONE implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from argosy.decisions.proposals import ProposalStatus, assert_legal
from argosy.services.proposal_expiry import DEFAULT_TTL_HOURS
from argosy.state.models import Proposal as ProposalRow
from argosy.state.models import ProposalHistory


class DeferNotSupportedError(ValueError):
    """Raised when the row's lifecycle cannot round-trip through a defer."""


@dataclass(frozen=True)
class DeferResult:
    history: ProposalHistory
    cooling_off_until: datetime
    expires_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def defer_trade_proposal(
    row: ProposalRow,
    *,
    defer_until: date,
    note: str | None = None,
    actor_user_id: str,
    now: datetime | None = None,
) -> DeferResult:
    """Park ``row`` (must be ``awaiting_human``) until ``defer_until``.

    Mutates the row in place; the caller adds ``result.history`` to its
    session and commits. Raises :class:`DeferNotSupportedError` for
    limited-account rows and ``IllegalTransitionError`` for any status other
    than ``awaiting_human`` (the only state a user can defer from).
    """
    now = now or _utcnow()

    if row.account_class == "limited":
        raise DeferNotSupportedError(
            "defer is unsupported for limited-account proposals: their cooling "
            "path auto-promotes to execution instead of returning to review"
        )

    src = ProposalStatus(row.status)
    assert_legal(src, ProposalStatus.COOLING)

    cooling_until = datetime.combine(defer_until, time.min, tzinfo=UTC)

    parts = [f"defer_until={defer_until.isoformat()}"]
    if note:
        parts.append(note)
    encoded = "; ".join(parts)

    row.status = ProposalStatus.COOLING.value
    row.cooling_off_until = cooling_until
    row.updated_at = now
    # A deferred proposal must survive its own defer window and still leave a
    # review window after it resurfaces — otherwise the funnel expiry sweep
    # (COOLING is in its open set) would kill it mid-defer.
    resurface_ttl = cooling_until + timedelta(hours=DEFAULT_TTL_HOURS)
    existing_expiry = row.expires_at
    if existing_expiry is not None and existing_expiry.tzinfo is None:
        # SQLite hands naive UTC back even for DateTime(timezone=True) columns.
        existing_expiry = existing_expiry.replace(tzinfo=UTC)
    if existing_expiry is not None and existing_expiry < resurface_ttl:
        row.expires_at = resurface_ttl

    history = ProposalHistory(
        proposal_id=row.id,
        status=row.status,
        transitioned_at=now,
        transitioned_by=f"user:{actor_user_id}",
        note=f"deferred: {encoded}",
    )
    return DeferResult(
        history=history,
        cooling_off_until=cooling_until,
        expires_at=row.expires_at,
    )


__all__ = ["DeferNotSupportedError", "DeferResult", "defer_trade_proposal"]
