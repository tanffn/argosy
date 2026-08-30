"""Persistence seam for standalone ``BaseAgent`` callers.

``BaseAgent.run`` deliberately returns telemetry without writing it: synthesis
flows batch their reports at phase boundaries to avoid concurrent SQLite
writers.  Autonomous one-shot callers still need an explicit, single-writer
path or their spend and outputs are invisible to the cost guard and audit loop.
"""

from __future__ import annotations

import threading
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow
from argosy.state.models import AgentReportBlob

# Standalone agents may finish concurrently (the three blind deployment
# reviewers do). SQLite has one writer; serialize these tiny commits instead of
# turning successful model calls into lost telemetry or database-locked noise.
_SYNC_WRITE_LOCK = threading.Lock()


def stage_agent_report(
    session: Session,
    report: Any,
    *,
    decision_id: str | None = None,
) -> AgentReportRow:
    """Add one returned agent report to an existing transaction.

    The caller owns commit/rollback.  This is the correct path when the agent
    call is part of a larger atomic ingest, such as recipient resolution.
    """
    row = AgentReportRow(
        user_id=report.user_id,
        agent_role=report.agent_role,
        decision_id=decision_id if decision_id is not None else report.decision_id,
        prompt_hash=report.prompt_hash,
        response_text=report.response_text,
        tokens_in=report.tokens_in,
        tokens_out=report.tokens_out,
        cost_usd=float(report.cost_usd),
        model=report.model,
        confidence=report.confidence.value if report.confidence else None,
        cache_input_tokens=report.cache_input_tokens,
        cache_creation_tokens=report.cache_creation_tokens,
        thinking_tokens=report.thinking_tokens,
        citations_json=report.citations_json,
        sources_json=report.sources_json,
        run_correlation_id=report.run_correlation_id,
        system_prompt=report.system_prompt,
        user_prompt=report.user_prompt,
        created_at=report.created_at,
    )
    blobs = dict(getattr(report, "blobs", {}) or {})
    try:
        blobs.setdefault("output_json", report.output.model_dump_json())
    except Exception:  # noqa: BLE001 - raw response remains on the parent row
        pass
    row.blobs.extend(
        AgentReportBlob(key=str(key)[:64], value=str(value))
        for key, value in blobs.items()
    )
    session.add(row)
    return row


def persist_agent_report_sync(
    report: Any,
    *,
    decision_id: str | None = None,
) -> int:
    """Commit one standalone report using the configured live database."""
    async_url = str(db_mod.get_engine().url)
    sync_url = async_url.replace("+aiosqlite", "")
    engine = sa.create_engine(
        sync_url,
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with _SYNC_WRITE_LOCK, factory() as session:
            row = stage_agent_report(session, report, decision_id=decision_id)
            session.commit()
            return int(row.id)
    finally:
        engine.dispose()


async def persist_agent_report_async(
    report: Any,
    *,
    decision_id: str | None = None,
) -> int | None:
    """Commit one standalone report through the process async engine.

    Injected test/dry-run agents sometimes return a minimal namespace instead
    of the ``AgentReport`` contract. Those have no telemetry to preserve and
    are ignored; every real ``BaseAgent`` return is the concrete dataclass.
    """
    from argosy.agents.base import AgentReport

    if not isinstance(report, AgentReport):
        return None
    async with db_mod.get_session(user_id=report.user_id) as session:
        row = AgentReportRow(
            user_id=report.user_id,
            agent_role=report.agent_role,
            decision_id=(
                decision_id if decision_id is not None else report.decision_id
            ),
            prompt_hash=report.prompt_hash,
            response_text=report.response_text,
            tokens_in=report.tokens_in,
            tokens_out=report.tokens_out,
            cost_usd=float(report.cost_usd),
            model=report.model,
            confidence=(
                report.confidence.value if report.confidence else None
            ),
            cache_input_tokens=report.cache_input_tokens,
            cache_creation_tokens=report.cache_creation_tokens,
            thinking_tokens=report.thinking_tokens,
            citations_json=report.citations_json,
            sources_json=report.sources_json,
            run_correlation_id=report.run_correlation_id,
            system_prompt=report.system_prompt,
            user_prompt=report.user_prompt,
            created_at=report.created_at,
        )
        blobs = dict(report.blobs or {})
        try:
            blobs.setdefault("output_json", report.output.model_dump_json())
        except Exception:  # noqa: BLE001
            pass
        row.blobs.extend(
            AgentReportBlob(key=str(key)[:64], value=str(value))
            for key, value in blobs.items()
        )
        session.add(row)
        await session.commit()
        return int(row.id)


__all__ = [
    "persist_agent_report_async",
    "persist_agent_report_sync",
    "stage_agent_report",
]
