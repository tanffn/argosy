"""Cost-cap pause enforcement (SDD §14.7, Phase 7).

The watchdog from Phase 6 detects monthly Claude spend at 80% (alert)
and 100% (pause). Phase 6 wired only the alert; Phase 7 wires the
**pause**: cadence loops EXCEPT `daily_brief` (defined as routine) and
`process_cooling` (state advancer; cheap) consult `CostGuard` at tick
start and skip when paused.

Override:
  - `POST /internal/cost-guard/override` lifts the pause for a window
    (audit-logged). `CostGuard.override_until` is consulted before the
    threshold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select

from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import AgentReport

_log = get_logger("argosy.cost_guard")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Loops that are exempt from the pause (SDD §14.7 — "non-routine cadences").
ROUTINE_LOOPS = frozenset({"daily_brief", "process_cooling"})


class CostGuard:
    """Per-tenant cost guard.

    A small object the cadence loops call at tick start. Tracks an
    in-memory `override_until` that the API endpoint sets to lift the
    pause for a window.
    """

    def __init__(
        self,
        *,
        user_id: str,
        settings: AgentSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.user_id = user_id
        self.settings = settings or load_agent_settings(user_id)
        self.clock = clock or _utcnow
        self._override_until: datetime | None = None

    # ------------------------------------------------------------------
    # Override lifecycle
    # ------------------------------------------------------------------

    def set_override(self, *, minutes: int) -> datetime:
        """Lift the pause for `minutes` minutes; return the new expiry."""
        self._override_until = self.clock() + timedelta(minutes=max(0, minutes))
        return self._override_until

    def clear_override(self) -> None:
        self._override_until = None

    @property
    def override_until(self) -> datetime | None:
        return self._override_until

    def _override_active(self) -> bool:
        if self._override_until is None:
            return False
        return self.clock() < self._override_until

    # ------------------------------------------------------------------
    # Pause check
    # ------------------------------------------------------------------

    async def should_pause_non_routine(self, *, loop_name: str | None = None) -> bool:
        """Return True if non-routine loops should skip this tick.

        Routine loops (`daily_brief`, `process_cooling`) are exempt by
        loop name regardless of spend; passing `loop_name` short-circuits
        the spend check.
        """
        if loop_name in ROUTINE_LOOPS:
            return False
        if self._override_active():
            return False

        budget = float(self.settings.cost.monthly_budget_usd or 0)
        pct = float(self.settings.cost.pause_at_pct or 100)
        if budget <= 0 or pct <= 0:
            return False  # disabled

        threshold = budget * (pct / 100.0)
        spend = await self._monthly_spend_usd()
        return spend >= threshold

    async def monthly_spend_usd(self) -> float:
        """Public helper — same calc the watchdog uses."""
        return await self._monthly_spend_usd()

    async def _monthly_spend_usd(self) -> float:
        moment = self.clock()
        start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with db_mod.get_session(user_id=self.user_id) as session:
            stmt = (
                select(func.coalesce(func.sum(AgentReport.cost_usd), 0))
                .where(AgentReport.user_id == self.user_id)
                .where(AgentReport.created_at >= start)
            )
            return float((await session.execute(stmt)).scalar_one() or 0)


# Process-wide singleton accessor so loops + the API route share state.
_GUARD: CostGuard | None = None


def get_cost_guard(
    *,
    user_id: str = "ariel",
    settings: AgentSettings | None = None,
) -> CostGuard:
    """Return a process-wide CostGuard. Tests reset via `reset_cost_guard`."""
    global _GUARD
    if _GUARD is None or _GUARD.user_id != user_id:
        _GUARD = CostGuard(user_id=user_id, settings=settings)
    return _GUARD


def reset_cost_guard() -> None:
    """Test helper — drop the singleton."""
    global _GUARD
    _GUARD = None


__all__ = [
    "CostGuard",
    "ROUTINE_LOOPS",
    "get_cost_guard",
    "reset_cost_guard",
]
