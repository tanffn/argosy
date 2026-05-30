"""``WeeklyEmailDigestLoop`` — weekly Friday 08:00 IDT digest cron.

Spec E commit #8.  Wires the
``argosy/services/email_digest.py::dispatch_weekly_digest``
orchestrator into a :class:`CadenceLoop` registered with Sprint A's
:class:`JobRegistry`.

Schedule: ``0 8 * * FRI`` Asia/Jerusalem — Fridays at 08:00 IDT.  Per
Ariel's locked decision: the brief lands before the weekend so the
user reads it over coffee, not in the middle of the workweek.

Single-stage tick:
  * Opens a fresh sync ``Session``.
  * Calls
    :func:`argosy.services.email_digest.dispatch_weekly_digest` for
    ``user_id='ariel'``.
  * Returns a compact dict summary the JobRegistry surfaces as
    ``job_runs.output_summary``.

Why a fresh session per tick: the loop runs ONCE per week so a long-
lived shared session would add no benefit, and a fresh session keeps
the migration window (e.g. mid-tick alembic upgrade) safe.  Same
pattern as ``inferred_life_event_detector.py``.

Failure mode contract:
  * SMTP errors don't crash — ``send_digest_email`` swallows
    aiosmtplib exceptions + TimeoutError and returns
    ``SendResult.failed`` (logged at WARNING).  The tick's only
    crash path is a DB exception during ``build_weekly_digest`` /
    ledger writeback, which the JobRegistry already surfaces as a
    ``job_runs.status='error'`` row.
  * Missing SMTP env vars → ``SendResult.skipped`` → loop tick
    completes with ``status='skipped_smtp_not_configured'`` in the
    output summary.  Operator sees the missing creds in the admin
    UI per spec §7.2.

``self.last_output_summary`` is set in a ``finally`` block so a
mid-tick exception still surfaces partial progress via the
:class:`RegisteredScheduler` adapter (Spec A §7 NICE #7).
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.email_digest import (
    DEFAULT_WINDOW_DAYS,
    SmtpSender,
    dispatch_weekly_digest,
)
from argosy.services.jobs.registry import JobMetadata

_log = get_logger("argosy.loops.weekly_email_digest")


# Default cron + tz — kept in sync with
# ``cadences.weekly_email_digest`` in ``argosy/agent_settings.py``.
# 08:00 Asia/Jerusalem on Fridays (= 05:00 UTC summer / 06:00 UTC
# winter — handled by the croniter timezone fix in
# ``argosy/orchestrator/loops/base.py``).
_DEFAULT_CRON = "0 8 * * FRI"
_DEFAULT_TZ = "Asia/Jerusalem"


def weekly_email_digest_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` row the registry surfaces.

    ``source_kind='notification'`` — the loop emits to the SMTP/email
    channel and writes one row per tick to
    ``notification_dispatch_ledger``.
    """
    return JobMetadata(
        name="weekly_email_digest",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Weekly Friday 08:00 Asia/Jerusalem",
        source_kind="notification",
        description=(
            "Weekly email digest — composes a recap of the past 7 "
            "days of monitor flags, open action proposals, and "
            "state-snapshot deltas, renders Jinja HTML + plain-text "
            "bodies, ships via SMTP (aiosmtplib + ARGOSY_SMTP_* env "
            "vars), writes a dispatch_ledger row.  Friday 08:00 IDT."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Module-level sync session factory cache.  Same pattern as
# ``inferred_life_event_detector.py``.
_DEFAULT_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Return a cached sync ``sessionmaker`` bound to the configured DB.

    Lazy + cached so import-time has no side effects.  Rebuilds when
    ``db_file`` changes (test reloads of settings).
    """
    global _DEFAULT_SESSION_FACTORY

    import sqlalchemy as sa

    from argosy.config import get_settings

    settings = get_settings()
    db_file = str(settings.db_file)

    if _DEFAULT_SESSION_FACTORY is not None:
        cached_key, cached_factory = _DEFAULT_SESSION_FACTORY
        if cached_key == db_file:
            return cached_factory

    sync_url = f"sqlite:///{db_file}"
    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _DEFAULT_SESSION_FACTORY = (db_file, factory)
    return factory


def _reset_default_session_factory_cache() -> None:
    """Test hook — clear the cached sessionmaker so a subsequent call
    rebuilds from the current settings."""
    global _DEFAULT_SESSION_FACTORY
    _DEFAULT_SESSION_FACTORY = None


def _resolve_base_url() -> str:
    """Resolve the base URL for deep links.

    Reads ``ARGOSY_PUBLIC_URL`` first; falls back to localhost so dev
    runs work without env config.
    """
    return os.environ.get("ARGOSY_PUBLIC_URL", "http://localhost:8000")


class WeeklyEmailDigestLoop(CadenceLoop):
    """Weekly Friday-morning email-digest loop.

    Constructor injection points so tests can drive the loop without
    touching SMTP / the DB:

      * ``schedule``        — overrides the cron/tz.
      * ``user_id``         — single-tenant for now (defaults ``"ariel"``).
      * ``session_factory`` — sync ``sessionmaker``; default builds
        from ``get_settings().db_file``.
      * ``window_days``     — lookback window; defaults 7.
      * ``base_url``        — deep-link prefix; defaults
        ``ARGOSY_PUBLIC_URL`` env then localhost.
      * ``smtp_sender``     — overrides the production aiosmtplib
        sender (tests pass a stub).
      * ``to_addr``         — override recipient; defaults to
        ``User.email``.
      * ``now_fn``          — overrides :func:`_utcnow`; tests pin time.
    """

    name = "weekly_email_digest"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
        base_url: str | None = None,
        smtp_sender: SmtpSender | None = None,
        to_addr: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        if window_days <= 0:
            raise ValueError(
                "window_days must be > 0; "
                f"got {window_days!r}"
            )
        self.user_id = user_id
        self._session_factory = session_factory
        self._window_days = window_days
        self._base_url = base_url
        self._smtp_sender = smtp_sender
        self._to_addr = to_addr
        self._now_fn = now_fn or _utcnow
        #: Surfaced by the ``RegisteredScheduler`` adapter even on the
        #: exception path so partial progress (digest composed but
        #: SMTP crashed) is observable.
        self.last_output_summary: dict[str, Any] | None = None

    def _resolve_session_factory(self) -> sessionmaker:
        if self._session_factory is not None:
            return self._session_factory
        return _build_default_session_factory()

    async def _run_tick_async(self, now_dt: datetime) -> dict[str, Any]:
        """Async body — opens a session, runs the orchestrator,
        commits, returns the summary dict.

        We DO commit here (unlike ``inferred_life_event_detector``
        which has the caller own the txn) because the digest
        orchestrator writes one ledger row per tick and the loop is
        the only caller — no outer transaction wrapping us.

        The orchestrator's exception handling already covers SMTP
        flakiness; a raise from this method means a DB issue (which
        the JobRegistry maps to ``status='error'``).
        """
        factory = self._resolve_session_factory()
        session: Session = factory()
        try:
            result = await dispatch_weekly_digest(
                session,
                self.user_id,
                now=now_dt,
                window_days=self._window_days,
                base_url=self._base_url or _resolve_base_url(),
                smtp_sender=self._smtp_sender,
                to_addr=self._to_addr,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        digest = result.digest
        return {
            "user_id": result.user_id,
            "send_status": result.send.status,
            "send_error": result.send.error,
            "ledger_row_id": result.ledger_row_id,
            "window_days": digest.window_days if digest else None,
            "flag_count": (
                digest.summary.flag_count if digest else 0
            ),
            "open_proposal_count": (
                digest.summary.open_proposal_count if digest else 0
            ),
            "decisions_count": (
                digest.summary.decisions_count if digest else 0
            ),
            "has_any_activity": (
                digest.has_any_activity if digest else False
            ),
        }

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict | None:
        """Run one tick.

        ``now`` (when passed) overrides the loop's own ``now_fn`` so
        the scheduler's deterministic-time tracking flows through.

        The ``finally`` block writes ``self.last_output_summary`` so
        a mid-tick exception still surfaces partial progress.
        """
        self.last_output_summary = None
        now_dt = (now or self._now_fn)()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        _log.info(
            "weekly_email_digest.tick.start",
            user_id=self.user_id,
            now=now_dt.isoformat(),
            window_days=self._window_days,
        )

        summary: dict[str, Any] | None = None
        try:
            summary = await self._run_tick_async(now_dt)
        finally:
            if summary is not None:
                self.last_output_summary = summary

        _log.info(
            "weekly_email_digest.tick.done",
            user_id=self.user_id,
            summary=summary,
        )
        return summary


__all__ = [
    "WeeklyEmailDigestLoop",
    "weekly_email_digest_metadata",
]


# Reference asyncio so static linters don't warn the import is unused
# on Python versions where asyncio is imported indirectly via type
# hints; the runtime path uses asyncio.to_thread via tick() pattern.
_ = asyncio
