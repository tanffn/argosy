"""Monthly cycle loop (SDD §5.1, Phase 7).

Cron `0 8 1 * *`. Performs:
  - Statement reconciliation (placeholder hook — re-runs broker imports
    via injected callable).
  - RSU vest pulled in (placeholder — a real RSU calendar is OPEN).
  - Gap-weighted buy template generation.
  - Full plan-critique re-run (delegates to `PlanCritiqueAgent`).
  - Plan-synthesis trigger (Wave 2, Task 2.11): for every user with an
    active baseline, fire ``plan_synthesis.run_synthesis(...,
    trigger='scheduled')`` so the monthly check-in produces a fresh
    role='draft' PlanVersion.

Records `monthly_cycle.completed` audit event. Honors the cost-guard
pause and kill switch.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import sqlalchemy as sa
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.api.events import publish_event
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import PlanCritique, PlanVersion

_log = get_logger("argosy.loops.monthly_cycle")


# ---------------------------------------------------------------------------
# Sync entry point — Task 2.11
#
# Mirrors the ``plan_watcher.tick`` pattern: a synchronous module-level
# function the async ``MonthlyCycleLoop.tick`` bridges into via
# ``asyncio.to_thread``. ``run_synthesis`` is a sync function that calls
# agent ``run_sync`` internally (which itself wraps ``asyncio.run``), so
# it must not be called from an event-loop thread.
# ---------------------------------------------------------------------------


def tick(session: Session) -> None:
    """Sync entry point for the monthly cycle's synthesis trigger.

    Iterates every user with an active baseline plan and fires
    ``plan_synthesis.run_synthesis(..., trigger='scheduled')`` for each.
    A failure for one user does not stop the loop for others.

    Async pre/post-work (statement reconciliation, RSU vest, plan
    critique, audit + publish) lives on ``MonthlyCycleLoop.tick``; that
    method calls into this function via ``asyncio.to_thread`` after the
    async work completes.
    """
    _trigger_plan_synthesis_for_all(session)
    _route_breach_tranches_for_all(session)


def _route_breach_tranches_for_all(session: Session) -> None:
    """T4.5: for each user with an active baseline, auto-route an NVDA
    concentration-breach SELL tranche to approval (best-effort, idempotent).

    NEVER executes — the proposal lands in ``awaiting_human``, routed into the
    §10 approval pipeline. One user's failure must not stop the others."""
    from argosy.services.breach_router import route_breach_tranche

    rows = (
        session.query(PlanVersion.user_id)
        .filter(PlanVersion.role == "baseline")
        .distinct()
        .all()
    )
    for (user_id,) in rows:
        try:
            pid = route_breach_tranche(session, user_id)
            if pid is not None:
                session.commit()
                _log.info(
                    "monthly_cycle.breach_tranche_routed",
                    user_id=user_id,
                    proposal_id=pid,
                )
        except Exception as exc:  # noqa: BLE001 — isolate per-user failures
            session.rollback()
            _log.error(
                "monthly_cycle.breach_route_failed", user_id=user_id, error=str(exc)
            )


def _trigger_plan_synthesis_for_all(session: Session) -> None:
    """Fire ``plan_synthesis.run_synthesis`` for each eligible user."""
    from argosy.orchestrator.flows import plan_synthesis as flow

    rows = (
        session.query(PlanVersion.user_id)
        .filter(PlanVersion.role == "baseline")
        .distinct()
        .all()
    )
    for (user_id,) in rows:
        try:
            flow.run_synthesis(session, user_id=user_id, trigger="scheduled")
        except flow.NoBaselineError:
            # Race: row was demoted between query and call. Skip.
            continue
        except Exception as exc:  # noqa: BLE001 — one user's failure must not stop others
            _log.error(
                "monthly_cycle.synthesis_failed",
                user_id=user_id,
                error=str(exc),
            )


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
        self._rsu_vest_pull = rsu_vest_pull or _real_rsu_pull
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

        # Wave 2 (Task 2.11): trigger plan synthesis for every user with
        # an active baseline. Bridged via asyncio.to_thread because
        # run_synthesis (and the agents it calls via run_sync) cannot run
        # inside the active event loop.
        import asyncio

        from argosy.config import get_settings

        def _run_sync_tick() -> None:
            settings = get_settings()
            sync_url = settings.database_url.replace("+aiosqlite", "")
            engine = sa.create_engine(
                sync_url, connect_args={"check_same_thread": False}
            )
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
            sess = SessionLocal()
            try:
                tick(sess)
            finally:
                sess.close()
                engine.dispose()

        try:
            await asyncio.to_thread(_run_sync_tick)
        except Exception:  # pragma: no cover - defensive
            _log.exception("monthly_cycle.synthesis_trigger_failed")


async def _noop_reconcile(_uid: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": "no reconcile callable wired"}


async def _noop_rsu_pull(_uid: str) -> list[dict[str, Any]]:
    """Kept for tests that explicitly want no-op behavior. The default
    production callable on ``MonthlyCycleLoop.__init__`` is
    :func:`_real_rsu_pull`, not this; this stays around so existing
    tests passing ``rsu_vest_pull=None`` semantics aren't broken
    if a future refactor wants the no-op back.
    """
    return []


async def _real_rsu_pull(user_id: str) -> list[dict[str, Any]]:
    """Default production RSU vest pull (replaces ``_noop_rsu_pull``).

    Bridges into the sync ``ingest_samples_root`` helper via
    ``asyncio.to_thread`` because ``ingest_schwab_vest_events``
    operates on a sync SQLAlchemy ``Session`` (not an
    ``AsyncSession``). Returns the list of per-file result dicts that
    monthly_cycle's audit-log payload measures with ``len(...)``.

    Skips quietly when ``$ARGOSY_EXPENSE_SAMPLES_ROOT`` is unset or
    the path doesn't exist — see ``rsu_vest_pull.ingest_samples_root``.
    """
    import asyncio

    from argosy.services.rsu_vest_pull import ingest_samples_root

    return await asyncio.to_thread(ingest_samples_root, user_id)


async def _default_buy_template(_uid: str) -> dict[str, Any]:
    """Placeholder buy template — flat-weighted across positions.

    Production wiring will compute a gap-weighted template from the
    plan's allocation targets vs current snapshot. Phase 7 returns
    `{"template": "flat", "items": []}` so the audit log records the
    intent without fabricating numbers.
    """
    return {"template": "flat", "items": []}


__all__ = ["MonthlyCycleLoop", "tick"]
