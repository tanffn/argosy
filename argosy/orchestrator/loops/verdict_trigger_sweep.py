"""Daily deterministic verdict-trigger SWEEP that ESCALATES to a re-verdict.

Sibling of ``verdict_trigger_daily`` (which only writes needs-confirm *unlock*
inbox rows). This loop runs the cheap deterministic
:func:`argosy.services.verdict_triggers.evaluate_standing_verdict_triggers`
sweep and, for any STANDING verdict whose typed trigger actually TRIPS, fires the
SAME full re-verdict path the thesis monitor / consult flow uses
(``run_deep_decision``). Only tripped symbols pay the LLM cost; the sweep itself
is nearly free. Idempotent — a ``verdict_trigger_fired`` marker guarantees one
escalation per standing verdict.

Pure-seam design (mirrors ThesisMonitorLoop / VerdictTriggerDailyLoop):
``session_factory`` / ``quote_fn`` / ``macro_fn`` / ``decide_fn`` / ``now_fn`` are
injectable so the loop is unit-testable with no live feed, no LLM, and no live DB.

Schedule: 07:30 Asia/Jerusalem — just after ``verdict_trigger_daily`` (07:00)
writes its unlock rows and after overnight quotes settle.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.services.verdict_triggers import MacroFn, QuoteFn, sweep_and_fire

_log = get_logger("argosy.loops.verdict_trigger_sweep")
_DEFAULT_CRON = "30 7 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def verdict_trigger_sweep_metadata() -> JobMetadata:
    return JobMetadata(
        name="verdict_trigger_sweep",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 07:30 Asia/Jerusalem",
        source_kind="monitor",
        description=(
            "Deterministic settled-verdict trigger sweep that ESCALATES: "
            "price_below/above vs quotes, dated_event vs calendar, "
            "metric_condition vs macro feed. A TRIPPED trigger fires a full "
            "re-verdict (run_deep_decision) once per verdict — never an LLM "
            "cost for untripped names; unevaluable triggers are recorded, "
            "never treated as tripped."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_session_factory() -> sessionmaker:
    import sqlalchemy as sa

    from argosy.state import db as db_mod

    url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
    engine = sa.create_engine(url, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, expire_on_commit=False)


def _default_quote_fn(session: Session) -> QuoteFn:
    """kv_cache-backed last price (``quote:{SYMBOL}``), same source the daily
    unlock loop reads. Returns ``None`` on any miss/parse failure so the trigger
    is UNEVALUABLE rather than falsely not-tripped."""

    def _lookup(subject: str) -> float | None:
        try:
            from sqlalchemy import text

            raw = session.execute(
                text(
                    "SELECT value FROM kv_cache WHERE key = :k "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"k": f"quote:{subject.upper()}"},
            ).scalar()
            if raw is None:
                return None
            payload = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(payload, dict):
                return None
            px = payload.get("price") or payload.get("last") or payload.get("close")
            return float(px) if px is not None else None
        except Exception as exc:  # noqa: BLE001 — a lookup error is UNEVALUABLE
            _log.warning(
                "verdict_trigger_sweep.quote_lookup_failed",
                subject=subject, error=str(exc)[:160],
            )
            return None

    return _lookup


class VerdictTriggerSweepLoop(CadenceLoop):
    """Sweep settled verdicts; escalate tripped ones to a full re-verdict."""

    name = "verdict_trigger_sweep"

    def __init__(
        self,
        *,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        quote_fn: QuoteFn | None = None,
        macro_fn: MacroFn | None = None,
        decide_fn: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        schedule: LoopSchedule | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._quote_fn = quote_fn
        # No macro/metric feed wired yet — metric_condition triggers stay
        # UNEVALUABLE (recorded honestly) until a macro seam is injected.
        self._macro_fn = macro_fn
        self._decide_fn = decide_fn
        self._now_fn = now_fn or _utcnow
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict[str, Any]:
        run_at = (now or self._now_fn)()
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        import asyncio

        summary = await asyncio.to_thread(self._run_sync, run_at=run_at)
        self.last_output_summary = summary
        _log.info("verdict_trigger_sweep.tick.done", user_id=self.user_id, **{
            k: v for k, v in summary.items() if k != "results"
        })
        return summary

    def _run_sync(self, *, run_at: datetime) -> dict[str, Any]:
        # Construction is INSIDE the try (defect #5): a DB-engine / session
        # build failure must be caught and returned as a failure summary — the
        # job never raises out of tick()/to_thread.
        sess: Session | None = None
        try:
            factory = self._session_factory or _default_session_factory()
            sess = factory()
            quote_fn = self._quote_fn or _default_quote_fn(sess)
            summary = sweep_and_fire(
                sess,
                self.user_id,
                now=run_at,
                quote_fn=quote_fn,
                macro_fn=self._macro_fn,
                decide_fn=self._decide_fn,
            )
            sess.commit()
            return summary
        except Exception as exc:  # noqa: BLE001
            if sess is not None:
                try:
                    sess.rollback()
                except Exception:  # noqa: BLE001
                    pass
            _log.exception(
                "verdict_trigger_sweep.tick_failed", error=str(exc)[:200]
            )
            return {"error": str(exc)[:200]}
        finally:
            if sess is not None:
                sess.close()


def run_verdict_trigger_sweep_now(
    *, user_id: str = "ariel", session_factory=None
) -> dict[str, Any] | None:
    """Manual-trigger entry (the /api/jobs '{name}/trigger' route + backfills)."""
    import asyncio

    loop = VerdictTriggerSweepLoop(user_id=user_id, session_factory=session_factory)
    return asyncio.run(loop.tick())


__all__ = [
    "VerdictTriggerSweepLoop",
    "verdict_trigger_sweep_metadata",
    "run_verdict_trigger_sweep_now",
]
