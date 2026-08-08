"""``HoldingsReviewJob`` — daily per-holding research -> verdict pass.

Runs ``run_holdings_review`` on the user's book: triage to material positions,
fetch fresh per-name data (news / price / plan thesis), decide BUY/HOLD/SELL/TRIM,
and surface ONLY actionable verdicts to the inbox (HOLD stays silent). This is the
"acting half" the monitoring stack was missing — a weakening holding now becomes an
evidence-backed verdict instead of a passive note.

Same-code-path contract: the 17:30 IDT cadence and the manual ``Run now`` both go
through :meth:`tick`. The decision work makes a live LLM call per material holding
and is executed via ``asyncio.to_thread`` (``decide_stock`` / the fetchers use
``asyncio.run`` internally and cannot nest in the loop). NOTE: it is still a
``CadenceLoop``, so ``JobMetadata.long_running`` must be False — that flag is the
registry's LongRunningJob-vs-CadenceLoop discriminator (a mismatch makes
``JobRegistry.register`` raise, leaving the loop on the scheduler but unrunnable:
``RegisteredScheduler._fire_once`` fails fast for unregistered loops), NOT a
"takes a long time" hint.
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
from argosy.services.stock_decision import run_holdings_review

_log = get_logger("argosy.jobs.holdings_review")

_DEFAULT_CRON = "30 17 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"

_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Cached sync ``sessionmaker`` bound to the configured DB (rebuilds if the
    db_file changes). Mirrors ``news_daily`` — the decision services need a sync
    Session; the async get_session yields an AsyncSession they cannot consume."""
    global _SESSION_FACTORY
    import sqlalchemy as sa

    from argosy.config import get_settings

    db_file = str(get_settings().db_file)
    if _SESSION_FACTORY is not None and _SESSION_FACTORY[0] == db_file:
        return _SESSION_FACTORY[1]
    engine = sa.create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _SESSION_FACTORY = (db_file, factory)
    return factory


def holdings_review_metadata() -> JobMetadata:
    return JobMetadata(
        name="holdings_review",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 17:30 IDT",
        source_kind="monitor",
        description=(
            "Per-holding research -> verdict review: triage to material positions "
            "(a weakened/broken thesis_monitor flag elevates a name past the size "
            "gate), fetch fresh data (news/price/plan thesis) on each, decide "
            "BUY/HOLD/SELL/TRIM. Only actionable verdicts surface to the inbox; "
            "HOLD (thesis intact) stays silent. Manual Run now uses the same tick."
        ),
        long_running=False,
    )


class HoldingsReviewJob(CadenceLoop):
    """Daily holdings research->decide loop."""

    name = "holdings_review"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        min_position_usd: float = 5_000.0,
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        review_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._min = min_position_usd
        self._session_factory = session_factory
        self._review_fn = review_fn or run_holdings_review
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        self.last_output_summary = None
        run_at = (now or (lambda: datetime.now(timezone.utc)))()
        _log.info("holdings_review.tick.start", run_at=run_at.isoformat())

        def _work() -> dict[str, Any]:
            factory = self._session_factory or _build_default_session_factory()
            session = factory()
            try:
                res = self._review_fn(session, self.user_id, min_position_usd=self._min)
            finally:
                session.close()
            return res

        summary = await asyncio.to_thread(_work)
        out = {
            "reviewed": summary.get("reviewed", 0),
            "actionable": summary.get("actionable", 0),
            "written": summary.get("written", 0),
            # An actionable verdict that FAILED the blind re-derivation gate —
            # previously vanished from the job summary entirely (only a log
            # line), so the honest "we saw a trade signal but couldn't confirm
            # it" count was invisible. Surfaced per FIX 3b (2026-07-08 audit).
            "held_unverified": summary.get("held_unverified", 0),
            "abstained": summary.get("abstained", 0),
            "decisions": summary.get("decisions", 0),
            "elevated": summary.get("elevated", []),
        }
        self.last_output_summary = out
        _log.info("holdings_review.tick.done", **out)
        return out


__all__ = ["HoldingsReviewJob", "holdings_review_metadata"]
