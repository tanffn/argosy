"""Daily backup loop (SDD §14.4, Phase 7).

Cron `0 3 * * *` (03:00). Snapshots the SQLite DB to
`${ARGOSY_HOME}/backups/argosy-YYYYMMDD.db.gz` (or to
`agent_settings.backups.backups_dir` when set).

Retention enforcement (default):
  - 7 daily
  - 4 weekly  (Sunday)
  - 3 monthly (1st of month)
  - indefinite annual (Jan 1)

Old files outside retention are deleted.
Verified online snapshots never fall back to raw-copying a live database.
The production loop also archives transcripts older than 30 days at most weekly.

Weekly off-machine snapshot path is configurable via
`agent_settings.backups.offsite_path`; when set, the loop also
byte-verifies and atomically publishes the day's snapshot there (Sundays per SDD §14.4
"weekly off-machine").
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.config import get_settings
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.backup_storage import copy_artifact, plain_path

_log = get_logger("argosy.loops.backup")


def _utcnow() -> datetime:
    return datetime.now(UTC)


_DATE_RE = re.compile(r"argosy-(\d{8})\.db(?:\.gz)?$")


class BackupLoop(CadenceLoop):
    """Daily SQLite backup with retention rotation."""

    name = "backup"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        settings: AgentSettings | None = None,
        backup_dir: Path | None = None,
        db_path: Path | None = None,
        backup_fn: Callable[[Path, Path], None] | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self.settings = settings or load_agent_settings(user_id)
        self._backup_dir = backup_dir
        self._db_path = db_path
        # backup_fn(src, dst) — defaults to a real sqlite3.backup, but
        # tests override with a simple copy so they can run in-memory.
        self._backup_fn = backup_fn or _default_backup_fn

    def _resolve_paths(self) -> tuple[Path, Path]:
        cfg = get_settings()
        db_path = self._db_path or cfg.db_file
        if self._backup_dir is not None:
            backup_dir = self._backup_dir
        elif self.settings.backups.backups_dir:
            backup_dir = Path(self.settings.backups.backups_dir).expanduser()
        else:
            backup_dir = cfg.backups_dir
        backup_dir = plain_path(backup_dir)
        return db_path, backup_dir

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        if not self.settings.backups.enabled:
            _log.info("backup.disabled")
            return

        moment = (now or _utcnow)()
        db_path, backup_dir = self._resolve_paths()

        backup_dir.mkdir(parents=True, exist_ok=True)
        date_str = moment.strftime("%Y%m%d")
        target = backup_dir / f"argosy-{date_str}.db.gz"

        try:
            await asyncio.to_thread(self._backup_fn, db_path, target)
        except FileNotFoundError:
            # DB doesn't exist yet (e.g., very first run); record + skip.
            _log.warning("backup.db_missing", db=str(db_path))
            raise
        except Exception as exc:  # pragma: no cover - defensive
            _log.exception("backup.failed")
            await record_audit_event(
                user_id=self.user_id,
                event_type="backup.failed",
                entity_type="backup",
                entity_id=str(target),
                payload={"error": str(exc), "now": moment.isoformat()},
            )
            raise

        # Off-site copy on Sundays when configured.
        offsite = (self.settings.backups.offsite_path or "").strip()
        if offsite and moment.weekday() == 6:  # Sunday
            try:
                offsite_dir = plain_path(Path(offsite).expanduser())
                await asyncio.to_thread(copy_artifact, target, offsite_dir / target.name)
            except Exception:  # pragma: no cover - defensive
                _log.exception("backup.offsite_copy_failed")
                raise

        # Retention rotation.
        deleted = self._enforce_retention(backup_dir, moment=moment)

        # Same daily recovery path; archive at most weekly, including catch-up.
        from argosy.services.transcript_archive import archive_transcripts
        transcripts = await asyncio.to_thread(
            archive_transcripts, Path(get_settings().home) / "transcripts", now=moment,
        ) if self._backup_dir is None else {"status": "custom_backup_path"}

        await record_audit_event(
            user_id=self.user_id,
            event_type="backup.completed",
            entity_type="backup",
            entity_id=str(target),
            payload={
                "path": str(target),
                "now": moment.isoformat(),
                "deleted_count": len(deleted),
                "transcripts": transcripts,
            },
        )
        _log.info("backup.completed", path=str(target), deleted=len(deleted))

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def _enforce_retention(self, backup_dir: Path, *, moment: datetime) -> list[Path]:
        """Apply the SDD retention policy. Returns the list of deleted paths."""
        backup_dir = plain_path(backup_dir)
        keep_daily = int(self.settings.backups.retention_daily or 0)
        keep_weekly = int(self.settings.backups.retention_weekly or 0)
        keep_monthly = int(self.settings.backups.retention_monthly or 0)

        files: list[tuple[datetime, Path]] = []
        for p in backup_dir.glob("argosy-*.db*"):
            m = _DATE_RE.fullmatch(p.name)
            if not m:
                continue
            try:
                d = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=UTC)
            except ValueError:  # pragma: no cover - defensive
                continue
            files.append((d, p))

        # Newest first.
        files.sort(key=lambda x: x[0], reverse=True)

        dates = sorted({d for d, _ in files}, reverse=True)
        keep: set[datetime] = set()
        # Daily: latest N
        keep.update(dates[:keep_daily])
        # Weekly: latest N Sundays
        keep.update([d for d in dates if d.weekday() == 6][:keep_weekly])
        # Monthly: latest N 1st-of-month
        keep.update([d for d in dates if d.day == 1][:keep_monthly])
        # Annual: every Jan 1, indefinite
        for d, _p in files:
            if d.month == 1 and d.day == 1:
                keep.add(d)

        deleted: list[Path] = []
        for d, p in files:
            if d in keep:
                continue
            try:
                p.unlink()
                deleted.append(p)
            except OSError:  # pragma: no cover - defensive
                continue
        return deleted


def _default_backup_fn(src: Path, dst: Path) -> None:
    """Consistent, compressed, verified snapshot; never fall back to an unsafe copy."""
    from argosy.services.backup_storage import snapshot
    snapshot(src, dst)


__all__ = ["BackupLoop"]
