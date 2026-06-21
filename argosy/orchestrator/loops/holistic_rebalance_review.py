"""Quarterly holistic rebalance-review cadence loop.

Runs the already-built, deterministic whole-portfolio rebalance review
(:func:`argosy.services.holistic_rebalance_review.run_holistic_rebalance_review`)
on a quarterly cadence and persists a proposed-only ``rebalance`` ActionProposal
when the review yields actionable legs. The underlying review function loads its
own inputs (drift / per-position verdicts / thesis+news flags / estate gate) and
has built-in dedup + cooldown on the persisted proposal, so re-running it is
safe and writes nothing when drift is immaterial.

This module is the SCHEDULING SEAM only — it adds no money logic. It mirrors the
pure-seam shape of :class:`StateObserverLoop` / :class:`ThesisMonitorLoop`:
``session_factory`` / ``review_fn`` / ``now_fn`` are injectable so the loop is
unit-testable without any live DB or the real composer.

Cadence rationale
-----------------
The rebalancing engine this builds on is a quarterly-drift check, so the loop
runs QUARTERLY (10:00 IDT on the 1st of Jan/Apr/Jul/Oct). The review function's
own dedup/cooldown + material-drift gating (it only writes when
``status == "ok"`` and there are legs) means an off-cadence manual run-now is
also safe and idempotent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata

log = get_logger(__name__)

# Quarterly: 10:00 IDT on the 1st of Jan / Apr / Jul / Oct (after quarter open).
_DEFAULT_CRON = "0 10 1 1,4,7,10 *"
_DEFAULT_TZ = "Asia/Jerusalem"


def holistic_rebalance_review_metadata() -> JobMetadata:
    """``source_kind='monitor'`` — the rebalance proposal joins the same
    monitor → action-proposal family as the thesis / state observers."""
    return JobMetadata(
        name="holistic_rebalance_review",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Quarterly 10:00 IDT (1st of Jan/Apr/Jul/Oct)",
        source_kind="monitor",
        description=(
            "Holistic, plan-driven rebalance/sell review — composes ONE "
            "whole-portfolio rebalance proposal (trim over-target sleeves to "
            "fund under-target ones), gated by thesis strength + drift + the "
            "estate gate. Deterministic (no LLM); writes a proposed-only "
            "'rebalance' ActionProposal with built-in dedup/cooldown, only "
            "when drift is material."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HolisticRebalanceReviewLoop(CadenceLoop):
    """Quarterly holistic rebalance review."""

    name = "holistic_rebalance_review"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        review_fn: Callable[..., tuple[Any, bool]] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._review_fn = review_fn
        self._now_fn = now_fn or _utcnow
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        run_at = (now or self._now_fn)()
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        summary = await asyncio.to_thread(self._run_sync, run_at=run_at)
        self.last_output_summary = summary
        log.info("holistic_rebalance_review.tick.done", user_id=self.user_id, **summary)
        return summary

    def _run_sync(self, *, run_at: datetime) -> dict[str, Any]:
        # Default seam imports are deferred so import-time has no side effects
        # (mirrors ThesisMonitorLoop / StateObserverLoop).
        from argosy.orchestrator.loops.state_observer import (
            _build_default_session_factory,
        )

        review_fn = self._review_fn
        if review_fn is None:
            from argosy.services.holistic_rebalance_review import (
                run_holistic_rebalance_review,
            )

            review_fn = run_holistic_rebalance_review

        factory = self._session_factory or _build_default_session_factory()
        session = factory()
        summary: dict[str, Any] = {
            "status": None,
            "legs": 0,
            "proposal_written": False,
            "errors": [],
        }
        try:
            review, proposal_written = review_fn(
                self.user_id, session, write_proposal=True, now=run_at
            )
            summary["status"] = str(getattr(review, "status", "") or "")
            summary["legs"] = len(getattr(review, "legs", []) or [])
            summary["proposal_written"] = bool(proposal_written)
            return summary
        except Exception as exc:  # noqa: BLE001 — one bad run never crashes the scheduler
            session.rollback()
            summary["errors"].append(str(exc))
            log.warning(
                "holistic_rebalance_review.failed",
                user_id=self.user_id,
                error=str(exc),
            )
            return summary
        finally:
            session.close()


def run_holistic_rebalance_review_now(
    *, user_id: str = "ariel", session_factory=None
) -> dict[str, Any] | None:
    """Manual-trigger entry (the /api/jobs '{name}/run-now' route + ad-hoc calls)."""
    loop = HolisticRebalanceReviewLoop(user_id=user_id, session_factory=session_factory)
    return asyncio.run(loop.tick())


__all__ = [
    "HolisticRebalanceReviewLoop",
    "holistic_rebalance_review_metadata",
    "run_holistic_rebalance_review_now",
]
