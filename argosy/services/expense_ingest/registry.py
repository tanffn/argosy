"""Source registration: idempotent insert/get for ExpenseSource rows."""

from __future__ import annotations

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.types import SourceHint
from argosy.state.models import ExpenseSource


def _default_display_name(hint: SourceHint) -> str:
    parts = [hint.issuer.title(), hint.external_id]
    return " ".join(parts)


def register_or_get_source(
    session: Session, user_id: str, hint: SourceHint
) -> ExpenseSource:
    """Find an existing source by (user_id, kind, external_id) or create one.

    On a re-register, never blank out non-empty cardholder_name with None.
    """
    src = session.query(ExpenseSource).filter_by(
        user_id=user_id, kind=hint.kind, external_id=hint.external_id,
    ).one_or_none()
    if src is not None:
        if hint.cardholder_name and not src.cardholder_name:
            src.cardholder_name = hint.cardholder_name
        if hint.display_name and not src.display_name:
            src.display_name = hint.display_name
        return src

    src = ExpenseSource(
        user_id=user_id,
        kind=hint.kind,
        issuer=hint.issuer,
        external_id=hint.external_id,
        display_name=hint.display_name or _default_display_name(hint),
        cardholder_name=hint.cardholder_name,
        active=True,
    )
    session.add(src)
    session.flush()                # so callers get src.id
    return src


def list_active_sources(session: Session, user_id: str) -> list[ExpenseSource]:
    return list(session.query(ExpenseSource).filter_by(
        user_id=user_id, active=True
    ).order_by(ExpenseSource.created_at).all())
