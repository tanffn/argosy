"""All-holdings verdict-COVERAGE sweep loop.

Sibling of ``verdict_trigger_sweep`` (which escalates TRIPPED typed triggers).
This loop closes a DIFFERENT gap: the daily ``thesis_monitor`` reasons on
individual STOCKS only, so ETFs / funds / bonds / REITs (and the durable
unmanaged NVDA) can sit with NO standing verdict forever. Each tick runs the
cheap, deterministic, read-only
:func:`argosy.services.verdict_coverage.holdings_coverage_report` over EVERY
held symbol from the conserved current book, then — capped by ``limit`` (cost
guard), most-overdue first — escalates a full re-verdict
(``run_deep_decision``) for the ``UNCOVERED`` / ``STALE`` names. Only those
capped names pay the LLM cost; the report itself is nearly free.

Pure-seam design (mirrors ThesisMonitorLoop / VerdictTriggerSweepLoop):
``session_factory`` / ``decide_fn`` / ``now_fn`` are injectable so the loop is
unit-testable with no LLM, no network, and no live DB.

Schedule: DAILY (08:00 Asia/Jerusalem) — matches the sibling
``verdict_trigger_sweep`` cadence and, crucially, honors ``RETRY_COOLDOWN_DAYS=2``
(a 2-day retry window is meaningless under a 7-day cadence: a failed name would
always be retryable by the next sweep, defeating the short-suppression purpose).
Cost stays bounded regardless of cadence because the per-run ``limit`` caps how
many names pay the Opus-fleet cost each sweep, and a ``checked`` cooldown keeps a
covered/defended name out of the candidate set for the full ``max_age_days``
horizon — so a daily cadence just lets the book converge to full coverage faster,
it does NOT multiply spend.

Concurrency / pre-call race: the scheduled sweep runs inside the single
in-process asyncio ``Scheduler`` (one registered ``HoldingsCoverageSweepLoop``
instance, guarded by the per-job lock so a tick never overlaps ITSELF). The
cooldown marker is written AFTER the fleet call by design. Accepted LOW-RISK
because no concurrent caller exists in production. Note the standalone
``run_holdings_coverage_sweep_now()`` helper constructs an INDEPENDENT loop
object and therefore BYPASSES the scheduler's per-job lock: if it were ever
invoked concurrently with the scheduled tick (or with itself), the two sweeps
would NOT arbitrate before firing, so they could each fire the fleet on the same
overdue names and duplicate up to ``DEFAULT_LIMIT`` (3) expensive fleet calls
before the unique ``(dedup_key, status='open')`` markers converge — not just
one. This is safe ONLY because there is currently no concurrent caller (the
helper is manual-trigger / backfill, single-shot). No atomic pre-claim is added
to avoid over-engineering an effectively single-instance loop.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.services.verdict_coverage import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_AGE_DAYS,
    ensure_coverage,
)

_log = get_logger("argosy.loops.holdings_coverage_sweep")
# DEFECT D — DAILY at 08:00 IDT (was weekly "0 8 * * 0"). Daily matches the
# sibling verdict_trigger_sweep and makes RETRY_COOLDOWN_DAYS=2 meaningful; the
# per-run limit + checked-cooldown keep LLM spend bounded (see module docstring).
_DEFAULT_CRON = "0 8 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def holdings_coverage_sweep_metadata() -> JobMetadata:
    return JobMetadata(
        name="holdings_coverage_sweep",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 08:00 Asia/Jerusalem",
        source_kind="monitor",
        description=(
            "All-holdings verdict-coverage sweep. Deterministic read-only "
            "report over EVERY held symbol (ETFs / funds / bonds / REITs + "
            "single stocks + unmanaged NVDA; cash + real estate excluded) "
            "classifies coverage as covered / stale / uncovered. Capped by a "
            "per-run limit (most-overdue first), it escalates a full re-verdict "
            "(run_deep_decision) for uncovered/stale names — the rest next run. "
            "ETF A2 fee/tracking metadata limits are recorded honestly, never "
            "faked."
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


class HoldingsCoverageSweepLoop(CadenceLoop):
    """Sweep all holdings; escalate uncovered/stale names to a full re-verdict."""

    name = "holdings_coverage_sweep"

    def __init__(
        self,
        *,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        decide_fn: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
        limit: int = DEFAULT_LIMIT,
        schedule: LoopSchedule | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._decide_fn = decide_fn
        self._now_fn = now_fn or _utcnow
        self._max_age_days = max_age_days
        self._limit = limit
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict[str, Any]:
        import asyncio

        # DEFECT C — the ENTIRE tick body is guarded, including the clock
        # resolution: ``(now or self._now_fn)()`` was previously called OUTSIDE
        # the try, so a raising clock (or tz coercion) escaped tick(). The job
        # contract is "no exception escapes tick()", so wrap it all.
        try:
            run_at = (now or self._now_fn)()
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=timezone.utc)
            summary = await asyncio.to_thread(self._run_sync, run_at=run_at)
        except Exception as exc:  # noqa: BLE001 — NO exception escapes tick()
            _log.exception(
                "holdings_coverage_sweep.tick_failed", error=str(exc)[:200]
            )
            summary = {"error": str(exc)[:200]}
        self.last_output_summary = summary
        _log.info(
            "holdings_coverage_sweep.tick.done",
            user_id=self.user_id,
            **{k: v for k, v in summary.items() if k not in ("report", "escalations")},
        )
        return summary

    def _run_sync(self, *, run_at: datetime) -> dict[str, Any]:
        sess: Session | None = None
        try:
            factory = self._session_factory or _default_session_factory()
            sess = factory()
            summary = ensure_coverage(
                sess,
                self.user_id,
                now=run_at,
                max_age_days=self._max_age_days,
                limit=self._limit,
                decide_fn=self._decide_fn,
            )
            # The report + escalation are read-only against the book; any
            # writes happen inside the fleet (its own sessions). Commit to
            # release the read transaction cleanly.
            sess.commit()
            return summary
        except Exception as exc:  # noqa: BLE001 — the job never raises out of tick()
            if sess is not None:
                try:
                    sess.rollback()
                except Exception:  # noqa: BLE001
                    pass
            _log.exception("holdings_coverage_sweep.tick_failed", error=str(exc)[:200])
            return {"error": str(exc)[:200]}
        finally:
            # DEFECT C — the close() must never escape tick() (the job contract
            # is "no exception escapes"). A session whose close() raises (pool /
            # driver teardown fault) previously propagated out of the finally and
            # past the except above. Swallow + log so tick() always returns.
            if sess is not None:
                try:
                    sess.close()
                except Exception as close_exc:  # noqa: BLE001 — close must not escape
                    _log.warning(
                        "holdings_coverage_sweep.session_close_failed",
                        error=str(close_exc)[:200],
                    )


def run_holdings_coverage_sweep_now(
    *, user_id: str = "ariel", session_factory=None
) -> dict[str, Any] | None:
    """Manual-trigger entry (the /api/jobs '{name}/trigger' route + backfills)."""
    import asyncio

    loop = HoldingsCoverageSweepLoop(user_id=user_id, session_factory=session_factory)
    return asyncio.run(loop.tick())


__all__ = [
    "HoldingsCoverageSweepLoop",
    "holdings_coverage_sweep_metadata",
    "run_holdings_coverage_sweep_now",
]
