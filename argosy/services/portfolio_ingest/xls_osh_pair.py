"""Bidirectional pair resolver: Leumi portfolio XLS <-> Leumi Osh statement.

The Leumi monthly portfolio XLS export carries positions but no cash. The
cash balance must come from the corresponding Leumi Osh (current-account)
statement's closing running-balance. This service implements the auto-pair
logic in both directions:

  * XLS arrives first: look for a recent Osh statement in expense_statements.
    If found within the +/-15d match window, synthesize a TSV and fire the
    windfall detector. Otherwise, write a portfolio_snapshot_parts row with
    status='pending'; return detect_status='pending_pair' to the route.

  * Osh arrives first (i.e. an Osh statement ingests via /expenses while a
    pending XLS row is already in the DB): try_resolve_pending_on_osh_arrival
    walks portfolio_snapshot_parts for pending rows whose snapshot_date is
    in window, picks the closest, assembles, and fires the detector.

The TSV splice (synthesize_tsv) addresses BLOCKERs from the 2026-05-29 codex
zigzag (see tools/codex-tandem/sessions/2026-05-29-xls-osh-pair-design/):

  * #1 symbol stability  -- preserve the user's prior TSV symbol convention
    when mapping XLS positions to TSV rows. Build the mapping by matching
    XLS ticker against prior-TSV Leumi-row symbols, fuzzy-matching by name
    when ticker is absent. Without this, the windfall detector would see
    every Leumi holding as "sold + bought" on the first XLS-driven month.

  * #2 currency consistency -- infer per-row currency from the prior TSV
    when possible (security X was NIS-denominated last month -> stays NIS).
    Unknown new positions default to USD with a parse_warning.

  * #5 snapshot-effective FX -- use the prior TSV's USD/NIS rate (not live
    FX, not current-date FX). Detector compares cross-TSV cash deltas; a
    misdated rate produces phantom windfall signals.

  * #11 Osh closing-balance ordering -- (txn_date DESC, id DESC) tiebreak
    deterministically picks the last txn when multiple share the same date.

Hook point on the Osh side: explicit call from the orchestrator after a
Leumi-bank statement commits (codex zigzag #8 -- preferred over SQLA
after_insert events because it stays source-aware and testable).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from argosy.ingest.tsv import (
    AllocationRow,
    PortfolioSnapshot,
    parse_portfolio_tsv,
)
from argosy.services.portfolio_ingest.parsers.leumi_xls import (
    LeumiPortfolioPosition,
    LeumiPortfolioSnapshot,
    is_leumi_portfolio_xls,
    parse_leumi_portfolio_xls,
)
from argosy.state.models import (
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    PortfolioSnapshotPart,
)

_log = logging.getLogger(__name__)

# +/-15d window for matching XLS snapshot_date to Osh period_end.
MATCH_WINDOW_DAYS = 15

# Pending parts older than this (relative to a newer snapshot_date) are
# auto-staled when a fresh XLS lands. Keeps the queue from accumulating
# orphaned May rows when the user starts uploading June.
STALE_WINDOW_DAYS = 45


# ---------------------------------------------------------------------------
# Public return shapes
# ---------------------------------------------------------------------------


@dataclass
class PairResolution:
    """Outcome of an XLS upload attempt.

    status:
      * "resolved"     -- TSV synthesized + persisted; detector ran.
      * "pending_pair" -- no matching Osh; pending part row written.
      * "duplicate"    -- this XLS (by sha or by semantic key) was
                          already processed; returning the prior outcome.
    """
    status: str
    pending_pair_id: int | None
    resolved_tsv_path: Path | None
    snapshot_date: date | None
    sha256: str
    detail: str | None = None
    parse_warnings: list[str] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def handle_xls_upload(
    *,
    db: Session,
    user_id: str,
    contents: bytes,
    snapshot_root: Path,
) -> PairResolution:
    """Parse + pair an XLS upload, or queue it as pending.

    Caller (the upload route) is responsible for firing the windfall
    detector on the resolved_tsv_path. This function does the synthesis
    + persistence only.
    """
    sha = hashlib.sha256(contents).hexdigest()

    # Fast-path: same XLS bytes seen before? Return the prior row's outcome.
    existing_by_sha = (
        db.execute(
            select(PortfolioSnapshotPart).where(
                PortfolioSnapshotPart.user_id == user_id,
                PortfolioSnapshotPart.sha256 == sha,
            )
        )
        .scalar_one_or_none()
    )
    if existing_by_sha is not None:
        return _resolution_from_existing(existing_by_sha)

    xls = parse_leumi_portfolio_xls(contents)
    if xls.snapshot_date is None:
        return PairResolution(
            status="pending_pair",
            pending_pair_id=None,
            resolved_tsv_path=None,
            snapshot_date=None,
            sha256=sha,
            detail="XLS missing snapshot_date in row 1; cannot pair without a date.",
            parse_warnings=xls.parse_warnings,
        )

    # Semantic dedup: same date + portfolio_number = same snapshot, even
    # if bytes differ. Codex zigzag finding #9 (2026-05-29).
    existing_by_semantic = (
        db.execute(
            select(PortfolioSnapshotPart).where(
                PortfolioSnapshotPart.user_id == user_id,
                PortfolioSnapshotPart.snapshot_date == xls.snapshot_date,
                PortfolioSnapshotPart.portfolio_number == xls.portfolio_number,
            )
        )
        .scalar_one_or_none()
    )
    if existing_by_semantic is not None:
        return _resolution_from_existing(existing_by_semantic)

    # Auto-stale older pending rows for the same user. Codex zigzag finding #5+#7.
    _stale_old_pending(db, user_id=user_id, fresh_snapshot_date=xls.snapshot_date)

    # Look for a matching Osh statement already in the DB.
    osh = _find_matching_osh(db, user_id=user_id, snapshot_date=xls.snapshot_date)

    payload_json = json.dumps(_serialize_xls(xls))

    # Carry-forward warnings appended to the resolution when the NIS cash
    # comes from a prior snapshot rather than a fresh Osh in window.
    carry_forward_warnings: list[str] = []

    if osh is None:
        # No NIS Osh in window. Rather than block a positions update on a cash
        # statement the user may not have, carry the prior snapshot's NIS cash
        # forward (clearly labelled). A later Osh upload re-resolves the pair
        # via the Osh-arrival hook. Brand-new users (no prior TSV) still queue.
        carried_nis, carried_from = _prior_leumi_nis_cash(snapshot_root)
        if carried_nis is None:
            part = _add_part_with_race_recovery(
                db, user_id=user_id, snapshot_date=xls.snapshot_date,
                portfolio_number=xls.portfolio_number, payload_json=payload_json,
                sha=sha, status="pending",
            )
            return PairResolution(
                status="pending_pair",
                pending_pair_id=part.id,
                resolved_tsv_path=None,
                snapshot_date=xls.snapshot_date,
                sha256=sha,
                detail=(
                    "XLS parsed and queued. No prior snapshot to carry NIS cash "
                    "from; upload a matching Leumi Osh statement via /expenses "
                    "to complete this month's snapshot."
                ),
                parse_warnings=xls.parse_warnings,
            )
        osh_closing_nis = carried_nis
        carry_forward_warnings.append(
            f"Leumi NIS cash carried forward (₪{carried_nis:,.0f}) from the "
            f"{carried_from.isoformat() if carried_from else 'prior'} snapshot — "
            f"no Leumi Osh (עו\"ש) statement within {MATCH_WINDOW_DAYS}d of "
            f"{xls.snapshot_date}. Upload a current Osh to refresh the NIS cash."
        )
    else:
        # Pair found -- assemble immediately.
        osh_closing_nis = _get_osh_closing_balance_nis(db, statement_id=osh.id)
    if osh is not None and osh_closing_nis is None:
        # Osh statement has no parsed transactions -> can't extract balance.
        # Treat as pending so the user can re-ingest the Osh.
        part = _add_part_with_race_recovery(
            db, user_id=user_id, snapshot_date=xls.snapshot_date,
            portfolio_number=xls.portfolio_number, payload_json=payload_json,
            sha=sha, status="pending",
        )
        return PairResolution(
            status="pending_pair",
            pending_pair_id=part.id,
            resolved_tsv_path=None,
            snapshot_date=xls.snapshot_date,
            sha256=sha,
            detail=(
                f"Matched Osh statement #{osh.id} but it has no parsed "
                f"transactions to extract closing balance from. Queued as pending."
            ),
            parse_warnings=xls.parse_warnings,
        )

    # Synthesize TSV in-memory first (no disk write yet), so we can
    # commit the DB row before persisting bytes. If the commit races
    # with another upload, we don't leave an orphan TSV on disk.
    # Codex zigzag (a)#6 (2026-05-29): filesystem-write-before-commit
    # could leave disk/DB divergence on commit failure.
    # Leumi USD cash (פמ"ח) — paired the same way as the Osh, so the
    # synthesized snapshot carries BOTH currencies (the USD account often
    # holds material balances, e.g. NVDA-sale proceeds awaiting deployment).
    usd_stmt = _find_matching_usd(db, user_id=user_id, snapshot_date=xls.snapshot_date)
    usd_closing = (
        _get_usd_closing_balance(db, statement_id=usd_stmt.id)
        if usd_stmt is not None else None
    )
    usd_warnings: list[str] = []
    if usd_stmt is None:
        usd_warnings.append(
            "No Leumi USD (פמ\"ח) statement within "
            f"{MATCH_WINDOW_DAYS}d of {xls.snapshot_date}; the snapshot has NO "
            "Leumi USD cash row — upload the Leumi USD statement via /expenses "
            "to capture the USD balance (it can hold material idle cash)."
        )
    elif usd_closing is None:
        usd_warnings.append(
            f"Leumi USD statement #{usd_stmt.id} found but its closing "
            "balance could not be extracted; Leumi USD cash row omitted."
        )

    tsv_text, synth_warnings = _synthesize_in_memory(
        xls=xls,
        osh_closing_nis=osh_closing_nis,
        snapshot_root=snapshot_root,
        usd_closing=usd_closing,
    )
    synth_warnings = synth_warnings + usd_warnings + carry_forward_warnings
    target_name = _canonical_tsv_filename(
        xls.snapshot_date,
    )
    target_path = snapshot_root / target_name

    part = _add_part_with_race_recovery(
        db, user_id=user_id, snapshot_date=xls.snapshot_date,
        portfolio_number=xls.portfolio_number, payload_json=payload_json,
        sha=sha, status="resolved",
        paired_osh_statement_id=(osh.id if osh is not None else None),
        paired_at=_utcnow(),
        resolved_tsv_path=str(target_path),
    )

    # DB commit succeeded -> safe to persist the bytes. If the disk
    # write fails AFTER the commit, log + raise; the user will see
    # detect_status=failed but the part row is durably resolved
    # (idempotent re-upload re-attempts).
    snapshot_root.mkdir(parents=True, exist_ok=True)
    target_path.write_text(tsv_text, encoding="utf-8")
    _log.info(
        "portfolio_snapshot.xls_synthesized",
        extra={"path": str(target_path)},
    )

    # Write the merged snapshot THROUGH to the DB store so GET /snapshot (which
    # is DB-first) reflects the paired month. commit=True: the part row was
    # already committed by _add_part_with_race_recovery, so this is a standalone
    # upload-time write.
    _write_through_resolved_snapshot(db, user_id=user_id, tsv_path=target_path,
                                     commit=True)

    # Reconciliation gate: diff the persisted snapshot against the RAW source
    # (XLS positions + both cash balances) and surface any mismatch LOUDLY.
    # Internal consistency is not correctness — this catches a dropped cash
    # currency or a symbol collision before it reaches the surfaces.
    from argosy.services.portfolio_ingest.reconcile import reconcile_leumi_against_xls
    try:
        persisted = parse_portfolio_tsv(target_path)
        discrepancies = reconcile_leumi_against_xls(
            snapshot_positions=persisted.positions,
            xls_positions=xls.positions,
            osh_closing_nis=osh_closing_nis,
            usd_closing=usd_closing,
            fx_usd_nis=persisted.fx_usd_nis or 3.7,
        )
        if discrepancies:
            _log.warning(
                "portfolio_snapshot.reconcile_discrepancies",
                extra={"count": len(discrepancies), "issues": discrepancies},
            )
            synth_warnings = synth_warnings + [
                "RECONCILIATION vs raw Leumi source: " + d for d in discrepancies
            ]
    except Exception as exc:  # noqa: BLE001 — never block ingest on the gate itself
        _log.warning("portfolio_snapshot.reconcile_failed", extra={"error": str(exc)})

    return PairResolution(
        status="resolved",
        pending_pair_id=part.id,
        resolved_tsv_path=target_path,
        snapshot_date=xls.snapshot_date,
        sha256=sha,
        detail=None,
        parse_warnings=xls.parse_warnings + synth_warnings,
    )


def _write_through_resolved_snapshot(
    db: Session, *, user_id: str, tsv_path: Path, commit: bool,
) -> None:
    """Persist the freshly-synthesized merged TSV into the DB snapshot store so
    GET /snapshot (DB-first) reflects the paired month immediately — instead of
    silently leaving the prior snapshot live (the write-through gap).

    Best-effort: the pair is already durable (file + part row), so a write-through
    failure must NEVER break the resolution — it degrades to "the next snapshot
    read picks it up on its own filesystem fallback." ``commit`` is False when the
    caller owns an atomic batch (the Osh-arrival hook runs mid-ingest)."""
    try:
        from argosy.ingest.tsv import parse_portfolio_tsv
        from argosy.services.portfolio_snapshot_store import (
            write_through_if_changed,
        )

        snap = parse_portfolio_tsv(tsv_path)
        write_through_if_changed(db, user_id=user_id, snapshot=snap, commit=commit)
    except Exception as exc:  # noqa: BLE001 — additive; never break the pair
        _log.warning(
            "portfolio_snapshot.pair_write_through_failed",
            extra={"path": str(tsv_path), "error": str(exc)},
        )


def try_resolve_pending_on_osh_arrival(
    *,
    db: Session,
    statement_id: int,
    snapshot_root: Path,
) -> PairResolution | None:
    """Called from the expense-ingest orchestrator after a Leumi-bank
    statement commits. If there's a matching pending XLS, resolve the pair.

    Returns the resolution if a pair was completed, None otherwise.
    Idempotent: calling twice for the same statement is safe -- the second
    call finds no pending row (the first marked it resolved).
    """
    osh = db.get(ExpenseStatement, statement_id)
    if osh is None:
        return None
    if osh.parser_name != _LEUMI_OSH_PARSER_NAME:
        # Discriminate Osh (NIS current account) from Leumi USD (פמ"ח)
        # -- both share issuer="leumi" + kind="bank". Codex zigzag
        # finding 2026-05-29 (a)#3.
        return None
    source = db.get(ExpenseSource, osh.source_id)
    if source is None or source.kind != "bank" or "leumi" not in (
        source.issuer or ""
    ).lower():
        return None

    # Find resolvable parts within the match window: either still pending, OR
    # already resolved via NIS-cash carry-forward (status='resolved' with a NULL
    # paired_osh_statement_id) — the latter so a real Osh arriving after a
    # portfolio-only upload REFRESHES the carried-forward NIS cash instead of
    # being ignored (which would leave stale cash live indefinitely).
    lo = osh.period_end - timedelta(days=MATCH_WINDOW_DAYS)
    hi = osh.period_end + timedelta(days=MATCH_WINDOW_DAYS)
    pending = (
        db.execute(
            select(PortfolioSnapshotPart)
            .where(
                PortfolioSnapshotPart.user_id == osh.user_id,
                or_(
                    PortfolioSnapshotPart.status == "pending",
                    and_(
                        PortfolioSnapshotPart.status == "resolved",
                        PortfolioSnapshotPart.paired_osh_statement_id.is_(None),
                    ),
                ),
                PortfolioSnapshotPart.snapshot_date >= lo,
                PortfolioSnapshotPart.snapshot_date <= hi,
            )
            .order_by(PortfolioSnapshotPart.created_at.desc())
        )
        .scalars()
        .all()
    )
    if not pending:
        return None

    # Pick the part with snapshot_date closest to the Osh period_end.
    # Ties: prefer the more recently created (deterministic).
    pending.sort(
        key=lambda p: (
            abs((p.snapshot_date - osh.period_end).days),
            -int(p.created_at.timestamp()) if p.created_at else 0,
        )
    )
    part = pending[0]

    osh_closing_nis = _get_osh_closing_balance_nis(db, statement_id=osh.id)
    if osh_closing_nis is None:
        return None

    xls = _deserialize_xls(part.payload_json)
    usd_stmt = _find_matching_usd(db, user_id=osh.user_id, snapshot_date=part.snapshot_date)
    usd_closing = (
        _get_usd_closing_balance(db, statement_id=usd_stmt.id)
        if usd_stmt is not None else None
    )
    usd_warnings: list[str] = []
    if usd_stmt is None:
        usd_warnings.append(
            "No Leumi USD (פמ\"ח) statement within "
            f"{MATCH_WINDOW_DAYS}d of {part.snapshot_date}; the snapshot has NO "
            "Leumi USD cash row — upload the Leumi USD statement to capture it."
        )
    elif usd_closing is None:
        usd_warnings.append(
            f"Leumi USD statement #{usd_stmt.id} found but its closing balance "
            "could not be extracted; Leumi USD cash row omitted."
        )
    tsv_text, synth_warnings = _synthesize_in_memory(
        xls=xls,
        osh_closing_nis=osh_closing_nis,
        snapshot_root=snapshot_root,
        usd_closing=usd_closing,
    )
    synth_warnings = synth_warnings + usd_warnings
    target_name = _canonical_tsv_filename(part.snapshot_date)
    target_path = snapshot_root / target_name

    part.status = "resolved"
    part.paired_osh_statement_id = osh.id
    part.paired_at = _utcnow()
    part.resolved_tsv_path = str(target_path)
    # Hook is invoked mid-pipeline by the expense-ingest orchestrator;
    # the caller (route) owns the transaction boundary. Codex zigzag
    # (a)#5 (2026-05-29) flagged that an internal commit here would
    # split the ingest's atomic batch. Flush so the orchestrator can
    # commit (or rollback) the whole pipeline as one unit.
    db.flush()

    # File write AFTER the flush (so the caller's commit covers both).
    snapshot_root.mkdir(parents=True, exist_ok=True)
    target_path.write_text(tsv_text, encoding="utf-8")
    _log.info(
        "portfolio_snapshot.osh_arrival_resolved_pair",
        extra={"path": str(target_path), "osh_id": osh.id},
    )

    # Write-through so /portfolio reflects the paired month. commit=False: this
    # hook runs mid-ingest and the caller (orchestrator/route) owns the commit,
    # so the snapshot row lands atomically with the part resolution.
    _write_through_resolved_snapshot(db, user_id=osh.user_id,
                                     tsv_path=target_path, commit=False)

    return PairResolution(
        status="resolved",
        pending_pair_id=part.id,
        resolved_tsv_path=target_path,
        snapshot_date=part.snapshot_date,
        sha256=part.sha256,
        detail=f"Resolved by Osh statement #{osh.id} arriving after XLS.",
        parse_warnings=synth_warnings,
    )


# ---------------------------------------------------------------------------
# Helpers: matching + balance extraction
# ---------------------------------------------------------------------------


# Parser name on ExpenseStatement that uniquely identifies a Leumi Osh
# (NIS current account) statement. Codex zigzag review 2026-05-29 (a)#3
# flagged that ExpenseSource.issuer == "leumi" + kind == "bank" also
# matches Leumi USD (פמ"ח) statements, which would feed the wrong cash
# balance into TSV synthesis (NIS interpretation of a USD-denominated
# running balance is off by ~3.7x). The parser_name discriminator
# pins the match to leumi_osh specifically.
_LEUMI_OSH_PARSER_NAME = "leumi_osh"


def _find_matching_osh(
    db: Session, *, user_id: str, snapshot_date: date,
) -> ExpenseStatement | None:
    """Return the Leumi Osh statement whose period_end is closest to
    snapshot_date within MATCH_WINDOW_DAYS. Picks the closest period_end;
    ties broken by higher statement id (newer). Discriminates Leumi Osh
    from Leumi USD via ExpenseStatement.parser_name == "leumi_osh"
    (codex zigzag findings #6 + 2026-05-29-impl review #3).
    """
    lo = snapshot_date - timedelta(days=MATCH_WINDOW_DAYS)
    hi = snapshot_date + timedelta(days=MATCH_WINDOW_DAYS)
    candidates = (
        db.execute(
            select(ExpenseStatement)
            .join(ExpenseSource, ExpenseSource.id == ExpenseStatement.source_id)
            .where(
                ExpenseStatement.user_id == user_id,
                ExpenseStatement.period_end >= lo,
                ExpenseStatement.period_end <= hi,
                ExpenseStatement.parser_name == _LEUMI_OSH_PARSER_NAME,
                ExpenseSource.kind == "bank",
                ExpenseSource.issuer == "leumi",
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return None
    candidates.sort(
        key=lambda s: (
            abs((s.period_end - snapshot_date).days),
            -s.id,
        )
    )
    return candidates[0]


def _get_osh_closing_balance_nis(
    db: Session, *, statement_id: int,
) -> float | None:
    """Return the closing running-balance (NIS) for an Osh statement.

    Defined as the balance after the chronologically last transaction.
    Same-day ties broken by higher transaction id. Codex zigzag #11
    (2026-05-29) flagged that a naive txn_date sort is ambiguous when
    multiple transactions share a date.
    """
    last_txn = (
        db.execute(
            select(ExpenseTransaction)
            .where(ExpenseTransaction.statement_id == statement_id)
            .order_by(
                desc(ExpenseTransaction.occurred_on),
                desc(ExpenseTransaction.id),
            )
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if last_txn is None:
        return None
    raw_json = last_txn.raw_row_json or "{}"
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else (raw_json or {})
    except json.JSONDecodeError:
        return None
    bal = raw.get("balance") if isinstance(raw, dict) else None
    if bal is None:
        return None
    try:
        return float(str(bal).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


_LEUMI_USD_PARSER_NAME = "leumi_usd"


def _find_matching_usd(
    db: Session, *, user_id: str, snapshot_date: date,
) -> ExpenseStatement | None:
    """Return the Leumi USD (פמ"ח) statement whose period_end is closest to
    snapshot_date within MATCH_WINDOW_DAYS — the analogue of
    `_find_matching_osh` for the USD current account, so the synthesized
    snapshot carries the Leumi USD cash balance (not just NIS)."""
    lo = snapshot_date - timedelta(days=MATCH_WINDOW_DAYS)
    hi = snapshot_date + timedelta(days=MATCH_WINDOW_DAYS)
    candidates = (
        db.execute(
            select(ExpenseStatement)
            .join(ExpenseSource, ExpenseSource.id == ExpenseStatement.source_id)
            .where(
                ExpenseStatement.user_id == user_id,
                ExpenseStatement.period_end >= lo,
                ExpenseStatement.period_end <= hi,
                ExpenseStatement.parser_name == _LEUMI_USD_PARSER_NAME,
                ExpenseSource.kind == "bank",
                ExpenseSource.issuer == "leumi",
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda s: (abs((s.period_end - snapshot_date).days), -s.id),
    )


def _get_usd_closing_balance(db: Session, *, statement_id: int) -> float | None:
    """Closing running balance (USD) for a Leumi USD statement — the balance
    after the chronologically last transaction (same-day ties → higher id)."""
    last_txn = (
        db.execute(
            select(ExpenseTransaction)
            .where(ExpenseTransaction.statement_id == statement_id)
            .order_by(
                desc(ExpenseTransaction.occurred_on),
                desc(ExpenseTransaction.id),
            )
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if last_txn is None:
        return None
    raw_json = last_txn.raw_row_json or "{}"
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else (raw_json or {})
    except json.JSONDecodeError:
        return None
    bal = raw.get("balance_usd") if isinstance(raw, dict) else None
    if bal is None:
        return None
    try:
        return float(str(bal).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _display_symbol_from_name(name_he: str) -> str | None:
    """Derive a readable symbol for a TASE-listed tracker with no Latin
    ticker. The Leumi name is '<issuer> מחקה <index>'
    (e.g. 'אי בי אי מחקה STOXX Europe 600'); the index portion after the
    tracking verb 'מחקה' is the meaningful label ('STOXX Europe 600').
    Returns None when there's no such portion (caller falls back to the
    security_id) — this is what stops a no-ticker holding from inheriting a
    stale/wrong symbol cell (e.g. the pasted 'O')."""
    if not name_he:
        return None
    marker = "מחקה"
    if marker in name_he:
        tail = name_he.split(marker)[-1].strip()
        if tail:
            return tail
    return None


def _stale_old_pending(
    db: Session, *, user_id: str, fresh_snapshot_date: date,
) -> None:
    """Mark pending parts more than STALE_WINDOW_DAYS older than the fresh
    snapshot as 'stale'. Idempotent -- only touches 'pending' rows.
    """
    cutoff = fresh_snapshot_date - timedelta(days=STALE_WINDOW_DAYS)
    stale_rows = (
        db.execute(
            select(PortfolioSnapshotPart).where(
                PortfolioSnapshotPart.user_id == user_id,
                PortfolioSnapshotPart.status == "pending",
                PortfolioSnapshotPart.snapshot_date < cutoff,
            )
        )
        .scalars()
        .all()
    )
    for row in stale_rows:
        row.status = "stale"


# ---------------------------------------------------------------------------
# TSV synthesis
# ---------------------------------------------------------------------------


def _synthesize_in_memory(
    *,
    xls: LeumiPortfolioSnapshot,
    osh_closing_nis: float,
    snapshot_root: Path,
    usd_closing: float | None = None,
) -> tuple[str, list[str]]:
    """Synthesize the new TSV content as a string (no disk write yet).

    Codex zigzag (a)#6 (2026-05-29) split the persist step out of
    synthesis so the route can commit the DB row BEFORE writing the
    TSV to disk -- this avoids disk/DB divergence when a DB commit
    fails after an on-disk write.

    Codex zigzag (a)#9 (2026-05-29) flagged that the old path raised
    RuntimeError when no prior TSV existed, bricking the brand-new
    user's first upload. The graceful path is the
    ``_full_rewrite_from_snapshot`` fallback with an empty prior
    snapshot.
    """
    warnings: list[str] = []
    prior_tsv = _find_most_recent_prior_tsv(snapshot_root)
    if prior_tsv is None:
        warnings.append(
            "No prior 'Family Finances Status' TSV found at the scan "
            "root. Synthesizing a minimal TSV from XLS positions + "
            "Osh cash only (no Schwab / Aborad / NVDA-sales / pensions "
            "carry-forward). Drop a prior month's TSV into the scan "
            "root for richer carry-forward."
        )
        # Build an empty prior_snapshot so _full_rewrite_from_snapshot's
        # graceful path produces just the positions + cash block.
        empty_prior = PortfolioSnapshot(source_path="(no-prior-tsv)")
        symbol_map, currency_map, type_map = _build_prior_mappings(empty_prior, xls)
        tsv_text = _full_rewrite_from_snapshot(
            prior_snapshot=empty_prior,
            xls=xls,
            osh_closing_nis=osh_closing_nis,
            fx_usd_nis=3.7,
            fx_usd_eur=1.05,
            symbol_map=symbol_map,
            currency_map=currency_map,
            type_map=type_map,
            usd_closing=usd_closing,
        )
        return tsv_text, warnings

    prior_snapshot = parse_portfolio_tsv(prior_tsv)
    tsv_text, splice_warnings = _splice_xls_into_tsv(
        prior_tsv_path=prior_tsv,
        prior_snapshot=prior_snapshot,
        xls=xls,
        osh_closing_nis=osh_closing_nis,
        usd_closing=usd_closing,
    )
    return tsv_text, warnings + splice_warnings


def _add_part_with_race_recovery(
    db: Session,
    *,
    user_id: str,
    snapshot_date: date,
    portfolio_number: str | None,
    payload_json: str,
    sha: str,
    status: str,
    paired_osh_statement_id: int | None = None,
    paired_at=None,
    resolved_tsv_path: str | None = None,
) -> PortfolioSnapshotPart:
    """Insert a portfolio_snapshot_parts row, recovering gracefully when
    a concurrent upload wins the uniqueness race.

    Codex zigzag (a)#7 (2026-05-29) flagged that two concurrent uploads
    with the same SHA (or the same semantic key) could both pass the
    pre-insert lookup and race into IntegrityError on commit. We catch
    the IntegrityError, rollback, re-query, and return the winner's row.
    """
    part = PortfolioSnapshotPart(
        user_id=user_id,
        kind="xls_positions",
        snapshot_date=snapshot_date,
        portfolio_number=portfolio_number,
        payload_json=payload_json,
        sha256=sha,
        status=status,
        paired_osh_statement_id=paired_osh_statement_id,
        paired_at=paired_at,
        resolved_tsv_path=resolved_tsv_path,
    )
    db.add(part)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Another tx won the race; return its row.
        existing = (
            db.execute(
                select(PortfolioSnapshotPart).where(
                    PortfolioSnapshotPart.user_id == user_id,
                    PortfolioSnapshotPart.sha256 == sha,
                )
            )
            .scalar_one_or_none()
        )
        if existing is None and portfolio_number is not None:
            existing = (
                db.execute(
                    select(PortfolioSnapshotPart).where(
                        PortfolioSnapshotPart.user_id == user_id,
                        PortfolioSnapshotPart.snapshot_date == snapshot_date,
                        PortfolioSnapshotPart.portfolio_number == portfolio_number,
                    )
                )
                .scalar_one_or_none()
            )
        if existing is None:
            # Shouldn't happen but re-raise so the caller sees the real error.
            raise
        return existing
    return part


def _splice_xls_into_tsv(
    *,
    prior_tsv_path: Path,
    prior_snapshot: PortfolioSnapshot,
    xls: LeumiPortfolioSnapshot,
    osh_closing_nis: float,
    usd_closing: float | None = None,
) -> tuple[str, list[str]]:
    """Produce the new TSV by replacing the prior TSV's Leumi rows with
    XLS-derived rows (positions + cash from Osh) and recomputing the
    Current-allocation block's current_pct/current_k.

    Carries forward verbatim:
      * Schwab + Aborad position rows
      * Real estate block
      * NVDA Sales History block
      * Pensions block
      * Target allocations (target_pct, target_k_usd)
    """
    warnings: list[str] = []

    # FX: use prior TSV's rate (snapshot-effective -- codex zigzag #5).
    fx_usd_nis = prior_snapshot.fx_usd_nis or 3.7  # defensive fallback
    fx_usd_eur = prior_snapshot.fx_usd_eur or 1.05
    if prior_snapshot.fx_usd_nis is None:
        warnings.append(
            "Prior TSV has no 'USD to NIS:' rate; defaulted to 3.7. "
            "Cash USD-equivalent may be imprecise."
        )

    # Build security_id -> prior_symbol / currency / asset_type maps so
    # the windfall detector keeps matching positions across months (codex
    # zigzag #1) and the Type-aggregation in the allocation block stays
    # consistent with the user's prior categorization (codex zigzag (a)#4).
    symbol_map, currency_map, type_map = _build_prior_mappings(prior_snapshot, xls)

    # Read the prior TSV verbatim so we can preserve all non-Leumi-position
    # rows + section structure + comments.
    prior_lines = prior_tsv_path.read_text(
        encoding="utf-8-sig", errors="ignore"
    ).splitlines()

    # Identify the position-table row span by scanning for the header marker
    # and the first section terminator.
    pos_start_idx, pos_end_idx = _locate_position_block(prior_lines)
    if pos_start_idx is None or pos_end_idx is None:
        warnings.append(
            "Could not locate the Bank account / funds allocation block in "
            "the prior TSV; XLS splice fell back to a full rewrite."
        )
        return _full_rewrite_from_snapshot(
            prior_snapshot=prior_snapshot,
            xls=xls,
            osh_closing_nis=osh_closing_nis,
            fx_usd_nis=fx_usd_nis,
            fx_usd_eur=fx_usd_eur,
            symbol_map=symbol_map,
            currency_map=currency_map,
            type_map=type_map,
            usd_closing=usd_closing,
        ), warnings

    # Split the prior position-block lines:
    #   * Leumi rows (drop -- to be replaced with XLS-derived)
    #   * Non-Leumi rows (Schwab, Aborad, Sum, totals -- keep)
    pos_block_lines = prior_lines[pos_start_idx:pos_end_idx]
    non_leumi_position_lines = [
        ln for ln in pos_block_lines if not _is_leumi_position_line(ln)
    ]

    # Build new Leumi rows from XLS + cash row from Osh.
    new_leumi_lines = _xls_to_tsv_rows(
        xls=xls,
        osh_closing_nis=osh_closing_nis,
        fx_usd_nis=fx_usd_nis,
        symbol_map=symbol_map,
        currency_map=currency_map,
        type_map=type_map,
        usd_closing=usd_closing,
    )

    # Reassemble: prior header rows + non-Leumi position lines (header + Schwab + Sum)
    # + new Leumi lines inserted before the Sum row (if any), or appended.
    # Find the Sum row inside non_leumi_position_lines to insert just above.
    sum_idx_in_nonleumi = None
    for i, ln in enumerate(non_leumi_position_lines):
        first_cell = ln.split("\t", 1)[0].strip()
        if (
            len(ln.split("\t")) > 1
            and "sum" in ln.split("\t")[1].strip().lower()
        ):
            sum_idx_in_nonleumi = i
            break
    if sum_idx_in_nonleumi is None:
        spliced_position_lines = non_leumi_position_lines + new_leumi_lines
    else:
        spliced_position_lines = (
            non_leumi_position_lines[:sum_idx_in_nonleumi]
            + new_leumi_lines
            + non_leumi_position_lines[sum_idx_in_nonleumi:]
        )

    # Recompute the Current-allocation block: current_pct + current_k_usd
    # from new totals, target_* verbatim from prior.
    new_total_usd_k = _compute_total_usd_k(
        non_leumi_lines=non_leumi_position_lines,
        new_leumi_lines=new_leumi_lines,
    )
    recomputed_allocation_lines = _recompute_allocation_block(
        prior_lines=prior_lines,
        prior_allocations=prior_snapshot.allocations,
        new_total_usd_k=new_total_usd_k,
        new_leumi_lines=new_leumi_lines,
        non_leumi_position_lines=non_leumi_position_lines,
    )

    # Header rows: update snapshot_date in row 1 col B, FX in rows 2-3.
    header_lines = list(prior_lines[:pos_start_idx])
    header_lines = _update_header_rows(
        header_lines,
        snapshot_date=xls.snapshot_date,
        fx_usd_nis=fx_usd_nis,
        fx_usd_eur=fx_usd_eur,
    )

    # Tail: everything after the position-block end, but with the allocation
    # block replaced.
    tail_lines = list(prior_lines[pos_end_idx:])
    tail_lines = _replace_allocation_block(tail_lines, recomputed_allocation_lines)

    return "\n".join(header_lines + spliced_position_lines + tail_lines) + "\n", warnings


# ---------------------------------------------------------------------------
# Splice helpers
# ---------------------------------------------------------------------------


_POSITION_HEADER_MARKER = "Bank account / funds allocation"
_SECTION_TERMINATORS = (
    "real estate details",
    "current allocation",
    "nvda sales history",
    "pensions",
)


def _locate_position_block(
    prior_lines: list[str],
) -> tuple[int | None, int | None]:
    """Return (start_idx, end_idx) where prior_lines[start:end] covers the
    position block (header row + column headers + rows + Sum row), exclusive
    of the next section header."""
    start = None
    for i, ln in enumerate(prior_lines):
        if _POSITION_HEADER_MARKER in ln:
            start = i
            break
    if start is None:
        return None, None
    end = None
    for j in range(start + 1, len(prior_lines)):
        joined_lower = prior_lines[j].lower()
        if any(t in joined_lower for t in _SECTION_TERMINATORS):
            end = j
            break
    if end is None:
        end = len(prior_lines)
    return start, end


_LEUMI_LOCATION_RE = re.compile(r"^\s*[^\t]*\t\s*leumi", re.IGNORECASE)


def _is_leumi_position_line(line: str) -> bool:
    """A position line whose `Location` cell (col 1, tab-separated) starts
    with 'Leumi'. The header row + Sum row + section headers do not match."""
    cells = line.split("\t")
    if len(cells) < 2:
        return False
    loc = cells[1].strip()
    if not loc:
        return False
    # Don't capture section headers / sum rows.
    if "leumi" not in loc.lower():
        return False
    return True


def _build_prior_mappings(
    prior: PortfolioSnapshot, xls: LeumiPortfolioSnapshot,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Build (security_id -> symbol), (security_id -> currency), and
    (security_id -> asset_type) maps from the prior TSV's Leumi rows so
    the new XLS rows reuse the user's existing convention.

    Match strategy:
      1. XLS ticker exactly equals prior TSV symbol -> map.
      2. XLS name_he contains prior TSV symbol as a Latin substring -> map.
      3. No match -> default to (ticker or security_id, USD, "Equity").

    Codex zigzag finding (a)#4 (2026-05-29): hardcoding asset_type="Equity"
    for every XLS-derived row silently collapses prior Type distinctions
    (Dividend/Treasuries/Growth/etc.) once the XLS-driven pipeline takes
    over from the hand-maintained TSV. Asset_type carry-forward fixes the
    Type-aggregation drift downstream of _recompute_allocation_block.
    """
    prior_leumi = [
        p for p in prior.positions
        if (p.location or "").lower().startswith("leumi")
        and (p.asset_type or "").lower() != "cash"
    ]
    sym_map: dict[str, str] = {}
    curr_map: dict[str, str] = {}
    type_map: dict[str, str] = {}
    for xp in xls.positions:
        matched = None
        # Strategy 1: exact ticker match.
        if xp.ticker:
            for pp in prior_leumi:
                if pp.symbol.strip() == xp.ticker.strip():
                    matched = pp
                    break
        # Strategy 2: name substring match. Require the prior symbol to be at
        # least 2 chars — a 1-char symbol like "O" (Realty Income) spuriously
        # substring-matches almost any name and would re-stamp it onto a
        # no-ticker tracker (the STOXX-as-"O" bug). Codex review.
        if matched is None:
            for pp in prior_leumi:
                ps = (pp.symbol or "").strip()
                if len(ps) >= 2 and ps in (xp.name_he or ""):
                    matched = pp
                    break
        if matched is not None:
            sym_map[xp.security_id] = matched.symbol
            curr_map[xp.security_id] = matched.currency or "USD"
            type_map[xp.security_id] = matched.asset_type or "Equity"
        else:
            # No prior match: prefer the XLS Latin ticker, else a name-derived
            # label for TASE trackers (so STOXX Europe 600 doesn't fall back to
            # a bare security_id or inherit a wrong cell), else the security_id.
            sym_map[xp.security_id] = (
                xp.ticker or _display_symbol_from_name(xp.name_he) or xp.security_id
            )
            curr_map[xp.security_id] = "USD"  # default
            type_map[xp.security_id] = "Equity"  # default for new positions
    return sym_map, curr_map, type_map


def _leumi_usd_cash_row(usd_closing: float) -> str:
    """A Leumi USD cash row. Local value == USD value (no FX); (K) USD is /1000."""
    return "\t".join([
        "", "Leumi", "USD", "Cash", "", "", "", "", "",
        f"{usd_closing:.2f}", f"{usd_closing / 1000.0:.2f}", "", "",
    ])


def _xls_to_tsv_rows(
    *,
    xls: LeumiPortfolioSnapshot,
    osh_closing_nis: float,
    fx_usd_nis: float,
    symbol_map: dict[str, str],
    currency_map: dict[str, str],
    type_map: dict[str, str],
    usd_closing: float | None = None,
) -> list[str]:
    """Synthesize TSV rows for the new Leumi block: NIS cash row, optional
    USD cash row, then one row per XLS position.
    """
    out: list[str] = []
    # Cash row (NIS, from the Leumi Osh statement).
    cash_usd_k = (osh_closing_nis / max(fx_usd_nis, 0.01)) / 1000.0
    cash_cells = [
        "",                                                  # 0 Review
        "Leumi",                                             # 1 Location
        "NIS",                                               # 2 Currency
        "Cash",                                              # 3 Type
        "",                                                  # 4 Details
        "",                                                  # 5 Symbol
        "",                                                  # 6 Shares
        "",                                                  # 7 Current price
        "",                                                  # 8 Avg price
        f"{osh_closing_nis:.2f}",                            # 9 Current Value (local)
        f"{cash_usd_k:.2f}",                                 # 10 (K) USD Value
        "",                                                  # 11 % Change
        "",                                                  # 12 % Yearly
    ]
    out.append("\t".join(cash_cells))
    # USD cash row (from the Leumi USD statement) — without this the large
    # Leumi USD balance (e.g. NVDA-sale proceeds) is silently dropped.
    if usd_closing is not None:
        out.append(_leumi_usd_cash_row(usd_closing))

    for p in xls.positions:
        symbol = symbol_map.get(p.security_id, p.ticker or p.security_id)
        currency = currency_map.get(p.security_id, "USD")
        asset_type = type_map.get(p.security_id, "Equity")
        # The XLS holding value is USD until mid-2026, NIS thereafter; convert
        # to USD at the snapshot FX so the TSV's (K) USD column is always USD
        # regardless of which currency Leumi exported. `quantity` is the
        # authoritative input — value is derived from the broker's own number.
        usd_value = p.usd_value(fx_usd_nis)
        usd_k = usd_value / 1000.0
        if currency == "NIS":
            value_local = usd_value * fx_usd_nis
        else:
            value_local = usd_value
        cells = [
            "",                                              # 0 Review
            "Leumi",                                         # 1 Location
            currency,                                        # 2 Currency
            asset_type,                                      # 3 Type (carried from prior)
            p.name_he or "",                                 # 4 Details
            symbol,                                          # 5 Symbol
            f"{p.quantity:g}",                               # 6 Shares
            f"{p.last_price:g}",                             # 7 Current price
            f"{p.avg_buy_price:g}" if p.avg_buy_price else "",  # 8 Avg price
            f"{value_local:.2f}",                            # 9 Current Value (local)
            f"{usd_k:.2f}",                                  # 10 (K) USD Value
            f"{p.gain_pct:.4f}" if p.gain_pct is not None else "",  # 11 % Change
            "",                                              # 12 % Yearly (not in XLS)
        ]
        out.append("\t".join(cells))
    return out


def _compute_total_usd_k(
    *, non_leumi_lines: list[str], new_leumi_lines: list[str],
) -> float:
    """Sum the (K) USD Value column (idx 10) across all kept + new rows.
    Skips the position-table header + Sum row + empty rows."""
    total = 0.0
    for ln in non_leumi_lines + new_leumi_lines:
        cells = ln.split("\t")
        if len(cells) <= 10:
            continue
        loc = cells[1].strip() if len(cells) > 1 else ""
        if not loc or "sum" in loc.lower():
            continue
        try:
            v = float(cells[10].replace(",", "").strip())
        except (ValueError, AttributeError):
            continue
        total += v
    return total


def _recompute_allocation_block(
    *,
    prior_lines: list[str],
    prior_allocations: list[AllocationRow],
    new_total_usd_k: float,
    new_leumi_lines: list[str],
    non_leumi_position_lines: list[str],
) -> list[str]:
    """Return the new 'Current allocation:' block as a list of TSV lines.

    target_pct + target_k_usd are carried forward verbatim from prior.
    current_pct + current_k_usd are recomputed by aggregating
    new + carried-over position rows by asset_class.
    """
    # Aggregate USD-K by asset_type across all position rows.
    by_type: dict[str, float] = {}
    for ln in non_leumi_position_lines + new_leumi_lines:
        cells = ln.split("\t")
        if len(cells) <= 10:
            continue
        loc = cells[1].strip() if len(cells) > 1 else ""
        asset_type = cells[3].strip() if len(cells) > 3 else ""
        if not loc or "sum" in loc.lower() or not asset_type:
            continue
        try:
            v = float(cells[10].replace(",", "").strip())
        except (ValueError, AttributeError):
            continue
        by_type[asset_type] = by_type.get(asset_type, 0.0) + v

    # Find the original allocation block in prior_lines to preserve any
    # surrounding rows (the section header itself, etc.).
    alloc_start = None
    alloc_end = None
    for i, ln in enumerate(prior_lines):
        if "current allocation" in ln.lower():
            alloc_start = i
            break
    if alloc_start is None:
        return []
    for j in range(alloc_start + 1, len(prior_lines)):
        joined_lower = prior_lines[j].lower()
        if any(
            t in joined_lower
            for t in ("nvda sales history", "pensions", "saving accounts")
        ):
            alloc_end = j
            break
    if alloc_end is None:
        alloc_end = len(prior_lines)

    # Pre-index prior allocations by category for O(1) lookup.
    prior_by_cat = {a.category: a for a in prior_allocations}
    grand_total_new = sum(by_type.values())

    out: list[str] = []
    out.append(prior_lines[alloc_start])  # section header verbatim
    for ln in prior_lines[alloc_start + 1:alloc_end]:
        cells = ln.split("\t")
        # Fully-empty row: keep verbatim (separators between sections).
        if not any((c or "").strip() for c in cells):
            out.append(ln)
            continue
        # Allocation rows start with a leading tab -> cells[0] is empty;
        # category lives at cells[1] (verified against
        # argosy.ingest.tsv._parse_allocation_row's index layout).
        # Codex zigzag (2026-05-29) flagged that the pre-fix code read
        # cells[0] and the empty-cell guard was inverted, so the whole
        # block silently fell through verbatim.
        if len(cells) <= 1:
            out.append(ln)
            continue
        category = cells[1].strip()
        # Column-header row: skip verbatim.
        if category.lower() in {"category", "type"} or not category:
            out.append(ln)
            continue
        # Grand-Total row: recompute aggregates against the new total.
        if "total" in category.lower():
            new_cells = list(cells)
            while len(new_cells) < 7:
                new_cells.append("")
            new_cells[2] = "100.00%"
            new_cells[3] = f"{grand_total_new:.2f}"
            # cells[4] target_pct + cells[5] target_k preserved
            try:
                target_k = float((cells[5] or "").replace(",", "").strip())
                new_cells[6] = f"{target_k - grand_total_new:.2f}"
            except (ValueError, AttributeError):
                pass
            out.append("\t".join(new_cells))
            continue
        # Data row: recompute current_pct + current_k from by_type
        # aggregate; preserve target_pct + target_k verbatim from prior.
        prior_row = prior_by_cat.get(category)
        if prior_row is None:
            out.append(ln)
            continue
        new_current_k = by_type.get(category, prior_row.usd_value_k or 0.0)
        new_current_pct = (
            (new_current_k / new_total_usd_k * 100.0)
            if new_total_usd_k > 0 else 0.0
        )
        new_delta_k = (
            (prior_row.target_k or 0.0) - new_current_k
            if prior_row.target_k is not None else None
        )
        new_cells = list(cells)
        while len(new_cells) < 7:
            new_cells.append("")
        # cells[0] (leading tab placeholder) + cells[1] (category)
        # preserved verbatim. Recompute cells[2..3] + cells[6].
        new_cells[2] = f"{new_current_pct:.2f}%"
        new_cells[3] = f"{new_current_k:.2f}"
        # cells[4] target_pct + cells[5] target_k preserved.
        if new_delta_k is not None:
            new_cells[6] = f"{new_delta_k:.2f}"
        out.append("\t".join(new_cells))
    return out


def _replace_allocation_block(
    tail_lines: list[str], new_allocation_lines: list[str],
) -> list[str]:
    """Substitute the Current-allocation block within tail_lines with
    new_allocation_lines. Preserves everything before + after the block."""
    alloc_start = None
    alloc_end = None
    for i, ln in enumerate(tail_lines):
        if "current allocation" in ln.lower():
            alloc_start = i
            break
    if alloc_start is None:
        # No prior allocation block; just append.
        return tail_lines + [""] + new_allocation_lines
    for j in range(alloc_start + 1, len(tail_lines)):
        joined_lower = tail_lines[j].lower()
        if any(
            t in joined_lower
            for t in ("nvda sales history", "pensions", "saving accounts")
        ):
            alloc_end = j
            break
    if alloc_end is None:
        alloc_end = len(tail_lines)
    return tail_lines[:alloc_start] + new_allocation_lines + tail_lines[alloc_end:]


def _update_header_rows(
    header_lines: list[str],
    *,
    snapshot_date: date | None,
    fx_usd_nis: float,
    fx_usd_eur: float,
) -> list[str]:
    """Update row 1 col B with the new date; rows 2-3 with the FX rates."""
    out = list(header_lines)
    if snapshot_date is not None and out:
        cells = out[0].split("\t")
        while len(cells) < 2:
            cells.append("")
        cells[1] = snapshot_date.strftime("%d-%b-%y")
        out[0] = "\t".join(cells)
    # Walk for USD to NIS / USD to EUR labels and update the value cell.
    for i in range(1, min(len(out), 6)):
        cells = out[i].split("\t")
        if len(cells) < 3:
            continue
        label = (cells[1] or "").strip().lower()
        if "usd to nis" in label:
            cells[2] = f"{fx_usd_nis:.5f}"
            out[i] = "\t".join(cells)
        elif "usd to eur" in label:
            cells[2] = f"{fx_usd_eur:.5f}"
            out[i] = "\t".join(cells)
    return out


def _full_rewrite_from_snapshot(
    *,
    prior_snapshot: PortfolioSnapshot,
    xls: LeumiPortfolioSnapshot,
    osh_closing_nis: float,
    fx_usd_nis: float,
    fx_usd_eur: float,
    symbol_map: dict[str, str],
    currency_map: dict[str, str],
    type_map: dict[str, str],
    usd_closing: float | None = None,
) -> str:
    """Fallback path when prior TSV layout can't be located by markers.
    Produces a minimal but valid TSV from the parsed prior snapshot +
    the new XLS. Used only when _locate_position_block fails."""
    rows: list[list[str]] = []
    # Row 1: date.
    date_str = (xls.snapshot_date or prior_snapshot.snapshot_date)
    rows.append(["", date_str.strftime("%d-%b-%y") if date_str else "", "", ""])
    rows.append(["", "USD to NIS:", f"{fx_usd_nis:.5f}"])
    rows.append(["", "USD to EUR:", f"{fx_usd_eur:.5f}"])
    rows.append([])
    rows.append(["Bank account / funds allocation"])
    rows.append([
        "Review Status", "Location", "Currency", "Type", "Details", "Symbol",
        "# Shares", "Current price", "Avg Price", "Current Value",
        "(K) USD Value", "% Change", "% Yearly",
    ])
    # Non-Leumi positions verbatim from prior_snapshot.
    for p in prior_snapshot.positions:
        if (p.location or "").lower().startswith("leumi"):
            continue
        rows.append(_position_to_cells(p))
    # New Leumi cash + position rows.
    cash_usd_k = (osh_closing_nis / max(fx_usd_nis, 0.01)) / 1000.0
    rows.append([
        "", "Leumi", "NIS", "Cash", "", "", "", "", "",
        f"{osh_closing_nis:.2f}", f"{cash_usd_k:.2f}", "", "",
    ])
    usd_cash_k = (usd_closing / 1000.0) if usd_closing is not None else 0.0
    if usd_closing is not None:
        rows.append(_leumi_usd_cash_row(usd_closing).split("\t"))
    for p in xls.positions:
        symbol = symbol_map.get(p.security_id, p.ticker or p.security_id)
        currency = currency_map.get(p.security_id, "USD")
        asset_type = type_map.get(p.security_id, "Equity")
        # Convert the XLS holding value (USD pre-2026 / NIS after) to USD at
        # the snapshot FX — mirror of the main splice path.
        usd_value = p.usd_value(fx_usd_nis)
        usd_k = usd_value / 1000.0
        # Mirror main splice path's currency-aware local-value (codex
        # zigzag (a) impl review #I8: previously hard-coded USD).
        if currency == "NIS":
            value_local = usd_value * fx_usd_nis
        else:
            value_local = usd_value
        rows.append([
            "", "Leumi", currency, asset_type, p.name_he or "", symbol,
            f"{p.quantity:g}", f"{p.last_price:g}",
            f"{p.avg_buy_price:g}" if p.avg_buy_price else "",
            f"{value_local:.2f}", f"{usd_k:.2f}",
            f"{p.gain_pct:.4f}" if p.gain_pct is not None else "", "",
        ])
    # Real estate -- verbatim from prior_snapshot.
    if prior_snapshot.real_estate:
        rows.append([])
        rows.append(["Real estate details:"])
        for re_row in prior_snapshot.real_estate:
            # value_local is parsed from c7 (idx 7); write it back to c7/c8/c9
            # (the source has all three equal) so a regenerate→reparse round
            # trip preserves it. Writing only c9 would be lost on reparse.
            v = f"{re_row.value_local:.2f}" if re_row.value_local else ""
            rows.append([
                "", re_row.location, re_row.currency, re_row.role, "", "", "",
                v, v, v, "", "", "",
            ])
    # Current allocation -- carry forward, but recompute current.
    if prior_snapshot.allocations:
        rows.append([])
        rows.append(["Current allocation:"])
        rows.append([
            "Category", "Current %", "Current K USD", "Target %", "Target K USD", "Delta K",
        ])
        new_total = sum(
            (p.usd_value(fx_usd_nis) / 1000.0) for p in xls.positions
        ) + cash_usd_k + usd_cash_k + sum(
            (p.usd_value_k or 0.0) for p in prior_snapshot.positions
            if not (p.location or "").lower().startswith("leumi")
        )
        for a in prior_snapshot.allocations:
            tgt_k = a.target_k or 0.0
            cur_k = a.usd_value_k or 0.0
            cur_pct = (cur_k / new_total * 100.0) if new_total > 0 else 0.0
            delta_k = tgt_k - cur_k
            rows.append([
                a.category, f"{cur_pct:.2f}%", f"{cur_k:.2f}",
                f"{a.target_pct or 0.0:.2f}%", f"{tgt_k:.2f}", f"{delta_k:.2f}",
            ])
    return "\n".join("\t".join(r) for r in rows) + "\n"


def _position_to_cells(p: Any) -> list[str]:
    """Convert a parsed PortfolioPosition back to TSV cells (best effort)."""
    return [
        p.review_status or "",
        p.location or "",
        p.currency or "",
        p.asset_type or "",
        p.details or "",
        p.symbol or "",
        f"{p.shares:g}" if p.shares is not None else "",
        f"{p.current_price:g}" if p.current_price is not None else "",
        f"{p.avg_price:g}" if p.avg_price is not None else "",
        f"{p.current_value_local:.2f}" if p.current_value_local is not None else "",
        f"{p.usd_value_k:.2f}" if p.usd_value_k is not None else "",
        f"{p.pct_change:.4f}" if p.pct_change is not None else "",
        f"{p.pct_yearly:.4f}" if p.pct_yearly is not None else "",
    ]


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _find_most_recent_prior_tsv(snapshot_root: Path) -> Path | None:
    """Return the newest 'Family Finances Status *.tsv' under snapshot_root."""
    candidates = sorted(
        snapshot_root.glob("Family Finances Status*.tsv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _prior_leumi_nis_cash(snapshot_root: Path) -> tuple[float | None, date | None]:
    """Return the prior snapshot's Leumi NIS cash balance (local NIS value) and
    that snapshot's date, for carry-forward when no fresh Osh is in window.

    A portfolio-only upload (the common "I bought a few stocks" case) shouldn't
    be blocked on the user also exporting a NIS current-account statement: the
    NIS cash rarely moves between exports, so carrying the prior balance forward
    (clearly labelled) is more useful than refusing to update the holdings.
    Returns (None, None) for a brand-new user with no prior TSV — that case
    still falls through to the pending queue."""
    prior = _find_most_recent_prior_tsv(snapshot_root)
    if prior is None:
        return None, None
    try:
        snap = parse_portfolio_tsv(prior)
    except Exception:  # noqa: BLE001 — carry-forward is best-effort
        return None, None
    for p in snap.positions:
        if (
            (p.location or "").lower().startswith("leumi")
            and (p.asset_type or "").lower() == "cash"
            and (p.currency or "").upper() == "NIS"
        ):
            return (p.current_value_local, snap.snapshot_date)
    return None, snap.snapshot_date


_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _canonical_tsv_filename(d: date | None) -> str:
    if d is None:
        return "Family Finances Status - unknown.tsv"
    return f"Family Finances Status - {d.year % 100:02d} {_MONTH_NAMES[d.month - 1]}.tsv"


def _serialize_xls(xls: LeumiPortfolioSnapshot) -> dict[str, Any]:
    return {
        "snapshot_date": xls.snapshot_date.isoformat() if xls.snapshot_date else None,
        "portfolio_number": xls.portfolio_number,
        "securities_count": xls.securities_count,
        "total_value": xls.total_value,
        "total_value_currency": xls.total_value_currency,
        "positions": [dataclasses.asdict(p) for p in xls.positions],
        "parse_warnings": xls.parse_warnings,
    }


def _deserialize_xls(payload_json: str) -> LeumiPortfolioSnapshot:
    raw = json.loads(payload_json)

    def _to_position(p: dict) -> LeumiPortfolioPosition:
        # Tolerate the pre-2026 payload schema (holding_value_usd, no currency)
        # so any pending part serialized before the NIS-format change still
        # deserializes — those were always USD.
        p = dict(p)
        if "holding_value" not in p and "holding_value_usd" in p:
            p["holding_value"] = p.pop("holding_value_usd")
        p.setdefault("holding_value_currency", "USD")
        return LeumiPortfolioPosition(**p)

    positions = [_to_position(p) for p in raw.get("positions", [])]
    snap_date_raw = raw.get("snapshot_date")
    snap_date = (
        date.fromisoformat(snap_date_raw) if snap_date_raw else None
    )
    return LeumiPortfolioSnapshot(
        snapshot_date=snap_date,
        portfolio_number=raw.get("portfolio_number"),
        securities_count=raw.get("securities_count", 0),
        # Back-compat: pre-2026 payloads used total_value_usd (always USD).
        total_value=raw.get("total_value", raw.get("total_value_usd")),
        total_value_currency=raw.get("total_value_currency", "USD"),
        positions=positions,
        parse_warnings=raw.get("parse_warnings", []),
    )


def _resolution_from_existing(part: PortfolioSnapshotPart) -> PairResolution:
    if part.status == "resolved":
        return PairResolution(
            status="duplicate",
            pending_pair_id=part.id,
            resolved_tsv_path=Path(part.resolved_tsv_path)
            if part.resolved_tsv_path else None,
            snapshot_date=part.snapshot_date,
            sha256=part.sha256,
            detail=(
                f"Already processed -- pair id {part.id}, resolved on "
                f"{part.paired_at.isoformat() if part.paired_at else 'unknown'}."
            ),
        )
    return PairResolution(
        status="pending_pair",
        pending_pair_id=part.id,
        resolved_tsv_path=None,
        snapshot_date=part.snapshot_date,
        sha256=part.sha256,
        detail=(
            f"Already queued as pending (pair id {part.id}). "
            "Upload the matching Leumi Osh statement to complete it."
        ),
    )


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


__all__ = [
    "PairResolution",
    "handle_xls_upload",
    "try_resolve_pending_on_osh_arrival",
    "is_leumi_portfolio_xls",  # re-export for the route's sniffer
]
