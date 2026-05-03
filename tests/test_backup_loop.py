"""BackupLoop tests — temp dir, fake clock, retention rotation."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from argosy.agent_settings import AgentSettings, BackupsBlock
from argosy.orchestrator.loops.backup import BackupLoop
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, User


def _fake_backup_fn(src: Path, dst: Path) -> None:
    """Test backup: just write the date to the file. Skips if src missing."""
    if not src.exists():
        # Simulate "DB exists" by writing a dummy file.
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"FAKE-DB")
    shutil.copy2(src, dst)


@pytest.mark.asyncio
async def test_backup_creates_dated_file_and_audit(tmp_path: Path, engine: None) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    db_file = tmp_path / "src.db"
    db_file.write_bytes(b"FAKE-DB-INITIAL")
    backup_dir = tmp_path / "backups"

    settings = AgentSettings(backups=BackupsBlock(enabled=True))
    loop = BackupLoop(
        schedule=LoopSchedule(cron="0 3 * * *"),
        user_id="ariel",
        settings=settings,
        backup_dir=backup_dir,
        db_path=db_file,
        backup_fn=_fake_backup_fn,
    )

    fixed_now = datetime(2026, 5, 4, 3, 0, tzinfo=timezone.utc)  # Monday
    await loop.tick(now=lambda: fixed_now)

    expected = backup_dir / "argosy-20260504.db"
    assert expected.is_file()

    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "backup.completed")
            )
        ).scalars().all()
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_backup_retention_keeps_daily_30_drops_older(
    tmp_path: Path, engine: None
) -> None:
    """Older daily files outside retention (and not weekly/monthly/annual) are deleted."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    db_file = tmp_path / "src.db"
    db_file.write_bytes(b"FAKE")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Pre-create 60 daily backup files (older than the new tick).
    base = datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)  # Thursday
    fixtures: list[Path] = []
    for i in range(60):
        d = base - timedelta(days=i + 1)
        # Skip weeklies / monthlies / annuals so we know which should survive.
        if d.weekday() == 6 or d.day == 1 or (d.month == 1 and d.day == 1):
            continue
        p = backup_dir / f"argosy-{d.strftime('%Y%m%d')}.db"
        p.write_bytes(b"FAKE")
        fixtures.append(p)

    settings = AgentSettings(
        backups=BackupsBlock(
            enabled=True, retention_daily=30, retention_weekly=12, retention_monthly=12
        )
    )
    loop = BackupLoop(
        schedule=LoopSchedule(cron="0 3 * * *"),
        user_id="ariel",
        settings=settings,
        backup_dir=backup_dir,
        db_path=db_file,
        backup_fn=_fake_backup_fn,
    )
    await loop.tick(now=lambda: base)

    surviving = sorted(p.name for p in backup_dir.glob("argosy-*.db"))
    # Today's file is in there.
    assert f"argosy-{base.strftime('%Y%m%d')}.db" in surviving
    # Some old fixtures should have been deleted.
    fixture_names = {p.name for p in fixtures}
    survived_fixtures = [n for n in surviving if n in fixture_names]
    # We pre-created up to 60 daily-only files; retention_daily=30 keeps at
    # most the 30 newest dailies (minus today). At least some of the
    # pre-created ones must be gone.
    assert len(survived_fixtures) < len(fixtures)


@pytest.mark.asyncio
async def test_backup_keeps_weekly_monthly_annual(tmp_path: Path, engine: None) -> None:
    """Weekly (Sun), monthly (1st), and annual (Jan 1) files survive even
    when the daily retention window has rolled past the date.

    We use retention_daily=1 so only today's snapshot stays in the daily
    bucket; the strategic anchor dates must be retained by their
    weekly/monthly/annual buckets instead.
    """
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    db_file = tmp_path / "src.db"
    db_file.write_bytes(b"FAKE")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Pre-create files at strategic dates.
    annual = backup_dir / "argosy-20240101.db"  # Jan 1 2024 — annual (also Mon, also 1st)
    monthly = backup_dir / "argosy-20240301.db"  # Fri 1 Mar 2024 — 1st of month
    sunday = backup_dir / "argosy-20240204.db"  # Sun
    daily_old = backup_dir / "argosy-20230615.db"  # Thu — neither 1st nor Sun nor Jan 1
    for p in (annual, monthly, sunday, daily_old):
        p.write_bytes(b"FAKE")

    settings = AgentSettings(
        backups=BackupsBlock(
            enabled=True, retention_daily=1, retention_weekly=12, retention_monthly=12
        )
    )
    loop = BackupLoop(
        schedule=LoopSchedule(cron="0 3 * * *"),
        user_id="ariel",
        settings=settings,
        backup_dir=backup_dir,
        db_path=db_file,
        backup_fn=_fake_backup_fn,
    )
    await loop.tick(now=lambda: datetime(2026, 5, 4, 3, 0, tzinfo=timezone.utc))

    assert annual.is_file(), "annual snapshot must survive indefinitely"
    assert monthly.is_file(), "monthly 1st-of-month must survive within 12-month window"
    assert sunday.is_file(), "weekly Sunday must survive within 12-week window"
    # daily_old is not a Sunday, not a 1st-of-month, not Jan 1 → it falls
    # out of every retention bucket once retention_daily=1 keeps only today.
    assert not daily_old.is_file()
