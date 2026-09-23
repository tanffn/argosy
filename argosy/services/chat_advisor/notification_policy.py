"""Presentation/cadence only; urgency comes from canonical upstream severity."""
from __future__ import annotations

import hashlib
from datetime import datetime, time
from zoneinfo import ZoneInfo

from argosy.services.chat_advisor.contracts import OutboxEvent

DAILY = "overview.daily"
CRITICAL = "alert.critical"
OPERATIONAL = "status.jobs"


def delivery_events(
    events: list[OutboxEvent], *, now: datetime, timezone: str, overview_time: time,
) -> list[OutboxEvent]:
    """One daily snapshot, plus distinct critical alerts; never a catch-up backlog."""
    alerts = [event for event in events if event.category in {CRITICAL, OPERATIONAL}]
    local = now.astimezone(ZoneInfo(timezone))
    if local.time() < overview_time:
        return alerts
    # Worker replaces this honest fallback with a grounded, independently reviewed
    # research briefing. Inbox counts alone cannot establish market significance.
    lines = [f"**Argosy · {local:%d %b}**",
             "Today's research summary is unavailable; I can't verify whether there are worthwhile updates."]
    body = "\n".join(lines)
    version = hashlib.sha256((body + repr([(e.semantic_key, e.material_version)
                                          for e in events])).encode()).hexdigest()
    digest = OutboxEvent(
        semantic_key=f"overview:{local.date().isoformat()}", material_version=version,
        category=DAILY, body=body, citations=[ref for e in events for ref in e.citations],
    )
    return [*alerts, digest]
