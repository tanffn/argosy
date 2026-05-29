"""``JobRunsRetentionLoop`` — daily ``job_runs`` retention + orphan reap.

Sprint A commit #9 (Spec A §2.1, §1.7).

Two complementary maintenance passes run inside one tick:

1. **Delete old ``status='ok'`` rows.** The audit table grows ~1 row per
   tick per registered job; at 14 loops firing minutely the table would
   reach ~6M rows in a year. ``status='ok'`` rows older than
   ``retention_days_ok`` (default 30 days) are deleted. ``status='error'``
   rows are KEPT FOREVER — the operator needs the historical failure
   trail for postmortems. ``status='skipped'`` + ``status='cancelled'``
   are also retained beyond the window (cheap; helps debug "why didn't
   this fire?" questions).

2. **Reap stale ``status='running'`` rows.** Per §1.7 failure matrix
   row "close fails, record_tick ok", a tick whose ``_close_job_run``
   raises leaves an orphan ``running`` row. This pass flips
   ``status='running' AND started_at < now - stale_running_hours`` (default
   24h) to ``status='cancelled'`` with ``error_message='reaped: stale
   running row'``.

   **LongRunningJob exclusion (codex review BLOCKER #1).** A
   :class:`LongRunningJob` supervisor's cycle row genuinely stays in
   ``status='running'`` for the duration of a connect cycle — a healthy
   Discord WS connection lasting >24h is the expected case, not a stalled
   tick. Reaping those rows would force-flip a live cycle to
   ``cancelled``, violating supervisor invariants. The loop accepts a
   ``long_running_names_fn`` callable that returns the current set of
   ``LongRunningJob`` names; rows for those jobs are EXCLUDED from the
   reap pass even when their ``started_at`` is beyond the 24h cutoff.
   The startup hook in ``argosy/api/main.py`` threads the registry's
   current set; tests pass an explicit set.

   The reap pass therefore targets only ``CadenceLoop`` orphan rows —
   the §1.7 matrix's "close-path failed" case for short ticks that
   should have closed within seconds.

Idempotent: a second tick in the same day finds no work and is a
no-op (DELETE / UPDATE against an empty result set). The cron is
``30 3 * * *`` (03:30 Asia/Jerusalem daily) — 30 minutes after the
backup loop's 03:00 snapshot so the backup captures the pre-retention
state.

Single-writer note: this loop writes to ``job_runs`` directly via
``UPDATE``/``DELETE`` — NOT through ``_open_job_run`` / ``_close_job_run``
because those helpers are for individual run rows, not bulk maintenance.
The single-writer regex sweep in ``test_jobs_registry`` allow-lists
``argosy/services/jobs/registry.py`` for INSERTs + ORM-constructor
patterns + helper calls; bulk maintenance via ``DELETE``/``UPDATE``
is OUTSIDE the single-writer contract (it doesn't INSERT new rows,
only prunes / marks existing ones).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, update

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.state import db as db_mod
from argosy.state.models import JobRun

_log = get_logger("argosy.loops.job_runs_retention")


# Defaults — kept in sync with ``JobRunsRetentionConfig`` in
# ``argosy/agent_settings.py``. Tests construct the loop with explicit
# args; production reads ``cadences.job_runs_retention`` for cron/tz +
# ``job_runs_retention.retention_days_ok`` / ``.stale_running_hours``.
_DEFAULT_CRON = "30 3 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"
_DEFAULT_RETENTION_DAYS_OK = 30
_DEFAULT_STALE_RUNNING_HOURS = 24


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def job_runs_retention_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` for the retention loop.

    Source kind is ``maintenance`` per §6 mapping table (alongside
    ``backup``, ``audit``, ``minute``, ``hour``).
    """
    return JobMetadata(
        name="job_runs_retention",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 03:30 Asia/Jerusalem",
        source_kind="maintenance",
        description=(
            "Daily prune of job_runs: deletes status='ok' rows older "
            "than retention_days_ok (default 30d); reaps stale "
            "status='running' rows older than stale_running_hours "
            "(default 24h) to status='cancelled'. Status='error' rows "
            "are kept forever."
        ),
        long_running=False,
    )


class JobRunsRetentionLoop(CadenceLoop):
    """Daily maintenance pass over the ``job_runs`` table.

    Constructor accepts optional injection points so tests can drive
    the loop without touching agent_settings:

    * ``schedule``              — overrides the default cron/tz.
    * ``retention_days_ok``     — overrides the 30d delete window.
    * ``stale_running_hours``   — overrides the 24h reap window.
    * ``now_fn``                — overrides :func:`_utcnow`; tests pass a
                                  frozen-clock callable so the windows
                                  resolve deterministically.
    """

    name = "job_runs_retention"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        retention_days_ok: int = _DEFAULT_RETENTION_DAYS_OK,
        stale_running_hours: int = _DEFAULT_STALE_RUNNING_HOURS,
        now_fn: Callable[[], datetime] | None = None,
        long_running_names_fn: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        if retention_days_ok < 0:
            raise ValueError(
                f"retention_days_ok must be >= 0; got {retention_days_ok!r}"
            )
        if stale_running_hours < 0:
            raise ValueError(
                f"stale_running_hours must be >= 0; got {stale_running_hours!r}"
            )
        self._retention_days_ok = retention_days_ok
        self._stale_running_hours = stale_running_hours
        self._now_fn = now_fn or _utcnow
        # Codex review BLOCKER #1 — exclude LongRunningJob rows from the
        # reap pass so a healthy >24h connect cycle isn't force-flipped
        # to 'cancelled'. ``long_running_names_fn`` is called once per
        # tick so a late-registered LongRunningJob (registered after
        # startup) is also excluded. Default returns the empty set —
        # tests that don't care about long-running exclusion can omit
        # this argument.
        self._long_running_names_fn: Callable[[], Iterable[str]] = (
            long_running_names_fn or (lambda: ())
        )
        # Standard side-channel — RegisteredScheduler reads this on the
        # success/exception path if `tick()` doesn't return a dict.
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict | None:
        """Run both passes inside one session; return the summary dict.

        Returns a dict with two keys for ``job_runs.output_summary``:
          * ``deleted_ok_count`` — number of ``status='ok'`` rows pruned
          * ``reaped_running_count`` — number of stale ``running`` rows flipped

        Both passes are idempotent: re-running in the same day finds no
        work and returns ``{deleted_ok_count: 0, reaped_running_count: 0}``.
        """
        # Side-channel reset so an exception path that fires before the
        # finally block doesn't leak the prior tick's summary.
        self.last_output_summary = None

        # Resolve the cutoffs once per tick so both DELETE and UPDATE
        # see the same wall-clock — avoids a hypothetical race where
        # the clock advances mid-tick.
        now_dt = (now or self._now_fn)()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        delete_cutoff = now_dt - timedelta(days=self._retention_days_ok)
        reap_cutoff = now_dt - timedelta(hours=self._stale_running_hours)

        # Codex BLOCKER #1 — resolve the long-running exclusion set per
        # tick so late registrations are picked up. Defensive: if the
        # callable raises, log + proceed with an empty set (the reap
        # may mis-flip; logged so the operator notices).
        try:
            long_running_names: list[str] = sorted(
                set(self._long_running_names_fn())
            )
        except Exception:  # pragma: no cover - defensive
            _log.exception(
                "job_runs_retention.long_running_names_fn_failed"
            )
            long_running_names = []

        _log.info(
            "job_runs_retention.tick.start",
            now=now_dt.isoformat(),
            delete_cutoff=delete_cutoff.isoformat(),
            reap_cutoff=reap_cutoff.isoformat(),
            retention_days_ok=self._retention_days_ok,
            stale_running_hours=self._stale_running_hours,
            excluded_long_running=long_running_names,
        )

        async with db_mod.get_session() as session:
            # Pass 1 — delete old ok rows. ``finished_at`` is the right
            # column to compare against (NOT ``started_at``): a tick
            # that started 31d ago but finished 29d ago is still within
            # the retention window for "we want to be able to inspect
            # the last 30 days of completed work".
            del_stmt = delete(JobRun).where(
                (JobRun.status == "ok")
                & (JobRun.finished_at.is_not(None))
                & (JobRun.finished_at < delete_cutoff)
            )
            del_result = await session.execute(del_stmt)
            deleted_ok_count = int(del_result.rowcount or 0)

            # Pass 2 — reap stale running rows. ``started_at`` is the
            # right column here (NOT ``finished_at``): the row IS still
            # in ``running``, so ``finished_at IS NULL`` by definition;
            # we want "started more than N hours ago and still hasn't
            # closed."
            #
            # Codex BLOCKER #1 — EXCLUDE LongRunningJob rows. A healthy
            # connect cycle row legitimately stays in 'running' for the
            # connection's lifetime; reaping would mis-flip a live row.
            reap_predicate = (JobRun.status == "running") & (
                JobRun.started_at < reap_cutoff
            )
            if long_running_names:
                reap_predicate = reap_predicate & (
                    JobRun.job_name.not_in(long_running_names)
                )
            upd_stmt = (
                update(JobRun)
                .where(reap_predicate)
                .values(
                    status="cancelled",
                    error_message="reaped: stale running row",
                    finished_at=now_dt,
                )
            )
            upd_result = await session.execute(upd_stmt)
            reaped_running_count = int(upd_result.rowcount or 0)

            await session.commit()

        summary: dict[str, Any] = {
            "deleted_ok_count": deleted_ok_count,
            "reaped_running_count": reaped_running_count,
            "windows": {
                "retention_days_ok": self._retention_days_ok,
                "stale_running_hours": self._stale_running_hours,
            },
            "excluded_long_running": long_running_names,
        }
        self.last_output_summary = summary

        _log.info(
            "job_runs_retention.tick.done",
            deleted_ok_count=deleted_ok_count,
            reaped_running_count=reaped_running_count,
        )
        return summary


__all__ = [
    "JobRunsRetentionLoop",
    "job_runs_retention_metadata",
]
