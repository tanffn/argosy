"""Anomaly highlights + inline-badge API endpoints (sprint #2 commits #10-#11).

Distinct from the legacy ``/api/anomalies/*`` router (singular vs plural)
which serves the full ``anomaly_reports`` documents written by the EX2
detector. This router consumes the per-row ``expense_review_queue`` rows
written by the sprint #2 detectors in ``argosy/services/anomaly/`` and
formats them as ``AnomalyCard``s for the Home tile + inline transaction
badges.

Endpoints:

  * ``GET /api/anomaly/highlights?user_id=&limit=5`` — top-N most-material
    open review-queue rows, formatted as ``AnomalyCard[]``. Sorted by
    severity (critical > warning > info) DESC then by ``created_at`` DESC.
  * ``GET /api/anomaly/by-txn?user_id=&txn_ids=1,2,3`` — map of
    ``{tx_id: AnomalyCard[]}`` for the inline badge column on
    ``<TransactionsTable>``. The UI batches per page load.
  * ``POST /api/anomaly/dismiss/{id}`` — flips an open queue row to
    ``status='resolved'`` and stamps ``resolved_at``. Used by the
    "Anomaly" tab on ``<TransactionDetailsDialog>``.

Spec: docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md §2.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, desc, select
from sqlalchemy.orm import Session

from argosy.api.routes.plan import get_db
from argosy.state.models import ExpenseReviewQueue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/anomaly", tags=["anomaly"])


# ---------------------------------------------------------------------------
# DTOs.
# ---------------------------------------------------------------------------

AnomalyCardKind = Literal[
    "uncategorized",
    "novel_merchant",
    "large_outlier",
    "fee_waiver_missed",
    "conservation_gap",
    "merchant_spike",
    "new_high_value_merchant",
    "recurring_missing",
    "category_drift",
    "cross_card_duplicate",
]

Severity = Literal["info", "warning", "critical"]


class AnomalyCardDTO(BaseModel):
    """Wire shape for one anomaly card. Matches the spec §2.2 schema."""

    id: int
    kind: AnomalyCardKind
    message: str
    detail: str | None = None
    severity: Severity
    link: str | None = None
    created_at: str
    txn_id: int | None = None


# ---------------------------------------------------------------------------
# Kind mapping (detector "kind" string → AnomalyCard kind).
# ---------------------------------------------------------------------------


_DETECTOR_KIND_TO_CARD_KIND: dict[str, AnomalyCardKind] = {
    # Bucket A — amount outliers.
    "a1_category_outlier": "large_outlier",
    "a2_merchant_spike": "merchant_spike",
    # Bucket B — recurring/watchlist.
    "bucket_b_fee_waiver_missing": "fee_waiver_missed",
    "bucket_b_recurring_missing": "recurring_missing",
    # Bucket C — merchant cache.
    "c1_novel_merchant": "novel_merchant",
    "c2_category_drift": "category_drift",
    # Bucket D — duplicate detection.
    "d1_cross_card_duplicate": "cross_card_duplicate",
    # Legacy categorizer queue rows.
    "uncategorized": "uncategorized",
}


_SEVERITY_RANK: dict[str, int] = {
    "critical": 3,
    "warning": 2,
    "info": 1,
}


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        # SQLite strips timezone on read; assume UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_payload(row: ExpenseReviewQueue) -> dict:
    try:
        data = json.loads(row.payload_json or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _normalize_severity(raw: str | None) -> Severity:
    """Coerce the row's materiality to a card severity.

    The review-queue ``materiality`` column already takes
    ``info|warning|critical`` via CHECK constraint, but the legacy
    ``uncategorized`` rows pre-date the materiality column and may be
    NULL. Default those to ``warning`` (yellow): they need attention but
    aren't a money-loss event.
    """
    if raw in ("info", "warning", "critical"):
        return raw  # type: ignore[return-value]
    return "warning"


def _message_for(row: ExpenseReviewQueue, payload: dict) -> str:
    """Human-friendly one-liner for the card body."""
    kind = row.kind
    merchant = (
        payload.get("merchant_normalized")
        or payload.get("merchant_pattern")
        or "—"
    )
    amount = payload.get("amount_nis") or payload.get("expected_amount_nis")
    if kind == "a1_category_outlier":
        return f"Large outlier at {merchant} (₪{amount})"
    if kind == "a2_merchant_spike":
        return f"{merchant} spend spiked vs trailing mean (₪{amount})"
    if kind == "bucket_b_fee_waiver_missing":
        return "Discount Bank fee-waiver discount appears to be missing"
    if kind == "bucket_b_recurring_missing":
        days = payload.get("days_overdue")
        suffix = f" ({days}d overdue)" if days else ""
        return f"Expected recurring charge from {merchant} is missing{suffix}"
    if kind == "c1_novel_merchant":
        return f"First-seen merchant: {merchant}"
    if kind == "c2_category_drift":
        return f"Category drift at {merchant}"
    if kind == "d1_cross_card_duplicate":
        return f"Possible duplicate charge at {merchant} (₪{amount})"
    if kind == "uncategorized":
        return "Uncategorized transaction needs review"
    return f"Anomaly: {kind}"


def _detail_for(row: ExpenseReviewQueue, payload: dict) -> str | None:
    """Optional second-line detail. Prefer the detector's rationale."""
    rationale = payload.get("rationale")
    if rationale and isinstance(rationale, str):
        return rationale
    # Recurring path stores no rationale; surface the dates instead.
    if row.kind == "bucket_b_recurring_missing":
        last_seen = payload.get("last_seen")
        expected_on = payload.get("expected_on")
        if last_seen and expected_on:
            return f"Expected on {expected_on}; last seen {last_seen}."
    return None


def _link_for(row: ExpenseReviewQueue, payload: dict) -> str | None:
    """Deep-link target for the click affordance.

    For txn-anchored rows, deep-link into the transactions tab and
    highlight by id. For the duplicate detector we link to the *earlier*
    leg; the dialog's Anomaly tab surfaces the pair from the payload.
    """
    txn_id = row.related_tx_id or payload.get("transaction_id") or payload.get("min_tx_id")
    if txn_id:
        return f"/expenses/transactions?highlight_tx={int(txn_id)}"
    if row.kind == "uncategorized":
        return "/expenses/transactions?category=uncategorized"
    return None


def _row_to_dto(row: ExpenseReviewQueue) -> AnomalyCardDTO | None:
    """Return ``None`` when the row's kind has no UI mapping (defensive)."""
    card_kind = _DETECTOR_KIND_TO_CARD_KIND.get(row.kind)
    if card_kind is None:
        logger.debug("anomaly card kind not mapped: %s", row.kind)
        return None
    payload = _parse_payload(row)
    txn_id = row.related_tx_id or payload.get("transaction_id") or payload.get("min_tx_id")
    return AnomalyCardDTO(
        id=row.id,
        kind=card_kind,
        message=_message_for(row, payload),
        detail=_detail_for(row, payload),
        severity=_normalize_severity(row.materiality),
        link=_link_for(row, payload),
        created_at=_iso(row.created_at),
        txn_id=int(txn_id) if txn_id is not None else None,
    )


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------


@router.get("/highlights", response_model=list[AnomalyCardDTO])
def get_highlights(
    user_id: str = Query("ariel"),
    limit: int = Query(5, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[AnomalyCardDTO]:
    """Top-N most-material open ``expense_review_queue`` rows.

    Order: severity (critical > warning > info) DESC, ``created_at`` DESC.
    Rows whose ``kind`` has no UI mapping are silently dropped (so a
    future detector that hasn't been wired into the highlights surface
    doesn't 500 the dashboard).
    """
    severity_rank = case(
        (ExpenseReviewQueue.materiality == "critical", 3),
        (ExpenseReviewQueue.materiality == "warning", 2),
        (ExpenseReviewQueue.materiality == "info", 1),
        else_=0,
    )
    # Fetch a generous superset so we can drop unmappable kinds and still
    # return ``limit`` cards. 4x is plenty in practice.
    rows = db.execute(
        select(ExpenseReviewQueue)
        .where(ExpenseReviewQueue.user_id == user_id)
        .where(ExpenseReviewQueue.status == "open")
        .order_by(desc(severity_rank), desc(ExpenseReviewQueue.created_at))
        .limit(max(limit * 4, limit))
    ).scalars().all()

    out: list[AnomalyCardDTO] = []
    for row in rows:
        dto = _row_to_dto(row)
        if dto is None:
            continue
        out.append(dto)
        if len(out) >= limit:
            break
    return out


@router.get("/by-txn", response_model=dict[str, list[AnomalyCardDTO]])
def get_anomalies_by_txn(
    user_id: str = Query("ariel"),
    txn_ids: str = Query("", description="Comma-separated transaction ids."),
    db: Session = Depends(get_db),
) -> dict[str, list[AnomalyCardDTO]]:
    """Map of ``{tx_id: AnomalyCard[]}`` for inline badge rendering.

    Returns string keys (JSON object) so the JS side can index by
    ``String(tx.id)``; an empty map when ``txn_ids`` is empty or no rows
    match. We match on either ``related_tx_id`` OR the duplicate-pair's
    ``max_tx_id`` from the payload so both legs of a Bucket-D pair light
    up. Anchored to ``status='open'`` only.
    """
    if not txn_ids.strip():
        return {}
    try:
        ids = sorted({
            int(piece.strip()) for piece in txn_ids.split(",") if piece.strip()
        })
    except ValueError:
        raise HTTPException(
            status_code=422, detail="txn_ids must be comma-separated integers"
        )
    if not ids:
        return {}

    # Pull every open queue row anchored to one of the requested txns via
    # related_tx_id. Also pull rows that mention the txn as the LATER leg
    # of a duplicate pair (payload.max_tx_id) — for those we can't index
    # in SQL portably, so we over-fetch by user and filter in Python.
    direct_rows = db.execute(
        select(ExpenseReviewQueue)
        .where(ExpenseReviewQueue.user_id == user_id)
        .where(ExpenseReviewQueue.status == "open")
        .where(ExpenseReviewQueue.related_tx_id.in_(ids))
    ).scalars().all()

    duplicate_rows = db.execute(
        select(ExpenseReviewQueue)
        .where(ExpenseReviewQueue.user_id == user_id)
        .where(ExpenseReviewQueue.status == "open")
        .where(ExpenseReviewQueue.kind == "d1_cross_card_duplicate")
    ).scalars().all()

    out: dict[str, list[AnomalyCardDTO]] = {str(i): [] for i in ids}
    seen_row_ids: set[tuple[int, int]] = set()  # (tx_id, row_id)

    for row in direct_rows:
        dto = _row_to_dto(row)
        if dto is None or row.related_tx_id is None:
            continue
        key = (row.related_tx_id, row.id)
        if key in seen_row_ids:
            continue
        seen_row_ids.add(key)
        bucket = out.setdefault(str(row.related_tx_id), [])
        bucket.append(dto)

    for row in duplicate_rows:
        payload = _parse_payload(row)
        max_tx = payload.get("max_tx_id")
        if max_tx is None or int(max_tx) not in ids:
            continue
        dto = _row_to_dto(row)
        if dto is None:
            continue
        key = (int(max_tx), row.id)
        if key in seen_row_ids:
            continue
        seen_row_ids.add(key)
        # Override the card's txn_id so the dialog opens against the LATER leg.
        dto = dto.model_copy(update={"txn_id": int(max_tx)})
        out.setdefault(str(int(max_tx)), []).append(dto)

    return out


class DismissResponse(BaseModel):
    id: int
    status: str
    resolved_at: str


@router.post("/dismiss/{queue_id}", response_model=DismissResponse)
def dismiss_anomaly(
    queue_id: int,
    user_id: str = Query("ariel"),
    db: Session = Depends(get_db),
) -> DismissResponse:
    """Flip an open queue row to ``status='resolved'`` and stamp resolved_at.

    Returns 404 when the row doesn't exist or belongs to a different
    tenant (never reveals existence cross-tenant). Returns the new row
    state when the flip succeeds. Idempotent — re-dismissing an
    already-resolved row returns its existing state without changes.
    """
    row = db.get(ExpenseReviewQueue, queue_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="queue row not found")
    if row.status != "open":
        return DismissResponse(
            id=row.id,
            status=row.status,
            resolved_at=_iso(row.resolved_at),
        )
    row.status = "resolved"
    row.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return DismissResponse(
        id=row.id,
        status=row.status,
        resolved_at=_iso(row.resolved_at),
    )


__all__ = ["AnomalyCardDTO", "AnomalyCardKind", "DismissResponse", "router"]
