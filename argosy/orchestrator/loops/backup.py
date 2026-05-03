"""Daily backup loop (SDD §14.4, Phase 7).

Cron `0 3 * * *` (03:00). Snapshots the SQLite DB to
`${ARGOSY_HOME}/backups/argosy-YYYYMMDD.db` (or to
`agent_settings.backups.backups_dir` when set).

Retention enforcement (default):
  - 30 daily
  - 12 weekly  (Sunday)
  - 12 monthly (1st of month)
  - indefinite annual (Jan 1)

Old files outside retention are deleted.

Weekly off-machine snapshot path is configurable via
`agent_settings.backups.offsite_path`; when set, the loop also
`shutil.copy2`s the day's snapshot to that path (Sundays per SDD §14.4
"weekly off-machine").
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.config import get_settings
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

_log = get_logger("argosy.loops.backup")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_DATE_RE = re.compile(r"argosy-(\d{8})\.db$")


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
        backup_dir = backup_dir.resolve()
        return db_path, backup_dir

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        if not self.settings.backups.enabled:
            _log.info("backup.disabled")
            return

        moment = (now or _utcnow)()
        db_path, backup_dir = self._resolve_paths()

        backup_dir.mkdir(parents=True, exist_ok=True)
        date_str = moment.strftime("%Y%m%d")
        target = backup_dir / f"argosy-{date_str}.db"

        try:
            self._backup_fn(db_path, target)
        except FileNotFoundError:
            # DB doesn't exist yet (e.g., very first run); record + skip.
            _log.warning("backup.db_missing", db=str(db_path))
            return
        except Exception as exc:  # pragma: no cover - defensive
            _log.exception("backup.failed")
            await record_audit_event(
                user_id=self.user_id,
                event_type="backup.failed",
                entity_type="backup",
                entity_id=str(target),
                payload={"error": str(exc), "now": moment.isoformat()},
            )
            return

        # Off-site copy on Sundays when configured.
        offsite = (self.settings.backups.offsite_path or "").strip()
        if offsite and moment.weekday() == 6:  # Sunday
            try:
                offsite_dir = Path(offsite).expanduser().resolve()
                offsite_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, offsite_dir / target.name)
            except Exception:  # pragma: no cover - defensive
                _log.exception("backup.offsite_copy_failed")

        # Retention rotation.
        deleted = self._enforce_retention(backup_dir, moment=moment)

        await record_audit_event(
            user_id=self.user_id,
            event_type="backup.completed",
            entity_type="backup",
            entity_id=str(target),
            payload={
                "path": str(target),
                "now": moment.isoformat(),
                "deleted_count": len(deleted),
            },
        )
        _log.info("backup.completed", path=str(target), deleted=len(deleted))

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def _enforce_retention(self, backup_dir: Path, *, moment: datetime) -> list[Path]:
        """Apply the SDD retention policy. Returns the list of deleted paths."""
        keep_daily = int(self.settings.backups.retention_daily or 0)
        keep_weekly = int(self.settings.backups.retention_weekly or 0)
        keep_monthly = int(self.settings.backups.retention_monthly or 0)

        files: list[tuple[datetime, Path]] = []
        for p in backup_dir.glob("argosy-*.db"):
            m = _DATE_RE.search(p.name)
            if not m:
                continue
            try:
                d = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:  # pragma: no cover - defensive
                continue
            files.append((d, p))

        # Newest first.
        files.sort(key=lambda x: x[0], reverse=True)

        keep: set[Path] = set()
        # Daily: latest N
        for d, p in files[:keep_daily]:
            keep.add(p)
        # Weekly: latest N Sundays
        sundays = [(d, p) for d, p in files if d.weekday() == 6]
        for d, p in sundays[:keep_weekly]:
            keep.add(p)
        # Monthly: latest N 1st-of-month
        firsts = [(d, p) for d, p in files if d.day == 1]
        for d, p in firsts[:keep_monthly]:
            keep.add(p)
        # Annual: every Jan 1, indefinite
        for d, p in files:
            if d.month == 1 and d.day == 1:
                keep.add(p)

        deleted: list[Path] = []
        for d, p in files:
            if p in keep:
                continue
            try:
                p.unlink()
                deleted.append(p)
            except OSError:  # pragma: no cover - defensive
                continue
        return deleted


def _default_backup_fn(src: Path, dst: Path) -> None:
    """Default backup: SQLite `.backup` API; falls back to `shutil.copy2`."""
    if not src.exists():
        raise FileNotFoundError(str(src))
    try:
        import sqlite3

        # Note: must use the sync sqlite3 module, NOT aiosqlite, because
        # `.backup()` is a blocking native operation. We accept the brief
        # block here (DB is small) — the loop runs at 03:00 anyway.
        with sqlite3.connect(str(src)) as src_conn:
            with sqlite3.connect(str(dst)) as dst_conn:
                src_conn.backup(dst_conn)
    except Exception:  # pragma: no cover - defensive fallback
        _log.warning("backup.sqlite_backup_fallback_to_copy")
        shutil.copy2(src, dst)


__all__ = ["BackupLoop"]
