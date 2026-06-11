"""Speculative-monitor cadence loop — the "live daily monitor" the user asked
for on the high-risk satellite names.

Once a day it runs :func:`argosy.services.speculative_monitor.run_monitor` over
the user's held speculative single names and surfaces any ACTIONABLE signal
(SELL / WATCH / TRIM) via a published event + a structured log line, so a stop
trigger reaches the user without them having to open the monitor card.

Deliberately lean (v1): the watched set is the held single-name sleeve seeds
(``held_today``). When a speculative-holdings table lands, point the watch
list at it. yfinance is synchronous, so the fetch runs in a worker thread to
keep the scheduler's event loop free.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from argosy.api.events import publish_event
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

_log = get_logger("argosy.loops.speculative_monitor")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _held_speculative_tickers() -> list[str]:
    from argosy.services.high_potential_sleeve import _SEED_CANDIDATES

    return [
        c.ticker for c in _SEED_CANDIDATES
        if c.vehicle == "single_name" and c.held_today
    ]


class SpeculativeMonitorLoop(CadenceLoop):
    """Daily stop-loss / sell-signal sweep over the speculative names."""

    name = "speculative_monitor"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(interval_seconds=86_400),
            enabled=enabled,
        )
        self.user_id = user_id

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        from argosy.services.speculative_monitor import WatchEntry, run_monitor

        tickers = _held_speculative_tickers()
        if not tickers:
            return {"watched": 0, "actionable": 0, "note": "no held speculative names"}

        entry_date = date.today() - timedelta(days=90)
        watch = [
            WatchEntry(ticker=t, entry_price=0.0, entry_date=entry_date)
            for t in tickers
        ]
        # yfinance is blocking; run off the event loop.
        signals = await asyncio.to_thread(run_monitor, watch)

        actionable = [s for s in signals if s.action in ("SELL", "WATCH", "TRIM")]
        for s in actionable:
            _log.info(
                "speculative_monitor.signal",
                user_id=self.user_id,
                ticker=s.ticker,
                action=s.action,
                current=s.current_price,
                binding_stop=s.binding_stop_level,
                pct_from_peak=s.pct_from_peak,
            )
            try:
                await publish_event(
                    "speculative.monitor_signal",
                    {
                        "user_id": self.user_id,
                        "ticker": s.ticker,
                        "action": s.action,
                        "current_price": s.current_price,
                        "binding_stop_level": s.binding_stop_level,
                        "reason": s.reason,
                    },
                )
            except Exception:  # pragma: no cover — event bus is best-effort
                _log.exception("speculative_monitor.publish_failed", ticker=s.ticker)

        return {
            "watched": len(tickers),
            "actionable": len(actionable),
            "sell": sum(1 for s in actionable if s.action == "SELL"),
        }


__all__ = ["SpeculativeMonitorLoop"]
