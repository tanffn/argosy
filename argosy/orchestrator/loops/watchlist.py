"""Watchlist loop (SDD §3.6, §5).

Runs daily (default 08:30 user TZ). Invokes `WatchlistAgent` to refresh
the universe of tickers tracked: positions + candidates + reduce-list.
Phase 7 emits the structured output via `watchlist.updated` WebSocket
event; persistence to a `watchlists` table is deferred (the agent's
output is consumed by downstream loops live).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from argosy.agents.watchlist import WatchlistAgent
from argosy.api.events import publish_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

_log = get_logger("argosy.loops.watchlist")


@dataclass
class WatchlistInputs:
    """Inputs to one watchlist run, gathered before the LLM call."""

    user_id: str
    current_positions_summary: str
    candidates_under_review: list[str]
    reduce_list: list[str]


class WatchlistLoop(CadenceLoop):
    """Daily watchlist maintenance. Wired when `cadences.watchlist.enabled`."""

    name = "watchlist"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        watchlist_agent_factory: Callable[[], WatchlistAgent] | None = None,
        gather_inputs: Callable[[str], "WatchlistInputs | Any"] | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._agent_factory = watchlist_agent_factory or (
            lambda: WatchlistAgent(user_id=user_id)
        )
        self._gather = gather_inputs or _default_gather_inputs

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        run_at = (now or _utcnow)()

        # Cost-cap pause: watchlist is non-routine; skip when budget breached.
        if get_cost_guard().should_pause_non_routine():
            _log.info("watchlist.cost_cap_paused", user_id=self.user_id)
            return

        inputs = await _maybe_async(self._gather(self.user_id))
        if not isinstance(inputs, WatchlistInputs):  # pragma: no cover
            raise TypeError(
                f"gather_inputs must return WatchlistInputs, got {type(inputs)!r}"
            )

        agent = self._agent_factory()
        report = await agent.run(
            current_positions_summary=inputs.current_positions_summary,
            candidates_under_review=inputs.candidates_under_review,
            reduce_list=inputs.reduce_list,
        )

        current = getattr(report.output, "current_tickers", []) or []
        added = getattr(report.output, "added_today", []) or []
        removed = getattr(report.output, "removed_today", []) or []

        _log.info(
            "watchlist.refreshed",
            user_id=self.user_id,
            count=len(current),
            added=len(added),
            removed=len(removed),
            run_at=run_at.isoformat(),
        )

        try:
            await publish_event(
                "watchlist.updated",
                {
                    "user_id": self.user_id,
                    "run_at": run_at.isoformat(),
                    "count": len(current),
                    "added": added,
                    "removed": removed,
                },
            )
        except Exception:  # pragma: no cover - defensive
            _log.exception("watchlist.publish_failed")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _maybe_async(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def _default_gather_inputs(user_id: str) -> WatchlistInputs:
    """Empty defaults — the loop produces a valid agent report even without
    a portfolio snapshot or pre-seeded candidate list. Production callers
    supply their own gatherer."""
    return WatchlistInputs(
        user_id=user_id,
        current_positions_summary="(no portfolio snapshot ingested today)",
        candidates_under_review=[],
        reduce_list=[],
    )


__all__ = ["WatchlistInputs", "WatchlistLoop"]
