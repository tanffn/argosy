"""Discovery-funnel cadence loop (codex #10) — a SEPARATE loop from the
speculative monitor.

The speculative monitor stays a cheap daily yfinance stop-loss sweep; the
discovery funnel is a heavier radar->estimator->fleet pass with its own cadence,
timeout, cost budget, and failure isolation, so a funnel hiccup never disturbs
the monitor (and vice-versa). The daily tick runs a SMART refresh (``force=False``)
so only new/changed names are re-researched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.high_potential_funnel import run_funnel

_log = get_logger("argosy.loops.discovery_funnel")


class DiscoveryFunnelLoop(CadenceLoop):
    """Daily smart refresh of the high-potential discovery funnel."""

    name = "discovery_funnel"

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
        try:
            result = await run_funnel(self.user_id, force=False)
        except Exception as exc:  # noqa: BLE001 — failure isolation (codex #10)
            _log.warning("discovery_funnel.tick_failed", error=str(exc)[:200])
            return {"error": str(exc)[:200]}
        _log.info(
            "discovery_funnel.tick_done",
            user_id=self.user_id,
            radar=len(result.radar),
            estimated=len(result.estimated),
            picks=len(result.picks),
        )
        return {
            "radar": len(result.radar),
            "estimated": len(result.estimated),
            "picks": len(result.picks),
            "last_refreshed_at": result.last_refreshed_at,
        }


__all__ = ["DiscoveryFunnelLoop"]
