"""Ingest Schwab Equity Awards Center vest events into rsu_vest_events.

Reads a Schwab Equity Awards CSV via the existing parser at
`argosy.services.rsu_reconciliation.schwab_csv`, then persists each
SchwabVestEvent as an RsuVestEvent row. Idempotent on
(user_id, grant_id, vest_date) — re-ingesting the same CSV is a no-op.

Consumers:
  - <HolisticTimelineCard> (sprint commit #10) — render historical vest
    markers on the /retirement timeline.
  - cashflow_projection.effective_retire_ready_age() (commit #9) — uses
    the most recent vest event per grant to seed projected next-vest.
  - argosy/services/rsu_vest_projection.py (commit #10 follow-on) —
    forward-projects upcoming vests from per-grant cadence.

Migration: alembic 0044 created the table.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from argosy.services.rsu_reconciliation.schwab_csv import (
    SchwabVestEvent,
    parse_csv,
)
from argosy.state.models import RsuVestEvent


@dataclass(frozen=True)
class VestIngestResult:
    """Outcome of an ingest run."""
    user_id: str
    source_file: str
    parsed_event_count: int      # how many vest events the parser produced
    inserted_count: int          # how many new rows landed in the DB
    duplicate_count: int         # how many were already present (idempotent skip)


def ingest_schwab_vest_events(
    session: Session,
    user_id: str,
    csv_path: Path,
) -> VestIngestResult:
    """Parse `csv_path` and persist its vest events for `user_id`.

    Returns counts so callers can render a useful confirmation. Re-running
    against the same file is safe — `(user_id, grant_id, vest_date)` is
    UNIQUE in the table, so duplicates skip silently.

    **First-write-wins semantics (codex IMPORTANT, commit #7 review):**
    if Schwab re-issues a corrected CSV with adjusted share counts or FMV
    for an existing (grant, vest_date) tuple, this function will SKIP it,
    not update the existing row. The assumption is that vest-event truth
    is established at the moment of the original lapse and Schwab doesn't
    retroactively correct it. If a real corrected-reissue scenario arises,
    the operator should DELETE the affected row + re-run (or extend this
    function with explicit upsert-on-diff logic).
    """
    report = parse_csv(csv_path)
    source_file = str(csv_path)

    inserted = 0
    duplicates = 0
    for event in report.vest_events:
        if _row_exists(session, user_id, event):
            duplicates += 1
            continue
        session.add(
            RsuVestEvent(
                user_id=user_id,
                symbol=event.symbol,
                grant_id=event.grant_id,
                vest_date=event.date,
                shares_vested=Decimal(str(event.shares_vested)),
                shares_withheld=Decimal(str(event.shares_withheld)),
                shares_net=Decimal(str(event.shares_net)),
                fmv_per_share_usd=Decimal(str(event.fmv_per_share_usd)),
                award_date=event.award_date,
                source_file=source_file,
            )
        )
        inserted += 1

    session.commit()

    return VestIngestResult(
        user_id=user_id,
        source_file=source_file,
        parsed_event_count=len(report.vest_events),
        inserted_count=inserted,
        duplicate_count=duplicates,
    )


def _row_exists(
    session: Session,
    user_id: str,
    event: SchwabVestEvent,
) -> bool:
    """Check whether a row matching the UNIQUE key already exists."""
    return session.query(
        session.query(RsuVestEvent)
        .filter(RsuVestEvent.user_id == user_id)
        .filter(RsuVestEvent.grant_id == event.grant_id)
        .filter(RsuVestEvent.vest_date == event.date)
        .exists()
    ).scalar()
