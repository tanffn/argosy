"""Daily decision-funnel loop.

Fires after the 17:00/18:00 monitors (news_daily, state_observer, alpha_report)
so Stage 0 reads a fresh macro picture. Gated by the master kill switch
``ARGOSY_DECISION_FUNNEL_ENABLED`` — when off, the loop no-ops cleanly and the
job-runs row records ``skipped``. Shadow mode + Stage-3 gating are handled
inside the orchestrator via their own flags.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata

log = get_logger("argosy.orchestrator.loops.decision_funnel")

_DEFAULT_CRON = "30 18 * * *"  # Daily 18:30 IDT, after the 17:00/18:00 monitors
_DEFAULT_TZ = "Asia/Jerusalem"


def decision_funnel_metadata() -> JobMetadata:
    return JobMetadata(
        name="decision_funnel",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 18:30 IDT",
        source_kind="monitor",
        description=(
            "Daily tiered decision funnel: a market review (Stage 0) -> "
            "deterministic relevance routing onto the book (Stage 1) -> cheap "
            "Sonnet triage (Stage 2) -> full Opus deep-decision fleet for "
            "survivors (Stage 3, propose-and-ask). Conservative escalation, not "
            "a daily recommender: gated by ARGOSY_DECISION_FUNNEL_ENABLED, runs "
            "in shadow mode by default (records proposals + full trace, surfaces "
            "nothing), and Stage 3 is separately gated. Every run is fully "
            "traced for replay (see /api/decisions/funnel/runs)."
        ),
        long_running=False,
    )


class DecisionFunnelLoop(CadenceLoop):
    name = "decision_funnel"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        run_fn: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._run_fn = run_fn
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        from argosy.config import get_settings

        settings = get_settings()
        run_at = (now or self._now_fn)()
        if callable(run_at):  # tolerate either a callable or a value
            run_at = run_at()
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)

        # Master kill switch: no-op (skipped) when disabled.
        if not getattr(settings, "decision_funnel_enabled", False):
            summary = {"status": "skipped", "reason": "decision_funnel_enabled=False"}
            self.last_output_summary = summary
            log.info("decision_funnel.tick.skipped", user_id=self.user_id)
            return summary

        run_fn = self._run_fn
        if run_fn is None:
            from argosy.services.decision_funnel.orchestrator import run_funnel

            run_fn = run_funnel

        summary = await run_fn(
            self.user_id, now=run_at, trigger="scheduler", settings=settings
        )
        self.last_output_summary = summary
        log.info("decision_funnel.tick.done", user_id=self.user_id, **{
            k: v for k, v in (summary or {}).items() if isinstance(v, (int, bool))
        })
        return summary


__all__ = ["DecisionFunnelLoop", "decision_funnel_metadata"]
