"""Monthly cycle loop (SDD §5.1, Phase 7).

Cron `0 8 1 * *`. Performs:
  - Statement reconciliation (placeholder hook — re-runs broker imports
    via injected callable).
  - RSU vest pulled in (placeholder — a real RSU calendar is OPEN).
  - Gap-weighted buy template generation.
  - Full plan-critique re-run (delegates to `PlanCritiqueAgent`).

Records `monthly_cycle.completed` audit event. Honors the cost-guard
pause and kill switch.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import desc, select

from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.api.events import publish_event
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import PlanCritique, PlanVersion

_log = get_logger("argosy.loops.monthly_cycle")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MonthlyCycleLoop(CadenceLoop):
    """1st-of-month cycle: reconciliation + plan-critique re-run."""

    name = "monthly_cycle"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        plan_critique_factory: Callable[[], PlanCritiqueAgent] | None = None,
        statement_reconcile: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        rsu_vest_pull: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
        buy_template_generator: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._critique_factory = plan_critique_factory or (
            lambda: PlanCritiqueAgent(user_id=user_id)
        )
        self._statement_reconcile = statement_reconcile or _noop_reconcile
        self._rsu_vest_pull = rsu_vest_pull or _noop_rsu_pull
        self._buy_template_generator = buy_template_generator or _default_buy_template

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        if os.environ.get("ARGOSY_KILL") == "1":
            _log.info("monthly_cycle.kill_switch_skip")
            return

        guard = get_cost_guard(user_id=self.user_id)
        if await guard.should_pause_non_routine(loop_name=self.name):
            _log.info("monthly_cycle.cost_guard_paused")
            return

        moment = (now or _utcnow)()

        # 1. Statement reconciliation (placeholder)
        try:
            reconcile_result = await self._statement_reconcile(self.user_id)
        except Exception:  # pragma: no cover - defensive
            _log.exception("monthly_cycle.reconcile_failed")
            reconcile_result = {"status": "error"}

        # 2. RSU vest pull (placeholder; real RSU calendar is OPEN)
        try:
            rsu_events = await self._rsu_vest_pull(self.user_id)
        except Exception:  # pragma: no cover - defensive
            _log.exception("monthly_cycle.rsu_pull_failed")
            rsu_events = []

        # 3. Gap-weighted buy template
        try:
            buy_template = await self._buy_template_generator(self.user_id)
        except Exception:  # pragma: no cover - defensive
            _log.exception("monthly_cycle.buy_template_failed")
            buy_template = {}

        # 4. Plan-critique re-run (only when a plan exists)
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
                    snapshot_label=f"monthly_cycle:{moment.isoformat()}",
                    snapshot_summary="(monthly cycle re-run)",
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
                _log.exception("monthly_cycle.critique_failed")

        await record_audit_event(
            user_id=self.user_id,
            event_type="monthly_cycle.completed",
            entity_type="cadence",
            entity_id="monthly_cycle",
            payload={
                "now": moment.isoformat(),
                "reconcile": reconcile_result,
                "rsu_events_count": len(rsu_events),
                "buy_template": buy_template,
                "critique_summary": critique_summary,
            },
        )
        try:
            await publish_event(
                "monthly_cycle.completed",
                {
                    "user_id": self.user_id,
                    "run_at": moment.isoformat(),
                    "critique_summary": critique_summary,
                },
            )
        except Exception:  # pragma: no cover - defensive
            _log.exception("monthly_cycle.publish_failed")


async def _noop_reconcile(_uid: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": "no reconcile callable wired"}


async def _noop_rsu_pull(_uid: str) -> list[dict[str, Any]]:
    return []


async def _default_buy_template(_uid: str) -> dict[str, Any]:
    """Placeholder buy template — flat-weighted across positions.

    Production wiring will compute a gap-weighted template from the
    plan's allocation targets vs current snapshot. Phase 7 returns
    `{"template": "flat", "items": []}` so the audit log records the
    intent without fabricating numbers.
    """
    return {"template": "flat", "items": []}


__all__ = ["MonthlyCycleLoop"]
