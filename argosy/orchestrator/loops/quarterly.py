"""Quarterly loop (SDD §5.1, Phase 7).

Runs after quarter close. Triggers prompt-style reminders surfaced via
WebSocket events for the dashboard to display:
  - Real estate P&L update prompt
  - Bonus event ingest prompt
  - Plan-drift check vs targets

The plan-drift check delegates to the plan-critique agent (same pattern
as the weekly review). The other two are user-facing prompts only.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import desc, select

from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.api.events import publish_event
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import PlanCritique, PlanVersion

_log = get_logger("argosy.loops.quarterly")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QuarterlyLoop(CadenceLoop):
    """Quarterly review + prompt loop."""

    name = "quarterly"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        plan_critique_factory: Callable[[], PlanCritiqueAgent] | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._critique_factory = plan_critique_factory or (
            lambda: PlanCritiqueAgent(user_id=user_id)
        )

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        if os.environ.get("ARGOSY_KILL") == "1":
            _log.info("quarterly.kill_switch_skip")
            return

        guard = get_cost_guard(user_id=self.user_id)
        if await guard.should_pause_non_routine(loop_name=self.name):
            _log.info("quarterly.cost_guard_paused")
            return

        moment = (now or _utcnow)()

        # Surface user-facing prompts
        prompts = [
            {"kind": "real_estate_pnl", "message": "Update Romania + Atlanta rental P&L for the quarter."},
            {"kind": "bonus_event", "message": "Record any bonus / RSU vest events in the past quarter."},
        ]
        for p in prompts:
            try:
                await publish_event(
                    "quarterly.prompt",
                    {"user_id": self.user_id, "run_at": moment.isoformat(), **p},
                )
            except Exception:  # pragma: no cover - defensive
                _log.exception("quarterly.publish_failed")

        # Plan-drift check
        critique_summary: str | None = None
        async with db_mod.get_session() as session:
            plan = (
                await session.execute(
                    select(PlanVersion)
                    .where(PlanVersion.user_id == self.user_id)
                    .order_by(desc(PlanVersion.imported_at))
                    .limit(1)
                )
            ).scalar_one_or_none()

        if plan is not None and plan.raw_markdown.strip():
            try:
                agent = self._critique_factory()
                report = await agent.run(
                    plan_label=plan.version_label or f"plan_version_id={plan.id}",
                    plan_markdown=plan.raw_markdown,
                    snapshot_label=f"quarterly:{moment.isoformat()}",
                    snapshot_summary="(quarterly drift check)",
                    user_context_yaml="",
                    domain_kb_files={},
                )
                async with db_mod.get_session() as session:
                    session.add(
                        PlanCritique(
                            user_id=self.user_id,
                            plan_version_id=plan.id,
                            critique_json=report.output.model_dump_json(),
                            model=report.model,
                        )
                    )
                    await session.commit()
                critique_summary = report.output.overall_summary
            except Exception:  # pragma: no cover - defensive
                _log.exception("quarterly.critique_failed")

        await record_audit_event(
            user_id=self.user_id,
            event_type="quarterly.completed",
            entity_type="cadence",
            entity_id="quarterly",
            payload={
                "now": moment.isoformat(),
                "prompts_count": len(prompts),
                "critique_summary": critique_summary,
            },
        )


__all__ = ["QuarterlyLoop"]
