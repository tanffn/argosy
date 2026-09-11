"""Daily discovery of new videos from subscribed YouTube sources."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.youtube_intelligence import sync_youtube_subscriptions


class YouTubeSubscriptionsLoop(CadenceLoop):
    name = "youtube_subscriptions"

    def __init__(self, *, enabled: bool = True, user_id: str = "ariel") -> None:
        super().__init__(
            schedule=LoopSchedule(cron="0 14 * * *", timezone="Asia/Jerusalem"),
            enabled=enabled,
        )
        self.user_id = user_id

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict:
        if await get_cost_guard(user_id=self.user_id).should_pause_non_routine(loop_name=self.name):
            return {"status": "paused", "reason": "cost_cap"}
        return await sync_youtube_subscriptions(user_id=self.user_id)


__all__ = ["YouTubeSubscriptionsLoop"]
