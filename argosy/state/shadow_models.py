"""Append-only, versioned paper portfolio accounting; never broker orders."""
from datetime import datetime

from sqlalchemy import DDL, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from argosy.state.models import ActionProposal, Base


class ShadowEvent(Base):
    __tablename__ = "shadow_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    cohort_id: Mapped[str] = mapped_column(String(64), index=True)
    event_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(24))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("user_id", "cohort_id", "event_key", name="uq_shadow_event_identity"),)


for operation in ("UPDATE", "DELETE"):
    name = "trg_shadow_events_no_" + operation.lower()
    event.listen(ShadowEvent.__table__, "after_create", DDL(
        f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {operation} ON shadow_events "
        "BEGIN SELECT RAISE(ABORT, 'append-only: shadow_events is immutable'); END"))
    event.listen(ShadowEvent.__table__, "before_drop", DDL(f"DROP TRIGGER IF EXISTS {name}"))


def _capture_publication(mapper, connection, target):
    from argosy.services.shadow_portfolio import _capture
    _capture(connection, target)


event.listen(ActionProposal, "after_insert", _capture_publication)
event.listen(ActionProposal, "after_update", _capture_publication)
