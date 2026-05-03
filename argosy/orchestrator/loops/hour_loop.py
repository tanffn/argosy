"""Hour loop (SDD §5.1, Phase 7).

60min tick, 24/7. Polls:
  - News-feed delta (FinnhubAdapter)
  - Macro release calendar (FRED)
  - Corp-actions feed
  - FX move > threshold (BoI / FRED)

Records material events; emits WebSocket events:
  - `news.material`         — when news materiality > threshold
  - `macro.surprise`        — when a macro print exceeds the configured surprise threshold
  - `fx.threshold_breach`   — when an FX pair's intraday move > threshold

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
        fx_threshold_pct: float = 1.0,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._news = news_provider or _empty_provider
        self._macro = macro_provider or _empty_provider
        self._fx = fx_provider or _empty_provider
        self._corp = corp_actions_provider or _empty_provider
        self._news_threshold = news_materiality_threshold
        self._fx_threshold = fx_threshold_pct

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

        # 3. FX moves
        fx_items = await self._fx()
        fx_breaches = [
            x for x in fx_items if abs(float(x.get("pct_change", 0))) >= self._fx_threshold
        ]
        for item in fx_breaches:
            await _publish("fx.threshold_breach", {"user_id": self.user_id, **item})

        # 4. Corp actions (recorded but no event for Phase 7).
        corp_items = await self._corp()

        if material_news or surprises or fx_breaches or corp_items:
            await record_audit_event(
                user_id=self.user_id,
                event_type="hour_loop.events_recorded",
                entity_type="cadence",
                entity_id="hour",
                payload={
                    "now": moment.isoformat(),
                    "news_material_count": len(material_news),
                    "macro_surprise_count": len(surprises),
                    "fx_breach_count": len(fx_breaches),
                    "corp_actions_count": len(corp_items),
                },
            )
            _log.info(
                "hour_loop.events_recorded",
                news=len(material_news),
                macro=len(surprises),
                fx=len(fx_breaches),
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
