"""Daytime shared research intake with per-channel daily video selection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.youtube_intelligence import sync_youtube_subscriptions


class YouTubeSubscriptionsLoop(CadenceLoop):
    name = "youtube_subscriptions"

    def __init__(self, *, enabled: bool = True, user_id: str = "ariel") -> None:
        super().__init__(
            schedule=LoopSchedule(cron="0 7-23/2 * * *", timezone="Asia/Jerusalem"),
            enabled=enabled,
        )
        self.user_id = user_id

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict:
        # Cheap intake remains durable even when paid analysis is budget-paused.
        # The shared worker checks the cost guard before each fleet analysis.
        return await sync_youtube_subscriptions(user_id=self.user_id)


__all__ = ["YouTubeSubscriptionsLoop"]
