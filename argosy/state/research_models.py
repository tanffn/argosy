"""Durable multi-source research, review requests, and evidence-use receipts."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from argosy.state.models import Base, _utcnow


class ResearchSource(Base):
    __tablename__ = "research_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str] = mapped_column(Text)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cadence_hours: Mapped[int] = mapped_column(Integer, default=24)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("user_id", "kind", "reference", name="uq_research_source"),)


class ResearchItem(Base):
    __tablename__ = "research_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("research_sources.id"), index=True)
    external_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    content_hash: Mapped[str] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    analysis_json: Mapped[str] = mapped_column(Text, default="{}")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = (Index("ix_research_queue", "user_id", "status", "observed_at"),)


class ResearchClaim(Base):
    __tablename__ = "research_claims"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("research_items.id"), index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    scope: Mapped[str] = mapped_column(String(24), default="ticker")
    statement: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    direction: Mapped[str | None] = mapped_column(String(16))
    horizon_days: Mapped[int | None] = mapped_column(Integer)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    outcome_json: Mapped[str | None] = mapped_column(Text)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchReviewRequest(Base):
    __tablename__ = "research_review_requests"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("research_items.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), default="pending")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ResearchEvidenceUse(Base):
    __tablename__ = "research_evidence_uses"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("research_items.id"), index=True)
    report_id: Mapped[int] = mapped_column(Integer)
    decision_id: Mapped[str | None] = mapped_column(String(256))
    agent_role: Mapped[str] = mapped_column(String(128))
    cited: Mapped[bool] = mapped_column(Boolean, default=False)
    verdict: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


def _record_report_use(mapper, connection, target):
    from argosy.services.research_impact import record_report_connection
    record_report_connection(connection, target)


from sqlalchemy import event
from argosy.state.models import AgentReport
event.listen(AgentReport, "after_insert", _record_report_use)
