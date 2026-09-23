"""Rate-limited editor whose milestones come only from durable progress rows."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from argosy.services.chat_advisor.outbound import OutboundFilter


class ProgressEditor:
    def __init__(
        self,
        store,
        gateway,
        *,
        min_edit_seconds: float = 10.0,
        heartbeat_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.min_edit_seconds = max(10.0, min_edit_seconds)
        self.heartbeat_seconds = max(60.0, heartbeat_seconds)
        self.clock = clock
        self._last_edit = 0.0
        self._last_cursor = 0
        self._started = clock()

    async def update(
        self, request_id: str, channel_id: str, message_id: str, *, force_heartbeat: bool = False
    ) -> bool:
        now = self.clock()
        rows = await self.store.progress_rows(request_id, after=self._last_cursor)
        due = now - self._last_edit >= self.min_edit_seconds
        heartbeat = force_heartbeat and now - self._last_edit >= self.heartbeat_seconds
        if not (due and rows) and not heartbeat:
            return False
        # No predicted phases or percentages: only persisted agent receipts plus elapsed time.
        all_rows = await self.store.progress_rows(request_id, after=0)
        latest = {}
        for row in all_rows:
            latest[row.agent] = row
        lines = [f"Analysis {request_id} · elapsed {int(now - self._started)}s"]
        for row in list(latest.values())[-20:]:
            detail = (row.detail or row.error or "")[:240]
            lines.append(f"{row.agent}: {row.state}" + (f" — {detail}" if detail else ""))
        rendered = OutboundFilter.redact("\n".join(lines))[:2000]
        await self.gateway.edit(channel_id, message_id, rendered)
        if all_rows:
            self._last_cursor = all_rows[-1].sequence
        self._last_edit = now
        return True

    async def heartbeat_loop(self, request_id: str, channel_id: str, message_id: str) -> None:
        while True:
            await asyncio.sleep(10)
            request = await self.store.get_request(request_id)
            await self.update(request_id, channel_id, message_id, force_heartbeat=True)
            if request is None or request.state in {"completed", "failed", "cancelled"}:
                return


__all__ = ["ProgressEditor"]
