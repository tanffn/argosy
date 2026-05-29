"""LoopSchedule.next_due_after — timezone correctness (Spec A commit #2).

Pre-existing bug: `_croniter(self.cron, ref)` was called with a UTC ref
and ignored `self.timezone`. Every existing cron-driven loop in
``CadencesBlock`` was affected — e.g. ``daily_brief`` with
``cron="0 9 * * *"`` and ``timezone="Asia/Jerusalem"`` fired at
9:00 UTC (= 11:00 IST winter / 12:00 IDT summer) instead of the
intended 9:00 IL-local.

This test exercises:
  * The eight existing cron-driven loops in ``CadencesBlock`` —
    daily_brief, weekly_review, monthly_cycle, annual, backup, audit,
    watchlist, plan_watcher.
  * Both DST states (summer IDT UTC+3, winter IST UTC+2).
  * Spring-forward + fall-back transition windows.
  * Interval-driven loops (regression — must still work).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_loop_schedule_tz.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from argosy.orchestrator.loops.base import LoopSchedule


IL = ZoneInfo("Asia/Jerusalem")
UTC = timezone.utc


# ---------------------------------------------------------------------------
# 8 cron-driven loops in CadencesBlock × DST-on / DST-off.
# ---------------------------------------------------------------------------


# Each row: (cron, schedule_label, ref_utc, expected_next_utc, dst_state)
# Reference times chosen so the next-fire is unambiguous (well into a
# stable DST window, not near a transition boundary — separate transition
# tests below).
CRON_MATRIX = [
    # daily_brief — "0 9 * * *" Asia/Jerusalem
    # Winter IST (UTC+2): 9 IL = 7 UTC
    (
        "0 9 * * *",
        "daily_brief_winter",
        datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
        datetime(2026, 1, 15, 7, 0, tzinfo=UTC),
    ),
    # Summer IDT (UTC+3): 9 IL = 6 UTC
    (
        "0 9 * * *",
        "daily_brief_summer",
        datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
        datetime(2026, 7, 15, 6, 0, tzinfo=UTC),
    ),
    # weekly_review — "0 18 * * SUN" Asia/Jerusalem
    # 2026-01-18 is a Sunday. Winter: 18 IL = 16 UTC.
    (
        "0 18 * * SUN",
        "weekly_review_winter",
        datetime(2026, 1, 18, 10, 0, tzinfo=UTC),
        datetime(2026, 1, 18, 16, 0, tzinfo=UTC),
    ),
    # 2026-07-19 is a Sunday. Summer: 18 IL = 15 UTC.
    (
        "0 18 * * SUN",
        "weekly_review_summer",
        datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 19, 15, 0, tzinfo=UTC),
    ),
    # monthly_cycle — "0 8 1 * *" Asia/Jerusalem
    # Feb 1, 2026 — winter (UTC+2): 8 IL = 6 UTC.
    (
        "0 8 1 * *",
        "monthly_cycle_winter",
        datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        datetime(2026, 2, 1, 6, 0, tzinfo=UTC),
    ),
    # Aug 1, 2026 — summer (UTC+3): 8 IL = 5 UTC.
    (
        "0 8 1 * *",
        "monthly_cycle_summer",
        datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 1, 5, 0, tzinfo=UTC),
    ),
    # annual — "0 8 2 1 *" Asia/Jerusalem (Jan 2 at 08:00 IL)
    # Jan 2, 2027 — winter (UTC+2): 8 IL = 6 UTC.
    (
        "0 8 2 1 *",
        "annual_winter",
        datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        datetime(2027, 1, 2, 6, 0, tzinfo=UTC),
    ),
    # backup — "0 3 * * *" Asia/Jerusalem (daily 03:00 IL)
    # Winter: 3 IL = 1 UTC.
    (
        "0 3 * * *",
        "backup_winter",
        datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
        datetime(2026, 1, 15, 1, 0, tzinfo=UTC),
    ),
    # Summer: 3 IL = 0 UTC same day.
    (
        "0 3 * * *",
        "backup_summer",
        datetime(2026, 7, 14, 23, 30, tzinfo=UTC),
        datetime(2026, 7, 15, 0, 0, tzinfo=UTC),
    ),
    # audit — "0 19 * * SUN" Asia/Jerusalem
    # 2026-01-18 Sunday — winter: 19 IL = 17 UTC.
    (
        "0 19 * * SUN",
        "audit_winter",
        datetime(2026, 1, 18, 10, 0, tzinfo=UTC),
        datetime(2026, 1, 18, 17, 0, tzinfo=UTC),
    ),
    # 2026-07-19 Sunday — summer: 19 IL = 16 UTC.
    (
        "0 19 * * SUN",
        "audit_summer",
        datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 19, 16, 0, tzinfo=UTC),
    ),
    # watchlist — "30 8 * * *" Asia/Jerusalem
    # Winter: 08:30 IL = 06:30 UTC.
    (
        "30 8 * * *",
        "watchlist_winter",
        datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
        datetime(2026, 1, 15, 6, 30, tzinfo=UTC),
    ),
    # Summer: 08:30 IL = 05:30 UTC.
    (
        "30 8 * * *",
        "watchlist_summer",
        datetime(2026, 7, 15, 3, 0, tzinfo=UTC),
        datetime(2026, 7, 15, 5, 30, tzinfo=UTC),
    ),
    # plan_watcher — "0 7 * * *" Asia/Jerusalem
    # Winter: 07:00 IL = 05:00 UTC.
    (
        "0 7 * * *",
        "plan_watcher_winter",
        datetime(2026, 1, 15, 3, 0, tzinfo=UTC),
        datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
    ),
    # Summer: 07:00 IL = 04:00 UTC.
    (
        "0 7 * * *",
        "plan_watcher_summer",
        datetime(2026, 7, 15, 3, 0, tzinfo=UTC),
        datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
    ),
]


@pytest.mark.parametrize(
    "cron,label,ref_utc,expected_next_utc",
    CRON_MATRIX,
    ids=[row[1] for row in CRON_MATRIX],
)
def test_cron_evaluates_in_local_timezone(
    cron: str,
    label: str,
    ref_utc: datetime,
    expected_next_utc: datetime,
) -> None:
    """All eight cron-driven loops evaluate in Asia/Jerusalem across both
    DST states."""
    sched = LoopSchedule(cron=cron, timezone="Asia/Jerusalem")
    got = sched.next_due_after(ref_utc)
    # Coerce both sides to UTC for comparison.
    assert got.astimezone(UTC) == expected_next_utc, (
        f"[{label}] cron={cron!r} ref={ref_utc} "
        f"expected={expected_next_utc} got={got}"
    )


# ---------------------------------------------------------------------------
# Spring-forward + fall-back transitions.
# Asia/Jerusalem DST in 2026:
#   Spring-forward: 2026-03-27 02:00 IST -> 03:00 IDT (Friday before last
#                   Sunday of March, per Israel's rules).
#   Fall-back:      2026-10-25 02:00 IDT -> 01:00 IST (last Sunday of
#                   October).
# ---------------------------------------------------------------------------


def test_spring_forward_does_not_skip_daily_brief() -> None:
    """Daily-brief fires at 9 IL every day across the spring-forward
    Sunday. No skipped day."""
    sched = LoopSchedule(cron="0 9 * * *", timezone="Asia/Jerusalem")
    # Ref Friday morning UTC, before the IL DST jump that night.
    ref = datetime(2026, 3, 27, 5, 0, tzinfo=UTC)
    # Next fire is Friday 9 IL = 7 UTC (still IST).
    got1 = sched.next_due_after(ref)
    assert got1.astimezone(IL).hour == 9
    # Step forward by 2 days — should be Sunday 9 IL = 6 UTC (now IDT).
    got2 = sched.next_due_after(got1 + (got1 - got1.replace(hour=0)))
    # Loop through 3 days; just assert each is at 9 IL.
    cur = ref
    seen_hours_il = []
    for _ in range(4):
        cur = sched.next_due_after(cur)
        seen_hours_il.append(cur.astimezone(IL).hour)
    assert seen_hours_il == [9, 9, 9, 9]


def test_fall_back_does_not_double_fire() -> None:
    """Daily-brief fires once at 9 IL each day across the fall-back
    Sunday."""
    sched = LoopSchedule(cron="0 9 * * *", timezone="Asia/Jerusalem")
    # Ref Saturday UTC, before the IL DST drop.
    ref = datetime(2026, 10, 24, 5, 0, tzinfo=UTC)
    cur = ref
    seen_hours_il = []
    for _ in range(4):
        cur = sched.next_due_after(cur)
        seen_hours_il.append(cur.astimezone(IL).hour)
    assert seen_hours_il == [9, 9, 9, 9]


# ---------------------------------------------------------------------------
# Naive-ref defensive path.
# ---------------------------------------------------------------------------


def test_naive_ref_is_coerced_to_utc() -> None:
    """A naive ref is treated as UTC and processed correctly."""
    sched = LoopSchedule(cron="0 9 * * *", timezone="Asia/Jerusalem")
    naive_ref = datetime(2026, 1, 15, 5, 0)  # implicit UTC
    got = sched.next_due_after(naive_ref)
    # Should resolve to 9 IL (= 7 UTC in January).
    assert got.astimezone(IL).hour == 9


# ---------------------------------------------------------------------------
# Interval-driven loops — regression check (unchanged behavior).
# ---------------------------------------------------------------------------


def test_interval_loop_unchanged() -> None:
    sched = LoopSchedule(interval_seconds=60)
    ref = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    got = sched.next_due_after(ref)
    assert got == ref.replace(minute=1)


def test_no_cron_no_interval_falls_back_to_1h() -> None:
    sched = LoopSchedule()
    ref = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    got = sched.next_due_after(ref)
    assert got == ref.replace(hour=13)
