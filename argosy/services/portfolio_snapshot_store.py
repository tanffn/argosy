"""Persistence helpers for `portfolio_snapshots` (migration 0030).

The legacy pattern walks ``${ARGOSY_HOME}/**/*.tsv`` on every request and
re-parses the freshest matching file. Two failure modes:

1. A stray small upload under ``uploads/<user>/.../`` shadows the real
   ``Family Finances Status - <date>.tsv`` if its mtime is newer. The
   `_find_latest_tsv` helper filters by header marker now, but the
   filesystem walk is still per-request hot path.
2. Synthesis Phase 1 inputs and the per-tab `/api/portfolio/snapshot`
   endpoint do the same work twice on every check-in / page load.

This module persists the parsed shape so:

* `persist_snapshot(...)` is called from the ingest path on TSV upload
  (or lazily on first `/api/portfolio/snapshot` request for backwards
  compat).
* `get_latest_snapshot(...)` returns the most recent persisted row for
  a user, or ``None`` if the table is empty.

JSON encoding mirrors the PortfolioSnapshot pydantic model so the
hydration step in `to_dto(...)` can ``PortfolioSnapshot(**...)`` over
the round-trip.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.ingest.tsv import PortfolioSnapshot
from argosy.state.models import PortfolioSnapshotRow


def persist_snapshot(
    session: Session,
    *,
    user_id: str,
    snapshot: PortfolioSnapshot,
    commit: bool = True,
) -> PortfolioSnapshotRow:
    """Write one parsed snapshot row. Returns the persisted ORM row.

    Each call appends a NEW row (no upsert) — keeping the history is
    cheap and lets the chart pages render historical allocation curves
    later without a migration.

    Idempotency: callers should check ``latest_matches_snapshot`` first
    so re-running the same TSV doesn't bloat the table with duplicates.
    This function is intentionally dumb (always writes); the idempotency
    decision lives at the call site.

    ``commit=False`` adds + flushes but leaves the commit to the caller —
    for write-throughs that must be ATOMIC with a surrounding batch (e.g.
    the XLS↔Osh pairing resolution runs mid-ingest; an internal commit
    there would split the ingest's atomic transaction).
    """
    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=snapshot.snapshot_date,
        imported_at=datetime.now(timezone.utc),
        source_path=snapshot.source_path,
        positions_json=json.dumps(
            [p.model_dump() for p in snapshot.positions], default=str,
        ),
        allocations_json=json.dumps(
            [a.model_dump() for a in snapshot.allocations], default=str,
        ),
        nvda_sales_json=json.dumps(
            [s.model_dump() for s in snapshot.nvda_sales], default=str,
        ),
        real_estate_json=json.dumps(
            [r.model_dump() for r in snapshot.real_estate], default=str,
        ),
        pensions_json=json.dumps(
            [pe.model_dump() for pe in snapshot.pensions], default=str,
        ),
        totals_json=json.dumps({
            "total_usd_value_k": snapshot.total_usd_value_k,
            "cash_balances_usd_k": snapshot.cash_balances_usd_k(),
        }),
        fx_usd_nis=snapshot.fx_usd_nis,
        fx_usd_eur=snapshot.fx_usd_eur,
        parse_warnings_json=json.dumps(list(snapshot.parse_warnings)),
    )
    session.add(row)
    if commit:
        session.commit()
    else:
        session.flush()
    session.refresh(row)
    return row


def get_latest_snapshot_row(
    session: Session, user_id: str
) -> PortfolioSnapshotRow | None:
    """Return the most recently persisted snapshot for ``user_id`` or None."""
    return session.execute(
        select(PortfolioSnapshotRow)
        .where(PortfolioSnapshotRow.user_id == user_id)
        .order_by(desc(PortfolioSnapshotRow.imported_at))
        .limit(1)
    ).scalar_one_or_none()


def row_to_snapshot(row: PortfolioSnapshotRow) -> PortfolioSnapshot:
    """Re-hydrate a persisted row back into the pydantic PortfolioSnapshot.

    Inverse of ``persist_snapshot``. Used by call sites that historically
    called ``parse_portfolio_tsv()`` and now want to read from the DB
    without changing their downstream code.
    """
    return PortfolioSnapshot(
        source_path=row.source_path or "",
        snapshot_date=row.snapshot_date,
        fx_usd_nis=row.fx_usd_nis,
        fx_usd_eur=row.fx_usd_eur,
        positions=json.loads(row.positions_json or "[]"),
        real_estate=json.loads(row.real_estate_json or "[]"),
        allocations=json.loads(row.allocations_json or "[]"),
        nvda_sales=json.loads(row.nvda_sales_json or "[]"),
        pensions=json.loads(row.pensions_json or "[]"),
        parse_warnings=json.loads(row.parse_warnings_json or "[]"),
    )


def persist_snapshot_from_tsv(
    session: Session, *, user_id: str, tsv_path: Path | str
) -> PortfolioSnapshotRow:
    """Parse a TSV path and write the resulting snapshot row.

    Convenience entry point for the ingest CLI and the lazy write-through
    path in ``/api/portfolio/snapshot``.
    """
    from argosy.ingest.tsv import parse_portfolio_tsv

    snap = parse_portfolio_tsv(tsv_path)
    return persist_snapshot(session, user_id=user_id, snapshot=snap)


def latest_matches_snapshot(
    session: Session, *, user_id: str, snapshot: PortfolioSnapshot
) -> bool:
    """Return True iff the latest row already represents ``snapshot``.

    Used by the write-through path so we don't bloat ``portfolio_snapshots``
    with duplicate rows when ``/api/portfolio/snapshot`` is hit repeatedly
    against the same source TSV. The match criterion is ``source_path`` +
    ``snapshot_date`` + position count + total USD value — strong enough
    to detect "same parse output" but cheap (no JSON deep-compare).
    """
    row = get_latest_snapshot_row(session, user_id)
    if row is None:
        return False
    if (row.source_path or "") != (snapshot.source_path or ""):
        return False
    if row.snapshot_date != snapshot.snapshot_date:
        return False
    # Position-count + totals proxy for content equality. JSON deep-compare
    # would be defensible but adds CPU cost for the hot path with no
    # benefit — a TSV with the same source_path + date + position count +
    # total value is the same parse output for our purposes.
    try:
        positions = json.loads(row.positions_json or "[]")
        if len(positions) != len(snapshot.positions):
            return False
        totals = json.loads(row.totals_json or "{}")
        if abs(
            float(totals.get("total_usd_value_k", 0.0))
            - float(snapshot.total_usd_value_k)
        ) > 1e-6:
            return False
    except (ValueError, TypeError):
        return False
    return True


def write_through_if_changed(
    session: Session, *, user_id: str, snapshot: PortfolioSnapshot,
    commit: bool = True,
) -> PortfolioSnapshotRow | None:
    """Persist ``snapshot`` iff the latest row doesn't already match it.

    Returns the newly-written row, or ``None`` when the existing latest
    row already represents this snapshot (idempotent no-op). This is the
    entry point ``/api/portfolio/snapshot`` and the synthesis input
    assembler use when they fall back to filesystem-walk + parse but want
    future requests to read from the DB.

    ``commit=False`` defers the commit to the caller (atomic write-through
    inside a surrounding batch — see ``persist_snapshot``).
    """
    if latest_matches_snapshot(session, user_id=user_id, snapshot=snapshot):
        return None
    return persist_snapshot(
        session, user_id=user_id, snapshot=snapshot, commit=commit,
    )


__all__ = [
    "get_latest_snapshot_row",
    "latest_matches_snapshot",
    "persist_snapshot",
    "persist_snapshot_from_tsv",
    "row_to_snapshot",
    "write_through_if_changed",
]
