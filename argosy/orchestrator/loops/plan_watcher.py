"""plan_watcher — daily cadence loop.

Per spec §3.7: detects when the user's baseline plan source file has
changed on disk and re-runs distillation, preserving user edits.

Cheap when nothing has changed: O(N_users) sha256 of the file contents
against the stored ``source_hash`` column.

Configured in agent_settings.yaml::
    cadences:
      plan_watcher:
        enabled: true
        cron: "0 7 * * *"   # 07:00 user TZ; before daily_brief

Scheduler integration: ``PlanWatcherLoop`` wraps the synchronous ``tick``
function into the async ``CadenceLoop`` interface.  The inner call runs in
a thread via ``asyncio.to_thread`` because ``distill_baseline_plan`` uses
``agent.run_sync`` (which calls ``asyncio.run`` internally and therefore
must not be invoked from inside a running event loop).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state.models import PlanVersion

log = get_logger(__name__)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def tick(session: Session) -> int:
    """One loop iteration. Returns the count of re-distillations triggered.

    Iterates every active baseline plan_version row across all users
    (multi-tenant ready). For each:

      1. If ``source_path`` is empty -> skip (uploaded via UI, no
         disk file to watch).
      2. If the file is missing -> log warning, skip.
      3. If sha256(file_contents) == row.source_hash -> skip (no change).
      4. Else update raw_markdown and call distill_baseline_plan with
         ``preserve_user_edits=True``.
    """
    from argosy.services.plan_distiller_service import distill_baseline_plan

    rerun_count = 0
    rows = (
        session.query(PlanVersion)
        .filter(PlanVersion.role == "baseline")
        .all()
    )
    for pv in rows:
        if not pv.source_path:
            continue

        path = Path(pv.source_path)
        try:
            contents = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.warning(
                "plan_watcher.source_missing",
                user_id=pv.user_id,
                source_path=pv.source_path,
            )
            continue
        except OSError as exc:
            log.warning(
                "plan_watcher.source_unreadable",
                user_id=pv.user_id,
                source_path=pv.source_path,
                error=str(exc),
            )
            continue

        new_hash = _sha256(contents)
        if new_hash == (pv.source_hash or ""):
            continue

        log.info(
            "plan_watcher.diff_detected",
            user_id=pv.user_id,
            plan_version_id=pv.id,
            old_hash=(pv.source_hash or "")[:8],
            new_hash=new_hash[:8],
        )

        # Update the raw_markdown so distill sees fresh content.
        pv.raw_markdown = contents
        session.commit()

        try:
            distill_baseline_plan(
                session=session,
                plan_version_id=pv.id,
                user_id=pv.user_id,
                preserve_user_edits=True,
            )
            rerun_count += 1
        except Exception as exc:  # noqa: BLE001
            log.error(
                "plan_watcher.distill_failed",
                user_id=pv.user_id,
                plan_version_id=pv.id,
                error=str(exc),
            )

    return rerun_count


class PlanWatcherLoop(CadenceLoop):
    """Daily plan-watcher loop for the async scheduler.

    Wraps the synchronous ``tick(session)`` function into the
    ``CadenceLoop`` interface expected by ``Scheduler``.  The sync work
    (including any LLM call inside ``distill_baseline_plan``) is offloaded
    to a thread via ``asyncio.to_thread`` so it does not block the event
    loop and avoids the "event loop already running" error from
    ``agent.run_sync``.
    """

    name = "plan_watcher"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        """Run one plan-watcher tick in a background thread."""
        import asyncio

        from argosy.config import get_settings

        def _run() -> int:
            settings = get_settings()
            sync_url = settings.database_url.replace("+aiosqlite", "")
            engine = sa.create_engine(
                sync_url, connect_args={"check_same_thread": False}
            )
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
            sess = SessionLocal()
            try:
                return tick(sess)
            finally:
                sess.close()
                engine.dispose()

        count = await asyncio.to_thread(_run)
        log.info("plan_watcher.tick_complete", rerun_count=count, user_id=self.user_id)


__all__ = ["tick", "PlanWatcherLoop"]
