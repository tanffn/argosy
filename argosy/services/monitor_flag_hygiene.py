"""Monitor-flag expiry hygiene.

Every flag-writing sink now stamps a default ``expires_at`` (observer
flags 7d, thesis flags 7d, mc_regression 14d, alpha_report_caution 14d
— see the respective writers). Rows written BEFORE a sink gained its
default TTL carry ``expires_at IS NULL`` and can never leave the
active surface (observed: ``alpha_report_caution`` id 1 active for 38+
days).

:func:`backfill_missing_flag_expiry` is the one-time repair path: for
active rows of kinds in :data:`DEFAULT_TTL_DAYS_BY_KIND` with a NULL
``expires_at``, set ``expires_at = surfaced_at + default TTL``. Rows
are never deleted (the flags table is audit truth); a backfilled row
whose computed expiry is already in the past simply stops being served
by the active-flags filter.

Kinds NOT in the map (state_observer_*, thesis_monitor_*,
mc_regression) already have their own lifecycle — writer-stamped TTLs
plus producer-scope supersession — and are left untouched.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import MonitorFlag

_log = get_logger("argosy.services.monitor_flag_hygiene")

#: Default TTLs for kinds whose historical rows may have NULL expiry.
#: Keep in sync with the writers (``DEFAULT_CAUTION_TTL_DAYS`` in
#: ``alpha_report_analyst_runner``).
DEFAULT_TTL_DAYS_BY_KIND: dict[str, int] = {
    "alpha_report_caution": 14,
}


def backfill_missing_flag_expiry(session: Session) -> int:
    """Stamp ``expires_at = surfaced_at + default TTL`` on active rows
    with NULL expiry for the kinds in :data:`DEFAULT_TTL_DAYS_BY_KIND`.

    Idempotent (a stamped row no longer matches the NULL filter).
    Returns the number of rows backfilled. Commits when it changed
    anything.
    """
    rows = (
        session.execute(
            select(MonitorFlag).where(
                MonitorFlag.kind.in_(sorted(DEFAULT_TTL_DAYS_BY_KIND)),
                MonitorFlag.expires_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for row in rows:
        ttl_days = DEFAULT_TTL_DAYS_BY_KIND[row.kind]
        row.expires_at = row.surfaced_at + timedelta(days=ttl_days)
        _log.info(
            "monitor_flag_hygiene.expiry_backfilled",
            flag_id=row.id,
            kind=row.kind,
            surfaced_at=str(row.surfaced_at),
            expires_at=str(row.expires_at),
            ttl_days=ttl_days,
        )
        count += 1
    if count:
        session.commit()
    return count


__all__ = ["DEFAULT_TTL_DAYS_BY_KIND", "backfill_missing_flag_expiry"]
