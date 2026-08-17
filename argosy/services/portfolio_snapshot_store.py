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

Ingest semantics (stream D repair):
  * Per-account merge — a feed that omits an account carries no information
    about it; those holdings are carried forward with their own
    ``observed_as_of``.
  * Coverage metadata is recorded on ``totals_json.accounts_covered`` /
    ``accounts_carried`` so consumers can tell coverage from emptiness.
  * Stale-date and within-coverage catastrophic-drop guards refuse destructive
    writes unless an audited override (with a required reason) is passed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
from argosy.state.models import AuditLog, PortfolioSnapshotRow


def _record_ingest_audit(
    session: Session,
    *,
    user_id: str,
    event_type: str,
    payload: dict[str, Any],
    entity_id: str = "",
) -> None:
    """Durable audit_log row for ingest accept/reject (sync session)."""
    session.add(
        AuditLog(
            user_id=user_id,
            event_type=event_type,
            entity_type="portfolio_snapshot",
            entity_id=str(entity_id or ""),
            payload_json=json.dumps(payload, default=str),
            created_at=datetime.now(timezone.utc),
        )
    )


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
      * reject a silent catastrophic drop WITHIN accounts the feed covers
      * merge per-account so uncovered accounts are carried forward
    Pass ``allow_stale`` / ``allow_catastrophic_drop`` only for deliberate
    repairs, and always with a non-empty ``override_reason``.
    """
    from argosy.services.holding_books import (
        SnapshotIngestRejected,
        accounts_covered_from_positions,
        assess_snapshot_ingest,
        load_policy_symbols,
        merge_positions_per_account,
        position_feed_fingerprint,
        resolve_prior_positions_by_account_coverage,
        stamp_management_flags,
        sync_unmanaged_from_positions,
    )

    if (allow_stale or allow_catastrophic_drop) and not (override_reason or "").strip():
        raise SnapshotIngestRejected(
            "override_reason_required",
            "allow_stale / allow_catastrophic_drop require a non-empty "
            "override_reason for the audit trail",
        )

    latest = get_latest_snapshot_row(session, user_id)
    incoming_positions = list(snapshot.positions)
    covered = accounts_covered_from_positions(incoming_positions)

    try:
        assess_snapshot_ingest(
            latest_row=latest,
            new_positions=incoming_positions,
            new_snapshot_date=snapshot.snapshot_date,
            allow_stale=allow_stale,
            allow_catastrophic_drop=allow_catastrophic_drop,
            accounts_covered=sorted(covered),
        )
    except SnapshotIngestRejected as exc:
        from argosy.logging import get_logger
        get_logger("argosy.portfolio_snapshot_store").warning(
            "snapshot_ingest.rejected",
            user_id=user_id,
            code=exc.code,
            detail=exc.detail,
            actor=actor,
            override_reason=override_reason,
            allow_stale=allow_stale,
            allow_catastrophic_drop=allow_catastrophic_drop,
            accounts_covered=sorted(covered),
            snapshot_date=str(snapshot.snapshot_date),
        )
        _record_ingest_audit(
            session,
            user_id=user_id,
            event_type="snapshot.ingest.rejected",
            payload={
                "code": exc.code,
                "detail": exc.detail,
                "actor": actor,
                "override_reason": override_reason,
                "allow_stale": allow_stale,
                "allow_catastrophic_drop": allow_catastrophic_drop,
                "accounts_covered": sorted(covered),
                "snapshot_date": str(snapshot.snapshot_date),
                "source_path": snapshot.source_path,
            },
        )
        if commit:
            session.commit()
        else:
            session.flush()
        raise

    # Per-account merge onto the last-coverage prior book — NOT merely the
    # globally latest snapshot (which may already omit uncovered accounts).
    prior_positions = resolve_prior_positions_by_account_coverage(session, user_id)
    prior_date = latest.snapshot_date if latest is not None else None

    try:
        merge = merge_positions_per_account(
            prior_positions=prior_positions,
            incoming_positions=incoming_positions,
            incoming_snapshot_date=snapshot.snapshot_date,
            prior_snapshot_date=prior_date,
        )
    except SnapshotIngestRejected as exc:
        from argosy.logging import get_logger
        get_logger("argosy.portfolio_snapshot_store").warning(
            "snapshot_ingest.rejected",
            user_id=user_id,
            code=exc.code,
            detail=exc.detail,
            actor=actor,
            accounts_covered=sorted(covered),
            snapshot_date=str(snapshot.snapshot_date),
        )
        _record_ingest_audit(
            session,
            user_id=user_id,
            event_type="snapshot.ingest.rejected",
            payload={
                "code": exc.code,
                "detail": exc.detail,
                "actor": actor,
                "accounts_covered": sorted(covered),
                "snapshot_date": str(snapshot.snapshot_date),
                "source_path": snapshot.source_path,
            },
        )
        if commit:
            session.commit()
        else:
            session.flush()
        raise

    policy = load_policy_symbols(session, user_id)
    stamped = stamp_management_flags(merge.positions, policy_symbols=policy)
    # Re-normalize asset types against the instrument reference.
    stamped = _normalized_position_dicts(
        [_dict_as_position(p) for p in stamped],
        policy_symbols=policy,
    )

    # Rebuild totals from the MERGED book (not the raw feed).
    total_usd_k = 0.0
    cash_balances = 0.0
    for p in stamped:
        try:
            usd = float(p.get("usd_value_k") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        total_usd_k += usd
        at = str(p.get("asset_type") or "").lower()
        if "cash" in at:
            cash_balances += usd

    warns = list(snapshot.parse_warnings)
    if merge.accounts_carried:
        warns.append(
            "ACCOUNT_MERGE carried_forward="
            + ",".join(merge.accounts_carried)
            + " covered="
            + ",".join(merge.accounts_covered)
        )
    for old_s, new_s, acct, sh in merge.renames:
        warns.append(
            f"SYMBOL_RENAME old={old_s!r} new={new_s!r} account={acct} shares={sh}"
        )

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
            "total_usd_value_k": total_usd_k,
            "cash_balances_usd_k": cash_balances,
            "accounts_covered": list(merge.accounts_covered),
            "accounts_carried": list(merge.accounts_carried),
            "feed_position_count": len(incoming_positions),
            "merged_position_count": len(stamped),
            "feed_fingerprint": position_feed_fingerprint(
                [
                    (p.model_dump() if hasattr(p, "model_dump") else dict(p))
                    for p in incoming_positions
                ]
            ),
        }),
        fx_usd_nis=snapshot.fx_usd_nis,
        fx_usd_eur=snapshot.fx_usd_eur,
        parse_warnings_json=json.dumps(warns),
    )
    session.add(row)
    # Lifecycle sync in SAVEPOINTs — never rolls back this snapshot write.
    # Account closure is NOT tied to catastrophic-drop — use
    # retire_unmanaged_account for an explicit single-account retirement.
    # Sync against the FEED positions for covered accounts (sales), not the
    # carried-forward book — unmanaged sync already keeps absent accounts.
    # Feed-only sync preserves carried rows' own observed_as_of.
    feed_for_sync = stamp_management_flags(
        [
            (p.model_dump() if hasattr(p, "model_dump") else dict(p))
            for p in incoming_positions
        ],
        policy_symbols=policy,
    )
    # Stamp feed rows with THIS feed's date only (covered accounts).
    for p in feed_for_sync:
        if p.get("observed_as_of") is None:
            p["observed_as_of"] = snapshot.snapshot_date
        if p.get("valued_as_of") is None:
            p["valued_as_of"] = snapshot.snapshot_date
    sync_result = sync_unmanaged_from_positions(
        session, user_id, feed_for_sync, commit=False,
        valued_as_of=snapshot.snapshot_date,
    )
    if sync_result.get("errors"):
        from argosy.logging import get_logger
        get_logger("argosy.portfolio_snapshot_store").warning(
            "unmanaged_sync_errors", **sync_result,
        )
    if allow_stale or allow_catastrophic_drop:
        try:
            row_warns = json.loads(row.parse_warnings_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            row_warns = []
        would_have: list[str] = []
        if allow_stale:
            would_have.append("stale_snapshot_date")
        if allow_catastrophic_drop:
            would_have.append("catastrophic_position_or_value_drop")
        actor_s = actor or "unspecified"
        reason_s = (override_reason or "").strip()
        row_warns.append(
            "INGEST_OVERRIDE "
            f"actor={actor_s} "
            f"reason={reason_s} "
            f"allow_stale={allow_stale} "
            f"allow_catastrophic_drop={allow_catastrophic_drop} "
            f"guards_bypassed={','.join(would_have) or 'none'} "
            f"snapshot_date={snapshot.snapshot_date}"
        )
        row.parse_warnings_json = json.dumps(row_warns)
        from argosy.logging import get_logger
        get_logger("argosy.portfolio_snapshot_store").warning(
            "snapshot_ingest.override",
            user_id=user_id,
            actor=actor_s,
            reason=reason_s,
            allow_stale=allow_stale,
            allow_catastrophic_drop=allow_catastrophic_drop,
            guards_bypassed=would_have,
            snapshot_date=str(snapshot.snapshot_date),
        )
        _record_ingest_audit(
            session,
            user_id=user_id,
            event_type="snapshot.ingest.override",
            payload={
                "actor": actor_s,
                "override_reason": reason_s,
                "allow_stale": allow_stale,
                "allow_catastrophic_drop": allow_catastrophic_drop,
                "guards_bypassed": would_have,
                "snapshot_date": str(snapshot.snapshot_date),
                "accounts_covered": list(merge.accounts_covered),
                "accounts_carried": list(merge.accounts_carried),
                "source_path": snapshot.source_path,
            },
        )
    else:
        _record_ingest_audit(
            session,
            user_id=user_id,
            event_type="snapshot.ingest.accepted",
            payload={
                "actor": actor,
                "snapshot_date": str(snapshot.snapshot_date),
                "accounts_covered": list(merge.accounts_covered),
                "accounts_carried": list(merge.accounts_carried),
                "merged_position_count": len(stamped),
                "feed_position_count": len(incoming_positions),
                "source_path": snapshot.source_path,
            },
        )
    if commit:
        session.commit()
    else:
        session.flush()
    session.refresh(row)
    return row


# Re-export for callers that need to catch the guard.
from argosy.services.holding_books import SnapshotIngestRejected  # noqa: E402


def _dict_as_position(d: dict) -> PortfolioPosition:
    """Best-effort coerce a merged dict into PortfolioPosition for normalize."""
    known = {f for f in PortfolioPosition.model_fields}
    payload = {k: v for k, v in d.items() if k in known}
    return PortfolioPosition(**payload)


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

    Coverage fields (``accounts_covered`` / ``accounts_carried``) are read
    from ``totals_json`` so API consumers can tell coverage from emptiness.
    """
    try:
        totals = json.loads(row.totals_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        totals = {}
    covered = [str(a) for a in (totals.get("accounts_covered") or [])]
    carried = [str(a) for a in (totals.get("accounts_carried") or [])]
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
        accounts_covered=covered,
        accounts_carried=carried,
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
    against the same source TSV. Match criterion: ``source_path`` +
    ``snapshot_date`` + **feed content fingerprint** (symbol, account,
    shares, usd_value_k) — shape alone is not enough (a sale or symbol
    swap with equal row count must write).
    """
    from argosy.services.holding_books import position_feed_fingerprint

    row = get_latest_snapshot_row(session, user_id)
    if row is None:
        return False
    if (row.source_path or "") != (snapshot.source_path or ""):
        return False
    if row.snapshot_date != snapshot.snapshot_date:
        return False
    incoming_fp = position_feed_fingerprint(
        [
            (p.model_dump() if hasattr(p, "model_dump") else dict(p))
            for p in (snapshot.positions or [])
        ]
    )
    try:
        totals = json.loads(row.totals_json or "{}")
        stored_fp = totals.get("feed_fingerprint")
        if stored_fp is not None:
            return list(stored_fp) == incoming_fp
        # Legacy rows without a fingerprint: reconstruct the feed slice
        # from accounts_covered when present; else refuse the match so a
        # same-shape content change cannot be silently dropped.
        positions = json.loads(row.positions_json or "[]")
        covered = {
            str(a).strip().lower()
            for a in (totals.get("accounts_covered") or [])
            if a
        }
        if covered:
            from argosy.services.holding_books import location_account_key

            feed_slice = [
                p for p in positions
                if isinstance(p, dict)
                and (location_account_key(p.get("location") or "") or "").lower()
                in covered
            ]
            return position_feed_fingerprint(feed_slice) == incoming_fp
        return False
    except (ValueError, TypeError):
        return False


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
