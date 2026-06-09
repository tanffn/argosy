"""HourLoop tests — mocks news/macro/fx providers, asserts events.

T5.5 design contract: HourLoop must NOT emit ``fx.threshold_breach``.
FX anomaly detection flows through the emergent StateObserverAgent only.
The FX provider is still polled (data ingested into state snapshot), but
no threshold-based WS event is fired from the loop.
"""

from __future__ import annotations

from typing import Any

import pytest

from argosy.api import events
from argosy.orchestrator.cost_guard import reset_cost_guard
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.orchestrator.loops.hour_loop import HourLoop
from argosy.state import db as db_mod
from argosy.state.models import User


@pytest.mark.asyncio
async def test_hour_loop_emits_news_and_macro_not_fx_threshold(engine: None) -> None:
    """news.material and macro.surprise are still emitted; fx.threshold_breach
    is gone (T5.5 — emergent observer only)."""
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    async def news_provider() -> list[dict[str, Any]]:
        return [
            {"ticker": "NVDA", "headline": "Earnings beat", "materiality": 0.8},
            {"ticker": "TSLA", "headline": "Recall", "materiality": 0.3},
        ]

    async def macro_provider() -> list[dict[str, Any]]:
        return [
            {"label": "CPI", "surprise": True, "delta_bps": 30},
            {"label": "ISM", "surprise": False, "delta_bps": 5},
        ]

    fx_polled: list[bool] = []

    async def fx_provider() -> list[dict[str, Any]]:
        fx_polled.append(True)
        return [
            {"pair": "USD/NIS", "pct_change": 1.5},
            {"pair": "USD/EUR", "pct_change": 0.2},
        ]

    received: list[str] = []
    sub_ctx = events.subscribe()
    q = await sub_ctx.__aenter__()

    loop = HourLoop(
        schedule=LoopSchedule(interval_seconds=3600),
        user_id="ariel",
        news_provider=news_provider,
        macro_provider=macro_provider,
        fx_provider=fx_provider,
        news_materiality_threshold=0.6,
    )
    await loop.tick()

    while not q.empty():
        received.append(q.get_nowait())
    await sub_ctx.__aexit__(None, None, None)

    joined = "\n".join(received)
    # Expected WS events still fire.
    assert "news.material" in joined
    assert "macro.surprise" in joined
    # FX threshold-breach event MUST NOT be emitted (T5.5).
    assert "fx.threshold_breach" not in joined
    # FX provider WAS called — data is still ingested for state snapshot.
    assert fx_polled, "fx_provider must be called even though no WS event is emitted"
    # Only NVDA news was material; TSLA was below threshold.
    assert "NVDA" in joined


@pytest.mark.asyncio
async def test_hour_loop_no_signals_no_events(engine: None) -> None:
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    sub_ctx = events.subscribe()
    q = await sub_ctx.__aenter__()

    loop = HourLoop(
        schedule=LoopSchedule(interval_seconds=3600),
        user_id="ariel",
    )
    await loop.tick()

    received: list[str] = []
    while not q.empty():
        received.append(q.get_nowait())
    await sub_ctx.__aexit__(None, None, None)
    assert received == []
