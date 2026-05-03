"""Phase 6: watchdog signal collection + breach detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from argosy.orchestrator.watchdog import (
    WatchdogSignals,
    collect_signals,
    compute_breaches,
    run_watchdog,
)
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, CadenceState, User


@pytest.mark.asyncio
async def test_collect_signals_baseline(engine: None) -> None:
    async with db_mod.get_session() as s:
        s.add(User(id="ariel"))
        await s.commit()

    sig = await collect_signals("ariel")
    assert sig.user_id == "ariel"
    # No prior heartbeat audit -> None.
    assert sig.engine_heartbeat_age_s is None
    # No backup audit -> backup_age_hours is None and a breach is raised.
    assert sig.backup_age_hours is None
    breach_signals = {b["signal"] for b in sig.breaches}
    assert "backup_failed" in breach_signals


@pytest.mark.asyncio
async def test_engine_heartbeat_breach(engine: None) -> None:
    now = datetime.now(timezone.utc)
    async with db_mod.get_session() as s:
        s.add(User(id="ariel"))
        s.add(
            AuditLog(
                user_id="ariel",
                event_type="engine.heartbeat",
                entity_type="engine",
                entity_id="ariel",
                payload_json="{}",
                created_at=now - timedelta(minutes=10),
            )
        )
        await s.commit()

    sig = await collect_signals("ariel")
    assert sig.engine_heartbeat_age_s is not None
    assert sig.engine_heartbeat_age_s >= 600
    breach_signals = {b["signal"] for b in sig.breaches}
    assert "engine_heartbeat" in breach_signals


@pytest.mark.asyncio
async def test_cadence_loop_stuck(engine: None) -> None:
    now = datetime.now(timezone.utc)
    async with db_mod.get_session() as s:
        s.add(User(id="ariel"))
        s.add(
            CadenceState(
                loop_name="hour",
                last_tick_at=now - timedelta(hours=2),
            )
        )
        await s.commit()

    sig = await collect_signals("ariel")
    assert "hour" in sig.cadence_loops_stuck


def test_compute_breaches_monthly_spend_warn() -> None:
    sig = WatchdogSignals(
        user_id="x",
        now="now",
        backup_age_hours=1.0,  # avoid backup breach noise
        claude_monthly_budget_usd=100.0,
        claude_monthly_spend_usd=85.0,
        claude_monthly_spend_pct=85.0,
    )
    breaches = compute_breaches(sig)
    levels = {b["signal"]: b["severity"] for b in breaches}
    assert levels.get("claude_monthly_spend") == "warning"


def test_compute_breaches_monthly_spend_critical() -> None:
    sig = WatchdogSignals(
        user_id="x",
        now="now",
        backup_age_hours=1.0,
        claude_monthly_budget_usd=100.0,
        claude_monthly_spend_usd=110.0,
        claude_monthly_spend_pct=110.0,
    )
    breaches = compute_breaches(sig)
    levels = {b["signal"]: b["severity"] for b in breaches}
    assert levels.get("claude_monthly_spend") == "critical"


def test_compute_breaches_disk_space() -> None:
    sig = WatchdogSignals(
        user_id="x", now="n", backup_age_hours=1.0, disk_space_pct_free=10.0
    )
    breaches = compute_breaches(sig)
    assert any(b["signal"] == "disk_space" for b in breaches)


@pytest.mark.asyncio
async def test_run_watchdog_once_sends_email(engine: None) -> None:
    async with db_mod.get_session() as s:
        s.add(User(id="ariel"))
        await s.commit()

    captured: list[tuple[str, str, str]] = []

    async def stub_sender(user_id: str, subject: str, body: str) -> None:
        captured.append((user_id, subject, body))

    await run_watchdog("ariel", once=True, email_sender=stub_sender)
    # Backup-not-recorded should fire and trigger an email.
    assert len(captured) == 1
    user_id, subject, body = captured[0]
    assert user_id == "ariel"
    assert "Argosy" in subject
    assert "backup" in body.lower()
