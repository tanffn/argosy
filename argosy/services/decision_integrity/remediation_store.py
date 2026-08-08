"""Persist + query remediation_requests rows (blocking provenance objects)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.agents.remediation import RemediationRequest
from argosy.logging import get_logger
from argosy.state.models import RemediationRequestRecord

log = get_logger(__name__)

OPEN = "open"
RESOLVED = "resolved"
OVERRIDDEN = "overridden"

# Only kinds whose condition a fresh *vintage* pass actually re-verifies.
# data_integrity / facilitator_condition must NEVER be cleared by vintage
# alone — that would launder the TRLV failure class (iter-2 item 3).
AUTO_RESOLVE_ON_VINTAGE_PASS: frozenset[str] = frozenset(
    {
        "vintage_stale",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def persist_remediation_requests(
    session: Session,
    *,
    user_id: str,
    requests: Sequence[RemediationRequest | dict[str, Any]],
    decision_run_id: int | None = None,
    agent_report_id: int | None = None,
    default_ticker: str | None = None,
    status: str = OPEN,
) -> list[RemediationRequestRecord]:
    """Insert one row per request. Returns the persisted ORM rows."""
    rows: list[RemediationRequestRecord] = []
    for req in requests:
        if isinstance(req, RemediationRequest):
            kind = req.kind
            target_role = req.target_role
            reason = req.reason
            ticker = req.ticker or default_ticker
        else:
            kind = str(req.get("kind") or "data_refresh")
            target_role = str(req.get("target_role") or "unknown")
            reason = str(req.get("reason") or "")
            ticker = req.get("ticker") or default_ticker
        row = RemediationRequestRecord(
            user_id=user_id,
            ticker=(str(ticker).upper() if ticker else None),
            decision_run_id=decision_run_id,
            agent_report_id=agent_report_id,
            kind=kind,
            target_role=target_role,
            reason=reason,
            status=status,
            created_at=_utcnow(),
        )
        session.add(row)
        rows.append(row)
    if rows:
        session.flush()
        log.info(
            "remediation_store.persisted",
            count=len(rows),
            user_id=user_id,
            decision_run_id=decision_run_id,
            kinds=[r.kind for r in rows],
        )
    return rows


def list_open_remediations(
    session: Session,
    *,
    user_id: str,
    ticker: str | None = None,
    decision_run_id: int | None = None,
) -> list[RemediationRequestRecord]:
    """Open remediations for a user, optionally scoped to ticker / run."""
    stmt = select(RemediationRequestRecord).where(
        RemediationRequestRecord.user_id == user_id,
        RemediationRequestRecord.status == OPEN,
    )
    if ticker:
        stmt = stmt.where(
            RemediationRequestRecord.ticker == ticker.strip().upper()
        )
    if decision_run_id is not None:
        stmt = stmt.where(
            RemediationRequestRecord.decision_run_id == decision_run_id
        )
    return list(session.scalars(stmt).all())


def has_open_remediation(
    session: Session,
    *,
    user_id: str,
    ticker: str,
    decision_run_id: int | None = None,
) -> bool:
    return bool(
        list_open_remediations(
            session,
            user_id=user_id,
            ticker=ticker,
            decision_run_id=decision_run_id,
        )
    )


def get_remediation_for_user(
    session: Session,
    row_id: int,
    *,
    user_id: str,
) -> RemediationRequestRecord | None:
    """Load by id AND user_id — id alone is never sufficient (iter-2 item 1)."""
    row = session.get(RemediationRequestRecord, row_id)
    if row is None:
        return None
    if row.user_id != user_id:
        return None
    return row


def resolve_remediation(
    session: Session,
    row_id: int,
    *,
    user_id: str,
    resolved_at: datetime | None = None,
) -> RemediationRequestRecord | None:
    """Resolve a remediation scoped to ``user_id``."""
    row = get_remediation_for_user(session, row_id, user_id=user_id)
    if row is None:
        return None
    row.status = RESOLVED
    row.resolved_at = resolved_at or _utcnow()
    session.flush()
    return row


def override_remediation(
    session: Session,
    row_id: int,
    *,
    user_id: str,
    override_reason: str,
    resolved_at: datetime | None = None,
) -> RemediationRequestRecord | None:
    """Explicit override with recorded reason — scoped to ``user_id``."""
    reason = (override_reason or "").strip()
    if not reason:
        raise ValueError("override_reason is required to override a remediation")
    row = get_remediation_for_user(session, row_id, user_id=user_id)
    if row is None:
        return None
    row.status = OVERRIDDEN
    row.override_reason = reason
    row.resolved_at = resolved_at or _utcnow()
    session.flush()
    return row


def clear_remediation(
    session: Session,
    *,
    user_id: str,
    ticker: str,
    kinds: Iterable[str] | None = None,
) -> int:
    """Resolve open remediations for ticker (optionally filtered by kind)."""
    open_rows = list_open_remediations(session, user_id=user_id, ticker=ticker)
    kind_set = set(kinds) if kinds is not None else None
    n = 0
    now = _utcnow()
    for row in open_rows:
        if kind_set is not None and row.kind not in kind_set:
            continue
        row.status = RESOLVED
        row.resolved_at = now
        n += 1
    if n:
        session.flush()
    return n


def auto_resolve_on_vintage_pass(
    session: Session,
    *,
    user_id: str,
    ticker: str,
) -> int:
    """Clear only ``vintage_stale`` rows after a verified fresh vintage pass.

    Does NOT clear ``data_integrity`` or ``facilitator_condition`` — those
    require operator override or a dedicated re-check (iter-2 item 3).
    """
    return clear_remediation(
        session,
        user_id=user_id,
        ticker=ticker,
        kinds=AUTO_RESOLVE_ON_VINTAGE_PASS,
    )


# Back-compat alias — now scoped to vintage-only kinds.
def auto_resolve_on_fresh_pass(
    session: Session,
    *,
    user_id: str,
    ticker: str,
    kinds: Iterable[str] | None = None,
) -> int:
    if kinds is not None:
        # Callers that pass explicit kinds keep them, but never widen past
        # the vintage-safe set unless they intersect.
        safe = set(kinds) & AUTO_RESOLVE_ON_VINTAGE_PASS
        if not safe:
            return 0
        return clear_remediation(
            session, user_id=user_id, ticker=ticker, kinds=safe,
        )
    return auto_resolve_on_vintage_pass(
        session, user_id=user_id, ticker=ticker,
    )


__all__ = [
    "AUTO_RESOLVE_ON_VINTAGE_PASS",
    "OPEN",
    "OVERRIDDEN",
    "RESOLVED",
    "auto_resolve_on_fresh_pass",
    "auto_resolve_on_vintage_pass",
    "clear_remediation",
    "get_remediation_for_user",
    "has_open_remediation",
    "list_open_remediations",
    "override_remediation",
    "persist_remediation_requests",
    "resolve_remediation",
]
