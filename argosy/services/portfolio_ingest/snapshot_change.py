"""Run windfall detection + the long-term buy proposal whenever a portfolio
snapshot is updated by ingest — from ANY path, not just the HTTP upload route.

Why this module exists: detection used to be inlined in the
``/api/portfolio/upload-snapshot`` route, so a snapshot updated by a different
path (the Leumi Osh-arrival pair resolution, the NIS-cash carry-forward
refresh) silently skipped detection — a material cash movement could go
unflagged. Detection belongs to the *snapshot-changed* event, not to one HTTP
endpoint. Both the route and the orchestrator's Osh-arrival hook call
``run_windfall_detection_on_snapshot`` so the behaviour is identical regardless
of which path produced the new snapshot.

Scope: this is for genuine *ingest* updates (a new/refreshed monthly snapshot).
It is deliberately NOT wired into the low-level ``write_through_if_changed``
store helper, which also fires during synthesis input-warming — those are
internal recomputations, not new user snapshots, and must not raise windfall
alerts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


@dataclass
class SnapshotChangeResult:
    """Outcome of running detection against a freshly persisted snapshot TSV.

    ``detect_status``: 'ok' (detector ran), 'skipped' (no prior TSV to diff
    against, or fire=False), or 'failed' (detector raised — logged, never
    propagated). ``event``/``plan`` are the JSON payloads (None when no windfall
    was detected or no accepted plan exists); ``raw_event`` is the underlying
    ``WindfallEvent`` for callers that want to publish/notify."""

    detect_status: str
    event: dict | None
    plan: dict | None
    raw_event: object | None


def _windfall_doc_and_holdings(db: Session, user_id: str):
    """(canonical TargetAllocationDoc | None, holdings_by_symbol) for the
    windfall long-term buy list, via the SAME accessors /deploy-cash uses.
    ``doc is None`` means no plan is accepted — the caller must skip the buy
    list (no hardcoded fallback) rather than invent instruments."""
    from argosy.services.allocation_engine import tradeable_holdings
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        row_to_snapshot,
    )
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    pv = get_current_plan(db, user_id)
    doc = load_plan_target_allocation(pv) if pv is not None else None
    row = get_latest_snapshot_row(db, user_id=user_id)
    holdings, _cash = tradeable_holdings(row_to_snapshot(row)) if row else ({}, 0.0)
    return doc, holdings


def event_to_dict(event) -> dict:
    """Project a WindfallEvent into the JSON payload the upload response model
    consumes. (The /retirement/windfall/detect route adds reconciliation
    fields on top of this base shape; those are not needed here.)"""
    return {
        "detected_at": event.detected_at.isoformat(),
        "cash_delta_usd": event.cash_delta_usd,
        "cash_delta_nis": event.cash_delta_nis,
        "cash_delta_total_usd_equiv": event.cash_delta_total_usd_equiv,
        "fx_usd_nis": event.fx_usd_nis,
        "classified_source": event.classified_source,
        "requires_user_classification": event.requires_user_classification,
        "matching_sales": [
            {
                "symbol": s.symbol,
                "shares_sold": s.shares_sold,
                "current_price": s.current_price,
                "value_usd": round(s.value_usd, 2),
            }
            for s in event.matching_sales
        ],
        "allocation_delta_table": [
            {
                "asset_class": l.asset_class,
                "current_pct": l.current_pct,
                "current_k_usd": l.current_k_usd,
                "target_pct": l.target_pct,
                "target_k_usd": l.target_k_usd,
                "delta_k_usd": l.delta_k_usd,
            }
            for l in event.allocation_delta_table
        ],
        "source_tsv": Path(event.source_tsv).name,
        "previous_tsv": (
            Path(event.previous_tsv).name if event.previous_tsv else None
        ),
    }


def run_windfall_detection_on_snapshot(
    db: Session,
    *,
    user_id: str,
    target_path: Path,
    fire: bool = True,
) -> SnapshotChangeResult:
    """Diff ``target_path`` (the just-persisted snapshot TSV) against the most
    recent OTHER snapshot at its scan root and run windfall detection +
    long-term buy proposal. Never raises — a detector failure is logged and
    returned as ``detect_status='failed'`` so it can never break ingest."""
    if not fire or target_path is None:
        return SnapshotChangeResult("skipped", None, None, None)
    try:
        from argosy.services.retirement.windfall_allocator import propose_allocations
        from argosy.services.retirement.windfall_detector import (
            DEFAULT_THRESHOLD_NIS,
            DEFAULT_THRESHOLD_USD,
            detect_windfall,
        )

        prev_candidates = sorted(
            (
                p
                for p in target_path.parent.glob("Family Finances Status*.tsv")
                if p.resolve() != target_path.resolve()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        prev = prev_candidates[0] if prev_candidates else None
        if prev is None:
            return SnapshotChangeResult("skipped", None, None, None)

        event = detect_windfall(
            target_path,
            prev,
            threshold_usd=DEFAULT_THRESHOLD_USD,
            threshold_nis=DEFAULT_THRESHOLD_NIS,
        )
        if event is None:
            return SnapshotChangeResult("ok", None, None, None)

        event_payload = event_to_dict(event)
        plan_payload = None
        doc, holdings = _windfall_doc_and_holdings(db, user_id)
        if doc is not None:
            plan = propose_allocations(
                event, doc=doc, holdings=holdings, as_of=date.today()
            )
            plan_payload = plan.to_dict()
        return SnapshotChangeResult("ok", event_payload, plan_payload, event)
    except Exception as exc:  # noqa: BLE001 — detection must never break ingest
        # Keep the legacy log event name the UI's troubleshooting hint points
        # at (snapshot-upload-card references 'portfolio_snapshot.detector_failed').
        _log.warning(
            "portfolio_snapshot.detector_failed",
            extra={"user_id": user_id, "error": str(exc)},
        )
        return SnapshotChangeResult("failed", None, None, None)


__all__ = [
    "SnapshotChangeResult",
    "event_to_dict",
    "run_windfall_detection_on_snapshot",
]
