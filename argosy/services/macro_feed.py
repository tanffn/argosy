"""Macro calendar feed (Stage 1 source adapter).

Sprint commit #13 of the plan/execute/monitor reorg. Surfaces upcoming
macroeconomic events — FOMC meetings, BLS CPI releases, BLS jobs
reports — as ``ExtractedSignal`` rows so the monitor agent can
forewarn the user before the actual release moves markets.

v1 strategy: a hardcoded curated list of known event dates for the
next four quarters. The events of interest are publicly scheduled
months in advance and rarely shift more than a day; a curated table
is more reliable + cheaper than scraping a vendor calendar. When the
list runs low (we're within ~30 days of the last entry) commit #16
will swap in a scraper / API binding.

Source-trust ranks ``high`` for all macro entries (official calendars).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from argosy.services.news_extractor import ExtractedSignal, extract

# ---------------------------------------------------------------------------
# Curated calendar (next ~4 quarters from 2026-05-29)
# ---------------------------------------------------------------------------
#
# Each entry is (event_date_utc, kind, description). The kind is folded
# into ``source_ref`` as ``{kind}-{YYYY-MM-DD}`` so the dedup unique
# index never collides across event types on the same date.
#
# Sources:
#   FOMC dates       — federalreserve.gov scheduled meeting calendar
#   CPI release dates — bls.gov scheduled CPI release calendar
#   Jobs report dates — bls.gov Employment Situation release calendar
#
# Description text deliberately mentions the keyword set the Stage 1
# extractor recognizes (FOMC / CPI / rate / Fed) so the resulting
# signals show up in keyword queries downstream.

_MACRO_EVENTS: list[tuple[datetime, str, str]] = [
    # --- FOMC meetings (rate decision days) ---
    (datetime(2026, 6, 17, 18, 0, tzinfo=UTC), "fomc",
     "FOMC rate decision — Federal Reserve announces target federal funds rate."),
    (datetime(2026, 7, 29, 18, 0, tzinfo=UTC), "fomc",
     "FOMC rate decision — Federal Reserve announces target federal funds rate."),
    (datetime(2026, 9, 16, 18, 0, tzinfo=UTC), "fomc",
     "FOMC rate decision — Federal Reserve announces target federal funds rate."),
    (datetime(2026, 11, 4, 18, 0, tzinfo=UTC), "fomc",
     "FOMC rate decision — Federal Reserve announces target federal funds rate."),
    (datetime(2026, 12, 16, 18, 0, tzinfo=UTC), "fomc",
     "FOMC rate decision — Federal Reserve announces target federal funds rate."),
    # --- CPI release dates ---
    (datetime(2026, 6, 10, 12, 30, tzinfo=UTC), "cpi",
     "BLS CPI release — Consumer Price Index inflation data."),
    (datetime(2026, 7, 15, 12, 30, tzinfo=UTC), "cpi",
     "BLS CPI release — Consumer Price Index inflation data."),
    (datetime(2026, 8, 12, 12, 30, tzinfo=UTC), "cpi",
     "BLS CPI release — Consumer Price Index inflation data."),
    (datetime(2026, 9, 10, 12, 30, tzinfo=UTC), "cpi",
     "BLS CPI release — Consumer Price Index inflation data."),
    (datetime(2026, 10, 14, 12, 30, tzinfo=UTC), "cpi",
     "BLS CPI release — Consumer Price Index inflation data."),
    (datetime(2026, 11, 12, 13, 30, tzinfo=UTC), "cpi",
     "BLS CPI release — Consumer Price Index inflation data."),
    (datetime(2026, 12, 10, 13, 30, tzinfo=UTC), "cpi",
     "BLS CPI release — Consumer Price Index inflation data."),
    # --- BLS jobs report (Employment Situation) ---
    (datetime(2026, 6, 5, 12, 30, tzinfo=UTC), "jobs",
     "BLS Employment Situation — nonfarm payrolls and unemployment rate."),
    (datetime(2026, 7, 2, 12, 30, tzinfo=UTC), "jobs",
     "BLS Employment Situation — nonfarm payrolls and unemployment rate."),
    (datetime(2026, 8, 7, 12, 30, tzinfo=UTC), "jobs",
     "BLS Employment Situation — nonfarm payrolls and unemployment rate."),
    (datetime(2026, 9, 4, 12, 30, tzinfo=UTC), "jobs",
     "BLS Employment Situation — nonfarm payrolls and unemployment rate."),
    (datetime(2026, 10, 2, 12, 30, tzinfo=UTC), "jobs",
     "BLS Employment Situation — nonfarm payrolls and unemployment rate."),
    (datetime(2026, 11, 6, 13, 30, tzinfo=UTC), "jobs",
     "BLS Employment Situation — nonfarm payrolls and unemployment rate."),
    (datetime(2026, 12, 4, 13, 30, tzinfo=UTC), "jobs",
     "BLS Employment Situation — nonfarm payrolls and unemployment rate."),
]


def get_upcoming_macro_events(
    within_days: int = 30,
    *,
    now: datetime | None = None,
) -> list[ExtractedSignal]:
    """Return ``ExtractedSignal`` rows for events scheduled within the
    next ``within_days`` days.

    Args:
        within_days: Forward-looking window in days from ``now``.
            Default 30 — far enough out to give the monitor agent time
            to pre-position warnings, tight enough to avoid stale
            chatter.
        now: Override for "now" — primarily for tests so the curated
            calendar can be exercised deterministically.

    Returns:
        ``ExtractedSignal`` list, one per event whose date falls within
        ``[now, now + within_days]``. Ordered by event date ascending.
    """
    now_dt = now if now is not None else datetime.now(UTC)
    horizon = now_dt + timedelta(days=within_days)

    out: list[ExtractedSignal] = []
    for event_dt, kind, description in _MACRO_EVENTS:
        if event_dt < now_dt or event_dt > horizon:
            continue
        source_ref = f"{kind}-{event_dt.date().isoformat()}"
        signal = extract(
            source="macro_feed",
            source_ref=source_ref,
            # raw_text for macro events is the event description itself
            # — stored for citation display but per BLOCKER #2 it never
            # reaches the Stage 2 LLM prompt.
            raw_text=description,
            received_at=event_dt,
        )
        out.append(signal)
    out.sort(key=lambda s: s.received_at)
    return out
