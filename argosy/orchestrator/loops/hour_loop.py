"""Hour loop (SDD §5.1, Phase 7).

60min tick, 24/7. Polls:
  - News-feed delta (FinnhubAdapter)
  - Macro release calendar (FRED)
  - Corp-actions feed
  - FX rates (BoI / FRED) — ingested into the state snapshot for emergent
    anomaly detection by StateObserverAgent; no hardcoded per-symptom threshold
    here (design principle: emergent observer only, no check_<symptom> detectors)

Records material events; emits WebSocket events:
  - `news.material`         — when news materiality > threshold
  - `macro.surprise`        — when a macro print exceeds the configured surprise threshold

Honors the cost-guard pause and kill switch.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from argosy.api.events import publish_event
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

_log = get_logger("argosy.loops.hour")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Provider callables — each returns a list of dicts the loop interprets.
NewsProvider = Callable[[], Awaitable[list[dict[str, Any]]]]
MacroProvider = Callable[[], Awaitable[list[dict[str, Any]]]]
FXProvider = Callable[[], Awaitable[list[dict[str, Any]]]]
CorpActionsProvider = Callable[[], Awaitable[list[dict[str, Any]]]]


class HourLoop(CadenceLoop):
    """Hourly polling loop. No LLM calls in Phase 7 — events only."""

    name = "hour"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        news_provider: NewsProvider | None = None,
        macro_provider: MacroProvider | None = None,
        fx_provider: FXProvider | None = None,
        corp_actions_provider: CorpActionsProvider | None = None,
        news_materiality_threshold: float = 0.6,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._news = news_provider or _empty_provider
        self._macro = macro_provider or _empty_provider
        self._fx = fx_provider or _empty_provider
        self._corp = corp_actions_provider or _empty_provider
        self._news_threshold = news_materiality_threshold

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        if os.environ.get("ARGOSY_KILL") == "1":
            _log.info("hour_loop.kill_switch_skip")
            return

        guard = get_cost_guard(user_id=self.user_id)
        if await guard.should_pause_non_routine(loop_name=self.name):
            _log.info("hour_loop.cost_guard_paused")
            return

        moment = (now or _utcnow)()

        # 1. News
        news_items = await self._news()
        material_news = [
            n for n in news_items if (n.get("materiality") or 0.0) >= self._news_threshold
        ]
        for item in material_news:
            await _publish("news.material", {"user_id": self.user_id, **item})

        # 2. Macro releases
        macro_items = await self._macro()
        surprises = [m for m in macro_items if m.get("surprise")]
        for item in surprises:
            await _publish("macro.surprise", {"user_id": self.user_id, **item})

        # 3. FX rates — ingested for snapshot freshness; anomaly detection is
        # emergent via StateObserverAgent, not a hardcoded threshold here.
        fx_items = await self._fx()

        # 4. Corp actions (recorded but no event for Phase 7).
        corp_items = await self._corp()

        if material_news or surprises or fx_items or corp_items:
            await record_audit_event(
                user_id=self.user_id,
                event_type="hour_loop.events_recorded",
                entity_type="cadence",
                entity_id="hour",
                payload={
                    "now": moment.isoformat(),
                    "news_material_count": len(material_news),
                    "macro_surprise_count": len(surprises),
                    "fx_items_count": len(fx_items),
                    "corp_actions_count": len(corp_items),
                },
            )
            _log.info(
                "hour_loop.events_recorded",
                news=len(material_news),
                macro=len(surprises),
                fx=len(fx_items),
                corp=len(corp_items),
            )


async def _empty_provider() -> list[dict[str, Any]]:
    return []


async def _publish(name: str, payload: dict[str, Any]) -> None:
    try:
        await publish_event(name, payload)
    except Exception:  # pragma: no cover - defensive
        _log.exception("hour_loop.publish_failed", event=name)


__all__ = ["HourLoop"]
