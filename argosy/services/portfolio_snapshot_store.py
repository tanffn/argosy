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
    allow_stale: bool = False,
    allow_catastrophic_drop: bool = False,
    actor: str | None = None,
    override_reason: str | None = None,
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

    Ingest guards (the Jul-13 NVDA-wipe class of failure):
      * reject ``snapshot_date`` older than the current latest
      * reject a silent catastrophic drop in position count / securities value
    Pass ``allow_stale`` / ``allow_catastrophic_drop`` only for deliberate
    repairs (self-refresh carry of a known-good book is dated today and
    passes; a re-import of an older incomplete TSV does not).
    """
    from argosy.services.holding_books import (
        SnapshotIngestRejected,
        assess_snapshot_ingest,
        load_policy_symbols,
        stamp_management_flags,
        sync_unmanaged_from_positions,
    )

    latest = get_latest_snapshot_row(session, user_id)
    assess_snapshot_ingest(
        latest_row=latest,
        new_positions=snapshot.positions,
        new_snapshot_date=snapshot.snapshot_date,
        allow_stale=allow_stale,
        allow_catastrophic_drop=allow_catastrophic_drop,
    )

    policy = load_policy_symbols(session, user_id)
    stamped = _normalized_position_dicts(snapshot.positions, policy_symbols=policy)

    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=snapshot.snapshot_date,
        imported_at=datetime.now(timezone.utc),
        source_path=snapshot.source_path,
        positions_json=json.dumps(stamped, default=str),
        allocations_json=json.dumps(
            [a.model_dump() for a in snapshot.allocations], default=str,
        ),
        nvda_sales_json=json.dumps(
            dedup_nvda_sale_dicts(
                [s.model_dump() for s in snapshot.nvda_sales]
            ),
            default=str,
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
    # Lifecycle sync in SAVEPOINTs — never rolls back this snapshot write.
    # Account closure is NOT tied to catastrophic-drop — use
    # retire_unmanaged_account for an explicit single-account retirement.
    sync_result = sync_unmanaged_from_positions(
        session, user_id, stamped, commit=False,
        valued_as_of=snapshot.snapshot_date,
    )
    if sync_result.get("errors"):
        from argosy.logging import get_logger
        get_logger("argosy.portfolio_snapshot_store").warning(
            "unmanaged_sync_errors", **sync_result,
        )
    if allow_stale or allow_catastrophic_drop:
        # Auditable override — stamp the persisted row's warnings with enough
        # to reconstruct who bypassed what and why.
        try:
            warns = json.loads(row.parse_warnings_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            warns = []
        would_have: list[str] = []
        if allow_stale:
            would_have.append("stale_snapshot_date")
        if allow_catastrophic_drop:
            would_have.append("catastrophic_position_or_value_drop")
        actor = actor or "unspecified"
        reason = override_reason or "unspecified"
        warns.append(
            "INGEST_OVERRIDE "
            f"actor={actor} "
            f"reason={reason} "
            f"allow_stale={allow_stale} "
            f"allow_catastrophic_drop={allow_catastrophic_drop} "
            f"guards_bypassed={','.join(would_have) or 'none'} "
            f"snapshot_date={snapshot.snapshot_date}"
        )
        row.parse_warnings_json = json.dumps(warns)
        from argosy.logging import get_logger
        get_logger("argosy.portfolio_snapshot_store").warning(
            "snapshot_ingest.override",
            user_id=user_id,
            actor=actor,
            reason=reason,
            allow_stale=allow_stale,
            allow_catastrophic_drop=allow_catastrophic_drop,
            guards_bypassed=would_have,
            snapshot_date=str(snapshot.snapshot_date),
        )
    if commit:
        session.commit()
    else:
        session.flush()
    session.refresh(row)
    return row


# Re-export for callers that need to catch the guard.
from argosy.services.holding_books import SnapshotIngestRejected  # noqa: E402


def dedup_nvda_sale_dicts(rows: list) -> list:
    """Drop IDENTICAL ``(month, shares, price)`` NVDA sale repeats.

    The hand-maintained TSV repeated the Apr-2026 row verbatim and the
    duplicate then rode every snapshot carry (self-refresh, apply-fills)
    forever. A verbatim repeat is a copy-paste artifact, never two real
    sales; same-month rows that differ in shares or price are KEPT (two
    genuine sales in one month are possible). Order is preserved.
    Historical rows are never rewritten — this runs on read hydration
    and on the next written row only.
    """
    seen: set[tuple] = set()
    out: list = []
    for r in rows:
        if isinstance(r, dict):
            key = (
                str(r.get("month") or "").strip().lower(),
                r.get("shares"),
                r.get("price"),
            )
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


def _normalized_position_dicts(
    positions: list, *, policy_symbols: frozenset | None = None
) -> list[dict]:
    """Serialize positions with asset_type CORRECTED against the instrument
    reference, so the stored snapshot is canonical for every consumer (not
    just display-corrected per-surface).

    The source Type column is hand-maintained and occasionally mislabels an
    instrument's asset CLASS (STOXX Europe 600 + EIMI tagged "REIT" though
    they're equity ETFs; IWDP tagged "Equity" though it's a property/REIT
    ETF). When the source type implies a different class than the reference,
    store the reference's sector; otherwise keep the source tilt
    (Growth/Dividend/Core/REIT-for-genuine-REITs), which the reference
    doesn't capture. Cash and unknown instruments are untouched.

    Also stamps explicit ``managed`` / ``excluded_from_sleeve_math`` flags
    (see ``holding_books``) so sleeve math vs total-book consumers never
    confuse deliberate exclusion with absence. Explicit TSV overrides
    (``review_status`` / ``details`` markers) are honored.
    """
    from argosy.services import instrument_reference
    from argosy.services.holding_books import stamp_management_flags
    from argosy.services.wealth_dashboard import _classify_asset_class

    raw: list[dict] = []
    for p in positions:
        d = p.model_dump() if hasattr(p, "model_dump") else dict(p)
        sym = (d.get("symbol") or "").strip()
        at = (d.get("asset_type") or "").strip()
        ref = instrument_reference.lookup(sym, d.get("details") or "")
        if ref is not None and ref.asset_class != _classify_asset_class(at, sym):
            d["asset_type"] = ref.sector
        raw.append(d)
    return stamp_management_flags(raw, policy_symbols=policy_symbols)


def get_latest_snapshot_row(
    session: Session, user_id: str
) -> PortfolioSnapshotRow | None:
    """Return the most recently persisted snapshot for ``user_id`` or None.

    Ordering is ``(imported_at DESC, id DESC)`` — the ``id`` tiebreak makes
    the pick deterministic when two rows share a timestamp. On SQLite the
    DateTime column compares as TEXT, so a second-precision timestamp
    (``14:04:41`` — written outside the SQLAlchemy default, which always
    emits microseconds) can tie or interleave with a microsecond one; the
    autoincrement id is the insertion order and settles it.
    """
    return session.execute(
        select(PortfolioSnapshotRow)
        .where(PortfolioSnapshotRow.user_id == user_id)
        .order_by(
            desc(PortfolioSnapshotRow.imported_at),
            desc(PortfolioSnapshotRow.id),
        )
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
        # Read-side dedup: historical rows carry the duplicate Apr-2026 sale
        # verbatim (never rewritten in place); every hydrating consumer —
        # incl. the TSV export — sees the clean block.
        nvda_sales=dedup_nvda_sale_dicts(json.loads(row.nvda_sales_json or "[]")),
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
    allow_stale: bool = False,
    allow_catastrophic_drop: bool = False,
    actor: str | None = None,
    override_reason: str | None = None,
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
        allow_stale=allow_stale,
        allow_catastrophic_drop=allow_catastrophic_drop,
        actor=actor,
        override_reason=override_reason,
    )


__all__ = [
    "SnapshotIngestRejected",
    "dedup_nvda_sale_dicts",
    "get_latest_snapshot_row",
    "latest_matches_snapshot",
    "persist_snapshot",
    "persist_snapshot_from_tsv",
    "row_to_snapshot",
    "write_through_if_changed",
]
