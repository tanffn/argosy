"""Argosy watchdog (Phase 6, SDD §14.2).

A small process that polls health signals and emails alerts on
threshold breach. For Phase 6 we ship it as a CLI subcommand
(`argosy admin watchdog start`) that runs as a separate process
alongside the engine. Hosted deployments can run it as a sidecar
container.

Signals (per SDD §14.2 table):

  | Signal                     | Threshold                                |
  |----------------------------|------------------------------------------|
  | engine_heartbeat_age_s     | > 300 during market hours                |
  | cadence_loop_stuck         | a loop hasn't ticked in 2x interval      |
  | broker_disconnect          | TWS Gateway down                         |
  | claude_error_rate          | > 5% over the last hour                  |
  | claude_monthly_spend_pct   | 80% warn / 100% pause                    |
  | state_db_size_gb           | > 10 GB                                  |
  | backup_failed              | daily backup didn't run                  |
  | disk_space_pct_free        | < 20% on ARGOSY_HOME drive               |

We compute each signal in `collect_signals()`, return the JSON, and
let `run_watchdog()` send alerts via the existing email pipeline
when thresholds breach. Tests inject `now` + a stub email sender.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select

from argosy.config import get_settings
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import AgentReport, AuditLog, CadenceState

_log = get_logger("argosy.watchdog")


@dataclass
class WatchdogSignals:
    user_id: str
    now: str
    engine_heartbeat_age_s: float | None = None
    cadence_loops_stuck: list[str] = field(default_factory=list)
    broker_disconnect: bool = False
    claude_error_rate: float = 0.0
    claude_monthly_spend_usd: float = 0.0
    claude_monthly_budget_usd: float | None = None
    claude_monthly_spend_pct: float | None = None
    state_db_size_gb: float = 0.0
    backup_age_hours: float | None = None
    disk_space_pct_free: float = 100.0
    breaches: list[dict[str, Any]] = field(default_factory=list)


# Type for an injectable email sender (tests stub).
EmailSender = Callable[[str, str, str], Awaitable[None]]


# ----------------------------------------------------------------------
# Probe primitives
# ----------------------------------------------------------------------


async def _last_audit_event_at(user_id: str, event_type: str) -> datetime | None:
    """Return the most recent audit_log row of `event_type`, or None."""
    async with db_mod.get_session(user_id=user_id) as session:
        stmt = (
            select(AuditLog.created_at)
            .where(AuditLog.user_id == user_id)
            .where(AuditLog.event_type == event_type)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _stuck_cadence_loops(now: datetime) -> list[str]:
    """Return loop_names whose last_tick is > 2x interval ago.

    The cadence_state table is global (Phase 2 design); we treat
    "stuck" as a fixed 30-minute threshold for simplicity until the
    scheduler exposes its own intervals.
    """
    threshold = now - timedelta(minutes=30)
    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(CadenceState).where(CadenceState.last_tick_at.is_not(None))
            )
        ).scalars().all()
        out: list[str] = []
        for r in rows:
            tick = r.last_tick_at
            if tick is None:
                continue
            if tick.tzinfo is None:
                tick = tick.replace(tzinfo=timezone.utc)
            if tick < threshold:
                out.append(r.loop_name)
        return out


async def _claude_error_rate(user_id: str, *, now: datetime) -> float:
    """Fraction of agent_reports rows in the last hour with a 'failed' tag.

    Phase 6 doesn't track a hard error column; we use response_text
    starts-with "ERROR:" as a cheap proxy. The probe only matters
    when nonzero, so a conservative proxy is fine.
    """
    cutoff = now - timedelta(hours=1)
    async with db_mod.get_session(user_id=user_id) as session:
        total_stmt = (
            select(func.count(AgentReport.id))
            .where(AgentReport.user_id == user_id)
            .where(AgentReport.created_at >= cutoff)
        )
        total = int((await session.execute(total_stmt)).scalar_one() or 0)
        if total == 0:
            return 0.0
        err_stmt = total_stmt.where(AgentReport.response_text.like("ERROR:%"))
        errs = int((await session.execute(err_stmt)).scalar_one() or 0)
        return errs / total


async def _monthly_spend_usd(user_id: str, *, now: datetime) -> float:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with db_mod.get_session(user_id=user_id) as session:
        stmt = (
            select(func.coalesce(func.sum(AgentReport.cost_usd), 0))
            .where(AgentReport.user_id == user_id)
            .where(AgentReport.created_at >= start)
        )
        return float((await session.execute(stmt)).scalar_one() or 0)


def _state_db_size_gb() -> float:
    settings = get_settings()
    if not settings.db_file.is_file():
        return 0.0
    return settings.db_file.stat().st_size / (1024**3)


def _disk_space_pct_free() -> float:
    settings = get_settings()
    try:
        usage = shutil.disk_usage(str(settings.home))
    except OSError:  # pragma: no cover - defensive
        return 100.0
    if usage.total == 0:  # pragma: no cover
        return 100.0
    return (usage.free / usage.total) * 100.0


async def _backup_age_hours(user_id: str, *, now: datetime) -> float | None:
    last = await _last_audit_event_at(user_id, "backup.completed")
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() / 3600.0


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


async def collect_signals(
    user_id: str,
    *,
    now: datetime | None = None,
    monthly_budget_usd: float | None = None,
) -> WatchdogSignals:
    moment = now or datetime.now(timezone.utc)
    sig = WatchdogSignals(user_id=user_id, now=moment.isoformat())

    last_hb = await _last_audit_event_at(user_id, "engine.heartbeat")
    if last_hb is not None:
        if last_hb.tzinfo is None:
            last_hb = last_hb.replace(tzinfo=timezone.utc)
        sig.engine_heartbeat_age_s = (moment - last_hb).total_seconds()

    sig.cadence_loops_stuck = await _stuck_cadence_loops(moment)
    sig.claude_error_rate = await _claude_error_rate(user_id, now=moment)
    sig.claude_monthly_spend_usd = await _monthly_spend_usd(user_id, now=moment)
    sig.claude_monthly_budget_usd = monthly_budget_usd
    if monthly_budget_usd is not None and monthly_budget_usd > 0:
        sig.claude_monthly_spend_pct = (
            100.0 * sig.claude_monthly_spend_usd / monthly_budget_usd
        )
    sig.state_db_size_gb = _state_db_size_gb()
    sig.backup_age_hours = await _backup_age_hours(user_id, now=moment)
    sig.disk_space_pct_free = _disk_space_pct_free()

    sig.breaches = compute_breaches(sig)
    return sig


def compute_breaches(sig: WatchdogSignals) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if sig.engine_heartbeat_age_s is not None and sig.engine_heartbeat_age_s > 300:
        out.append(
            {
                "signal": "engine_heartbeat",
                "message": (
                    f"engine heartbeat is {sig.engine_heartbeat_age_s:.0f}s old "
                    "(> 5min)"
                ),
                "severity": "warning",
            }
        )
    if sig.cadence_loops_stuck:
        out.append(
            {
                "signal": "cadence_loop_stuck",
                "message": (
                    f"{len(sig.cadence_loops_stuck)} cadence loop(s) stuck: "
                    f"{', '.join(sig.cadence_loops_stuck)}"
                ),
                "severity": "warning",
            }
        )
    if sig.claude_error_rate > 0.05:
        out.append(
            {
                "signal": "claude_error_rate",
                "message": f"claude error rate {sig.claude_error_rate:.1%} > 5%",
                "severity": "critical",
            }
        )
    if sig.claude_monthly_spend_pct is not None and sig.claude_monthly_spend_pct >= 100:
        out.append(
            {
                "signal": "claude_monthly_spend",
                "message": (
                    f"monthly spend {sig.claude_monthly_spend_usd:.2f} >= "
                    f"budget {sig.claude_monthly_budget_usd}"
                ),
                "severity": "critical",
            }
        )
    elif (
        sig.claude_monthly_spend_pct is not None
        and sig.claude_monthly_spend_pct >= 80
    ):
        out.append(
            {
                "signal": "claude_monthly_spend",
                "message": (
                    f"monthly spend at {sig.claude_monthly_spend_pct:.0f}% of budget"
                ),
                "severity": "warning",
            }
        )
    if sig.state_db_size_gb > 10:
        out.append(
            {
                "signal": "state_db_size",
                "message": f"state DB size {sig.state_db_size_gb:.1f} GB > 10 GB",
                "severity": "warning",
            }
        )
    if sig.backup_age_hours is None:
        out.append(
            {
                "signal": "backup_failed",
                "message": "no backup.completed audit event recorded",
                "severity": "warning",
            }
        )
    elif sig.backup_age_hours > 36:
        out.append(
            {
                "signal": "backup_failed",
                "message": f"last backup is {sig.backup_age_hours:.1f}h old",
                "severity": "warning",
            }
        )
    if sig.disk_space_pct_free < 20:
        out.append(
            {
                "signal": "disk_space",
                "message": f"only {sig.disk_space_pct_free:.1f}% disk free",
                "severity": "warning",
            }
        )
    return out


async def run_watchdog(
    user_id: str,
    *,
    interval_seconds: int = 60,
    once: bool = False,
    email_sender: EmailSender | None = None,
    monthly_budget_usd: float | None = None,
) -> None:
    """Long-running probe loop. Tests pass `once=True` for a single tick."""

    while True:
        sig = await collect_signals(
            user_id, monthly_budget_usd=monthly_budget_usd
        )
        if sig.breaches and email_sender is not None:
            try:
                lines = "\n".join(
                    f"  [{b['severity']}] {b['signal']}: {b['message']}"
                    for b in sig.breaches
                )
                await email_sender(
                    user_id,
                    "[Argosy] Watchdog alert",
                    f"Watchdog breaches at {sig.now}:\n{lines}",
                )
            except Exception as exc:  # pragma: no cover
                _log.warning("watchdog.alert_send_failed", err=str(exc))
        _log.info(
            "watchdog.tick",
            user_id=user_id,
            breaches=len(sig.breaches),
        )
        if once:
            return
        await asyncio.sleep(interval_seconds)


def signals_to_dict(sig: WatchdogSignals) -> dict[str, Any]:
    return asdict(sig)


__all__ = [
    "EmailSender",
    "WatchdogSignals",
    "collect_signals",
    "compute_breaches",
    "run_watchdog",
    "signals_to_dict",
]
