"""Minute loop (SDD §5.1, Phase 7).

Tick rate 60s, market-hours-only. Polls:
  - Open-order status (delegated to the existing `ReconcileLoop`)
  - Price vs limits on watchlist tickers
  - Volatility-band breach detection

Triggers (recorded in `cadence_state` for Phase 7; full LLM integration
deferred per the brief):
  - Limit-price re-evaluation (T0)
  - Breach of stop / target (T0/T1)
  - Flash-crash detection (T2)

The loop honors the cost-guard pause and the kill-switch.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable

from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

_log = get_logger("argosy.loops.minute")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MinuteLoop(CadenceLoop):
    """Per-minute polling loop. No LLM calls in Phase 7."""

    name = "minute"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        watchlist_provider: Callable[[], list[dict[str, Any]]] | None = None,
        price_provider: Callable[[str], float | None] | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        # Returns a list of `{ticker, target_price?, stop_price?,
        # vol_band_high?, vol_band_low?}` dicts.
        self._watchlist_provider = watchlist_provider or (lambda: [])
        # Returns the latest price for a ticker, or None on failure.
        self._price_provider = price_provider or (lambda _t: None)

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        if os.environ.get("ARGOSY_KILL") == "1":
            _log.info("minute_loop.kill_switch_skip")
            return

        guard = get_cost_guard(user_id=self.user_id)
        if await guard.should_pause_non_routine(loop_name=self.name):
            _log.info("minute_loop.cost_guard_paused", user_id=self.user_id)
            return

        moment = (now or _utcnow)()
        watchlist = self._watchlist_provider() or []

        events: list[dict[str, Any]] = []
        for entry in watchlist:
            ticker = entry.get("ticker")
            if not ticker:
                continue
            price = self._price_provider(ticker)
            if price is None:
                continue
            target = entry.get("target_price")
            stop = entry.get("stop_price")
            high = entry.get("vol_band_high")
            low = entry.get("vol_band_low")

            if target is not None and price >= float(target):
                events.append(
                    {
                        "ticker": ticker,
                        "kind": "target_breach",
                        "price": price,
                        "level": float(target),
                    }
                )
            if stop is not None and price <= float(stop):
                events.append(
                    {
                        "ticker": ticker,
                        "kind": "stop_breach",
                        "price": price,
                        "level": float(stop),
                    }
                )
            if high is not None and price >= float(high):
                events.append(
                    {
                        "ticker": ticker,
                        "kind": "vol_band_high",
                        "price": price,
                        "level": float(high),
                    }
                )
            if low is not None and price <= float(low):
                events.append(
                    {
                        "ticker": ticker,
                        "kind": "vol_band_low",
                        "price": price,
                        "level": float(low),
                    }
                )

        if events:
            await record_audit_event(
                user_id=self.user_id,
                event_type="minute_loop.trigger_recorded",
                entity_type="cadence",
                entity_id="minute",
                payload={
                    "now": moment.isoformat(),
                    "events": events,
                },
            )
            _log.info(
                "minute_loop.events_recorded",
                user_id=self.user_id,
                count=len(events),
            )


__all__ = ["MinuteLoop"]
