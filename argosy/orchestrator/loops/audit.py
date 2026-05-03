"""Audit loop (SDD §3.6, §5).

Runs weekly (default Sun 19:00). Invokes `AuditAgent` to review the
last week's `agent_reports` rows and surface systematic patterns
(consistently low-confidence outputs, tier-vs-fund-manager rejections,
etc.). Output persists as a new `AuditReport` row referenced from
`agent_reports`; findings are emitted via the `audit.findings` WebSocket
event so the dashboard can surface a "needs attention" badge.

The loop is deliberately thin: gather the last week of agent_reports →
invoke `AuditAgent` → persist + emit. No auto-action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import desc, select

from argosy.agents.audit_agent import AuditAgent
from argosy.api.events import publish_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow

_log = get_logger("argosy.loops.audit")


@dataclass
class AuditInputs:
    """Inputs to one audit run, gathered before the LLM call."""

    user_id: str
    window_start: datetime
    window_end: datetime
    reports_json: list[dict[str, Any]]


class AuditLoop(CadenceLoop):
    """Weekly self-review loop. Wired when `cadences.audit.enabled` is true."""

    name = "audit"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        audit_agent_factory: Callable[[], AuditAgent] | None = None,
        gather_inputs: Callable[[str], "AuditInputs | Any"] | None = None,
        window_days: int = 7,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self.window_days = window_days
        self._audit_factory = audit_agent_factory or (lambda: AuditAgent(user_id=user_id))
        self._gather = gather_inputs or _default_gather_inputs

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        run_at = (now or _utcnow)()

        # Cost-cap pause: audit is non-routine; skip when budget breached.
        if get_cost_guard().should_pause_non_routine():
            _log.info("audit.cost_cap_paused", user_id=self.user_id)
            return

        inputs = await _maybe_async(self._gather(self.user_id))
        if not isinstance(inputs, AuditInputs):  # pragma: no cover - defensive
            raise TypeError(f"gather_inputs must return AuditInputs, got {type(inputs)!r}")

        # Skip when there's nothing to audit.
        if not inputs.reports_json:
            _log.info(
                "audit.no_reports",
                user_id=self.user_id,
                window_start=inputs.window_start.isoformat(),
                window_end=inputs.window_end.isoformat(),
            )
            return

        agent = self._audit_factory()
        report = await agent.run(
            window_start=inputs.window_start.isoformat(),
            window_end=inputs.window_end.isoformat(),
            reports=inputs.reports_json,
        )

        findings = getattr(report.output, "findings", []) or []
        _log.info(
            "audit.completed",
            user_id=self.user_id,
            findings_count=len(findings),
            run_at=run_at.isoformat(),
        )

        try:
            await publish_event(
                "audit.findings",
                {
                    "user_id": self.user_id,
                    "run_at": run_at.isoformat(),
                    "findings_count": len(findings),
                },
            )
        except Exception:  # pragma: no cover - defensive
            _log.exception("audit.publish_failed")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _maybe_async(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def _default_gather_inputs(user_id: str) -> AuditInputs:
    """Pulls the past 7 days of agent_reports for the user."""
    end = _utcnow()
    start = end - timedelta(days=7)

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AgentReportRow)
                .where(AgentReportRow.user_id == user_id)
                .where(AgentReportRow.created_at >= start)
                .where(AgentReportRow.created_at <= end)
                .order_by(desc(AgentReportRow.created_at))
                .limit(2000)
            )
        ).scalars().all()

    payload = [
        {
            "id": r.id,
            "agent_role": r.agent_role,
            "decision_id": r.decision_id,
            "model": r.model,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "cost_usd": float(r.cost_usd or 0),
            "confidence": r.confidence,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]

    return AuditInputs(
        user_id=user_id,
        window_start=start,
        window_end=end,
        reports_json=payload,
    )


__all__ = ["AuditInputs", "AuditLoop"]
