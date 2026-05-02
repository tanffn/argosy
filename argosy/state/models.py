"""SQLAlchemy models for Phase 0.

Per SDD §8: only `users` and `user_context` for now. Other table groups
(holdings, plan, decisions, audit, external, domain, operations) are
introduced in subsequent phases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Argosy declarative base."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    context: Mapped["UserContext | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class UserContext(Base):
    __tablename__ = "user_context"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # YAML payloads are stored as TEXT; structured access is done in app code.
    identity_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="")
    goals_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="")
    constraints_yaml: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="context")


__all__ = ["Base", "User", "UserContext"]
