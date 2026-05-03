"""Cheap polling helpers for the cadence orchestrator (SDD §5).

These are NOT LLM calls. Their job is to answer trigger questions like
"is the market open right now?" so the scheduler can decide whether to
fire a market-hours-only loop. All helpers accept an optional `now`
callable for deterministic testing (tests inject a fixed clock).
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Callable
from zoneinfo import ZoneInfo

# NYSE: 9:30–16:00 America/New_York, Mon–Fri (excluding holidays we don't track yet).
_NYSE_TZ = ZoneInfo("America/New_York")
_NYSE_OPEN = time(9, 30)
_NYSE_CLOSE = time(16, 0)
_JERUSALEM_TZ = ZoneInfo("Asia/Jerusalem")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_market_open(*, now: Callable[[], datetime] = _utcnow) -> bool:
    """True if NYSE is currently in regular session (Mon–Fri 09:30–16:00 NY).

    Holidays are NOT yet tracked (Phase 2 keeps it cheap; the holiday
    calendar wires up in Phase 4 with the broker integration).
    """
    n = now().astimezone(_NYSE_TZ)
    if n.weekday() >= 5:  # 5,6 = Sat,Sun
        return False
    t = n.time()
    return _NYSE_OPEN <= t < _NYSE_CLOSE


def time_in_jerusalem(*, now: Callable[[], datetime] = _utcnow) -> datetime:
    """Return `now()` projected into Asia/Jerusalem."""
    return now().astimezone(_JERUSALEM_TZ)


def is_weekend_in_israel(*, now: Callable[[], datetime] = _utcnow) -> bool:
    """True on Israeli weekend (Friday from sundown / Saturday until sundown).

    Phase 2 simplification: True on Friday or Saturday in Asia/Jerusalem.
    A precise sundown calculation would need a solar library; the
    weekly-review loop runs on Sundays anyway so this approximation is fine.
    """
    n = time_in_jerusalem(now=now)
    return n.weekday() in (4, 5)  # Fri=4, Sat=5


__all__ = [
    "is_market_open",
    "time_in_jerusalem",
    "is_weekend_in_israel",
]
